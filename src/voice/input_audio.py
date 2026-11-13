# -*- coding: utf-8 -*-
"""麦克风采集前的有界能量门限与 WebRTC VAD 声学准入（stdlib 配置面；VAD 懒加载）。"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Tuple

SAMPLE_RATE_HZ = 16000
FRAME_MS = 30
BYTES_PER_SAMPLE = 2
FRAME_BYTES = int(SAMPLE_RATE_HZ * FRAME_MS / 1000) * BYTES_PER_SAMPLE
MAX_UTTERANCE_SEC = 15.0
MAX_PCM_BYTES = int(SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * MAX_UTTERANCE_SEC)
MAX_MICROPHONE_DEVICE_INDEX = 1024

VAD_WHEELS_PACKAGE = "webrtcvad-wheels==2.0.14.post1"
VAD_INSTALL_HINT = (
    f"声学 VAD 不可用：请在主程序环境中安装 {VAD_WHEELS_PACKAGE}（Windows CPython 3.12 wheel）"
)

CAPTURE_MODE_CONTINUOUS = "continuous"
CAPTURE_MODE_UTTERANCE = "utterance"
_VALID_CAPTURE_MODES = frozenset(
    {CAPTURE_MODE_CONTINUOUS, CAPTURE_MODE_UTTERANCE}
)

_KNOWN_INPUT_AUDIO_KEYS = frozenset(
    {
        "enabled",
        "backend",
        "capture_mode",
        "speech_probability_threshold",
        "energy_floor",
        "energy_ceiling",
        "vad_mode",
        "min_voiced_ms",
        "min_consecutive_ms",
        "min_voiced_ratio",
        "noise_suppression",
    }
)

INPUT_AUDIO_BACKEND_SILERO = "silero_onnx"
INPUT_AUDIO_BACKEND_WEBRTC = "webrtc"
_VALID_INPUT_AUDIO_BACKENDS = frozenset(
    {INPUT_AUDIO_BACKEND_SILERO, INPUT_AUDIO_BACKEND_WEBRTC}
)

NOISE_SUPPRESSION_WEBRTC = "webrtc"
NOISE_SUPPRESSION_NONE = "none"
_VALID_NOISE_SUPPRESSION = frozenset(
    {NOISE_SUPPRESSION_WEBRTC, NOISE_SUPPRESSION_NONE}
)

_vad_lock = threading.Lock()
_vad_module: Any = None
_vad_ready = False
_vad_failure_hint: str = ""
_vad_import_attempted = False


def _strict_positive_int(path: str, value: Any, *, min_val: int, max_val: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(path)
    if value < min_val or value > max_val:
        raise ValueError(path)
    return value


def _strict_ratio(path: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(path)
    if isinstance(value, int):
        out = float(value)
    elif isinstance(value, float):
        out = float(value)
    else:
        raise ValueError(path)
    if not math.isfinite(out) or out < 0.0 or out > 1.0:
        raise ValueError(path)
    return out


def validate_stt_energy_threshold_value(value: Any, *, path: str = "voice.stt.energy_threshold") -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(path)
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(path)
    return int(number)


def _parse_microphone_device_index(value: Any) -> Optional[int]:
    if value is None:
        return None
    return _strict_positive_int(
        "voice.stt.microphone_device_index",
        value,
        min_val=0,
        max_val=MAX_MICROPHONE_DEVICE_INDEX,
    )


def _parse_input_audio_backend(value: Any) -> str:
    if value is None:
        return INPUT_AUDIO_BACKEND_SILERO
    if not isinstance(value, str) or value not in _VALID_INPUT_AUDIO_BACKENDS:
        raise ValueError("backend must be 'silero_onnx' or 'webrtc'")
    return value


def _parse_noise_suppression(value: Any) -> str:
    if not isinstance(value, str) or value not in _VALID_NOISE_SUPPRESSION:
        raise ValueError("noise_suppression must be 'webrtc' or 'none'")
    return value


def _parse_capture_mode(value: Any) -> str:
    if value is None:
        raise ValueError("capture_mode must be 'continuous' or 'utterance'")
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("capture_mode must be 'continuous' or 'utterance'")
    if value not in _VALID_CAPTURE_MODES:
        raise ValueError("capture_mode must be 'continuous' or 'utterance'")
    return value


def _validated_input_audio_fields(
    *,
    enabled: Any,
    backend: Any,
    speech_probability_threshold: Any,
    noise_suppression: Any,
    energy_floor: Any,
    energy_ceiling: Any,
    vad_mode: Any,
    min_voiced_ms: Any,
    min_consecutive_ms: Any,
    min_voiced_ratio: Any,
    microphone_device_index: Any,
    capture_mode: Any,
) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be bool")
    backend_value = _parse_input_audio_backend(backend)
    threshold = _strict_ratio(
        "speech_probability_threshold", speech_probability_threshold
    )
    ns_value = _parse_noise_suppression(noise_suppression)
    floor = _strict_positive_int(
        "energy_floor", energy_floor, min_val=1, max_val=50000
    )
    ceiling = _strict_positive_int(
        "energy_ceiling", energy_ceiling, min_val=1, max_val=50000
    )
    vad = _strict_positive_int("vad_mode", vad_mode, min_val=0, max_val=3)
    voiced_ms = _strict_positive_int(
        "min_voiced_ms", min_voiced_ms, min_val=FRAME_MS, max_val=15000
    )
    consecutive_ms = _strict_positive_int(
        "min_consecutive_ms",
        min_consecutive_ms,
        min_val=FRAME_MS,
        max_val=15000,
    )
    ratio = _strict_ratio("min_voiced_ratio", min_voiced_ratio)
    mic_idx = _parse_microphone_device_index(microphone_device_index)
    capture_mode_value = _parse_capture_mode(capture_mode)
    if floor >= ceiling:
        raise ValueError("energy_floor must be < energy_ceiling")
    if consecutive_ms > voiced_ms:
        raise ValueError("min_consecutive_ms must be <= min_voiced_ms")
    if voiced_ms % FRAME_MS != 0:
        raise ValueError("min_voiced_ms must align to 30ms frames")
    if consecutive_ms % FRAME_MS != 0:
        raise ValueError("min_consecutive_ms must align to 30ms frames")
    return {
        "enabled": enabled,
        "backend": backend_value,
        "speech_probability_threshold": threshold,
        "noise_suppression": ns_value,
        "energy_floor": floor,
        "energy_ceiling": ceiling,
        "vad_mode": vad,
        "min_voiced_ms": voiced_ms,
        "min_consecutive_ms": consecutive_ms,
        "min_voiced_ratio": ratio,
        "microphone_device_index": mic_idx,
        "capture_mode": capture_mode_value,
    }


def validate_paraformer_pcm_bytes(pcm: Any) -> Optional[str]:
    """Paraformer 协议 PCM 几何校验（16 kHz mono s16）；失败原因供显式拒绝。"""
    if type(pcm) is not bytes:
        return "pcm_invalid_type"
    return validate_pcm_geometry(pcm)


def compute_paraformer_listen_phrase_limit_sec(
    source: Any,
    recognizer: Any,
    caller_phrase_limit: Optional[float],
) -> float:
    """按 source CHUNK（采样帧数）/SAMPLE_RATE 与 non_speaking 预留，再与 caller 取 min。"""
    chunk = int(getattr(source, "CHUNK", 1024))
    rate = int(getattr(source, "SAMPLE_RATE", SAMPLE_RATE_HZ))
    width = int(getattr(source, "SAMPLE_WIDTH", BYTES_PER_SAMPLE))
    if chunk <= 0 or rate <= 0 or width <= 0:
        raise ValueError("invalid audio source geometry")
    frame_duration = chunk / float(rate)
    non_speaking_raw = getattr(recognizer, "non_speaking_duration", 0.5)
    if isinstance(non_speaking_raw, bool) or not isinstance(
        non_speaking_raw, (int, float)
    ):
        raise ValueError("invalid non_speaking_duration")
    non_speaking = float(non_speaking_raw)
    if not math.isfinite(non_speaking) or non_speaking <= 0.0:
        raise ValueError("invalid non_speaking_duration")
    reserve_frames = math.ceil(non_speaking / frame_duration) + 1
    reserve_sec = reserve_frames * frame_duration
    budget = MAX_UTTERANCE_SEC - reserve_sec
    if not math.isfinite(budget) or budget <= frame_duration:
        raise ValueError("invalid phrase budget")
    if caller_phrase_limit is not None:
        if isinstance(caller_phrase_limit, bool) or not isinstance(
            caller_phrase_limit, (int, float)
        ):
            raise ValueError("invalid caller phrase limit")
        caller = float(caller_phrase_limit)
        if not math.isfinite(caller) or caller <= 0.0:
            raise ValueError("invalid caller phrase limit")
        budget = min(budget, caller)
    if budget <= 0.0 or budget > MAX_UTTERANCE_SEC:
        raise ValueError("invalid phrase budget")
    return budget


@dataclass(frozen=True)
class InputAudioSettings:
    """Provisional 阈值；不保证所有环境零误触发。"""

    enabled: bool = True
    backend: str = INPUT_AUDIO_BACKEND_SILERO
    speech_probability_threshold: float = 0.6
    noise_suppression: str = "webrtc"
    energy_floor: int = 300
    energy_ceiling: int = 12000
    vad_mode: int = 2
    min_voiced_ms: int = 180
    min_consecutive_ms: int = 90
    min_voiced_ratio: float = 0.12
    microphone_device_index: Optional[int] = None
    capture_mode: str = CAPTURE_MODE_CONTINUOUS

    def __post_init__(self) -> None:
        normalized = _validated_input_audio_fields(
            enabled=self.enabled,
            backend=self.backend,
            speech_probability_threshold=self.speech_probability_threshold,
            noise_suppression=self.noise_suppression,
            energy_floor=self.energy_floor,
            energy_ceiling=self.energy_ceiling,
            vad_mode=self.vad_mode,
            min_voiced_ms=self.min_voiced_ms,
            min_consecutive_ms=self.min_consecutive_ms,
            min_voiced_ratio=self.min_voiced_ratio,
            microphone_device_index=self.microphone_device_index,
            capture_mode=self.capture_mode,
        )
        for key, value in normalized.items():
            object.__setattr__(self, key, value)

    @classmethod
    def from_mapping(
        cls,
        data: Optional[Mapping[str, Any]],
        *,
        microphone_device_index: Any = None,
    ) -> "InputAudioSettings":
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise ValueError("voice.stt.input_audio must be a mapping")
        unknown = set(data.keys()) - _KNOWN_INPUT_AUDIO_KEYS
        if unknown:
            raise ValueError(f"voice.stt.input_audio unknown keys: {sorted(unknown)}")
        return cls(
            enabled=data.get("enabled", True),
            backend=data.get("backend", INPUT_AUDIO_BACKEND_SILERO),
            speech_probability_threshold=data.get(
                "speech_probability_threshold", 0.6
            ),
            noise_suppression=(
                NOISE_SUPPRESSION_WEBRTC
                if "noise_suppression" not in data
                else _parse_noise_suppression(data["noise_suppression"])
            ),
            energy_floor=data.get("energy_floor", 300),
            energy_ceiling=data.get("energy_ceiling", 12000),
            vad_mode=data.get("vad_mode", 2),
            min_voiced_ms=data.get("min_voiced_ms", 180),
            min_consecutive_ms=data.get("min_consecutive_ms", 90),
            min_voiced_ratio=data.get("min_voiced_ratio", 0.12),
            microphone_device_index=microphone_device_index,
            capture_mode=data.get("capture_mode", CAPTURE_MODE_CONTINUOUS),
        )


def resolve_input_audio_settings_from_stt_config(stt_cfg: Any) -> InputAudioSettings:
    if stt_cfg is None:
        stt_cfg = {}
    elif not isinstance(stt_cfg, dict):
        raise ValueError("voice.stt must be a mapping")
    inner = stt_cfg.get("input_audio")
    if inner is not None and not isinstance(inner, dict):
        raise ValueError("voice.stt.input_audio must be a mapping when present")
    mic_raw = stt_cfg.get("microphone_device_index")
    if "microphone_device_index" in stt_cfg and mic_raw is not None:
        _parse_microphone_device_index(mic_raw)
    if "energy_threshold" in stt_cfg:
        validate_stt_energy_threshold_value(stt_cfg["energy_threshold"])
    return InputAudioSettings.from_mapping(
        inner,
        microphone_device_index=mic_raw,
    )


@dataclass(frozen=True)
class AcousticGuardVerdict:
    accepted: bool
    reason: str
    voiced_ms: int = 0
    consecutive_ms: int = 0
    voiced_ratio: float = 0.0
    frames_analyzed: int = 0
    partial_frame_ignored_bytes: int = 0


def _clamp_energy_value(value: Any, *, floor: int, ceiling: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(floor)
    if not math.isfinite(number):
        number = float(floor)
    return max(float(floor), min(float(ceiling), number))


def create_bounded_recognizer(
    settings: InputAudioSettings,
    *,
    initial_energy: int,
    recognizer_cls: Optional[type] = None,
) -> Any:
    """工厂：真实 SR Recognizer 子类，listen/calibrate 内部写 threshold 仍走同一 self。"""
    import speech_recognition as sr

    base_cls = recognizer_cls or sr.Recognizer
    if not settings.enabled:
        return base_cls()

    floor = int(settings.energy_floor)
    ceiling = int(settings.energy_ceiling)
    if floor >= ceiling:
        raise ValueError("invalid energy bounds")

    class BoundedRecognizer(base_cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            self._bounded_floor = floor
            self._bounded_ceiling = ceiling
            super().__init__()
            self.energy_threshold = initial_energy

        def _clamp_energy(self, value: Any) -> float:
            return _clamp_energy_value(
                value, floor=self._bounded_floor, ceiling=self._bounded_ceiling
            )

        @property
        def energy_threshold(self) -> float:
            return float(
                self.__dict__.get(
                    "_bounded_energy_threshold",
                    self._bounded_floor,
                )
            )

        @energy_threshold.setter
        def energy_threshold(self, value: Any) -> None:
            self.__dict__["_bounded_energy_threshold"] = self._clamp_energy(value)

    return BoundedRecognizer()


def reset_vad_backend_state_for_tests() -> None:
    """仅测试：重置 VAD 懒加载缓存。"""
    global _vad_module, _vad_ready, _vad_failure_hint, _vad_import_attempted
    with _vad_lock:
        _vad_module = None
        _vad_ready = False
        _vad_failure_hint = ""
        _vad_import_attempted = False


def resolve_vad_backend() -> Tuple[bool, str, Any]:
    """线程安全懒加载；ImportError/OSError/构造失败均 fail-closed。"""
    global _vad_module, _vad_ready, _vad_failure_hint, _vad_import_attempted
    with _vad_lock:
        if _vad_ready and _vad_module is not None:
            return True, "", _vad_module
        if _vad_import_attempted and not _vad_ready:
            return False, _vad_failure_hint or VAD_INSTALL_HINT, None
        _vad_import_attempted = True
        try:
            import webrtcvad  # type: ignore[import-untyped]

            webrtcvad.Vad(2)
        except ImportError:
            _vad_failure_hint = VAD_INSTALL_HINT
            return False, _vad_failure_hint, None
        except OSError as exc:
            _vad_failure_hint = f"{VAD_INSTALL_HINT} ({type(exc).__name__})"
            return False, _vad_failure_hint, None
        except Exception as exc:
            _vad_failure_hint = f"{VAD_INSTALL_HINT} ({type(exc).__name__})"
            return False, _vad_failure_hint, None
        _vad_module = webrtcvad
        _vad_ready = True
        return True, "", _vad_module


def vad_backend_available() -> bool:
    ok, _, _ = resolve_vad_backend()
    return ok


def resolve_acoustic_input_readiness(settings: InputAudioSettings) -> Tuple[bool, str]:
    if not settings.enabled:
        return True, ""
    from src.voice.noise_suppression import (
        NOISE_SUPPRESSION_WEBRTC,
        probe_webrtc_noise_suppression_readiness,
    )

    if settings.noise_suppression == NOISE_SUPPRESSION_WEBRTC:
        ready, hint = probe_webrtc_noise_suppression_readiness()
        if not ready:
            return False, hint
    if settings.backend == INPUT_AUDIO_BACKEND_WEBRTC:
        ok, hint, _ = resolve_vad_backend()
        if ok:
            return True, ""
        return False, hint
    from src.voice.silero_vad import probe_silero_install_readiness

    return probe_silero_install_readiness()


def acoustic_guard_not_ready_hint(
    settings: InputAudioSettings,
    *,
    reason: str = "",
) -> str:
    from src.voice.noise_suppression import (
        WEBRTC_NS_INSTALL_HINT,
        is_noise_suppression_not_ready_reason,
    )

    if (
        settings.noise_suppression == NOISE_SUPPRESSION_WEBRTC
        and is_noise_suppression_not_ready_reason(reason)
    ):
        return WEBRTC_NS_INSTALL_HINT
    if settings.backend == INPUT_AUDIO_BACKEND_SILERO:
        from src.voice.silero_vad import SILERO_READINESS_HINT

        if reason in (
            "silero_unavailable",
            "model_unavailable",
            "model_load_failed",
            "model_io_invalid",
            "model_provider_invalid",
            "asset_missing",
            "asset_hash_mismatch",
            "asset_size_mismatch",
            "asset_symlink",
            "asset_unavailable",
            "numpy_unavailable",
            "ort_unavailable",
        ):
            return SILERO_READINESS_HINT
        ready, hint = resolve_acoustic_input_readiness(settings)
        if not ready and hint:
            return hint
        return SILERO_READINESS_HINT
    return VAD_INSTALL_HINT


def acoustic_guard_ready(settings: InputAudioSettings) -> Tuple[bool, str]:
    return resolve_acoustic_input_readiness(settings)


def validate_pcm_geometry(pcm: bytes) -> Optional[str]:
    if not pcm:
        return "pcm_empty"
    if len(pcm) % BYTES_PER_SAMPLE != 0:
        return "pcm_misaligned"
    if len(pcm) > MAX_PCM_BYTES:
        return "pcm_oversized"
    if pcm == b"\x00" * len(pcm):
        return "pcm_zero"
    return None


def _iter_complete_frames(pcm: bytes) -> Tuple[list[bytes], int]:
    full_count = len(pcm) // FRAME_BYTES
    frames = [
        pcm[index * FRAME_BYTES : (index + 1) * FRAME_BYTES]
        for index in range(full_count)
    ]
    remainder = len(pcm) - full_count * FRAME_BYTES
    return frames, remainder


def analyze_utterance_pcm_silero(
    pcm: bytes,
    settings: InputAudioSettings,
    silero_vad: Any,
    *,
    cancel_event: Optional[threading.Event] = None,
    skip_common_prechecks: bool = False,
) -> AcousticGuardVerdict:
    """Silero 概率 VAD；voiced/连续/比例按 valid_samples 累计，不含尾帧 padding。"""
    from src.voice.silero_vad import SAMPLE_RATE_HZ as SILERO_RATE

    if not skip_common_prechecks:
        if cancel_event is not None and cancel_event.is_set():
            return AcousticGuardVerdict(
                accepted=False, reason="cancelled", frames_analyzed=0
            )
        if not settings.enabled:
            return AcousticGuardVerdict(
                accepted=True, reason="disabled", frames_analyzed=0
            )
        geom = validate_pcm_geometry(pcm)
        if geom is not None:
            return AcousticGuardVerdict(accepted=False, reason=geom, frames_analyzed=0)

    scores = silero_vad.score_pcm(pcm, cancel_event=cancel_event)
    if not scores.ok:
        reason = scores.reason
        if reason == "cancelled":
            return AcousticGuardVerdict(accepted=False, reason="cancelled", frames_analyzed=0)
        if reason in ("closed", "closing", "busy"):
            return AcousticGuardVerdict(accepted=False, reason=reason, frames_analyzed=0)
        if reason.startswith("pcm_"):
            return AcousticGuardVerdict(accepted=False, reason=reason, frames_analyzed=0)
        return AcousticGuardVerdict(accepted=False, reason="silero_unavailable", frames_analyzed=0)

    threshold = settings.speech_probability_threshold
    voiced_samples = 0
    total_samples = 0
    consecutive_samples = 0
    best_consecutive_samples = 0
    frames_analyzed = len(scores.probabilities)
    for prob, valid in zip(scores.probabilities, scores.valid_samples):
        count = int(valid)
        if count <= 0:
            continue
        total_samples += count
        if float(prob) >= threshold:
            voiced_samples += count
            consecutive_samples += count
            if consecutive_samples > best_consecutive_samples:
                best_consecutive_samples = consecutive_samples
        else:
            consecutive_samples = 0

    if total_samples <= 0:
        return AcousticGuardVerdict(
            accepted=False,
            reason="pcm_too_short",
            frames_analyzed=frames_analyzed,
        )

    voiced_ms = int(voiced_samples * 1000 // SILERO_RATE)
    best_consecutive_ms = int(best_consecutive_samples * 1000 // SILERO_RATE)
    voiced_ratio = voiced_samples / float(total_samples)

    if voiced_ms < settings.min_voiced_ms:
        return AcousticGuardVerdict(
            accepted=False,
            reason="insufficient_voiced_ms",
            voiced_ms=voiced_ms,
            consecutive_ms=best_consecutive_ms,
            voiced_ratio=voiced_ratio,
            frames_analyzed=frames_analyzed,
        )
    if best_consecutive_ms < settings.min_consecutive_ms:
        return AcousticGuardVerdict(
            accepted=False,
            reason="insufficient_consecutive_ms",
            voiced_ms=voiced_ms,
            consecutive_ms=best_consecutive_ms,
            voiced_ratio=voiced_ratio,
            frames_analyzed=frames_analyzed,
        )
    if voiced_ratio < settings.min_voiced_ratio:
        return AcousticGuardVerdict(
            accepted=False,
            reason="insufficient_voiced_ratio",
            voiced_ms=voiced_ms,
            consecutive_ms=best_consecutive_ms,
            voiced_ratio=voiced_ratio,
            frames_analyzed=frames_analyzed,
        )
    return AcousticGuardVerdict(
        accepted=True,
        reason="accepted",
        voiced_ms=voiced_ms,
        consecutive_ms=best_consecutive_ms,
        voiced_ratio=voiced_ratio,
        frames_analyzed=frames_analyzed,
    )


def analyze_utterance_pcm(
    pcm: bytes,
    settings: InputAudioSettings,
    *,
    frame_is_speech: Optional[Callable[[bytes], bool]] = None,
    cancel_event: Optional[threading.Event] = None,
    silero_vad: Any = None,
) -> AcousticGuardVerdict:
    """30ms 帧级 WebRTC VAD 或 Silero 概率 VAD；尾帧不足 30ms 不参与 WebRTC voiced 统计。"""
    if cancel_event is not None and cancel_event.is_set():
        return AcousticGuardVerdict(accepted=False, reason="cancelled", frames_analyzed=0)
    if not settings.enabled:
        return AcousticGuardVerdict(
            accepted=True,
            reason="disabled",
            frames_analyzed=0,
        )
    geom = validate_pcm_geometry(pcm)
    if geom is not None:
        return AcousticGuardVerdict(accepted=False, reason=geom, frames_analyzed=0)

    if settings.backend == INPUT_AUDIO_BACKEND_SILERO:
        if silero_vad is None:
            return AcousticGuardVerdict(
                accepted=False,
                reason="silero_unavailable",
                frames_analyzed=0,
            )
        return analyze_utterance_pcm_silero(
            pcm,
            settings,
            silero_vad,
            cancel_event=cancel_event,
            skip_common_prechecks=True,
        )

    frames, remainder = _iter_complete_frames(pcm)
    if not frames:
        return AcousticGuardVerdict(
            accepted=False,
            reason="pcm_too_short",
            frames_analyzed=0,
            partial_frame_ignored_bytes=remainder,
        )

    decisions: list[bool] = []
    if frame_is_speech is None:
        ok, hint, vad_mod = resolve_vad_backend()
        if not ok:
            return AcousticGuardVerdict(
                accepted=False,
                reason="vad_unavailable",
                frames_analyzed=0,
                partial_frame_ignored_bytes=remainder,
            )
        try:
            vad = vad_mod.Vad(settings.vad_mode)
            for frame in frames:
                if cancel_event is not None and cancel_event.is_set():
                    return AcousticGuardVerdict(
                        accepted=False,
                        reason="cancelled",
                        frames_analyzed=len(decisions),
                        partial_frame_ignored_bytes=remainder,
                    )
                decisions.append(bool(vad.is_speech(frame, SAMPLE_RATE_HZ)))
        except OSError:
            return AcousticGuardVerdict(
                accepted=False,
                reason="vad_native_error",
                frames_analyzed=0,
                partial_frame_ignored_bytes=remainder,
            )
        except Exception:
            return AcousticGuardVerdict(
                accepted=False,
                reason="vad_native_error",
                frames_analyzed=0,
                partial_frame_ignored_bytes=remainder,
            )
    else:
        for frame in frames:
            if cancel_event is not None and cancel_event.is_set():
                return AcousticGuardVerdict(
                    accepted=False,
                    reason="cancelled",
                    frames_analyzed=len(decisions),
                    partial_frame_ignored_bytes=remainder,
                )
            decisions.append(bool(frame_is_speech(frame)))

    voiced_ms = 0
    consecutive_ms = 0
    best_consecutive_ms = 0
    for is_speech in decisions:
        if is_speech:
            voiced_ms += FRAME_MS
            consecutive_ms += FRAME_MS
            if consecutive_ms > best_consecutive_ms:
                best_consecutive_ms = consecutive_ms
        else:
            consecutive_ms = 0

    frames_analyzed = len(frames)
    voiced_ratio = voiced_ms / float(frames_analyzed * FRAME_MS)

    if voiced_ms < settings.min_voiced_ms:
        return AcousticGuardVerdict(
            accepted=False,
            reason="insufficient_voiced_ms",
            voiced_ms=voiced_ms,
            consecutive_ms=best_consecutive_ms,
            voiced_ratio=voiced_ratio,
            frames_analyzed=frames_analyzed,
            partial_frame_ignored_bytes=remainder,
        )
    if best_consecutive_ms < settings.min_consecutive_ms:
        return AcousticGuardVerdict(
            accepted=False,
            reason="insufficient_consecutive_ms",
            voiced_ms=voiced_ms,
            consecutive_ms=best_consecutive_ms,
            voiced_ratio=voiced_ratio,
            frames_analyzed=frames_analyzed,
            partial_frame_ignored_bytes=remainder,
        )
    if voiced_ratio < settings.min_voiced_ratio:
        return AcousticGuardVerdict(
            accepted=False,
            reason="insufficient_voiced_ratio",
            voiced_ms=voiced_ms,
            consecutive_ms=best_consecutive_ms,
            voiced_ratio=voiced_ratio,
            frames_analyzed=frames_analyzed,
            partial_frame_ignored_bytes=remainder,
        )
    return AcousticGuardVerdict(
        accepted=True,
        reason="accepted",
        voiced_ms=voiced_ms,
        consecutive_ms=best_consecutive_ms,
        voiced_ratio=voiced_ratio,
        frames_analyzed=frames_analyzed,
        partial_frame_ignored_bytes=remainder,
    )


def validate_microphone_device_index(
    device_index: Optional[int],
    *,
    list_device_count: Callable[[], int],
) -> Optional[str]:
    if device_index is None:
        return None
    try:
        count = int(list_device_count())
    except Exception:
        return "无法枚举麦克风设备"
    if device_index < 0 or device_index >= count:
        return f"麦克风 device_index={device_index} 不可用（设备数={count}）"
    return None
