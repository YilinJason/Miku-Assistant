"""资源协调配置：保活、退出卸载与 GPU 串行预热。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Optional

ResourceProfile = Literal["balanced", "low_vram", "aggressive_keepalive"]
WarmupOrder = Literal["ollama_then_rvc", "rvc_then_ollama"]

_VALID_PROFILES = frozenset({"balanced", "low_vram", "aggressive_keepalive"})
_VALID_ORDERS = frozenset({"ollama_then_rvc", "rvc_then_ollama"})

# 资源协调等待 Ollama 预热（串行 GPU）硬上限；与用户前台等待分离
DEFAULT_WARMUP_TIMEOUT_SEC = 120.0
MAX_WARMUP_TIMEOUT_SEC = 300.0
# 用户前台请求等待后台预热完成的短上限
DEFAULT_FOREGROUND_WARMUP_WAIT_SEC = 5.0
MAX_FOREGROUND_WARMUP_WAIT_SEC = 10.0


@dataclass(frozen=True)
class ResourceSettings:
    """装配与关闭路径读取的窄资源配置。"""

    profile: ResourceProfile = "balanced"
    ollama_warmup_on_start: bool = True
    ollama_unload_on_exit: bool = False
    rvc_warmup_on_voice_start: bool = True
    serialize_gpu_warmup: bool = True
    warmup_order: WarmupOrder = "ollama_then_rvc"
    warmup_timeout: float = DEFAULT_WARMUP_TIMEOUT_SEC
    foreground_warmup_wait_sec: float = DEFAULT_FOREGROUND_WARMUP_WAIT_SEC


def _require_bool(path: str, value: Any, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be bool, got {type(value).__name__}")
    return value


def _require_finite_positive(
    path: str,
    value: Any,
    default: float,
    *,
    max_value: Optional[float] = None,
) -> float:
    if value is None:
        number = float(default)
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"{path} must be a finite number, got {type(value).__name__}"
            )
        number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{path} must be a finite number > 0")
    if max_value is not None and number > float(max_value):
        raise ValueError(f"{path} must be <= {max_value:g}")
    return number


def parse_resource_settings(config: Any) -> ResourceSettings:
    """
    从应用配置解析 ``resource.*``。

    缺省 ``profile=balanced``：串行 GPU 预热；``unload_on_exit`` 默认 false，
    **不会**因 profile 暗中开启共享模型卸载（须显式配置）。
    ``ollama.warmup_on_start`` / ``warmup_timeout`` 在 ``resource.ollama.*`` 缺省时回退。
    """
    get = config.get if hasattr(config, "get") else lambda _k, d=None: d

    profile_raw = get("resource.profile", "balanced")
    if profile_raw is None or profile_raw == "":
        profile = "balanced"
    else:
        if not isinstance(profile_raw, str):
            raise ValueError("resource.profile must be str")
        profile = profile_raw.strip().lower()
        if profile not in _VALID_PROFILES:
            raise ValueError(
                f"resource.profile must be one of {sorted(_VALID_PROFILES)}, got {profile_raw!r}"
            )

    legacy_warmup = _require_bool(
        "ollama.warmup_on_start",
        get("ollama.warmup_on_start", True),
        True,
    )
    legacy_timeout = _require_finite_positive(
        "ollama.warmup_timeout",
        get("ollama.warmup_timeout", DEFAULT_WARMUP_TIMEOUT_SEC),
        DEFAULT_WARMUP_TIMEOUT_SEC,
        max_value=MAX_WARMUP_TIMEOUT_SEC,
    )

    ollama_warmup = _require_bool(
        "resource.ollama.warmup_on_start",
        get("resource.ollama.warmup_on_start", None),
        legacy_warmup,
    )
    unload = _require_bool(
        "resource.ollama.unload_on_exit",
        get("resource.ollama.unload_on_exit", False),
        False,
    )
    rvc_warmup = _require_bool(
        "resource.rvc.warmup_on_voice_start",
        get("resource.rvc.warmup_on_voice_start", True),
        True,
    )
    serialize = _require_bool(
        "resource.warmup.serialize_gpu",
        get("resource.warmup.serialize_gpu", True),
        True,
    )
    order_raw = get("resource.warmup.order", "ollama_then_rvc")
    if order_raw is None or order_raw == "":
        order: WarmupOrder = "ollama_then_rvc"
    else:
        if not isinstance(order_raw, str):
            raise ValueError("resource.warmup.order must be str")
        order_norm = order_raw.strip().lower()
        if order_norm not in _VALID_ORDERS:
            raise ValueError(
                f"resource.warmup.order must be one of {sorted(_VALID_ORDERS)}, got {order_raw!r}"
            )
        order = order_norm  # type: ignore[assignment]

    timeout = _require_finite_positive(
        "resource.warmup.timeout",
        get("resource.warmup.timeout", None),
        legacy_timeout,
        max_value=MAX_WARMUP_TIMEOUT_SEC,
    )
    foreground_wait = _require_finite_positive(
        "resource.warmup.foreground_wait_sec",
        get("resource.warmup.foreground_wait_sec", None),
        DEFAULT_FOREGROUND_WARMUP_WAIT_SEC,
        max_value=MAX_FOREGROUND_WARMUP_WAIT_SEC,
    )

    if profile == "aggressive_keepalive":
        if get("resource.warmup.serialize_gpu", None) is None:
            serialize = False

    return ResourceSettings(
        profile=profile,  # type: ignore[arg-type]
        ollama_warmup_on_start=ollama_warmup,
        ollama_unload_on_exit=unload,
        rvc_warmup_on_voice_start=rvc_warmup,
        serialize_gpu_warmup=serialize,
        warmup_order=order,
        warmup_timeout=timeout,
        foreground_warmup_wait_sec=foreground_wait,
    )
