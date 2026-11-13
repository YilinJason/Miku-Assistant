# -*- coding: utf-8 -*-
"""
Windows 单次 RVC infer/probe 归属：命名 Job + 有界 stdout/stderr 捕获。

仅由 ``SafetyPolicy.run_rvc_infer`` / ``run_rvc_probe``（win32）调用。
"""
from __future__ import annotations

import codecs
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from src.voice.rvc.cpu_budget import child_env_with_rvc_cpu_budget

from src.core.owned_worker_job import (
    OwnedWorkerJobBackend,
    OwnedWorkerProcess,
    spawn_owned_voice_worker,
    take_owned_worker_from_exception,
)

_CAPTURE_LIMIT = 65536
_READ_CHUNK = 4096
_POLL_SLEEP_SEC = 0.05
_DEFAULT_RETIRE_BUDGET_SEC = 2.0
_SPAWN_CLEANUP_BUDGET_SEC = 0.05
_RUN_EXCEPTION_ATTR = "_miku_owned_rvc_run_record"


@dataclass(frozen=True)
class CaptureMeta:
    """截断元信息（不含原始 stderr/音频内容）。"""

    stdout_truncated: bool
    stderr_truncated: bool
    stdout_bytes: int
    stderr_bytes: int


class _CaptureBuffer:
    __slots__ = ("_data", "_total", "read_error")

    def __init__(self) -> None:
        self._data = bytearray()
        self._total = 0
        self.read_error = False

    def feed(self, chunk: bytes) -> bool:
        if not chunk:
            return False
        self._total += len(chunk)
        truncated = False
        self._data.extend(chunk)
        overflow = len(self._data) - _CAPTURE_LIMIT
        if overflow > 0:
            del self._data[:overflow]
            truncated = True
        return truncated

    def as_bytes(self) -> bytes:
        return bytes(self._data)

    def as_text(self, *, encoding: str, errors: str) -> str:
        return self._data.decode(encoding, errors=errors)


class _StreamCapturer(threading.Thread):
    def __init__(
        self,
        stream: Any,
        buffer: _CaptureBuffer,
        stop_event: threading.Event,
        *,
        truncated_flag: list[bool],
    ) -> None:
        super().__init__(name="rvc-run-capture", daemon=True)
        self._stream = stream
        self._buffer = buffer
        self._stop_event = stop_event
        self._truncated_flag = truncated_flag

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = self._stream.read(_READ_CHUNK)
                except BaseException:
                    self._buffer.read_error = True
                    break
                if not chunk:
                    break
                if self._buffer.feed(chunk):
                    self._truncated_flag[0] = True
        finally:
            try:
                self._stream.close()
            except OSError:
                pass


@dataclass
class _RvcRunRecord:
    generation: int
    starting: bool = True
    cancel_requested: bool = False
    owned: Optional[OwnedWorkerProcess] = None
    stdout_capturer: Optional[_StreamCapturer] = None
    stderr_capturer: Optional[_StreamCapturer] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    io_lease_token: Optional[str] = None
    temp_file_manager: Any = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    capture_meta: Optional[CaptureMeta] = None


def attach_owned_run_to_exception(exc: BaseException, record: _RvcRunRecord) -> None:
    if getattr(exc, _RUN_EXCEPTION_ATTR, None) is not None:
        return
    setattr(exc, _RUN_EXCEPTION_ATTR, record)


def take_owned_run_from_exception(exc: BaseException) -> Optional[_RvcRunRecord]:
    record = getattr(exc, _RUN_EXCEPTION_ATTR, None)
    if not isinstance(record, _RvcRunRecord):
        return None
    try:
        delattr(exc, _RUN_EXCEPTION_ATTR)
    except AttributeError:
        pass
    return record


def _validate_timeout(value: Any, *, max_budget: float) -> float:
    if value is None:
        timeout = float(max_budget)
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PermissionError("RVC 子进程能力拒绝: 无效 timeout")
        timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise PermissionError("RVC 子进程能力拒绝: 无效 timeout")
    if timeout > float(max_budget):
        raise PermissionError("RVC 子进程能力拒绝: timeout 超出固定 budget")
    return timeout


