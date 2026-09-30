"""无设备资源采样与离线 soak（不访问麦克风/扬声器/GPU）。"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ResourceSnapshot:
    rss_bytes: Optional[int]
    thread_count: int
    child_process_count: Optional[int]
    queue_depths: Dict[str, int] = field(default_factory=dict)
    rss_available: bool = False
    children_available: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rss_bytes": self.rss_bytes,
            "rss_available": self.rss_available,
            "thread_count": self.thread_count,
            "child_process_count": self.child_process_count,
            "children_available": self.children_available,
            "queue_depths": dict(self.queue_depths),
        }


def _rss_bytes_psutil() -> Optional[int]:
    try:
        import psutil  # type: ignore

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        return None


def _rss_bytes_windows() -> Optional[int]:
    """无 psutil 时用 GetProcessMemoryInfo；失败返回 None（不可观测）。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_mem = psapi.GetProcessMemoryInfo
        get_mem.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        get_mem.restype = wintypes.BOOL

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = kernel32.GetCurrentProcess()
        if not get_mem(handle, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.WorkingSetSize)
    except Exception:
        return None


def _rss_bytes() -> Optional[int]:
    value = _rss_bytes_psutil()
    if value is not None:
        return value
    return _rss_bytes_windows()


def _child_process_count_psutil() -> Optional[int]:
    try:
        import psutil  # type: ignore

        return len(psutil.Process(os.getpid()).children(recursive=False))
    except Exception:
        return None


def _child_process_count_windows() -> Optional[int]:
    """Toolhelp32 枚举直属子进程；失败返回 None，绝不谎报 0。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
        CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        Process32First = kernel32.Process32FirstW
        Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        Process32First.restype = wintypes.BOOL
        Process32Next = kernel32.Process32NextW
        Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        Process32Next.restype = wintypes.BOOL
        CloseHandle = kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL

        snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE or snap is None:
            return None
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not Process32First(snap, ctypes.byref(entry)):
                return None
            self_pid = os.getpid()
            count = 0
            while True:
                if int(entry.th32ParentProcessID) == self_pid:
                    count += 1
                if not Process32Next(snap, ctypes.byref(entry)):
                    break
            return count
        finally:
            CloseHandle(snap)
    except Exception:
        return None


def _child_process_count() -> Optional[int]:
    value = _child_process_count_psutil()
    if value is not None:
        return value
    return _child_process_count_windows()


def sample_resource_snapshot(
    *,
    queue_depths: Optional[Dict[str, int]] = None,
) -> ResourceSnapshot:
    """采样当前进程资源；不可观测项为 ``None``，不伪装成 0。"""
    rss = _rss_bytes()
    children = _child_process_count()
    return ResourceSnapshot(
        rss_bytes=rss,
        thread_count=threading.active_count(),
        child_process_count=children,
        queue_depths=dict(queue_depths or {}),
        rss_available=rss is not None,
        children_available=children is not None,
    )


def collect_assistant_queue_depths(assistant: Any) -> Dict[str, int]:
    """从助手实例尽力读取已知队列深度（缺失则为空）。"""
    depths: Dict[str, int] = {}
    player = getattr(assistant, "_speech_player", None)
    if player is not None:
        q = getattr(player, "_queue", None)
        if q is not None and hasattr(q, "qsize"):
            try:
                depths["speech_queue"] = int(q.qsize())
            except Exception:
                pass
    runtime = getattr(assistant, "memory_runtime", None)
    if runtime is not None:
        service = getattr(runtime, "ingestion", None) or getattr(
            runtime, "_ingestion", None
        )
        if service is not None:
            pending = getattr(service, "pending_count", None)
            if callable(pending):
                try:
                    depths["memory_ingestion"] = int(pending())
                except Exception:
                    pass
            else:
                q = getattr(service, "_queue", None)
                if q is not None and hasattr(q, "qsize"):
                    try:
                        depths["memory_ingestion"] = int(q.qsize())
                    except Exception:
                        pass
    cleaner = getattr(assistant, "file_cleaner", None)
    if cleaner is not None:
        depth = getattr(cleaner, "pending_scan_hint", None)
        if callable(depth):
            try:
                depths["file_cleaner"] = int(depth())
            except Exception:
                pass
    return depths


@dataclass
class SoakSample:
    label: str
    snapshot: ResourceSnapshot


def assert_stable_upper_bounds(
    samples: List[SoakSample],
    *,
    max_thread_growth: int = 32,
    max_child_growth: int = 4,
    max_rss_growth_bytes: Optional[int] = None,
    require_queues_drained: bool = True,
) -> Dict[str, bool]:
    """
    验收：相对基线，线程/子进程/RSS（若可观测）与队列终点有界。

    返回各项是否实际可观测。不以「进程退出后 OS 回收」代替关闭契约。
    """
    if len(samples) < 2:
        raise AssertionError("soak requires baseline and at least one later sample")
    base = samples[0].snapshot
    last = samples[-1].snapshot
    observed = {
        "rss": base.rss_available and last.rss_available,
        "children": base.children_available and last.children_available,
        "threads": True,
        "queues": True,
    }

    thread_delta = last.thread_count - base.thread_count
    if thread_delta > max_thread_growth:
        raise AssertionError(
            f"thread growth {thread_delta} exceeds bound {max_thread_growth} "
            f"(base={base.thread_count} last={last.thread_count})"
        )

    if observed["children"]:
        assert base.child_process_count is not None
        assert last.child_process_count is not None
        child_delta = last.child_process_count - base.child_process_count
        if child_delta > max_child_growth:
            raise AssertionError(
                f"child process growth {child_delta} exceeds bound {max_child_growth} "
                f"(base={base.child_process_count} last={last.child_process_count})"
            )

    if observed["rss"] and max_rss_growth_bytes is not None:
        assert base.rss_bytes is not None and last.rss_bytes is not None
        rss_delta = last.rss_bytes - base.rss_bytes
        if rss_delta > max_rss_growth_bytes:
            raise AssertionError(
                f"rss growth {rss_delta} exceeds bound {max_rss_growth_bytes}"
            )

    if require_queues_drained:
        for name, depth in last.queue_depths.items():
            if depth != 0:
                raise AssertionError(f"queue {name} not drained at end: {depth}")

    return observed


def run_offline_soak_report(
    samples: List[SoakSample],
    *,
    max_thread_growth: int = 32,
    max_child_growth: int = 4,
    max_rss_growth_bytes: Optional[int] = 256 * 1024 * 1024,
) -> Dict[str, Any]:
    """汇总 soak 样本与可观测性；供诊断脚本输出。"""
    observed = assert_stable_upper_bounds(
        samples,
        max_thread_growth=max_thread_growth,
        max_child_growth=max_child_growth,
        max_rss_growth_bytes=max_rss_growth_bytes,
        require_queues_drained=True,
    )
    return {
        "samples": [
            {"label": s.label, **s.snapshot.as_dict()} for s in samples
        ],
        "observability": observed,
        "ok": True,
    }
