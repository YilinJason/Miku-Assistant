# -*- coding: utf-8 -*-
"""CosyVoice 持久 runtime worker 客户端（切片 B）。"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from src.core.log_privacy import exception_log_label
from src.core.safety import Operation, SafetyPolicy, capture_file_identity
from src.voice.cosyvoice.capabilities import LocalTtsWorkerLaunch
from src.voice.cosyvoice.protocol import (
    MSG_ERROR,
    MSG_READY,
    MSG_STARTUP_PROGRESS,
    MSG_SYNTH_RESULT,
    StartupProgressAccumulator,
    StartupProgressSnapshot,
    empty_startup_progress_snapshot,
    build_shutdown_request,
    build_synth_request,
)
from src.voice.cosyvoice.stdin_writer import CosyVoiceStdinWriter
from src.voice.cosyvoice.stdout_reader import CosyVoiceStdoutReader
from src.voice.cosyvoice.styles import KNOWN_STYLE_IDS
from src.voice.cosyvoice.settings import CosyVoiceSettings
from src.voice.cosyvoice.cross_file_identity import capture_portable_file_identity
from src.voice.cosyvoice.output_verify import verify_synth_output
from src.voice.cosyvoice.worker_output import validate_worker_output_path
from src.core.paths import PROJECT_ROOT, TMP_DIR

logger = logging.getLogger(__name__)

_DEFAULT_STOP_BUDGET_SEC = 2.0
_TERMINATE_GRACE_CAP_SEC = 1.0
_KILL_GRACE_CAP_SEC = 1.0
_POLL_INTERVAL_SEC = 0.05
_MAX_STALE_MESSAGES = 8
_IDLE_RETIREMENT_GUARD_SEC = 0.5
_MONOTONIC = time.monotonic
@dataclass(frozen=True)
class CosyVoiceShutdownStatus:
    complete: bool
    worker_alive: bool
    reader_alive: bool
    writer_alive: bool


_REGISTRY_LOCK = threading.Lock()
_ACTIVE_CLIENT: Optional["CosyVoiceWorkerClient"] = None


def _register_client(client: "CosyVoiceWorkerClient") -> None:
    global _ACTIVE_CLIENT
    with _REGISTRY_LOCK:
        if _ACTIVE_CLIENT is not None and _ACTIVE_CLIENT is not client:
            raise RuntimeError("CosyVoiceWorkerClient 单实例限制")
        _ACTIVE_CLIENT = client


def _unregister_client(client: "CosyVoiceWorkerClient") -> None:
    global _ACTIVE_CLIENT
    with _REGISTRY_LOCK:
        if _ACTIVE_CLIENT is client:
            _ACTIVE_CLIENT = None


class CosyVoiceWorkerClient:
    """管理 ``scripts/cosyvoice_runtime_worker.py`` 子进程与有界协议。"""

    def __init__(
        self,
        settings: CosyVoiceSettings,
        safety_policy: SafetyPolicy,
        temp_file_manager: Any,
    ) -> None:
        if safety_policy is None:
            raise PermissionError("CosyVoiceWorkerClient 需要显式 SafetyPolicy")
        if temp_file_manager is None:
            raise PermissionError("CosyVoiceWorkerClient 需要 TempFileManager")
        _register_client(self)
        self._settings = settings
        self._policy = safety_policy
        self._temp_files = temp_file_manager
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[CosyVoiceStdoutReader] = None
        self._stdin_writer: Optional[CosyVoiceStdinWriter] = None
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop_in_progress = False
        self._epoch = 0
        self._ready = False
        self._closing = False
        self._stop_failed = False
        self._active_request = False
        self._startup_in_progress = False
        self._worker_retiring = False
        self._last_stop_exited = True
        self._startup_progress = StartupProgressAccumulator()
        self._startup_progress_snapshot = empty_startup_progress_snapshot()
        self._worker_last_activity_mono: float | None = None

    def bind_temp_file_manager(self, temp_file_manager: Any) -> None:
        self._temp_files = temp_file_manager

    @property
    def last_stop_exited(self) -> bool:
        return self._last_stop_exited

    @property
    def stop_failed(self) -> bool:
        return self._stop_failed

    def startup_progress_snapshot(self) -> StartupProgressSnapshot:
        """启动等待期间的有界阶段快照（不可变；失败/取消后仍可读最后一次）。"""
        with self._spawn_lock:
            return self._startup_progress_snapshot

    def process_alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def reader_alive(self) -> bool:
        reader = self._reader
        return reader is not None and reader.is_alive()

    def writer_alive(self) -> bool:
        writer = self._stdin_writer
        return writer is not None and writer.is_alive()

    def interrupt(self) -> bool:
        """推进 epoch；活跃合成时终止 worker，空闲时仅丢弃晚到结果。"""
        with self._state_lock:
            self._epoch += 1
            active = self._active_request
        if active:
            self._terminate_worker_bounded()
        return active

    def synthesize(
        self,
        text: str,
        *,
        style_id: str,
        output_path: str,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        if self._closing:
            logger.error("CosyVoice synthesize 拒绝: 已关闭")
            return False
        if self._stop_failed:
            logger.error("CosyVoice synthesize 拒绝: 上次停止失败")
            return False
        if not isinstance(text, str) or len(text) > self._settings.max_text_chars:
            logger.error("CosyVoice synthesize 拒绝: 文本过长")
            return False
        if style_id not in KNOWN_STYLE_IDS:
            logger.error("CosyVoice synthesize 拒绝: 未知 style")
            return False
        try:
            self._policy.require(Operation.LOCAL_TTS_WORKER, "local_tts_worker")
            key = self._temp_files.assert_owned_regular_file(
                output_path, label="CosyVoice 输出"
            )
            identity = capture_file_identity(key)
            try:
                portable = capture_portable_file_identity(key)
            except OSError:
                logger.error("CosyVoice synthesize 拒绝: 输出 portable 身份不可用")
                return False
            if (
                validate_worker_output_path(
                    output_path,
                    portable.dev,
                    portable.ino,
                    repo_root=PROJECT_ROOT,
                    tmp_root=TMP_DIR,
                )
                is None
            ):
                logger.error("CosyVoice synthesize 拒绝: 输出不在固定 tmp 或身份无效")
                return False
            # portable 读取窗口后再次确认登记身份未变，拒绝替代物并零派发。
            key = self._temp_files.assert_owned_regular_file(
                output_path, label="CosyVoice 输出"
            )
            try:
                portable_check = capture_portable_file_identity(key)
            except OSError:
                logger.error("CosyVoice synthesize 拒绝: 输出 portable 复验不可用")
                return False
            if portable_check.dev != portable.dev or portable_check.ino != portable.ino:
                logger.error("CosyVoice synthesize 拒绝: 输出 portable 与登记不一致")
                return False
        except PermissionError:
            logger.error("CosyVoice synthesize 被安全策略或输出所有权拒绝")
            return False

        request_epoch = self._snapshot_epoch()
        request_id = uuid.uuid4().hex
        if not self._begin_request(request_epoch):
            logger.error("CosyVoice synthesize 拒绝: 单请求忙或启动中")
            return False
        try:
            if cancel_event is not None and cancel_event.is_set():
                return False
            if not self._ensure_worker_started(request_epoch, cancel_event):
                return False
            if not self._epoch_matches(request_epoch):
                return False
            proc = self._proc
            reader = self._reader
            writer = self._stdin_writer
            if (
                proc is None
                or proc.stdin is None
                or reader is None
                or writer is None
                or not self._ready
            ):
                return False
            line = build_synth_request(
                request_id=request_id,
                epoch=request_epoch,
                style_id=style_id,
                text=text,
                output_path=key,
                output_dev=portable.dev,
                output_ino=portable.ino,
            )
            deadline = time.monotonic() + float(self._settings.synth_timeout_sec)
            remaining = deadline - time.monotonic()
            def _write_cancelled() -> bool:
                if self._closing:
                    return True
                if cancel_event is not None and cancel_event.is_set():
                    return True
                return not self._epoch_matches(request_epoch)

            if not writer.write_bounded(
                line,
                deadline=deadline,
                should_cancel=_write_cancelled,
            ):
                self._terminate_worker_bounded(deadline=deadline)
                return False
            stale = 0
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    self.interrupt()
                    return False
                if not self._epoch_matches(request_epoch):
                    return False
                if self._closing:
                    return False
                if reader.had_fatal_protocol_error():
                    self._terminate_worker_bounded()
                    return False
                remaining = deadline - time.monotonic()
                msg = reader.poll_message(timeout=min(_POLL_INTERVAL_SEC, remaining))
                if msg is None:
                    if proc.poll() is not None:
                        self._ready = False
                        return False
                    continue
                if msg.get("type") == MSG_ERROR:
                    return False
                if msg.get("type") == MSG_SYNTH_RESULT:
                    if not self._epoch_matches(request_epoch):
                        return False
                    if msg.get("request_id") != request_id:
                        stale += 1
                        if stale >= _MAX_STALE_MESSAGES:
                            self._terminate_worker_bounded(deadline=deadline)
                            return False
                        continue
                    if int(msg.get("epoch", -1)) != request_epoch:
                        stale += 1
                        if stale >= _MAX_STALE_MESSAGES:
                            self._terminate_worker_bounded(deadline=deadline)
                            return False
                        continue
                    if msg.get("ok") is not True:
                        return False
                    nbytes = int(msg.get("bytes", 0))
                    ok = verify_synth_output(
                        key,
                        identity,
                        nbytes,
                        repo_root=PROJECT_ROOT,
                        tmp_root=TMP_DIR,
                    )
                    if ok:
                        self._touch_worker_activity()
                    return ok
            self.interrupt()
            return False
        finally:
            self._end_request()

    def prepare(
        self,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        """
        显式加载/复用本地 worker（零合成、零 TFM 创建、零音频输出）。

        成功仅表示当前 epoch 下 worker 已 ready；不是无副作用的心跳探测。
        """
        if self._closing:
            logger.error("CosyVoice prepare 拒绝: 已关闭")
            return False
        if self._stop_failed:
            logger.error("CosyVoice prepare 拒绝: 上次停止失败")
            return False
        try:
            self._policy.require(Operation.LOCAL_TTS_WORKER, "local_tts_worker")
        except PermissionError:
            logger.error("CosyVoice prepare 被安全策略拒绝")
            return False

        request_epoch = self._snapshot_epoch()
        if not self._begin_request(request_epoch):
            logger.error("CosyVoice prepare 拒绝: 单请求忙或启动中")
            return False
        try:
            if cancel_event is not None and cancel_event.is_set():
                return False
            if not self._ensure_worker_started(request_epoch, cancel_event):
                return False
            if not self._epoch_matches(request_epoch):
                return False
            if self._closing:
                return False
            if cancel_event is not None and cancel_event.is_set():
                return False
            if not self._worker_reuse_ok():
                return False
            if not self.writer_alive():
                return False
            return True
        finally:
            self._end_request()

    def shutdown(self, timeout: float = 2.0) -> CosyVoiceShutdownStatus:
        self._closing = True
        with self._state_lock:
            self._epoch += 1
        deadline = time.monotonic() + max(0.0, float(timeout))
        self._wait_worker_retiring_until(deadline)
        self._stop_worker_until(deadline)
        return self._build_shutdown_status(unregister=True)

    def continue_shutdown(self, timeout: float = 2.0) -> CosyVoiceShutdownStatus:
        """已在关闭路径上：在剩余 budget 内继续停止并复核 registry/在途请求状态（不推进 epoch）。"""
        if not self._closing:
            raise RuntimeError(
                "continue_shutdown 仅允许在 client 已进入 closing 后调用"
            )
        deadline = time.monotonic() + max(0.0, float(timeout))
        self._wait_worker_retiring_until(deadline)
        self._stop_worker_until(deadline)
        return self._build_shutdown_status(unregister=True)

    def retire_worker(self, timeout: float = 2.0) -> CosyVoiceShutdownStatus:
        """
        有界退休当前 worker（失败输出回收前）；不永久 closing、不注销单实例 registry。

        仅在 proc/reader/writer/活跃请求确认退出后返回 complete=True。
        """
        if self._closing:
            return self._build_shutdown_status(unregister=False)
        with self._request_lock:
            if (
                self._active_request
                or self._startup_in_progress
                or self._worker_retiring
            ):
                return CosyVoiceShutdownStatus(
                    complete=False,
                    worker_alive=self.process_alive(),
                    reader_alive=self.reader_alive(),
                    writer_alive=self.writer_alive(),
                )
            self._worker_retiring = True
        try:
            deadline = time.monotonic() + max(0.0, float(timeout))
            with self._state_lock:
                self._epoch += 1
            self._ready = False
            self._stop_worker_until(deadline)
        finally:
            with self._request_lock:
                self._worker_retiring = False
        return self._build_shutdown_status(unregister=False)

    def _lifecycle_busy(self) -> bool:
        with self._request_lock:
            return (
                self._active_request
                or self._startup_in_progress
                or self._worker_retiring
            )

    def _build_shutdown_status(self, *, unregister: bool) -> CosyVoiceShutdownStatus:
        busy = self._lifecycle_busy()
        worker_alive = self.process_alive()
        reader_alive = self.reader_alive()
        writer_alive = self.writer_alive()
        complete = (
            not worker_alive
            and not reader_alive
            and not writer_alive
            and not busy
        )
        if worker_alive or reader_alive or writer_alive:
            self._stop_failed = True
            self._last_stop_exited = False
        elif not busy:
            self._last_stop_exited = True
        if complete and unregister:
            _unregister_client(self)
        return CosyVoiceShutdownStatus(
            complete=complete,
            worker_alive=worker_alive,
            reader_alive=reader_alive,
            writer_alive=writer_alive,
        )

    def _wait_worker_retiring_until(self, deadline: float) -> None:
        while self._remaining(deadline) > 0.0:
            with self._request_lock:
                if not self._worker_retiring:
                    return
            time.sleep(_POLL_INTERVAL_SEC)

    def _snapshot_epoch(self) -> int:
        with self._state_lock:
            return self._epoch

    def _epoch_matches(self, epoch: int) -> bool:
        with self._state_lock:
            return epoch == self._epoch

    def _begin_request(self, epoch: int) -> bool:
        with self._request_lock:
            if (
                self._active_request
                or self._closing
                or self._stop_failed
                or self._startup_in_progress
                or self._worker_retiring
            ):
                return False
            with self._state_lock:
                if epoch != self._epoch:
                    return False
            self._active_request = True
            return True

    def _end_request(self) -> None:
        with self._request_lock:
            self._active_request = False

    def _touch_worker_activity(self) -> None:
        self._worker_last_activity_mono = _MONOTONIC()

    def _worker_idle_retirement_due(self) -> bool:
        last = self._worker_last_activity_mono
        if last is None:
            return False
        idle = float(self._settings.idle_timeout_sec)
        guard = min(_IDLE_RETIREMENT_GUARD_SEC, idle * 0.25)
        threshold = max(0.0, idle - guard)
        return _MONOTONIC() - last >= threshold

    def worker_playback_ready_readonly(self) -> bool:
        """只读：当前 worker 是否仍可承担播报（不 spawn、不写状态）。"""
        return self._worker_reuse_ok()

    def _worker_reuse_ok(self) -> bool:
        if not self._ready or not self.process_alive():
            return False
        reader = self._reader
        if reader is None:
            return False
        if not reader.is_alive():
            return False
        if reader.had_fatal_protocol_error():
            return False
        if self._worker_idle_retirement_due():
            return False
        return True

    def _reap_worker_for_reuse(
        self,
        request_epoch: int,
        cancel_event: Optional[threading.Event],
    ) -> bool:
        if self._worker_reuse_ok():
            return True
        if (
            self._proc is None
            and self._reader is None
            and self._stdin_writer is None
        ):
            return True
        if cancel_event is not None and cancel_event.is_set():
            return False
        if self._closing or not self._epoch_matches(request_epoch):
            return False
        self._ready = False
        deadline = _MONOTONIC() + _DEFAULT_STOP_BUDGET_SEC
        self._stop_worker_until(deadline)
        if cancel_event is not None and cancel_event.is_set():
            return False
        if self._closing or not self._epoch_matches(request_epoch):
            return False
        if self._stop_failed:
            return False
        if self.process_alive() or self.reader_alive() or self.writer_alive():
            return False
        self._worker_last_activity_mono = None
        return True

    def _ensure_worker_started(
        self,
        request_epoch: int,
        cancel_event: Optional[threading.Event],
    ) -> bool:
        if self._worker_reuse_ok():
            return True
        with self._spawn_lock:
            if self._worker_reuse_ok():
                return True
            if not self._reap_worker_for_reuse(request_epoch, cancel_event):
                return False
            if self._worker_reuse_ok():
                return True
            if self._proc is not None and not self.process_alive():
                self._release_process_if_clean()
            if self._proc is not None and self.process_alive():
                logger.error("CosyVoice 拒绝重建: 旧 worker 仍在运行")
                return False
            if self._stop_failed:
                return False
            self._startup_in_progress = True
        try:
            return self._start_worker_poll(request_epoch, cancel_event)
        finally:
            with self._spawn_lock:
                self._startup_in_progress = False

    def _start_worker_poll(
        self,
        request_epoch: int,
        cancel_event: Optional[threading.Event],
    ) -> bool:
        from src.core.host_memory import STARTUP_MEMORY_USER_HINT, check_model_startup_memory

        _mem = check_model_startup_memory("cosyvoice")
        if not _mem.allowed:
            logger.error(
                "CosyVoice worker 启动被拒绝 reason=%s physical_avail=%s commit_avail=%s "
                "required_phys=%s required_commit=%s. %s",
                _mem.reason,
                _mem.physical_available_mb,
                _mem.commit_available_mb,
                _mem.required_physical_mb,
                _mem.required_commit_mb,
                STARTUP_MEMORY_USER_HINT,
            )
            return False
        launch = LocalTtsWorkerLaunch(
            parent_pid=os.getpid(),
            settings=self._settings,
        )
        try:
            proc = self._policy.spawn_local_tts_worker(launch)
        except BaseException as exc:
            from src.core.owned_worker_job import take_owned_worker_from_exception

            pending = take_owned_worker_from_exception(exc)
            if pending is not None:
                with self._spawn_lock:
                    if self._proc is None:
                        self._proc = pending
                    self._ready = False
                    self._stop_failed = True
            if isinstance(exc, PermissionError):
                logger.error(
                    "CosyVoice worker 启动被拒绝 err=%s",
                    exception_log_label(exc),
                )
                return False
            raise
        if (
            self._closing
            or self._stop_failed
            or not self._epoch_matches(request_epoch)
        ):
            with self._spawn_lock:
                if self._proc is None:
                    self._proc = proc
                self._ready = False
            self._terminate_worker_bounded(deadline=time.monotonic())
            return False
        with self._spawn_lock:
            self._startup_progress.reset()
            self._startup_progress_snapshot = empty_startup_progress_snapshot()
        self._worker_last_activity_mono = None
        self._proc = proc
        if proc.stdout is None or proc.stdin is None:
            self._terminate_worker_bounded()
            return False
        reader = CosyVoiceStdoutReader(proc.stdout)
        reader.start()
        self._reader = reader
        writer = CosyVoiceStdinWriter(proc.stdin)
        writer.start()
        self._stdin_writer = writer
        deadline = time.monotonic() + float(self._settings.startup_timeout_sec)
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                self._terminate_worker_bounded()
                return False
            if not self._epoch_matches(request_epoch):
                self._terminate_worker_bounded()
                return False
            if reader.had_fatal_protocol_error():
                self._terminate_worker_bounded()
                return False
            remaining = deadline - time.monotonic()
            msg = reader.poll_message(timeout=min(_POLL_INTERVAL_SEC, remaining))
            if msg is None:
                if proc.poll() is not None:
                    self._ready = False
                    return False
                continue
            if msg.get("type") == MSG_STARTUP_PROGRESS:
                try:
                    event = self._startup_progress.accept(msg)
                except ValueError:
                    self._terminate_worker_bounded()
                    return False
                with self._spawn_lock:
                    self._startup_progress_snapshot = (
                        self._startup_progress.snapshot()
                    )
                logger.info(
                    "CosyVoice startup stage=%s phase=%s elapsed_ms=%.3f",
                    event.stage,
                    event.phase,
                    event.elapsed_ms,
                )
                continue
            if msg.get("type") == MSG_READY:
                with self._spawn_lock:
                    if (
                        self._closing
                        or self._stop_failed
                        or not self._epoch_matches(request_epoch)
                    ):
                        self._terminate_worker_bounded(deadline=deadline)
                        return False
                    self._startup_progress_snapshot = (
                        self._startup_progress.snapshot()
                    )
                    self._ready = True
                    self._touch_worker_activity()
                return True
            if msg.get("type") == MSG_ERROR:
                with self._spawn_lock:
                    self._startup_progress_snapshot = (
                        self._startup_progress.snapshot()
                    )
                self._terminate_worker_bounded()
                return False
        with self._spawn_lock:
            self._startup_progress_snapshot = self._startup_progress.snapshot()
        self._terminate_worker_bounded()
        return False

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - _MONOTONIC())

    def _wait_proc(self, proc: subprocess.Popen, deadline: float) -> None:
        remaining = self._remaining(deadline)
        if remaining <= 0.0 or proc.poll() is not None:
            return
        try:
            proc.wait(timeout=min(_TERMINATE_GRACE_CAP_SEC, remaining))
        except (subprocess.TimeoutExpired, OSError):
            return

    def _stop_worker_until(self, deadline: float) -> None:
        remaining = self._remaining(deadline)
        if remaining <= 0.0:
            if not self._lifecycle_lock.acquire(blocking=False):
                self._stop_failed = True
                return
            try:
                if self._stop_in_progress:
                    return
                self._stop_in_progress = True
                self._terminate_worker_locked(deadline)
            finally:
                self._stop_in_progress = False
                self._lifecycle_lock.release()
            return
        if not self._lifecycle_lock.acquire(timeout=remaining):
            self._stop_failed = True
            return
        try:
            if self._stop_in_progress:
                return
            self._stop_in_progress = True
            self._terminate_worker_locked(deadline)
        finally:
            self._stop_in_progress = False
            self._lifecycle_lock.release()

    def _terminate_worker_bounded(self, *, deadline: float | None = None) -> None:
        end = (
            deadline
            if deadline is not None
            else _MONOTONIC() + _DEFAULT_STOP_BUDGET_SEC
        )
        self._stop_worker_until(end)

    def _terminate_worker_locked(self, deadline: float) -> None:
        self._ready = False
        proc = self._proc
        reader = self._reader
        writer = self._stdin_writer
        if proc is None and reader is None and writer is None:
            return
        if writer is not None and writer is self._stdin_writer:
            writer.close()
        if proc is not None and proc is self._proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            if self._remaining(deadline) > 0.0:
                self._wait_proc(proc, deadline)
        if proc is not None and proc is self._proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            remaining = self._remaining(deadline)
            if remaining > 0.0:
                try:
                    proc.wait(timeout=min(_KILL_GRACE_CAP_SEC, remaining))
                except (subprocess.TimeoutExpired, OSError):
                    pass
        if writer is not None and writer is self._stdin_writer:
            if not writer.join(timeout=self._remaining(deadline)):
                self._mark_stop_failed()
                return
        if reader is not None and reader is self._reader:
            reader.close()
            if not reader.join(timeout=self._remaining(deadline)):
                self._mark_stop_failed()
                return
        if proc is not None and proc is self._proc:
            if proc.poll() is None:
                self._mark_stop_failed()
                return
            self._clear_process_refs(proc, reader, writer)

    def _mark_stop_failed(self) -> None:
        self._stop_failed = True
        self._last_stop_exited = False

    def _clear_process_refs(
        self,
        proc: subprocess.Popen,
        reader: CosyVoiceStdoutReader | None,
        writer: CosyVoiceStdinWriter | None,
    ) -> None:
        if self._proc is not proc:
            return
        if self._reader is not reader:
            return
        if self._stdin_writer is not writer:
            return
        if proc.poll() is None:
            self._mark_stop_failed()
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._proc = None
        self._reader = None
        self._stdin_writer = None
        self._ready = False

    def _release_process_if_clean(self, *, deadline: float | None = None) -> None:
        end = (
            deadline
            if deadline is not None
            else _MONOTONIC() + _DEFAULT_STOP_BUDGET_SEC
        )
        self._stop_worker_until(end)