def _validate_retire_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return 0.0
    value = float(timeout)
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


def _decode_run_kwargs(
    run_kwargs: Mapping[str, Any],
) -> Tuple[bool, str, str]:
    """与 CPython 3.12 ``Popen`` 一致：``encoding`` 或显式 ``errors`` 或 ``text`` 启用 text 模式。"""
    if not bool(run_kwargs.get("capture_output", False)):
        raise PermissionError("RVC 子进程能力拒绝: 必须 capture_output")

    encoding_opt = run_kwargs.get("encoding")
    errors_opt = run_kwargs.get("errors")
    text_flag = bool(run_kwargs.get("text", False))
    text_mode = text_flag or encoding_opt is not None or errors_opt is not None

    if encoding_opt is not None:
        encoding = str(encoding_opt)
        try:
            codecs.lookup(encoding)
        except LookupError as exc:
            raise PermissionError("RVC 子进程能力拒绝: 无效 encoding") from exc
    else:
        encoding = "utf-8"

    if errors_opt is not None:
        errors = str(errors_opt)
        try:
            codecs.lookup_error(errors)
        except LookupError as exc:
            raise PermissionError("RVC 子进程能力拒绝: 无效 errors") from exc
    else:
        errors = "strict"

    return text_mode, encoding, errors


