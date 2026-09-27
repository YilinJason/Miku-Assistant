"""TTS 音频预处理（送入 RVC 前）"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

import librosa
import numpy as np
import soundfile as sf


_WARMED_SAMPLE_RATES: set[int] = set()
_WARMUP_LOCK = threading.Lock()


def warm_audio_preprocessing(target_sr: int = 40000) -> bool:
    """用内存短数组预热 librosa 重采样路径；幂等且不创建/播放音频文件。"""
    target_sr = int(target_sr)
    if target_sr <= 0:
        raise ValueError("target_sr must be positive")
    with _WARMUP_LOCK:
        if target_sr in _WARMED_SAMPLE_RATES:
            return False
        source = np.zeros(2048, dtype=np.float32)
        librosa.resample(source, orig_sr=24000, target_sr=target_sr)
        _WARMED_SAMPLE_RATES.add(target_sr)
        return True


def mp3_to_rvc_wav(
    mp3_path: str,
    wav_path: str,
    rvc_config: Optional[Dict[str, Any]] = None,
) -> str:
    """
    MP3 → WAV，并重采样到 RVC 友好采样率（默认 40000Hz）。

    统一采样率可减少 RVC 内部重采样带来的伪影/电流感。
    """
    rvc_config = rvc_config or {}
    target_sr = int(rvc_config.get("input_sample_rate", 40000))
    audio, _ = librosa.load(mp3_path, sr=target_sr, mono=True)
    audio = np.clip(audio, -1.0, 1.0)
    sf.write(wav_path, audio, target_sr)
    return wav_path
