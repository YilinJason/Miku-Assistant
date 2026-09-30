"""启动模式规范化：装配前可知，决定是否构造语音栈。"""
from __future__ import annotations

from typing import FrozenSet, Literal

StartupMode = Literal["text", "voice", "interactive"]

VALID_STARTUP_MODES: FrozenSet[str] = frozenset({"text", "voice", "interactive"})


def normalize_startup_mode(value: object, *, default: StartupMode = "interactive") -> StartupMode:
    """
    将外部输入规范为 ``text`` / ``voice`` / ``interactive``。

    ``None`` 或空串使用 ``default``；未知值抛 ``ValueError``。
    """
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise ValueError(f"startup_mode must be str, got {type(value).__name__}")
    mode = value.strip().lower()
    if mode not in VALID_STARTUP_MODES:
        raise ValueError(
            f"startup_mode must be one of {sorted(VALID_STARTUP_MODES)}, got {value!r}"
        )
    return mode  # type: ignore[return-value]


def voice_stack_at_assemble(mode: StartupMode) -> bool:
    """仅 ``voice`` 在装配期同步构造 STT/TTS/RVC；``text``/``interactive`` 延迟。"""
    return mode == "voice"