class OwnedRvcRunCoordinator:
    """每个 SafetyPolicy 至多一条单次 run 记录。"""

    def __init__(self, policy: Any) -> None:
        self._policy = policy
        self._lock = threading.RLock()
        self._record: Optional[_RvcRunRecord] = None
        self._generation = 0
        self._retiring = False
        self._run_inflight = 0
        self.last_capture_meta: Optional[CaptureMeta] = None

    def quiescent(self) -> bool:
        with self._lock:
            if self._run_inflight > 0:
                return False
            return self._fully_quiescent_locked()

    def _io_lease_active_safe(self, rec: _RvcRunRecord) -> bool:
        token = rec.io_lease_token
        mgr = rec.temp_file_manager
        if not token or mgr is None:
            return False
        try:
            return bool(mgr.io_lease_active(token))
        except BaseException:
            return True

    def _execution_quiescent_locked(self, rec: Optional[_RvcRunRecord] = None) -> bool:
        target = rec if rec is not None else self._record
        if target is None:
            return True
        if target.starting:
            return False
        owned = target.owned
        if owned is not None:
            try:
                if not owned._is_spawn_quiescent():  # noqa: SLF001
                    return False
            except BaseException:
                return False
        for cap in (target.stdout_capturer, target.stderr_capturer):
            if cap is not None and cap.is_alive():
                return False
        return True

    def _fully_quiescent_locked(self, rec: Optional[_RvcRunRecord] = None) -> bool:
        target = rec if rec is not None else self._record
        if target is None:
            return True
        if not self._execution_quiescent_locked(target):
            return False
        if self._io_lease_active_safe(target):
            return False
        return True

    def _end_execute(self, generation: int) -> None:
        """仅持有本次 reserve generation 的 caller 可清除 active。"""
        with self._lock:
            rec = self._record
            if rec is not None and rec.generation != generation:
                return
            self._run_inflight = 0

    def _creator_ack(self, rec: _RvcRunRecord) -> None:
        rec.starting = False

    def _adopt_pending_owner(self, rec: _RvcRunRecord, exc: BaseException) -> None:
        pending = take_owned_worker_from_exception(exc)
        if pending is not None:
            with self._lock:
                if self._record is rec and rec.owned is None:
                    rec.owned = pending
        if rec.starting:
            self._creator_ack(rec)

    def retire(self, timeout: float = _DEFAULT_RETIRE_BUDGET_SEC) -> bool:
        budget = _validate_retire_timeout(timeout)
        deadline = time.monotonic() + budget
        with self._lock:
            run_active = self._run_inflight > 0
            if self._record is None:
                return not run_active
            rec = self._record
            rec.cancel_requested = True
            rec.stop_event.set()
            owned = rec.owned
            starting = rec.starting or run_active
        if owned is not None:
            try:
                owned.kill()
            except BaseException:
                pass
        if starting:
            return False
        with self._lock:
            if self._retiring:
                return False
            self._retiring = True
        try:
            return self._retire_record(rec, deadline)
        finally:
            with self._lock:
                self._retiring = False

    def _retire_record(self, rec: _RvcRunRecord, deadline: float) -> bool:
        if rec.starting:
            rec.stop_event.set()
            owned = rec.owned
            if owned is not None:
                try:
                    owned.kill()
                except BaseException:
                    pass
            return False

        rec.stop_event.set()
        owned = rec.owned
        if owned is not None:
            try:
                owned.kill()
            except BaseException:
                pass
            rem = max(0.0, deadline - time.monotonic())
            if rem > 0:
                try:
                    owned.wait(timeout=rem)
                except BaseException:
                    pass
        if not self._join_capturers(rec, deadline):
            return False
        if owned is not None:
            try:
                if not owned._is_spawn_quiescent():  # noqa: SLF001
                    return False
            except BaseException:
                return False
        if not self._try_release_io_lease(rec):
            return False
        with self._lock:
            if self._record is rec and self._fully_quiescent_locked(rec):
                self._record = None
        return True

    def _ensure_previous_reclaimed(self) -> Tuple[_RvcRunRecord, int]:
        """原子 compare-and-reserve：``_run_inflight`` 与 starting record 同锁发布（probe 可 hook）。"""
        with self._lock:
            if self._retiring:
                raise PermissionError("RVC 子进程能力拒绝: 回收进行中")
            if self._run_inflight > 0:
                raise PermissionError("RVC 子进程能力拒绝: 活跃 run 进行中")
            current = self._record
            if current is not None and not self._fully_quiescent_locked(current):
                if current.starting:
                    raise PermissionError("RVC 子进程能力拒绝: 前次 run 启动未完成")
                raise PermissionError("RVC 子进程能力拒绝: 前次 run 未静止")
            self._run_inflight = 1
            self._generation += 1
            gen = self._generation
            rec = _RvcRunRecord(generation=gen, starting=True)
            self._record = rec
            return rec, gen

    def _try_release_io_lease(self, rec: _RvcRunRecord) -> bool:
        token = rec.io_lease_token
        mgr = rec.temp_file_manager
        if not token or mgr is None:
            rec.io_lease_token = None
            return True
        try:
            mgr.release_io_lease(token)
        except BaseException:
            return False
        try:
            if mgr.io_lease_active(token):
                return False
        except BaseException:
            return False
        rec.io_lease_token = None
        return True

    def _capturer_started(self, cap: _StreamCapturer) -> bool:
        return cap.ident is not None

    def _join_capturers(self, rec: _RvcRunRecord, deadline: float) -> bool:
        for cap in (rec.stdout_capturer, rec.stderr_capturer):
            if cap is None or not self._capturer_started(cap):
                continue
            rem = max(0.0, deadline - time.monotonic())
            if rem > 0:
                cap.join(timeout=rem)
            if cap.is_alive():
                return False
        return True

    def _finalize_failed_run(
        self, rec: _RvcRunRecord, deadline: float
    ) -> bool:
        return self._retire_record(rec, deadline)

    def execute(
        self,
        argv: Sequence[str],
        *,
        parent_pid: int,
        popen_fn: Callable[..., subprocess.Popen],
        job_backend: Optional[OwnedWorkerJobBackend],
        max_timeout_budget: float,
        io_paths: Optional[Tuple[str, str]] = None,
        temp_file_manager: Any = None,
        **run_kwargs: Any,
    ) -> subprocess.CompletedProcess:
        timeout = _validate_timeout(
            run_kwargs.get("timeout"), max_budget=max_timeout_budget
        )
        text, encoding, errors = _decode_run_kwargs(run_kwargs)
        check = bool(run_kwargs.get("check", False))

        rec: Optional[_RvcRunRecord] = None
        reserved_gen: Optional[int] = None
        cleanup_deadline = 0.0
        try:
            rec, gen = self._ensure_previous_reclaimed()
            reserved_gen = gen

            if io_paths and temp_file_manager is not None:
                try:
                    token = temp_file_manager.pin_io_paths(io_paths)
                except BaseException as exc:
                    self._creator_ack(rec)
                    self._finalize_failed_run(
                        rec, time.monotonic() + _SPAWN_CLEANUP_BUDGET_SEC
                    )
                    raise PermissionError(
                        "RVC 子进程能力拒绝: IO lease 失败"
                    ) from exc
                rec.io_lease_token = token
                rec.temp_file_manager = temp_file_manager

            stdout_buf = _CaptureBuffer()
            stderr_buf = _CaptureBuffer()
            stdout_trunc = [False]
            stderr_trunc = [False]
            stdout_cap: Optional[_StreamCapturer] = None
            stderr_cap: Optional[_StreamCapturer] = None

            popen_kwargs: Dict[str, Any] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "env": child_env_with_rvc_cpu_budget(os.environ),
            }

            try:
                owned = spawn_owned_voice_worker(
                    list(argv),
                    popen_kwargs,
                    popen_fn=popen_fn,
                    parent_pid=parent_pid,
                    job_backend=job_backend,
                )
                with self._lock:
                    if self._record is not rec or rec.generation != gen:
                        raise PermissionError("RVC 子进程能力拒绝: run 已取消")
                    rec.owned = owned
                    cancelled = rec.cancel_requested or rec.stop_event.is_set()
                    if owned.stdout is not None and not cancelled:
                        stdout_cap = _StreamCapturer(
                            owned.stdout,
                            stdout_buf,
                            rec.stop_event,
                            truncated_flag=stdout_trunc,
                        )
                        rec.stdout_capturer = stdout_cap
                    if owned.stderr is not None and not cancelled:
                        stderr_cap = _StreamCapturer(
                            owned.stderr,
                            stderr_buf,
                            rec.stop_event,
                            truncated_flag=stderr_trunc,
                        )
                        rec.stderr_capturer = stderr_cap
                if cancelled:
                    try:
                        owned.kill()
                    except BaseException:
                        pass
                    self._creator_ack(rec)
                    raise PermissionError("RVC 子进程能力拒绝: run 已取消")
                if stdout_cap is not None:
                    stdout_cap.start()
                if stderr_cap is not None:
                    stderr_cap.start()
            except BaseException as exc:
                self._adopt_pending_owner(rec, exc)
                raise

            self._creator_ack(rec)

            cleanup_deadline = time.monotonic() + timeout + _DEFAULT_RETIRE_BUDGET_SEC
            run_deadline = time.monotonic() + timeout
            expired = False
            cancelled = False
            while time.monotonic() < run_deadline:
                if owned.poll() is not None:
                    break
                if rec.stop_event.is_set() or rec.cancel_requested:
                    cancelled = True
                    try:
                        owned.kill()
                    except BaseException:
                        pass
                    break
                time.sleep(_POLL_SLEEP_SEC)
            else:
                expired = True
                rec.stop_event.set()
                try:
                    owned.kill()
                except BaseException:
                    pass

            if not expired and not cancelled:
                rem = max(0.0, run_deadline - time.monotonic())
                if rem > 0:
                    try:
                        owned.wait(timeout=rem)
                    except subprocess.TimeoutExpired:
                        expired = True

            if not self._join_capturers(rec, cleanup_deadline):
                expired = True

            rec.stdout_truncated = stdout_trunc[0]
            rec.stderr_truncated = stderr_trunc[0]
            meta = CaptureMeta(
                stdout_truncated=stdout_trunc[0],
                stderr_truncated=stderr_trunc[0],
                stdout_bytes=stdout_buf._total,
                stderr_bytes=stderr_buf._total,
            )
            rec.capture_meta = meta
            self.last_capture_meta = meta

            if stdout_buf.read_error or stderr_buf.read_error:
                raise RuntimeError("RVC 子进程 capture 读取失败")

            if cancelled or rec.cancel_requested:
                raise PermissionError("RVC 子进程能力拒绝: run 已取消")

            if expired:
                raise subprocess.TimeoutExpired(list(argv), timeout)

            try:
                if not owned._is_spawn_quiescent():  # noqa: SLF001
                    raise PermissionError("RVC 子进程能力拒绝: run 退出未确认")
            except BaseException as exc:
                if not isinstance(exc, PermissionError):
                    raise PermissionError(
                        "RVC 子进程能力拒绝: run 退出未确认"
                    ) from exc
                raise

            returncode = owned.poll()
            if returncode is None:
                returncode = -1

            if text:
                stdout_val: Any = stdout_buf.as_text(
                    encoding=encoding, errors=errors
                )
                stderr_val: Any = stderr_buf.as_text(
                    encoding=encoding, errors=errors
                )
            else:
                stdout_val = stdout_buf.as_bytes()
                stderr_val = stderr_buf.as_bytes()

            result = subprocess.CompletedProcess(
                list(argv),
                returncode,
                stdout=stdout_val,
                stderr=stderr_val,
            )
            if check and returncode != 0:
                raise subprocess.CalledProcessError(
                    returncode, list(argv), output=stdout_val, stderr=stderr_val
                )

            if rec.cancel_requested:
                raise PermissionError("RVC 子进程能力拒绝: run 已取消")

            if not self._try_release_io_lease(rec):
                raise PermissionError("RVC IO lease 释放失败")
            with self._lock:
                if self._record is rec and self._fully_quiescent_locked(rec):
                    self._record = None
            return result
        except BaseException as exc:
            if rec is not None:
                self._adopt_pending_owner(rec, exc)
                fail_deadline = cleanup_deadline
                if fail_deadline <= 0:
                    fail_deadline = (
                        time.monotonic() + timeout + _DEFAULT_RETIRE_BUDGET_SEC
                    )
                if not self._finalize_failed_run(rec, fail_deadline):
                    attach_owned_run_to_exception(exc, rec)
            raise
        finally:
            if reserved_gen is not None:
                self._end_execute(reserved_gen)


