"""
语音模块

提供 STT（语音转文字）与 TTS（文字转语音）引擎的对外导出。
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from src._lazy_exports import install_lazy_exports

if TYPE_CHECKING:
    from src.voice.stt import STTEngine
    from src.voice.tts.engine import TTSEngine

__all__ = ["TTSEngine", "STTEngine"]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "TTSEngine": ("src.voice.tts", "TTSEngine"),
    "STTEngine": ("src.voice.stt", "STTEngine"),
}

install_lazy_exports(sys.modules[__name__])
