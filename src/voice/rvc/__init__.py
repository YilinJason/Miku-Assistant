"""
RVC 音色转换子包

提供 RVC 转换器、健康检查与就绪校验的对外导出。
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from src._lazy_exports import install_lazy_exports

if TYPE_CHECKING:
    from src.voice.rvc.converter import RVCConverter
    from src.voice.rvc.health_check import (
        RVCHealthReport,
        check_rvc_health,
        ensure_rvc_ready,
    )

__all__ = [
    "RVCConverter",
    "RVCHealthReport",
    "check_rvc_health",
    "ensure_rvc_ready",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "RVCConverter": ("src.voice.rvc.converter", "RVCConverter"),
    "RVCHealthReport": ("src.voice.rvc.health_check", "RVCHealthReport"),
    "check_rvc_health": ("src.voice.rvc.health_check", "check_rvc_health"),
    "ensure_rvc_ready": ("src.voice.rvc.health_check", "ensure_rvc_ready"),
}

install_lazy_exports(sys.modules[__name__])