def coordinator_for_policy(policy: Any) -> OwnedRvcRunCoordinator:
    lock = getattr(policy, "_owned_rvc_run_factory_lock", None)
    if lock is None:
        coord = getattr(policy, "_owned_rvc_run_coordinator", None)
        if coord is None:
            coord = OwnedRvcRunCoordinator(policy)
            policy._owned_rvc_run_coordinator = coord
        return coord
    with lock:
        coord = getattr(policy, "_owned_rvc_run_coordinator", None)
        if coord is None:
            coord = OwnedRvcRunCoordinator(policy)
            policy._owned_rvc_run_coordinator = coord
        return coord


def execute_owned_rvc_run(
    policy: Any,
    argv: Sequence[str],
    *,
    max_timeout_budget: float,
    io_paths: Optional[Tuple[str, str]] = None,
    temp_file_manager: Any = None,
    **run_kwargs: Any,
) -> subprocess.CompletedProcess:
    if sys.platform != "win32":
        raise RuntimeError("owned rvc run requires win32")
    parent_pid = os.getpid()
    coord = coordinator_for_policy(policy)
    return coord.execute(
        argv,
        parent_pid=parent_pid,
        popen_fn=policy._popen_fn,
        job_backend=policy._owned_worker_job_backend,
        max_timeout_budget=max_timeout_budget,
        io_paths=io_paths,
        temp_file_manager=temp_file_manager,
        **run_kwargs,
    )


def retire_policy_rvc_runs(policy: Any, timeout: float = _DEFAULT_RETIRE_BUDGET_SEC) -> bool:
    coord = getattr(policy, "_owned_rvc_run_coordinator", None)
    if coord is None:
        return True
    return coord.retire(timeout=timeout) is True


def policy_rvc_runs_quiescent(policy: Any) -> bool:
    coord = getattr(policy, "_owned_rvc_run_coordinator", None)
    if coord is None:
        return True
    return coord.quiescent() is True
