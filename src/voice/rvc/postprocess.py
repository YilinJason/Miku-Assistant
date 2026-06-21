"""RVC 输出后处理：减轻电流感/底噪，并防止削波"""
from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

logger = logging.getLogger(__name__)


def polish_rvc_wav(wav_path: str, rvc_config: Dict[str, Any] | None = None) -> None:
    """
    对 RVC 输出 wav 做轻量后处理（原地覆盖）。

    - 高通滤波：去除 ~80Hz 以下 hum
    - 软限幅：峰值归一到 0.95，减轻数字失真
    """
    rvc_config = rvc_config or {}
    if not rvc_config.get("postprocess_enabled", True):
        return

    highpass_hz = float(rvc_config.get("postprocess_highpass_hz", 80))
    peak_target = float(rvc_config.get("postprocess_peak", 0.95))

    audio, sr = sf.read(wav_path, always_2d=False)
    if audio.size == 0:
        return

    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)

    audio = audio.astype(np.float32, copy=False)

    if highpass_hz > 0 and sr > highpass_hz * 2:
        sos = butter(2, highpass_hz, btype="highpass", fs=sr, output="sos")
        audio = sosfilt(sos, audio).astype(np.float32)

    peak = float(np.max(np.abs(audio)))
    if peak > 1e-6:
        audio = audio * min(1.0, peak_target / peak)

    sf.write(wav_path, audio, sr)
    logger.debug("RVC 后处理完成: %s (hp=%.0fHz)", wav_path, highpass_hz)
