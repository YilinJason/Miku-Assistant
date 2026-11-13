"""可选 CosyVoice 本地 TTS 后端（切片 A/B：资产、配置、worker 客户端）。"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from src._lazy_exports import install_lazy_exports

if TYPE_CHECKING:
    from src.voice.cosyvoice.client import CosyVoiceShutdownStatus, CosyVoiceWorkerClient
    from src.voice.cosyvoice.settings import CosyVoiceSettings, parse_cosyvoice_settings

__all__ = [
    "CosyVoiceSettings",
    "CosyVoiceShutdownStatus",
    "CosyVoiceWorkerClient",
    "parse_cosyvoice_settings",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "CosyVoiceSettings": ("src.voice.cosyvoice.settings", "CosyVoiceSettings"),
    "parse_cosyvoice_settings": (
        "src.voice.cosyvoice.settings",
        "parse_cosyvoice_settings",
    ),
    "CosyVoiceShutdownStatus": (
        "src.voice.cosyvoice.client",
        "CosyVoiceShutdownStatus",
    ),
    "CosyVoiceWorkerClient": ("src.voice.cosyvoice.client", "CosyVoiceWorkerClient"),
}

install_lazy_exports(sys.modules[__name__])
