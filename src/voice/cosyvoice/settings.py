# -*- coding: utf-8 -*-
"""CosyVoice 生产后端严格配置（纯解析，零副作用）。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class CosyVoiceSettings:
    startup_timeout_sec: int
    synth_timeout_sec: int
    idle_timeout_sec: int
    max_text_chars: int
    cpu_threads: int
    min_free_ram_mb: int
    min_free_vram_mb: int
    cuda_memory_limit_mb: int


_DEFAULTS: dict[str, int] = {
    "startup_timeout_sec": 30,
    "synth_timeout_sec": 20,
    "idle_timeout_sec": 60,
    "max_text_chars": 240,
    "cpu_threads": 4,
    "min_free_ram_mb": 8192,
    "min_free_vram_mb": 4096,
    "cuda_memory_limit_mb": 4096,
}

_BOUNDS: dict[str, tuple[int, int]] = {
    "startup_timeout_sec": (1, 60),
    "synth_timeout_sec": (1, 45),
    "idle_timeout_sec": (1, 120),
    "max_text_chars": (1, 240),
    "cpu_threads": (1, 4),
    "min_free_ram_mb": (8192, 2**31 - 1),
    "min_free_vram_mb": (4096, 2**31 - 1),
    "cuda_memory_limit_mb": (3072, 4096),
}

_ALLOWED_KEYS = frozenset(_DEFAULTS)


def _finite_int(path: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{path} must be a finite number")
    if isinstance(value, int):
        return value
    if not isinstance(value, float):
        raise ValueError(f"{path} must be a finite number")
    f = value
    if not math.isfinite(f):
        raise ValueError(f"{path} must be finite")
    if not f.is_integer():
        raise ValueError(f"{path} must be an integer")
    try:
        return int(f)
    except OverflowError:
        raise ValueError(f"{path} must be a finite number") from None


def _parse_field(path: str, value: Any, *, lo: int, hi: int) -> int:
    n = _finite_int(path, value)
    if n < lo or n > hi:
        raise ValueError(f"{path} out of range [{lo}, {hi}]")
    return n


def parse_cosyvoice_settings(
    voice_tts: Optional[Mapping[str, Any]],
) -> Optional[CosyVoiceSettings]:
    """
    仅当 ``voice.tts.engine == cosyvoice`` 时解析 ``voice.tts.cosyvoice`` 节。

    拒绝未知键、bool 冒充数字、非有限值，以及任意路径/解释器/模型/instruct 字段。
    """
    if not voice_tts:
        return None
    engine = voice_tts.get("engine")
    if engine != "cosyvoice":
        return None
    raw = voice_tts.get("cosyvoice")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("voice.tts.cosyvoice must be a mapping")
    unknown = sorted(k for k in raw if k not in _ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"voice.tts.cosyvoice unknown key: {unknown[0]}")
    _FORBIDDEN_PREFIXES = (
        "path",
        "model",
        "python",
        "instruct",
        "script",
        "executable",
        "voice",
        "style",
    )
    for key in raw:
        low = key.lower()
        if any(low.startswith(p) for p in _FORBIDDEN_PREFIXES):
            raise ValueError(f"voice.tts.cosyvoice forbidden key: {key}")

    fields: dict[str, int] = {}
    for key, default in _DEFAULTS.items():
        val = raw.get(key, default)
        lo, hi = _BOUNDS[key]
        fields[key] = _parse_field(f"voice.tts.cosyvoice.{key}", val, lo=lo, hi=hi)
    return CosyVoiceSettings(**fields)
