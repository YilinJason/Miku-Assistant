# -*- coding: utf-8 -*-
"""Paraformer 持久 ASR worker 客户端（单 inflight、有界生命周期）。"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.core.host_memory import STARTUP_MEMORY_USER_HINT, check_model_startup_memory
from src.core.log_privacy import exception_log_label
from src.core.owned_worker_job import take_owned_worker_from_exception
from src.core.safety import Operation, SafetyPolicy
from src.voice.cosyvoice.stdin_writer import CosyVoiceStdinWriter
from src.voice.paraformer.assets import SETUP_USER_HINT
from src.voice.paraformer.capabilities import LocalAsrWorkerLaunch
from src.voice.paraformer.protocol import (
    MAX_PCM_BYTES,
    MSG_ERROR,
    MSG_READY,
    MSG_TRANSCRIBE_RESULT,
    STATUS_EMPTY,
    STATUS_OK,
    build_cancel_request,
    build_shutdown_request,
    build_transcribe_request,
)
from src.voice.paraformer.reader import ParaformerStdoutReader
from src.voice.paraformer.settings import ParaformerSettings

logger = logging.getLogger(__name__)

_POLL_SEC = 0.05
_TERMINATE_GRACE_CAP_SEC = 1.0
_MONOTONIC = time.monotonic


@dataclass(frozen=True)
class ParaformerShutdownStatus:
    complete: bool
    worker_alive: bool
    reader_alive: bool
    writer_alive: bool


@dataclass(frozen=True)
class TranscribeOutcome:
    ok: bool
    text: str
    empty: bool
    error_label: str = ""


class ParaformerWorkerClient:
    def __init__(
        self,
        settings: ParaformerSettings,
        safety_policy: SafetyPolicy,
        *,
        spawn_fn: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = _MONOTONIC,
    ) -> None:
        if safety_policy is None:
            raise PermissionError("ParaformerWorkerClient 需要 SafetyPolicy")
        self._settings = settings
        self._policy = safety_policy
        self._spawn_fn = spawn_fn
        self._clock = clock
        self._proc: Any = None
        self._reader: Optional[ParaformerStdoutReader] = None
        self._writer: Optional[CosyVoiceStdinWriter] = None
        self._inflight_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._retire_lock = threading.Lock()
        self._closing = threading.Event()
        self._terminal_closed = False
        self._spawn_in_progress = False
        self._ready = False
        self._active_request = False
        self._shutdown_status = ParaformerShutdownStatus(
            complete=True,
            worker_alive=False,
            reader_alive=False,
            writer_alive=False,
        )

    def shutdown_status(self) -> ParaformerShutdownStatus:
        with self._lifecycle_lock:
            return self._shutdown_status

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self._clock())

    def _operation_outstanding(self) -> bool:
        with self._lifecycle_lock:
            return self._spawn_in_progress or self._active_request

    def _publish_shutdown_status(self, *, complete: bool) -> None:
        """Caller must hold ``_lifecycle_lock``; must not re-enter that lock."""
        if self._spawn_in_progress or self._active_request:
            complete = False
        live = self._has_live_owner()
        self._shutdown_status = ParaformerShutdownStatus(
            complete=complete and not live,
            worker_alive=self.process_alive(),
            reader_alive=self.reader_alive(),
            writer_alive=self.writer_alive(),
        )

    def process_alive(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        poll = getattr(proc, "poll", None)
        if callable(poll):
            try:
                return poll() is None
            except Exception:
                return True
        return True

    def reader_alive(self) -> bool:
        reader = self._reader
        return reader is not None and reader.is_alive()

    def writer_alive(self) -> bool:
        writer = self._writer
        return writer is not None and writer.is_alive()

    def _has_live_owner(self) -> bool:
        return (
            self.process_alive()
            or self.reader_alive()
            or self.writer_alive()
        )

    def shutdown(self, *, budget_sec: Optional[float] = None) -> bool:
        budget = budget_sec if budget_sec is not None else self._settings.close_budget_sec
        deadline = self._clock() + budget
        self._closing.set()
        with self._lifecycle_lock:
            if self._terminal_closed and not self._has_live_owner() and not self._operation_outstanding_unlocked():
                return True
        if self._inflight_lock.acquire(blocking=False):
            try:
                if self._active_request:
                    self._write_cancel(self._remaining(deadline))
                self._write_shutdown(self._remaining(deadline))
            finally:
                self._inflight_lock.release()
        stopped = self._stop_owner_until(deadline)
        with self._lifecycle_lock:
            outstanding = self._operation_outstanding_unlocked() or self._has_live_owner()
            complete = stopped and not outstanding
            if complete:
                self._terminal_closed = True
            self._publish_shutdown_status(complete=complete)
            return self._shutdown_status.complete

    def _operation_outstanding_unlocked(self) -> bool:
        return self._spawn_in_progress or self._active_request

    def transcribe(
        self,
        pcm_bytes: bytes,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> TranscribeOutcome:
        if self._closing.is_set() or self._terminal_closed:
            return TranscribeOutcome(ok=False, text="", empty=False, error_label="closing")
        try:
            self._policy.require(Operation.LOCAL_ASR_WORKER, "local_asr_worker")
        except PermissionError:
            return TranscribeOutcome(ok=False, text="", empty=False, error_label="permission")
        if len(pcm_bytes) > MAX_PCM_BYTES or len(pcm_bytes) == 0 or len(pcm_bytes) % 2 != 0:
            return TranscribeOutcome(ok=False, text="", empty=False, error_label="pcm_invalid")
        if not self._inflight_lock.acquire(blocking=False):
            return TranscribeOutcome(ok=False, text="", empty=False, error_label="busy")
        try:
            if self._closing.is_set() or self._terminal_closed:
                return TranscribeOutcome(ok=False, text="", empty=False, error_label="closing")
            if self._active_request:
                return TranscribeOutcome(ok=False, text="", empty=False, error_label="busy")
            self._active_request = True
            return self._transcribe_inflight(pcm_bytes, cancel_event=cancel_event)
        finally:
            self._active_request = False
            self._inflight_lock.release()

    def _transcribe_inflight(
        self,
        pcm_bytes: bytes,
        *,
        cancel_event: Optional[threading.Event],
    ) -> TranscribeOutcome:
        startup_deadline = self._clock() + self._settings.startup_deadline_sec
        if not self._ensure_worker_until(startup_deadline, cancel_event=cancel_event):
            self._discard_worker_after_error()
            return TranscribeOutcome(ok=False, text="", empty=False, error_label="startup")
        if self._should_abort(cancel_event):
            self._discard_worker_after_error()
            return TranscribeOutcome(ok=False, text="", empty=False, error_label="cancelled")
        infer_deadline = self._clock() + self._settings.inference_deadline_sec
        request_id = uuid.uuid4().hex
        try:
            line = build_transcribe_request(request_id, pcm_bytes)
        except ValueError:
            return TranscribeOutcome(ok=False, text="", empty=False, error_label="pcm_invalid")
        writer = self._writer
        if writer is None or self._closing.is_set():
            return TranscribeOutcome(ok=False, text="", empty=False, error_label="closing")
        if not writer.write_bounded(
            line,
            infer_deadline,
            should_cancel=lambda: self._should_abort(cancel_event),
        ):
            self._discard_worker_after_error()
            return TranscribeOutcome(ok=False, text="", empty=False, error_label="timeout")
        while self._clock() < infer_deadline:
            if self._should_abort(cancel_event):
                self._write_cancel(self._remaining(infer_deadline))
                self._discard_worker_after_error()
                return TranscribeOutcome(ok=False, text="", empty=False, error_label="cancelled")
            reader = self._reader
            if reader is None:
                return TranscribeOutcome(ok=False, text="", empty=False, error_label="not_ready")
            if reader.had_fatal_protocol_error():
                self._discard_worker_after_error()
                return TranscribeOutcome(ok=False, text="", empty=False, error_label="protocol")
            msg = reader.poll_message(_POLL_SEC)
            if msg is None:
                if not self.process_alive():
                    self._discard_worker_after_error()
                    return TranscribeOutcome(ok=False, text="", empty=False, error_label="died")
                continue
            if msg.get("msg") == MSG_ERROR and msg.get("request_id") == request_id:
                self._discard_worker_after_error()
                return TranscribeOutcome(
                    ok=False,
                    text="",
                    empty=False,
                    error_label=str(msg.get("error", "error")),
                )
            if msg.get("msg") != MSG_TRANSCRIBE_RESULT:
                continue
            if msg.get("request_id") != request_id:
                continue
            if self._should_abort(cancel_event):
                return TranscribeOutcome(ok=False, text="", empty=False, error_label="cancelled")
            status = msg.get("status")
            text = msg.get("text", "")
            if status == STATUS_OK and isinstance(text, str) and text.strip():
                return TranscribeOutcome(ok=True, text=text.strip(), empty=False)
            if status == STATUS_EMPTY:
                return TranscribeOutcome(ok=False, text="", empty=True)
            self._discard_worker_after_error()
            return TranscribeOutcome(ok=False, text="", empty=False, error_label="failed")
        self._discard_worker_after_error()
        return TranscribeOutcome(ok=False, text="", empty=False, error_label="timeout")

    def _should_abort(self, cancel_event: Optional[threading.Event]) -> bool:
        if self._closing.is_set():
            return True
        return cancel_event is not None and cancel_event.is_set()

    def _ensure_worker_until(
        self,
        deadline: float,
        *,
        cancel_event: Optional[threading.Event],
    ) -> bool:
        if self._terminal_closed:
            return False
        if self._ready and self.process_alive() and self._reader and self._writer:
            reader = self._reader
            if reader.had_fatal_protocol_error():
                return False
            return True
        if self._proc is not None and self.process_alive():
            return False
        if self._closing.is_set():
            return False
        self._release_owner_refs_if_quiescent()
        if self._has_live_owner():
            return False
        decision = check_model_startup_memory("paraformer")
        if not decision.allowed:
            logger.warning(
                "Paraformer worker 拒绝启动 reason=%s hint=%s",
                decision.reason,
                STARTUP_MEMORY_USER_HINT,
            )
            return False
        launch = LocalAsrWorkerLaunch(parent_pid=os.getpid(), settings=self._settings)
        with self._lifecycle_lock:
            if self._closing.is_set():
                return False
            self._spawn_in_progress = True
        try:
            proc: Any
            try:
                spawn = self._spawn_fn or self._policy.spawn_local_asr_worker
                proc = spawn(launch)
            except BaseException as exc:
                pending = take_owned_worker_from_exception(exc)
                if pending is not None:
                    self._proc = pending
                    self._ready = False
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(exc, PermissionError):
                    return False
                logger.error(
                    "Paraformer worker 启动失败 err=%s",
                    exception_log_label(exc),
                )
                return False
            self._proc = proc
            if self._closing.is_set():
                self._stop_owner_until(deadline)
                return False
            stdout = getattr(proc, "stdout", None)
            stdin = getattr(proc, "stdin", None)
            if stdout is None or stdin is None:
                self._stop_owner_until(deadline)
                return False
            try:
                reader = ParaformerStdoutReader(stdout)
                writer = CosyVoiceStdinWriter(stdin)
            except Exception:
                self._stop_owner_until(deadline)
                return False
            self._reader = reader
            self._writer = writer
            try:
                reader.start()
                writer.start()
            except Exception:
                self._stop_owner_until(deadline)
                return False
            while self._clock() < deadline:
                if self._should_abort(cancel_event):
                    self._stop_owner_until(deadline)
                    return False
                if reader.had_fatal_protocol_error():
                    self._stop_owner_until(deadline)
                    return False
                msg = reader.poll_message(_POLL_SEC)
                if msg is None:
                    if not self.process_alive():
                        self._stop_owner_until(deadline)
                        return False
                    continue
                if msg.get("msg") == MSG_READY:
                    self._ready = True
                    return True
                if msg.get("msg") == MSG_ERROR:
                    self._stop_owner_until(deadline)
                    return False
            self._stop_owner_until(deadline)
            return False
        finally:
            with self._lifecycle_lock:
                self._spawn_in_progress = False

    def _write_shutdown(self, remaining: float) -> None:
        if remaining <= 0.0:
            return
        writer = self._writer
        if writer is None:
            return
        writer.write_bounded(
            build_shutdown_request(),
            self._clock() + remaining,
            should_cancel=lambda: self._closing.is_set(),
        )

    def _write_cancel(self, remaining: float) -> None:
        if remaining <= 0.0:
            return
        writer = self._writer
        if writer is None:
            return
        writer.write_bounded(
            build_cancel_request(uuid.uuid4().hex),
            self._clock() + remaining,
            should_cancel=lambda: self._closing.is_set(),
        )

    def _terminate_job_first(self, proc: Any) -> None:
        if proc is None:
            return
        terminate = getattr(proc, "terminate", None)
        if callable(terminate):
            try:
                terminate()
            except OSError:
                pass

    def _stop_owner_until(self, deadline: float) -> bool:
        if not self._retire_lock.acquire(blocking=False):
            return False
        try:
            proc = self._proc
            reader = self._reader
            writer = self._writer
            if proc is None and reader is None and writer is None:
                return True
            self._ready = False
            self._terminate_job_first(proc)
            if writer is not None:
                writer.close()
                if not writer.join(timeout=self._remaining(deadline)):
                    return False
            if reader is not None:
                reader.close()
                if not reader.join(timeout=self._remaining(deadline)):
                    return False
            if proc is not None:
                wait = getattr(proc, "wait", None)
                if callable(wait):
                    try:
                        wait(timeout=self._remaining(deadline))
                    except subprocess.TimeoutExpired:
                        pass
                if self.process_alive():
                    kill = getattr(proc, "kill", None)
                    if callable(kill):
                        try:
                            kill()
                        except OSError:
                            pass
                    if self._remaining(deadline) > 0.0 and callable(wait):
                        try:
                            wait(
                                timeout=min(
                                    _TERMINATE_GRACE_CAP_SEC, self._remaining(deadline)
                                )
                            )
                        except subprocess.TimeoutExpired:
                            pass
            if self._has_live_owner():
                return False
            self._clear_owner_refs(proc, reader, writer)
            return True
        finally:
            self._retire_lock.release()

    def _discard_worker_after_error(self) -> None:
        if self._proc is None and self._reader is None and self._writer is None:
            return
        deadline = self._clock() + min(self._settings.close_budget_sec, 2.0)
        self._stop_owner_until(deadline)

    def _clear_owner_refs(
        self,
        proc: Any,
        reader: Optional[ParaformerStdoutReader],
        writer: Optional[CosyVoiceStdinWriter],
    ) -> None:
        if self._proc is not proc:
            return
        if self._reader is not reader:
            return
        if self._writer is not writer:
            return
        if self._has_live_owner():
            return
        self._proc = None
        self._reader = None
        self._writer = None
        self._ready = False

    def _release_owner_refs_if_quiescent(self) -> None:
        if not self._has_live_owner():
            self._clear_owner_refs(self._proc, self._reader, self._writer)

    @staticmethod
    def setup_hint() -> str:
        return SETUP_USER_HINT
