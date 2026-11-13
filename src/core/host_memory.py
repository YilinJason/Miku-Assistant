"""Windows host physical RAM / system commit admission (stdlib-only, no voice imports)."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final, Literal, Optional, Type

ModelStartupKind = Literal["rvc", "cosyvoice", "paraformer"]

RVC_REQUIRED_PHYSICAL_MB: Final[int] = 4096
RVC_REQUIRED_COMMIT_MB: Final[int] = 14336
COSY_REQUIRED_PHYSICAL_MB: Final[int] = 8192
COSY_REQUIRED_COMMIT_MB: Final[int] = 10240
PARAFORMER_REQUIRED_PHYSICAL_MB: Final[int] = 4096
PARAFORMER_REQUIRED_COMMIT_MB: Final[int] = 6144

REASON_HOST_MEMORY_UNOBSERVABLE: Final[str] = "host_memory_unobservable"
REASON_HOST_MEMORY_PHYSICAL_INSUFFICIENT: Final[str] = "host_memory_physical_insufficient"
REASON_HOST_MEMORY_COMMIT_INSUFFICIENT: Final[str] = "host_memory_commit_insufficient"

STARTUP_MEMORY_USER_HINT: Final[str] = (
    "系统物理内存或提交余量不足，请关闭其它应用后显式重试或重启助手。"
)

RVC_DISABLED_MEMORY_MESSAGE: Final[str] = (
    "RVC Nahida 未启用：主机内存或提交余量不足。"
)


class ModelStartupMemoryError(Exception):
    """Controlled refusal to cold-start a local model (path-free reason tag)."""

    def __init__(self, reason: str, decision: "ModelStartupMemoryDecision") -> None:
        super().__init__(reason)
        self.reason = reason
        self.decision = decision


@dataclass(frozen=True)
class HostMemorySnapshot:
    physical_available_mb: int
    commit_available_mb: int


@dataclass(frozen=True)
class ModelStartupMemoryDecision:
    allowed: bool
    reason: str
    physical_available_mb: Optional[int]
    commit_available_mb: Optional[int]
    required_physical_mb: int
    required_commit_mb: int


_PERFORMANCE_INFORMATION_CLS: Optional[Type] = None


def performance_information_type() -> Type:
    """Return the Windows ``PERFORMANCE_INFORMATION`` ctypes layout (Win64 ABI)."""
    global _PERFORMANCE_INFORMATION_CLS
    if _PERFORMANCE_INFORMATION_CLS is not None:
        return _PERFORMANCE_INFORMATION_CLS
    if sys.platform != "win32":
        raise RuntimeError("PERFORMANCE_INFORMATION is Windows-only")
    import ctypes
    from ctypes import wintypes

    class PERFORMANCE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("CommitTotal", ctypes.c_size_t),
            ("CommitLimit", ctypes.c_size_t),
            ("CommitPeak", ctypes.c_size_t),
            ("PhysicalTotal", ctypes.c_size_t),
            ("PhysicalAvailable", ctypes.c_size_t),
            ("SystemCache", ctypes.c_size_t),
            ("KernelTotal", ctypes.c_size_t),
            ("KernelPaged", ctypes.c_size_t),
            ("KernelNonpaged", ctypes.c_size_t),
            ("PageSize", ctypes.c_size_t),
            ("HandleCount", wintypes.DWORD),
            ("ProcessCount", wintypes.DWORD),
            ("ThreadCount", wintypes.DWORD),
        ]

    _PERFORMANCE_INFORMATION_CLS = PERFORMANCE_INFORMATION
    return PERFORMANCE_INFORMATION


def snapshot_from_performance_information(info: object) -> Optional[HostMemorySnapshot]:
    """Convert a populated ``PERFORMANCE_INFORMATION`` instance to MiB snapshot."""
    try:
        page_size = _non_negative_size_int(getattr(info, "PageSize", None))
        if page_size is None or page_size <= 0:
            return None
        commit_limit = _non_negative_size_int(getattr(info, "CommitLimit", None))
        commit_total = _non_negative_size_int(getattr(info, "CommitTotal", None))
        physical_available_pages = _non_negative_size_int(
            getattr(info, "PhysicalAvailable", None)
        )
        physical_total_pages = _non_negative_size_int(
            getattr(info, "PhysicalTotal", None)
        )
        if (
            commit_limit is None
            or commit_total is None
            or physical_available_pages is None
            or physical_total_pages is None
        ):
            return None
        if commit_limit < commit_total:
            return None
        if physical_available_pages > physical_total_pages:
            return None
        commit_available_pages = commit_limit - commit_total
        physical_mb = (physical_available_pages * page_size) // (1024 * 1024)
        commit_mb = (commit_available_pages * page_size) // (1024 * 1024)
        return HostMemorySnapshot(
            physical_available_mb=physical_mb,
            commit_available_mb=commit_mb,
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _non_negative_size_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if type(value) is int:
        return value if value >= 0 else None
    return None


def read_host_memory_snapshot() -> Optional[HostMemorySnapshot]:
    """
    Read physical available and system commit headroom (MiB) from one Windows snapshot.

    Returns ``None`` when the API is unsupported or unobservable (non-Windows or failure).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PERFORMANCE_INFORMATION = performance_information_type()
        info = PERFORMANCE_INFORMATION()
        info.cb = ctypes.sizeof(PERFORMANCE_INFORMATION)
        get_perf = ctypes.windll.psapi.GetPerformanceInfo
        get_perf.argtypes = [ctypes.POINTER(PERFORMANCE_INFORMATION), wintypes.DWORD]
        get_perf.restype = wintypes.BOOL
        if not get_perf(ctypes.byref(info), info.cb):
            return None
        return snapshot_from_performance_information(info)
    except Exception:
        return None


