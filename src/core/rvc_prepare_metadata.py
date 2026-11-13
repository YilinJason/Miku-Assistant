"""RVC 准备状态元数据（stdlib only；Core 与 TTSEngine 共享，无重引擎依赖）。"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, List, Optional


RVC_PREPARE_USER_WARNING = (
    "RVC Nahida 未就绪，将使用默认 edge-tts 播报（非目标音色）。"
)


@dataclass(frozen=True)
class RvcPrepareState:
    """只读 RVC 准备进度；无路径/正文。"""

    enabled: bool
    inflight: bool
    attempt_completed: bool
    ready: bool
    user_warning: str = ""


class RvcPrepareReadFailed:
    """已声明 ``get_rvc_prepare_state`` 但读取/校验失败（fail-closed）。"""

    __slots__ = ()


RVC_PREPARE_READ_FAILED = RvcPrepareReadFailed()

_RVC_PREPARE_API_SENTINEL = object()
_RVC_PREPARE_GETTER = "get_rvc_prepare_state"


def rvc_prepare_read_failed(value: Any) -> bool:
    return isinstance(value, RvcPrepareReadFailed)


def engine_declares_rvc_prepare_state(tts_engine: Any) -> bool:
    if tts_engine is None:
        return False
    return (
        inspect.getattr_static(
            tts_engine,
            _RVC_PREPARE_GETTER,
            _RVC_PREPARE_API_SENTINEL,
        )
        is not _RVC_PREPARE_API_SENTINEL
    )


def coerce_rvc_prepare_state(value: Any) -> Optional[RvcPrepareState]:
    if not isinstance(value, RvcPrepareState):
        return None
    if not isinstance(value.enabled, bool):
        return None
    if not isinstance(value.inflight, bool):
        return None
    if not isinstance(value.attempt_completed, bool):
        return None
    if not isinstance(value.ready, bool):
        return None
    if not isinstance(value.user_warning, str):
        return None
    return value


def read_rvc_prepare_state(tts_engine: Any) -> Any:
    """
    读取引擎 RVC 准备状态。

    Returns:
        ``None``：未声明状态 API（legacy）；
        ``RvcPrepareState``：有效快照；
        ``RVC_PREPARE_READ_FAILED``：已声明但调用/类型无效（fail-closed）。
    """
    if not engine_declares_rvc_prepare_state(tts_engine):
        return None
    reader = getattr(tts_engine, _RVC_PREPARE_GETTER, None)
    if not callable(reader):
        return RVC_PREPARE_READ_FAILED
    try:
        raw = reader()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return RVC_PREPARE_READ_FAILED
    coerced = coerce_rvc_prepare_state(raw)
    if coerced is None:
        return RVC_PREPARE_READ_FAILED
    return coerced


def rvc_prepare_verified_ready(state: Any) -> bool:
    """RVC 准备完成且可视为 retry/显式成功（非 inflight、非 sticky 失败）。"""
    coerced = coerce_rvc_prepare_state(state)
    if coerced is None:
        return False
    return (
        coerced.enabled
        and coerced.attempt_completed
        and coerced.ready
        and not coerced.inflight
    )


def rvc_prepare_failed_sticky(state: Any) -> bool:
    """RVC 已结束一次准备且未就绪（可 Edge 播报 fallback，非 inflight）。"""
    coerced = coerce_rvc_prepare_state(state)
    if coerced is None:
        return False
    return (
        coerced.enabled
        and coerced.attempt_completed
        and not coerced.ready
        and not coerced.inflight
    )


def collect_rvc_fallback_warnings(state: Any) -> List[str]:
    if state is None or rvc_prepare_read_failed(state):
        return []
    if state.enabled and state.attempt_completed and not state.ready:
        warning = state.user_warning or RVC_PREPARE_USER_WARNING
        return [warning]
    return []
