"""
文字转语音模块（向后兼容导出）

历史路径 ``src.voice.tts`` 仍指向 ``TTSEngine``，实际实现位于 ``src.voice.tts.engine``。
"""
from src.voice.tts.engine import TTSEngine

__all__ = ["TTSEngine"]
