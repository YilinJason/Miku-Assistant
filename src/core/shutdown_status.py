"""TTS 关闭聚合状态与显式 API 检测（stdlib only）。"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Tuple

_SHUTDOWN_API_SENTINEL = object()

AGGREGATE_SHUTDOWN_GETTER = "get_shutdown_status"
LOCAL_COSY_SHUTDOWN_GETTER = "local_cosyvoice_shutdown_status"


@dataclass(frozen=True)
class TtsShutdownStatus:
    """
    引擎关闭聚合状态（只读；不启动资源）。

    ``persistent_rvc_complete`` 仅覆盖持久 Worker。
    ``rvc_subprocess_complete`` 覆盖单次 infer/probe 子进程归属（末尾默认 True 兼容旧构造）。
    """

    complete: bool
    local_tts_complete: bool
    persistent_rvc_complete: bool
    synthesis_idle: bool
    temp_files_complete: bool
    rvc_subprocess_complete: bool = True


AGGREGATE_SHUTDOWN_STATUS_FIELDS: Tuple[str, ...] = (
    "complete",
    "local_tts_complete",
    "persistent_rvc_complete",
    "synthesis_idle",
    "temp_files_complete",
    "rvc_subprocess_complete",
)


def shutdown_completion_flag(value: object) -> bool:
    """仅 ``value is True`` 视为完成；拒绝 truthy 与非 bool。"""
    return value is True


def engine_declares_shutdown_api(engine: object, name: str) -> bool:
    """``name`` 是否在 ``engine`` 上静态存在（不含 ``__getattr__`` 动态伪造）。"""
    return (
        inspect.getattr_static(engine, name, _SHUTDOWN_API_SENTINEL)
        is not _SHUTDOWN_API_SENTINEL
    )


def engine_declares_aggregate_shutdown_status(engine: object) -> bool:
    return engine_declares_shutdown_api(engine, AGGREGATE_SHUTDOWN_GETTER)


def engine_declares_local_cosyvoice_shutdown_status(engine: object) -> bool:
    return engine_declares_shutdown_api(engine, LOCAL_COSY_SHUTDOWN_GETTER)


def verify_aggregate_shutdown_status(status: object) -> None:
    """聚合字段均为 ``bool``，且全部为 ``True``（旧 Adapter 缺 ``rvc_subprocess_complete`` 视为 True）。"""
    if status is None:
        raise RuntimeError("tts_shutdown: status missing (None)")
    field_values: dict[str, bool] = {}
    for name in AGGREGATE_SHUTDOWN_STATUS_FIELDS:
        if name == "rvc_subprocess_complete" and not hasattr(status, name):
            field_values[name] = True
            continue
        try:
            value = getattr(status, name)
        except AttributeError as exc:
            raise RuntimeError(f"tts_shutdown: missing field {name}") from exc
        if type(value) is not bool:
            raise RuntimeError(f"tts_shutdown: invalid field {name}")
        field_values[name] = value
    for name in AGGREGATE_SHUTDOWN_STATUS_FIELDS:
        if not field_values[name]:
            raise RuntimeError(f"tts_shutdown: incomplete field {name}")
