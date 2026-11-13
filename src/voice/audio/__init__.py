"""
音频 I/O 子包

提供音频播放（``AudioPlayer``）与临时文件安全清理（``TempFileManager``）。
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from src._lazy_exports import install_lazy_exports

if TYPE_CHECKING:
    from src.voice.audio.player import AudioPlayer
    from src.voice.audio.temp_files import TempFileManager

__all__ = ["AudioPlayer", "TempFileManager"]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AudioPlayer": ("src.voice.audio.player", "AudioPlayer"),
    "TempFileManager": ("src.voice.audio.temp_files", "TempFileManager"),
}

install_lazy_exports(sys.modules[__name__])
