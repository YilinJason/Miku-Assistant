"""
TTS 子包

导出 ``TTSEngine`` 门面类（edge-tts + 可选 RVC 后处理）。
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from src._lazy_exports import install_lazy_exports

if TYPE_CHECKING:
    from src.voice.tts.engine import TTSEngine

__all__ = ["TTSEngine"]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "TTSEngine": ("src.voice.tts.engine", "TTSEngine"),
}

install_lazy_exports(sys.modules[__name__])
