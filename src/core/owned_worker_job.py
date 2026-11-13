# -*- coding: utf-8 -*-
"""
Windows 本地 TTS runtime 解释器归属：父进程命名 Job + 子进程自加入。

仅由 ``SafetyPolicy.spawn_local_tts_worker``（Windows）与 runtime worker 早期入口使用；
非 Windows 与纯 import 路径无副作用。
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence

JOB_ENV_NAME = "MIKU_LOCAL_TTS_OWNED_JOB_NAME"
JOB_ENV_PARENT_PID = "MIKU_LOCAL_TTS_OWNED_JOB_PARENT_PID"
_JOB_NAME_PREFIX = "MikuLocalTts_"
_POLL_SLEEP_SEC = 0.05
_SPAWN_CLEANUP_BUDGET_SEC = 0.05
_OWNED_WORKER_EXCEPTION_ATTR = "_miku_owned_worker_process"
_SPAWN_FAILURE_EXITCODE = 2


def attach_owned_worker_to_exception(
    exc: BaseException,
    owner: OwnedWorkerProcess,
) -> None:
    if getattr(exc, _OWNED_WORKER_EXCEPTION_ATTR, None) is not None:
        return
    setattr(exc, _OWNED_WORKER_EXCEPTION_ATTR, owner)


def take_owned_worker_from_exception(
    exc: BaseException,
) -> Optional["OwnedWorkerProcess"]:
    owner = getattr(exc, _OWNED_WORKER_EXCEPTION_ATTR, None)
    if not isinstance(owner, OwnedWorkerProcess):
        return None
    try:
        delattr(exc, _OWNED_WORKER_EXCEPTION_ATTR)
    except AttributeError:
        pass
    return owner


class OwnedWorkerJobBackend(Protocol):
    def create_named_job(self, name: str) -> int: ...

    def open_named_job(self, name: str) -> int: ...

    def set_kill_on_close(self, job_handle: int) -> None: ...

    def assign_current_process(self, job_handle: int) -> None: ...

    def query_active_process_count(self, job_handle: int) -> Optional[int]: ...

    def terminate_job(self, job_handle: int, exit_code: int) -> None: ...

    def close_handle(self, handle: int) -> None: ...


def make_owned_job_name(parent_pid: int) -> str:
    token = uuid.uuid4().hex
    return f"Local\\{_JOB_NAME_PREFIX}{parent_pid}_{token}"


def parse_owned_job_name(name: str, *, expected_parent_pid: int) -> bool:
    if not name or len(name) > 260:
        return False
    base = name
    if base.startswith("Local\\"):
        base = base[6:]
    if not base.startswith(_JOB_NAME_PREFIX):
        return False
    rest = base[len(_JOB_NAME_PREFIX) :]
    parts = rest.split("_", 1)
    if len(parts) != 2:
        return False
    pid_part, hex_part = parts
    if not pid_part.isdigit() or not hex_part or len(hex_part) != 32:
        return False
    try:
        int(hex_part, 16)
    except ValueError:
        return False
    return int(pid_part) == expected_parent_pid


def apply_owned_job_env(
    env: Dict[str, str],
    *,
    job_name: str,
    parent_pid: int,
) -> None:
    """在 allowlist 环境构建之后写入内部 Job 字段（覆盖任何残留值）。"""
    env[JOB_ENV_NAME] = job_name
    env[JOB_ENV_PARENT_PID] = str(parent_pid)


def _default_backend() -> OwnedWorkerJobBackend:
    if sys.platform != "win32":
        raise RuntimeError("owned worker job requires win32")
    return _Win32OwnedWorkerJobBackend()


def worker_entry_join_owned_job(
    *,
    backend: OwnedWorkerJobBackend | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """
    Runtime worker 在重型 import 之前调用；失败时固定 stderr 消息并以退出码 2 结束。
    """
    if sys.platform != "win32":
        return
    source = environ if environ is not None else os.environ
    name = source.get(JOB_ENV_NAME)
    parent_raw = source.get(JOB_ENV_PARENT_PID)
    if not name or not parent_raw:
        print("local tts job: missing token", file=sys.stderr)
        sys.exit(2)
    try:
        parent_pid = int(parent_raw)
    except ValueError:
        print("local tts job: invalid parent", file=sys.stderr)
        sys.exit(2)
    if parent_pid <= 0:
        print("local tts job: invalid parent", file=sys.stderr)
        sys.exit(2)
    if not parse_owned_job_name(name, expected_parent_pid=parent_pid):
        print("local tts job: invalid token", file=sys.stderr)
        sys.exit(2)
    from src.core.parent_process import parent_process_is_alive

    if not parent_process_is_alive(parent_pid):
        print("local tts job: parent not alive", file=sys.stderr)
        sys.exit(2)
    job_backend = backend or _default_backend()
    job_handle = 0
    try:
        job_handle = job_backend.open_named_job(name)
        job_backend.assign_current_process(job_handle)
    except OSError:
        print("local tts job: join failed", file=sys.stderr)
        sys.exit(2)
    finally:
        if job_handle:
            try:
                job_backend.close_handle(job_handle)
            except OSError:
                print("local tts job: join failed", file=sys.stderr)
                sys.exit(2)


@dataclass
class _JobResources:
    backend: OwnedWorkerJobBackend
    job_handle: int
    closed: bool = False

    def close(self) -> None:
        if self.closed or not self.job_handle:
            return
        self.backend.close_handle(self.job_handle)
        self.closed = True
        self.job_handle = 0


def _best_effort_close_resources(resources: _JobResources) -> None:
    try:
        resources.close()
    except OSError:
        pass


def _validate_wait_timeout(timeout: float | None) -> None:
    if timeout is None:
        return
    value = float(timeout)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid wait timeout")


class OwnedWorkerProcess:
    """Popen 兼容适配器：redirector 退出且 Job ActiveProcesses==0 才视为完成。"""

    def __init__(
        self,
        resources: _JobResources,
        redirector: subprocess.Popen | None = None,
        argv: Sequence[str] | None = None,
    ) -> None:
        self._redirector = redirector
        self._argv = list(argv) if argv is not None else None
        self._resources = resources
        self._returncode: int | None = None
        self._query_failed = False
        self._state_lock = threading.RLock()
        weakref.finalize(self, _best_effort_close_resources, resources)

    def _bind_job_handle(self, job_handle: int) -> None:
        with self._state_lock:
            if self._resources.job_handle:
                raise RuntimeError("owned worker job handle already bound")
            self._resources.job_handle = int(job_handle)

    def _force_bind_redirector(self, redirector: subprocess.Popen) -> None:
        with self._state_lock:
            if self._redirector is None:
                self._redirector = redirector

    def _bind_redirector(self, redirector: subprocess.Popen) -> None:
        with self._state_lock:
            if self._redirector is not None:
                raise RuntimeError("owned worker redirector already bound")
            self._redirector = redirector

    @property
    def stdin(self):
        if self._redirector is None:
            return None
        return self._redirector.stdin

    @property
    def stdout(self):
        if self._redirector is None:
            return None
        return self._redirector.stdout

    @property
    def stderr(self):
        if self._redirector is None:
            return None
        return self._redirector.stderr

    @property
    def pid(self) -> int:
        if self._redirector is None:
            return 0
        return self._redirector.pid

    @property
    def args(self):
        if self._redirector is not None:
            return getattr(self._redirector, "args", None)
        return self._argv

    def _is_spawn_quiescent(self) -> bool:
        with self._state_lock:
            if self._redirector is not None and self._redirector.poll() is None:
                return False
            handle = self._resources.job_handle
            if handle and not self._resources.closed:
                active = self._resources.backend.query_active_process_count(handle)
                if active is None or active > 0:
                    return False
                try:
                    self._resources.close()
                except OSError:
                    return False
            return True

    def _bounded_spawn_cleanup(self, budget_sec: float = _SPAWN_CLEANUP_BUDGET_SEC) -> bool:
        deadline = time.monotonic() + float(budget_sec)
        try:
            self.kill()
        except BaseException:
            pass
        remaining = max(0.0, deadline - time.monotonic())
        if remaining > 0:
            try:
                self.wait(timeout=remaining)
            except BaseException:
                pass
        try:
            return self._is_spawn_quiescent()
        except BaseException:
            return False

    def _handle_usable(self) -> bool:
        return not self._resources.closed and bool(self._resources.job_handle)

    def _finalize(self, code: int) -> None:
        with self._state_lock:
            if self._returncode is not None:
                return
            if not self._handle_usable():
                return
            try:
                self._resources.close()
            except OSError:
                return
            self._returncode = code

    def poll(self) -> int | None:
        with self._state_lock:
            if self._returncode is not None:
                return self._returncode
            if not self._handle_usable():
                if self._redirector is None:
                    self._returncode = _SPAWN_FAILURE_EXITCODE
                    return self._returncode
                return None
            active = self._resources.backend.query_active_process_count(
                self._resources.job_handle
            )
            if active is None:
                self._query_failed = True
                return None
            if active > 0:
                return None
            if self._redirector is None:
                code = _SPAWN_FAILURE_EXITCODE
            else:
                raw_rc = self._redirector.poll()
                if raw_rc is None:
                    return None
                code = int(raw_rc)
        self._finalize(code)
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        _validate_wait_timeout(timeout)
        if self._returncode is not None:
            return self._returncode
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(
                    cmd=getattr(self._redirector, "args", None),
                    timeout=timeout,
                )
            if deadline is None:
                sleep_for = _POLL_SLEEP_SEC
            else:
                sleep_for = min(
                    _POLL_SLEEP_SEC, max(0.0, deadline - time.monotonic())
                )
            if sleep_for > 0:
                time.sleep(sleep_for)

    def terminate(self) -> None:
        with self._state_lock:
            if self._returncode is not None or not self._handle_usable():
                return
            if self._redirector is not None and self._redirector.poll() is None:
                try:
                    self._redirector.terminate()
                except OSError:
                    pass
            try:
                self._resources.backend.terminate_job(self._resources.job_handle, 1)
            except OSError:
                pass

    def kill(self) -> None:
        with self._state_lock:
            if self._returncode is not None or not self._handle_usable():
                return
            if self._redirector is not None and self._redirector.poll() is None:
                try:
                    self._redirector.kill()
                except OSError:
                    pass
            try:
                self._resources.backend.terminate_job(self._resources.job_handle, 1)
            except OSError:
                pass


def spawn_owned_local_tts_worker(
    argv: Sequence[str],
    popen_kwargs: Mapping[str, Any],
    *,
    popen_fn: Callable[..., subprocess.Popen],
    parent_pid: int,
    job_backend: OwnedWorkerJobBackend | None = None,
) -> subprocess.Popen | OwnedWorkerProcess:
    if sys.platform != "win32":
        return popen_fn(list(argv), **dict(popen_kwargs))
    if parent_pid <= 0:
        raise PermissionError("本地 TTS worker 能力拒绝: parent_pid 无效")
    backend = job_backend or _default_backend()
    job_name = make_owned_job_name(parent_pid)
    argv_list = list(argv)
    resources = _JobResources(backend=backend, job_handle=0)
    owned = OwnedWorkerProcess(resources=resources, redirector=None, argv=argv_list)
    orphan_handle = 0
    redirector: subprocess.Popen | None = None
    try:
        orphan_handle = backend.create_named_job(job_name)
        owned._bind_job_handle(orphan_handle)
        orphan_handle = 0
        backend.set_kill_on_close(owned._resources.job_handle)
        merged = dict(popen_kwargs)
        env = dict(merged.get("env") or {})
        apply_owned_job_env(env, job_name=job_name, parent_pid=parent_pid)
        merged["env"] = env
        redirector = popen_fn(argv_list, **merged)
        owned._bind_redirector(redirector)
        redirector = None
        return owned
    except BaseException as exc:
        if redirector is not None and owned._redirector is None:
            owned._force_bind_redirector(redirector)
            redirector = None
        if orphan_handle:
            try:
                owned._bind_job_handle(orphan_handle)
                orphan_handle = 0
            except RuntimeError:
                if owned._resources.job_handle == orphan_handle:
                    orphan_handle = 0
                else:
                    try:
                        backend.close_handle(orphan_handle)
                    except OSError:
                        pass
                    orphan_handle = 0
        if not owned._bounded_spawn_cleanup():
            attach_owned_worker_to_exception(exc, owned)
        raise


class _Win32OwnedWorkerJobBackend:
    """Win32 Job Object ctypes 实现（x64 指针宽度 HANDLE）。"""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._setup_signatures()

    def _setup_signatures(self) -> None:
        k = self._kernel32
        w = self._wintypes

        k.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.OpenJobObjectW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
        k.OpenJobObjectW.restype = w.HANDLE
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.SetInformationJobObject.argtypes = [w.HANDLE, w.DWORD, w.LPVOID, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.QueryInformationJobObject.argtypes = [
            w.HANDLE,
            w.DWORD,
            w.LPVOID,
            w.DWORD,
            w.LPDWORD,
        ]
        k.QueryInformationJobObject.restype = w.BOOL
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.TerminateJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.GetCurrentProcess.argtypes = []
        k.GetCurrentProcess.restype = w.HANDLE
        k.GetLastError.argtypes = []
        k.GetLastError.restype = w.DWORD

    def _check_bool(self, ok: bool, *, context: str) -> None:
        if ok:
            return
        err = self._kernel32.GetLastError()
        raise OSError(err, context)

    def create_named_job(self, name: str) -> int:
        handle = self._kernel32.CreateJobObjectW(None, name)
        if not handle:
            err = self._kernel32.GetLastError()
            raise OSError(err, "CreateJobObjectW failed")
        err = self._kernel32.GetLastError()
        if err == 183:  # ERROR_ALREADY_EXISTS
            self._kernel32.CloseHandle(handle)
            raise OSError(err, "job name collision")
        return int(handle)

    def open_named_job(self, name: str) -> int:
        access = 0x00000001  # JOB_OBJECT_ASSIGN_PROCESS
        handle = self._kernel32.OpenJobObjectW(access, False, name)
        if not handle:
            raise OSError(self._kernel32.GetLastError(), "OpenJobObjectW failed")
        return int(handle)

    def set_kill_on_close(self, job_handle: int) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class LARGE_INTEGER(ctypes.Structure):
            _fields_ = [("QuadPart", ctypes.c_longlong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", LARGE_INTEGER),
                ("PerJobUserTimeLimit", LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        ok = self._kernel32.SetInformationJobObject(
            job_handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        self._check_bool(bool(ok), context="SetInformationJobObject")

    def assign_current_process(self, job_handle: int) -> None:
        proc = self._kernel32.GetCurrentProcess()
        ok = self._kernel32.AssignProcessToJobObject(job_handle, proc)
        self._check_bool(bool(ok), context="AssignProcessToJobObject")

    def query_active_process_count(self, job_handle: int) -> Optional[int]:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        ok = self._kernel32.QueryInformationJobObject(
            job_handle,
            1,  # JobObjectBasicAccountingInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        if not ok:
            return None
        return int(info.ActiveProcesses)

    def terminate_job(self, job_handle: int, exit_code: int) -> None:
        ok = self._kernel32.TerminateJobObject(job_handle, int(exit_code))
        self._check_bool(bool(ok), context="TerminateJobObject")

    def close_handle(self, handle: int) -> None:
        if not handle:
            return
        ok = self._kernel32.CloseHandle(handle)
        if not ok:
            raise OSError(self._kernel32.GetLastError(), "CloseHandle failed")


# 中性别名：RVC 与本地 TTS 共用同一 Job 归属实现（环境字段契约不变）。
spawn_owned_voice_worker = spawn_owned_local_tts_worker
