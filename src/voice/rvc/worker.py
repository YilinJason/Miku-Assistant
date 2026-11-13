"""
RVC 常驻 Worker 客户端

通过 stdin/stdout JSON 与 ``scripts/rvc_worker.py`` 通信，模型仅加载一次以加速连续朗读。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Optional, TextIO

from src.core.log_privacy import exception_log_label
from src.core.host_memory import (
    ModelStartupMemoryError,
    STARTUP_MEMORY_USER_HINT,
    check_model_startup_memory,
)
from src.core.owned_worker_job import OwnedWorkerProcess
from src.core.rvc_capabilities import RVC_WORKER_SCRIPT, RvcWorkerLaunch
from src.core.safety import SafetyPolicy
from src.voice.rvc.device import resolve_rvc_device
from src.voice.rvc.infer import ensure_policy_rvc_python, resolve_rvc_python
from src.voice.timing import log_elapsed

logger = logging.getLogger(__name__)

_WORKER_START_TIMEOUT = 60.0
_READER_JOIN_TIMEOUT = 0.5
_STOP_GRACE_SEC = 2.0
_POLL_SLICE_SEC = 0.05


@dataclass(frozen=True)
class _OwnerBinding:
    generation: int
    proc: subprocess.Popen
    reader: Optional["_RVCStdoutReader"]
    stderr_thread: Optional[threading.Thread]


class _RVCStdoutReader:
    """单 worker 持续 stdout JSON reader；容量 1 队列，EOF 不挤占有效消息。"""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=1)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._eof = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._eof = False
        self._thread = threading.Thread(
            target=self._run,
            name="RVCWorkerStdout",
            daemon=True,
        )
        self._thread.start()

    def _enqueue(self, payload: dict) -> None:
        while not self._stop.is_set():
            try:
                self._queue.put(payload, timeout=_POLL_SLICE_SEC)
                return
            except queue.Full:
                continue

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    line = self._stream.readline()
                except Exception:
                    self._eof = True
                    break
                if not line:
                    self._eof = True
                    break
                line = line.strip()
                if not line or not line.startswith("{"):
                    logger.debug("Worker 跳过非协议行 chars=%s", len(line))
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Worker 跳过无效 JSON chars=%s", len(line))
                    continue
                if isinstance(payload, dict):
                    self._enqueue(payload)
        except Exception:
            logger.debug("读取 Worker 协议管道失败", exc_info=True)
            self._eof = True
        finally:
            self._eof = True

    def poll_message(self, timeout: float) -> Optional[dict]:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return None
            wait_for = min(_POLL_SLICE_SEC, remaining)
            try:
                return self._queue.get(timeout=wait_for)
            except queue.Empty:
                if self._stop.is_set() or self._eof:
                    try:
                        return self._queue.get_nowait()
                    except queue.Empty:
                        return None
                continue

    def close(self) -> None:
        self._stop.set()

    def join(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()


def _read_json_line(stream: TextIO, timeout: float = 60.0) -> Optional[dict]:
    """
    有界读取一条 Worker JSON（仅测试/无持久 reader 时的回退）。
    """
    result: queue.Queue[Optional[dict]] = queue.Queue(maxsize=1)

    def _read() -> None:
        payload: Optional[dict] = None
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        except Exception:
            logger.debug("读取 Worker 协议管道失败", exc_info=True)
        try:
            result.put_nowait(payload)
        except queue.Full:
            pass

    threading.Thread(target=_read, name="RVCWorkerStdout", daemon=True).start()
    try:
        return result.get(timeout=max(0.0, timeout))
    except queue.Empty:
        return None


class _WorkLease:
    """``try_acquire_work_lease`` 返回的可释放工作互斥租约。"""

    __slots__ = ("_lock", "_released")

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._lock.release()
            self._released = True

    def __enter__(self) -> "_WorkLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class RVCWorkerClient:
    """管理 ``scripts/rvc_worker.py`` 子进程，模型只加载一次。"""

    def __init__(
        self,
        rvc_config: Dict[str, Any],
        safety_policy: Optional[SafetyPolicy] = None,
        *,
        temp_file_manager: Any = None,
    ):
        if safety_policy is None:
            raise PermissionError("RVCWorkerClient 需要显式共享 SafetyPolicy")
        self.rvc_config = dict(rvc_config)
        self.safety_policy = safety_policy
        self._temp_files = temp_file_manager
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[_RVCStdoutReader] = None
        self._lock = threading.Lock()
        self._spawn_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._start_inflight = False
        self._stop_inflight = False
        self._retiring = False
        self._owner_generation = 0
        self._stdin_writes_inflight = 0
        self._operation_state_lock = threading.Lock()
        self._operation_epoch = 0
        self._ready = False
        _mem = check_model_startup_memory("rvc")
        if not _mem.allowed:
            raise ModelStartupMemoryError(_mem.reason, _mem)
        self._device = resolve_rvc_device(rvc_config, safety_policy=self.safety_policy)
        self._load_ms: Optional[float] = None
        self._converting = threading.Event()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_thread: Optional[threading.Thread] = None
        self._last_stop_exited = True
        self._convert_lease_lock = threading.Lock()
        self._io_lease_token: Optional[str] = None
        self._io_lease_mgr: Any = None
        self._io_lease_owner_generation: Optional[int] = None
        self._io_lease_request_epoch: Optional[int] = None
        self._io_lease_release_failed = False
        self._pin_inflight = False
        self._globally_retired = False

    def try_acquire_work_lease(self) -> Optional["_WorkLease"]:
        """非阻塞取得主工作互斥；调用方须在 finally 释放。"""
        if not self._lock.acquire(blocking=False):
            return None
        return _WorkLease(self._lock)

    def work_mutex_held_by_operation(self) -> bool:
        """Deprecated 观察接口；退休路径须使用 ``try_acquire_work_lease``。"""
        lease = self.try_acquire_work_lease()
        if lease is None:
            return True
        lease.release()
        return False

    def mark_globally_retired(self) -> None:
        """全局单例解绑后的永久退休；旧引用不得再 spawn/start。"""
        with self._spawn_lock:
            self._globally_retired = True
            self._owner_generation += 1

    def bind_temp_file_manager(self, temp_file_manager: Any) -> None:
        with self._convert_lease_lock:
            if self._pin_inflight:
                return
            if (
                self._io_lease_token is not None
                and self._io_lease_mgr is not None
                and temp_file_manager is not self._io_lease_mgr
            ):
                return
        self._temp_files = temp_file_manager

    @property
    def ready(self) -> bool:
        return self._ready and self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        with self._lock:
            return self._start_locked()

    def _stdout_reader_alive(self) -> bool:
        reader = self._reader
        return reader is not None and reader.is_alive()

    def _stderr_reader_alive(self) -> bool:
        thread = self._stderr_thread
        return thread is not None and thread.is_alive()

    def _stdin_write_active(self) -> bool:
        with self._spawn_lock:
            return self._stdin_writes_inflight > 0

    def _restart_blocked(self) -> bool:
        if self._start_inflight or self._stop_inflight or self._retiring:
            return True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return True
        if self._stdout_reader_alive():
            return True
        if proc is not None and self._stderr_reader_alive():
            return True
        if self._stdin_write_active():
            return True
        if proc is not None and not self._last_stop_exited:
            return True
        return False

    def _owner_binding(self) -> Optional[_OwnerBinding]:
        with self._spawn_lock:
            proc = self._proc
            if proc is None:
                return None
            return _OwnerBinding(
                generation=self._owner_generation,
                proc=proc,
                reader=self._reader,
                stderr_thread=self._stderr_thread,
            )

    def _owner_still_current(self, binding: _OwnerBinding) -> bool:
        with self._spawn_lock:
            return (
                self._proc is binding.proc
                and self._reader is binding.reader
                and self._owner_generation == binding.generation
            )

    def _read_worker_json(self, timeout: float) -> Optional[dict]:
        reader = self._reader
        if reader is not None:
            return reader.poll_message(timeout)
        proc = self._proc
        if proc is not None and proc.stdout is not None:
            return _read_json_line(proc.stdout, timeout=timeout)
        return None

    def _publish_ready(self, *, generation: int) -> bool:
        with self._spawn_lock:
            if generation != self._owner_generation:
                return False
            if self._stop_inflight or self._retiring:
                return False
            if self._proc is None or self._proc.poll() is not None:
                return False
            self._ready = True
            return True

    def _install_stdout_reader(self, stream: TextIO) -> _RVCStdoutReader:
        prior = self._reader
        if prior is not None:
            prior.close()
            if not prior.join(_READER_JOIN_TIMEOUT):
                raise RuntimeError("RVC Worker 旧 stdout reader 未结束")
        reader = _RVCStdoutReader(stream)
        with self._spawn_lock:
            self._reader = reader
        reader.start()
        return reader

    def _start_stderr_drain(self, proc: subprocess.Popen) -> Optional[threading.Thread]:
        stream = proc.stderr
        if stream is None:
            return None

        def _drain() -> None:
            try:
                for line in stream:
                    message = line.strip()
                    if message:
                        self._stderr_tail.append(message)
                        logger.debug(
                            "RVC Worker stderr line chars=%s", len(message)
                        )
            except Exception:
                logger.debug("排空 RVC Worker stderr 时结束", exc_info=True)

        thread = threading.Thread(
            target=_drain, name="RVCWorkerStderr", daemon=True
        )
        with self._spawn_lock:
            self._stderr_thread = thread
        thread.start()
        return thread

    def _stdin_write(
        self,
        binding: _OwnerBinding,
        line: str,
        *,
        request_epoch: int,
    ) -> bool:
        if not self._operation_is_current(request_epoch):
            return False
        proc: subprocess.Popen | None = None
        with self._spawn_lock:
            if not self._owner_still_current(binding):
                return False
            if (
                self._start_inflight
                or self._stop_inflight
                or self._retiring
            ):
                return False
            proc = binding.proc
            if proc.stdin is None or proc.poll() is not None:
                return False
            self._stdin_writes_inflight += 1
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
            return True
        except Exception:
            return False
        finally:
            with self._spawn_lock:
                self._stdin_writes_inflight -= 1

    def _startup_still_valid(
        self, binding: _OwnerBinding, *, owner_generation: int
    ) -> bool:
        with self._spawn_lock:
            if owner_generation != self._owner_generation:
                return False
            if not self._owner_still_current(binding):
                return False
            if self._stop_inflight or self._retiring:
                return False
        return True

    def _response_still_valid(
        self,
        binding: _OwnerBinding,
        *,
        request_epoch: int,
        owner_generation: int,
    ) -> bool:
        if not self._operation_is_current(request_epoch):
            return False
        with self._spawn_lock:
            if owner_generation != self._owner_generation:
                return False
            if not self._owner_still_current(binding):
                return False
            if self._stop_inflight or self._start_inflight or self._retiring:
                return False
        return True

    def _start_locked(self) -> bool:
        if getattr(self, "_globally_retired", False):
            return False
        if self.ready:
            return True
        _mem = check_model_startup_memory("rvc")
        if not _mem.allowed:
            logger.error(
                "RVC Worker 启动被拒绝 reason=%s physical_avail=%s commit_avail=%s "
                "required_phys=%s required_commit=%s. %s",
                _mem.reason,
                _mem.physical_available_mb,
                _mem.commit_available_mb,
                _mem.required_physical_mb,
                _mem.required_commit_mb,
                STARTUP_MEMORY_USER_HINT,
            )
            return False
        if self._restart_blocked():
            logger.error(
                "RVC Worker 仍在运行或回收未完成，拒绝在未确认退出前重复启动"
            )
            return False
        binding = self._owner_binding()
        if binding is not None and binding.proc.poll() is not None:
            if not self._try_release_owner(binding):
                logger.error(
                    "RVC Worker 退出后 reader 未结束，拒绝重复启动"
                )
                return False
        self._stderr_tail.clear()

        python = resolve_rvc_python(self.rvc_config)
        if not python.is_file() or not RVC_WORKER_SCRIPT.is_file():
            logger.error("RVC Worker 启动失败：缺少 Python 环境或 scripts/rvc_worker.py")
            return False

        model_path = self.rvc_config.get("model_path", "")
        if not model_path:
            logger.error("RVC Worker 启动失败：未配置 model_path")
            return False

        try:
            ensure_policy_rvc_python(self.safety_policy, self.rvc_config)
        except PermissionError:
            logger.error("RVC Worker 启动被安全策略拒绝")
            return False
        if self.safety_policy.rvc_model() is None:
            logger.error("RVC Worker 启动失败：未封存模型 profile")
            return False

        index_path = self.rvc_config.get("index_path")
        launch = RvcWorkerLaunch(
            model_path=str(model_path),
            device=self._device,
            pitch=int(self.rvc_config.get("pitch", 0)),
            index_rate=float(self.rvc_config.get("index_rate", 0.55)),
            protect=float(self.rvc_config.get("protect", 0.5)),
            filter_radius=int(self.rvc_config.get("filter_radius", 3)),
            rms_mix_rate=float(self.rvc_config.get("rms_mix_rate", 0.75)),
            f0_method=str(self.rvc_config.get("f0_method", "rmvpe")),
            parent_pid=int(os.getpid()),
            index_path=str(index_path) if index_path else None,
        )

        with self._spawn_lock:
            if self._restart_blocked():
                logger.error(
                    "RVC Worker 仍在运行或回收未完成，拒绝在未确认退出前重复启动"
                )
                return False
            self._start_inflight = True
            generation = self._owner_generation

        proc: subprocess.Popen | None = None
        succeeded = False
        preserve_owner = False
        try:
            with log_elapsed("RVC Worker 启动+模型加载"):
                with self._spawn_lock:
                    if getattr(self, "_globally_retired", False):
                        return False
                try:
                    proc = self.safety_policy.spawn_rvc_worker(
                        launch,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="ignore",
                        bufsize=1,
                    )
                    logger.debug("RVC Worker spawn 返回 proc=%s", type(proc).__name__)
                except BaseException as exc:
                    from src.core.owned_worker_job import (
                        take_owned_worker_from_exception,
                    )

                    pending = take_owned_worker_from_exception(exc)
                    if pending is not None:
                        with self._spawn_lock:
                            if self._proc is None:
                                self._proc = pending
                                self._ready = False
                                self._last_stop_exited = False
                    if isinstance(exc, PermissionError):
                        logger.error(
                            "RVC Worker 启动被安全策略拒绝 err=%s",
                            exception_log_label(exc),
                        )
                        return False
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        preserve_owner = True
                        raise
                    logger.error(
                        "RVC Worker 启动异常 err=%s",
                        exception_log_label(exc),
                        exc_info=True,
                    )
                    self.stop()
                    return False

                with self._spawn_lock:
                    self._proc = proc
                    self._ready = False
                    self._last_stop_exited = False

                if proc.stdout is None:
                    self._log_stderr_and_stop("Worker stdout 不可用")
                    return False

                try:
                    reader = self._install_stdout_reader(proc.stdout)
                except RuntimeError:
                    self._log_stderr_and_stop("Worker stdout reader 未结束")
                    return False

                stderr_thread = self._start_stderr_drain(proc)

                if generation != self._owner_generation:
                    self.stop()
                    return False

                binding = _OwnerBinding(
                    generation=generation,
                    proc=proc,
                    reader=reader,
                    stderr_thread=stderr_thread,
                )

                payload = self._read_worker_json(timeout=_WORKER_START_TIMEOUT)
                if not self._startup_still_valid(
                    binding, owner_generation=generation
                ):
                    self.stop()
                    return False
                if not payload:
                    self._log_stderr_and_stop("Worker 无就绪响应")
                    return False
                if not payload.get("ready"):
                    self._log_stderr_and_stop(
                        "Worker 就绪失败",
                        has_payload_error=bool(payload.get("error")),
                    )
                    return False
                if not self._startup_still_valid(
                    binding, owner_generation=generation
                ):
                    self.stop()
                    return False

                self._load_ms = float(payload.get("load_ms", 0))
                self._device = payload.get("device", self._device)
                if not self._publish_ready(generation=generation):
                    self.stop()
                    return False
                logger.info(
                    "[TTS计时] RVC Worker 已就绪 device=%s load=%.0f ms",
                    self._device,
                    self._load_ms,
                )
                succeeded = True
                return True
        except PermissionError as exc:
            logger.error(
                "RVC Worker 启动被安全策略拒绝 err=%s",
                exception_log_label(exc),
            )
            self.stop()
            return False
        except Exception as exc:
            logger.error(
                "RVC Worker 启动异常 err=%s",
                exception_log_label(exc),
                exc_info=True,
            )
            self.stop()
            return False
        finally:
            with self._spawn_lock:
                self._start_inflight = False
            if not succeeded and not preserve_owner:
                self._retry_release_after_startup()

    def _retry_release_after_startup(self) -> None:
        deadline = time.monotonic() + _STOP_GRACE_SEC
        while time.monotonic() < deadline:
            binding = self._owner_binding()
            if binding is None:
                return
            proc = binding.proc
            if proc.poll() is None:
                time.sleep(0.05)
                continue
            try:
                if self._try_release_owner(binding):
                    return
            except Exception:
                with self._spawn_lock:
                    if self._owner_still_current(binding):
                        self._last_stop_exited = False
                return
            time.sleep(0.05)
        with self._spawn_lock:
            binding = self._owner_binding()
            if binding is not None and binding.proc.poll() is not None:
                self._last_stop_exited = False

    @property
    def converting(self) -> bool:
        return self._converting.is_set()

    def _operation_epoch_snapshot(self) -> int:
        with self._operation_state_lock:
            return self._operation_epoch

    def _begin_operation(self, request_epoch: int) -> bool:
        with self._operation_state_lock:
            if request_epoch != self._operation_epoch:
                return False
            self._converting.set()
            return True

    def _operation_is_current(self, request_epoch: int) -> bool:
        with self._operation_state_lock:
            return request_epoch == self._operation_epoch

    def _convert_io_blocks_new_pin_locked(self) -> bool:
        if self._pin_inflight:
            return True
        token = self._io_lease_token
        if token is None:
            return False
        if self._io_lease_release_failed:
            return True
        mgr = self._io_lease_mgr
        if mgr is None:
            return True
        try:
            return bool(mgr.io_lease_active(token))
        except BaseException:
            return True

    def _convert_io_blocks_new_pin(self) -> bool:
        with self._convert_lease_lock:
            return self._convert_io_blocks_new_pin_locked()

    def _clear_convert_io_lease_locked(self) -> None:
        self._io_lease_token = None
        self._io_lease_mgr = None
        self._io_lease_owner_generation = None
        self._io_lease_request_epoch = None
        self._io_lease_release_failed = False

    def _try_release_convert_io_locked(self) -> bool:
        token = self._io_lease_token
        mgr = self._io_lease_mgr
        if not token or mgr is None:
            self._clear_convert_io_lease_locked()
            return True
        try:
            mgr.release_io_lease(token)
            if mgr.io_lease_active(token):
                self._io_lease_release_failed = True
                return False
        except BaseException:
            self._io_lease_release_failed = True
            return False
        self._clear_convert_io_lease_locked()
        return True

    def _pin_convert_io(
        self,
        input_path: str,
        output_path: str,
        *,
        request_epoch: int,
        owner_generation: int,
    ) -> bool:
        with self._convert_lease_lock:
            if self._io_lease_token is not None or self._convert_io_blocks_new_pin_locked():
                return False
            mgr = self._temp_files
            if mgr is None:
                return False
            self._pin_inflight = True
        try:
            try:
                token = mgr.pin_io_paths((input_path, output_path))
            except BaseException:
                return False
            with self._convert_lease_lock:
                self._io_lease_token = token
                self._io_lease_mgr = mgr
                self._io_lease_owner_generation = owner_generation
                self._io_lease_request_epoch = request_epoch
                self._io_lease_release_failed = False
                return True
        finally:
            with self._convert_lease_lock:
                self._pin_inflight = False

    def _release_convert_io_terminal(
        self,
        *,
        request_epoch: int,
        owner_generation: int,
    ) -> bool:
        with self._convert_lease_lock:
            if self._io_lease_token is None:
                return True
            if self._io_lease_request_epoch != request_epoch:
                return False
            if self._io_lease_owner_generation != owner_generation:
                return False
            return self._try_release_convert_io_locked()

    def _release_convert_io_if_owner_quiescent(self) -> bool:
        with self._convert_lease_lock:
            if self._io_lease_token is None:
                return True
            return self._try_release_convert_io_locked()

    def process_alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _release_exited_process(self, proc: subprocess.Popen) -> None:
        binding = self._owner_binding()
        if binding is not None and binding.proc is proc:
            self._try_release_owner(binding)

    def _try_release_process(self, proc: subprocess.Popen) -> bool:
        binding = self._owner_binding()
        if binding is None or binding.proc is not proc:
            return False
        return self._try_release_owner(binding)

    def _try_release_owner(self, binding: _OwnerBinding) -> bool:
        proc = binding.proc
        if proc.poll() is None:
            return False
        if isinstance(proc, OwnedWorkerProcess):
            proc.poll()
        with self._spawn_lock:
            if self._start_inflight:
                if self._owner_still_current(binding):
                    self._last_stop_exited = False
                return False
        if self._stdin_write_active():
            with self._spawn_lock:
                if self._owner_still_current(binding):
                    self._last_stop_exited = False
            return False
        reader = binding.reader
        if reader is not None:
            reader.close()
            if not reader.join(timeout=_READER_JOIN_TIMEOUT):
                with self._spawn_lock:
                    if self._owner_still_current(binding):
                        self._last_stop_exited = False
                return False
        thread = binding.stderr_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_READER_JOIN_TIMEOUT)
            if thread.is_alive():
                with self._spawn_lock:
                    if self._owner_still_current(binding):
                        self._last_stop_exited = False
                return False
        self._close_process_streams(proc, close_stdin=not self._stdin_write_active())
        with self._spawn_lock:
            if not self._owner_still_current(binding):
                return False
            if self._start_inflight:
                self._last_stop_exited = False
                return False
            self._proc = None
            self._reader = None
            self._stderr_thread = None
            self._ready = False
        if not self._release_convert_io_if_owner_quiescent():
            self._last_stop_exited = False
            return False
        return True

    def _public_stop_succeeded(self) -> bool:
        """public 层：client 完整静止（仅观察，不等待）。"""
        if self._proc is not None:
            self._last_stop_exited = False
            return False
        if self._start_inflight:
            self._last_stop_exited = False
            return False
        if self._pin_inflight or self._converting.is_set():
            self._last_stop_exited = False
            return False
        if self._stdout_reader_alive() or self._stderr_reader_alive():
            self._last_stop_exited = False
            return False
        if self._stdin_write_active():
            self._last_stop_exited = False
            return False
        if self._io_lease_release_failed:
            self._last_stop_exited = False
            return False
        if self._convert_io_blocks_new_pin():
            self._last_stop_exited = False
            return False
        with self._convert_lease_lock:
            if self._io_lease_token is not None:
                self._last_stop_exited = False
                return False
        self._last_stop_exited = True
        return True

    def _sync_after_terminate_attempt(self, proc: subprocess.Popen) -> None:
        self._ready = False
        binding = self._owner_binding()
        if binding is None or binding.proc is not proc:
            return
        if proc.poll() is not None:
            if not self._try_release_owner(binding):
                self._last_stop_exited = False
        else:
            self._last_stop_exited = False

    @property
    def last_stop_exited(self) -> bool:
        return self._last_stop_exited

    def interrupt_current(self) -> bool:
        with self._operation_state_lock:
            self._operation_epoch += 1
            active = self._converting.is_set()
        if active:
            self.stop()
        return active

    def warm_up(self, timeout: float = 45.0) -> bool:
        request_epoch = self._operation_epoch_snapshot()
        with self._lock:
            if not self._begin_operation(request_epoch):
                return False
            try:
                if not self._start_locked():
                    return False
                if not self._operation_is_current(request_epoch):
                    self.stop()
                    return False
                binding = self._owner_binding()
                if binding is None or not self.ready:
                    return False
                owner_generation = binding.generation
                line = json.dumps({"cmd": "warmup"}) + "\n"
                with log_elapsed("RVC Worker 首次推理预热"):
                    if not self._stdin_write(
                        binding, line, request_epoch=request_epoch
                    ):
                        return False
                    payload = self._read_worker_json(timeout=timeout)
                if not self._response_still_valid(
                    binding,
                    request_epoch=request_epoch,
                    owner_generation=owner_generation,
                ):
                    return False
                if not payload:
                    self._terminate_process(binding.proc)
                    self._sync_after_terminate_attempt(binding.proc)
                    return False
                if payload.get("ok") and payload.get("warmup"):
                    if not self._response_still_valid(
                        binding,
                        request_epoch=request_epoch,
                        owner_generation=owner_generation,
                    ):
                        return False
                    logger.info(
                        "[TTS计时] RVC Worker 预热 infer=%s ms total=%s ms",
                        payload.get("infer_ms"),
                        payload.get("total_ms"),
                    )
                    return True
                logger.warning(
                    "RVC Worker 首次推理预热失败 has_error=%s",
                    bool(payload.get("error")),
                )
                return False
            finally:
                self._converting.clear()

    def convert(
        self, input_path: str, output_path: str, timeout: float = 45.0
    ) -> bool:
        try:
            from src.core.safety import Operation

            self.safety_policy.require(Operation.RVC_WORKER, "rvc_worker")
            if self._temp_files is None:
                logger.error("RVC Worker convert 拒绝: 缺少 TempFileManager")
                return False
            self._temp_files.assert_rvc_io_paths(input_path, output_path)
        except PermissionError:
            logger.error("RVC Worker convert 被安全策略拒绝")
            return False
        if self._convert_io_blocks_new_pin():
            logger.error("RVC Worker convert 拒绝: 前次 IO lease 未回收")
            return False
        req = {
            "input": input_path,
            "output": output_path,
            "pitch": int(self.rvc_config.get("pitch", 0)),
            "index_rate": float(self.rvc_config.get("index_rate", 0.55)),
        }

        request_epoch = self._operation_epoch_snapshot()
        with self._lock:
            if not self._begin_operation(request_epoch):
                return False
            if not self._start_locked():
                self._converting.clear()
                return False
            if not self._operation_is_current(request_epoch):
                self.stop()
                self._converting.clear()
                return False
            binding = self._owner_binding()
            if (
                binding is None
                or not self.ready
                or binding.proc.stdin is None
                or binding.proc.stdout is None
            ):
                self._converting.clear()
                return False
            owner_generation = binding.generation
            try:
                if not self._pin_convert_io(
                    input_path,
                    output_path,
                    request_epoch=request_epoch,
                    owner_generation=owner_generation,
                ):
                    return False
                line = json.dumps(req, ensure_ascii=False) + "\n"
                with log_elapsed("RVC Worker 推理"):
                    if not self._stdin_write(
                        binding, line, request_epoch=request_epoch
                    ):
                        return False
                    payload = self._read_worker_json(timeout=timeout)
                if not self._response_still_valid(
                    binding,
                    request_epoch=request_epoch,
                    owner_generation=owner_generation,
                ):
                    return False
                if not payload:
                    self._terminate_process(binding.proc)
                    self._sync_after_terminate_attempt(binding.proc)
                    return False
                if payload.get("ok"):
                    if not self._response_still_valid(
                        binding,
                        request_epoch=request_epoch,
                        owner_generation=owner_generation,
                    ):
                        return False
                    infer_ms = payload.get("infer_ms")
                    total_ms = payload.get("total_ms")
                    logger.info(
                        "[TTS计时] RVC Worker 单段 infer=%s ms total=%s ms",
                        infer_ms,
                        total_ms,
                    )
                    self._release_convert_io_terminal(
                        request_epoch=request_epoch,
                        owner_generation=owner_generation,
                    )
                    return True
                logger.error(
                    "RVC Worker 转换失败 has_error=%s",
                    bool(payload.get("error")),
                )
                if binding.proc.poll() is not None:
                    self._ready = False
                self._release_convert_io_terminal(
                    request_epoch=request_epoch,
                    owner_generation=owner_generation,
                )
                return False
            except Exception as exc:
                logger.error(
                    "RVC Worker 通信异常 err=%s",
                    exception_log_label(exc),
                    exc_info=True,
                )
                self._terminate_process(binding.proc)
                self._sync_after_terminate_attempt(binding.proc)
                return False
            finally:
                self._converting.clear()

    def stop(self) -> bool:
        if not self._lifecycle_lock.acquire(blocking=False):
            return False
        try:
            return self._stop_locked()
        finally:
            self._lifecycle_lock.release()

    def _stop_locked(self) -> bool:
        self._ready = False
        with self._spawn_lock:
            self._owner_generation += 1
            self._stop_inflight = True
            self._retiring = True
        try:
            if self._pin_inflight:
                binding = self._owner_binding()
                if binding is not None:
                    proc = binding.proc
                    if proc.poll() is None:
                        self._terminate_process(proc)
                self._last_stop_exited = False
                return False
            if self._start_inflight and self._proc is None:
                self._last_stop_exited = False
                return False
            binding = self._owner_binding()
            if binding is None:
                if self._converting.is_set() or self._pin_inflight:
                    self._last_stop_exited = False
                    return False
                if self._start_inflight or self._stdout_reader_alive():
                    self._last_stop_exited = False
                    return False
                if self._pin_inflight:
                    self._last_stop_exited = False
                    return False
                if not self._release_convert_io_if_owner_quiescent():
                    self._last_stop_exited = False
                    return False
                return self._public_stop_succeeded()

            proc = binding.proc
            active = self._converting.is_set()
            idle_owned = (
                not active
                and sys.platform == "win32"
                and isinstance(proc, OwnedWorkerProcess)
            )
            if active:
                self._terminate_process(proc)
            elif idle_owned:
                self._terminate_process(proc)
            else:
                if not self._stdin_write_active():
                    try:
                        if proc.stdin and proc.poll() is None:
                            proc.stdin.write(
                                json.dumps({"cmd": "shutdown"}) + "\n"
                            )
                            proc.stdin.flush()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=_STOP_GRACE_SEC)
                except subprocess.TimeoutExpired:
                    self._terminate_process(proc)

            if proc.poll() is not None and self._try_release_owner(binding):
                if self._public_stop_succeeded():
                    logger.info("RVC Worker 已停止")
                    return True
                return False
            if self._proc is None:
                return self._public_stop_succeeded()
            deadline = time.monotonic() + _STOP_GRACE_SEC
            while time.monotonic() < deadline:
                if self._proc is None:
                    return self._public_stop_succeeded()
                current = self._owner_binding()
                if current is None or current.proc is not proc:
                    return self._public_stop_succeeded()
                if proc.poll() is not None and self._try_release_owner(binding):
                    if self._public_stop_succeeded():
                        logger.info("RVC Worker 已停止")
                        return True
                    return False
                if self._stdin_write_active():
                    time.sleep(0.05)
                    continue
                time.sleep(0.05)
            self._last_stop_exited = False
            logger.error("RVC Worker 停止后进程或 reader 仍在运行")
            return False
        finally:
            with self._spawn_lock:
                self._stop_inflight = False
                self._retiring = False

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                logger.error("RVC Worker 强制终止后仍未退出")

    @staticmethod
    def _close_process_streams(
        proc: subprocess.Popen, *, close_stdin: bool = True
    ) -> None:
        streams = []
        if close_stdin:
            streams.append(proc.stdin)
        streams.extend((proc.stdout, proc.stderr))
        for stream in streams:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def _log_stderr_and_stop(
        self, reason: str, *, has_payload_error: bool = False
    ) -> None:
        logger.error(
            "RVC Worker 启动失败 reason=%s stderr_lines=%s has_payload_error=%s",
            reason,
            len(self._stderr_tail),
            has_payload_error,
        )
        self.stop()