def _thresholds(kind: ModelStartupKind) -> tuple[int, int]:
    if kind == "rvc":
        return RVC_REQUIRED_PHYSICAL_MB, RVC_REQUIRED_COMMIT_MB
    if kind == "cosyvoice":
        return COSY_REQUIRED_PHYSICAL_MB, COSY_REQUIRED_COMMIT_MB
    if kind == "paraformer":
        return PARAFORMER_REQUIRED_PHYSICAL_MB, PARAFORMER_REQUIRED_COMMIT_MB
    raise ValueError("kind must be 'rvc', 'cosyvoice', or 'paraformer'")


def check_model_startup_memory(kind: str) -> ModelStartupMemoryDecision:
    """
    Fail-closed admission check before cold-starting a local voice model worker.

    Non-Windows: returns ``allowed=True`` (legacy behavior unchanged).
    """
    if type(kind) is not str or kind not in ("rvc", "cosyvoice", "paraformer"):
        raise ValueError("kind must be 'rvc', 'cosyvoice', or 'paraformer'")

    req_phys, req_commit = _thresholds(kind)  # type: ignore[arg-type]

    if sys.platform != "win32":
        return ModelStartupMemoryDecision(
            allowed=True,
            reason="",
            physical_available_mb=None,
            commit_available_mb=None,
            required_physical_mb=req_phys,
            required_commit_mb=req_commit,
        )

    snapshot = read_host_memory_snapshot()
    if snapshot is None:
        return ModelStartupMemoryDecision(
            allowed=False,
            reason=REASON_HOST_MEMORY_UNOBSERVABLE,
            physical_available_mb=None,
            commit_available_mb=None,
            required_physical_mb=req_phys,
            required_commit_mb=req_commit,
        )

    phys = snapshot.physical_available_mb
    commit = snapshot.commit_available_mb
    if phys < req_phys:
        return ModelStartupMemoryDecision(
            allowed=False,
            reason=REASON_HOST_MEMORY_PHYSICAL_INSUFFICIENT,
            physical_available_mb=phys,
            commit_available_mb=commit,
            required_physical_mb=req_phys,
            required_commit_mb=req_commit,
        )
    if commit < req_commit:
        return ModelStartupMemoryDecision(
            allowed=False,
            reason=REASON_HOST_MEMORY_COMMIT_INSUFFICIENT,
            physical_available_mb=phys,
            commit_available_mb=commit,
            required_physical_mb=req_phys,
            required_commit_mb=req_commit,
        )

    return ModelStartupMemoryDecision(
        allowed=True,
        reason="",
        physical_available_mb=phys,
        commit_available_mb=commit,
        required_physical_mb=req_phys,
        required_commit_mb=req_commit,
    )


def require_model_startup_memory(kind: str) -> ModelStartupMemoryDecision:
    """Raise ``ModelStartupMemoryError`` when cold start is not allowed."""
    decision = check_model_startup_memory(kind)
    if not decision.allowed:
        raise ModelStartupMemoryError(decision.reason, decision)
    return decision
