"""
语音模块

提供 STT（语音转文字）与 TTS（文字转语音）引擎的对外导出。
"""
from src.voice.stt import STTEngine
from src.voice.tts import TTSEngine

__all__ = ["TTSEngine", "STTEngine"]
