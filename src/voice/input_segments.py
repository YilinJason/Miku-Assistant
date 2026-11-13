# -*- coding: utf-8 -*-
"""
纯有界 utterance 分段器（32ms Silero 帧输入；无能量门；无麦克风）。

样本索引为单调 ``_sample_cursor``（非设备采集时钟）。``reset(stream_generation=N)``
用于显式代际/间隙；代际须为非负 int（不含 bool）。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, List, Optional, Tuple

SAMPLE_RATE_HZ = 16000
FRAME_SAMPLES = 512
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_MS = 32

PREROLL_MS = 256
PREROLL_FRAMES = PREROLL_MS // FRAME_MS
START_THRESHOLD = 0.6
START_CONFIRM_FRAMES = 3
END_THRESHOLD = 0.45
END_QUIET_FRAMES = 20

MIN_VOICED_MS = 180
MIN_CONSECUTIVE_MS = 90
MIN_VOICED_RATIO = 0.12

def _validate_admission_ratio(path: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(path)
    out = float(value)
    if not math.isfinite(out) or out < 0.0 or out > 1.0:
        raise ValueError(path)
    return out


def _validate_admission_ms(path: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(path)
    if value < 0:
        raise ValueError(path)
    return value


@dataclass(frozen=True)
class SegmenterAdmissionConfig:
    start_threshold: float = START_THRESHOLD
    end_threshold: float = END_THRESHOLD
    min_voiced_ms: int = MIN_VOICED_MS
    min_consecutive_ms: int = MIN_CONSECUTIVE_MS
    min_voiced_ratio: float = MIN_VOICED_RATIO

    def __post_init__(self) -> None:
        start = _validate_admission_ratio(
            "start_threshold", self.start_threshold
        )
        end = _validate_admission_ratio("end_threshold", self.end_threshold)
        min_voiced = _validate_admission_ms("min_voiced_ms", self.min_voiced_ms)
        min_consecutive = _validate_admission_ms(
            "min_consecutive_ms", self.min_consecutive_ms
        )
        ratio = _validate_admission_ratio("min_voiced_ratio", self.min_voiced_ratio)
        object.__setattr__(self, "start_threshold", start)
        object.__setattr__(self, "end_threshold", end)
        object.__setattr__(self, "min_voiced_ms", min_voiced)
        object.__setattr__(self, "min_consecutive_ms", min_consecutive)
        object.__setattr__(self, "min_voiced_ratio", ratio)
        if start > 0.0 and end >= start:
            raise ValueError("end_threshold must stay below start_threshold")
        if min_consecutive > min_voiced:
            raise ValueError("min_consecutive_ms cannot exceed min_voiced_ms")


def _bounded_end_threshold(start_threshold: float) -> float:
    if not math.isfinite(start_threshold):
        raise ValueError("start_threshold must be finite")
    if start_threshold <= 0.0:
        return 0.0
    margin = max(0.05, start_threshold * 0.1)
    end = min(END_THRESHOLD, start_threshold - margin)
    if end >= start_threshold:
        end = start_threshold * 0.5
    if end < 0.0:
        end = 0.0
    return end


def admission_config_from_input_audio(settings: Any) -> SegmenterAdmissionConfig:
    from src.voice.input_audio import InputAudioSettings

    if not isinstance(settings, InputAudioSettings):
        raise TypeError("settings must be InputAudioSettings")
    start = float(settings.speech_probability_threshold)
    return SegmenterAdmissionConfig(
        start_threshold=start,
        end_threshold=_bounded_end_threshold(start),
        min_voiced_ms=settings.min_voiced_ms,
        min_consecutive_ms=settings.min_consecutive_ms,
        min_voiced_ratio=settings.min_voiced_ratio,
    )


MAX_UTTERANCE_SEC = 12
MAX_SEGMENT_SAMPLES = SAMPLE_RATE_HZ * MAX_UTTERANCE_SEC
MAX_FRAMES_PER_SEGMENT = MAX_SEGMENT_SAMPLES // FRAME_SAMPLES


@dataclass(frozen=True)
class SegmentFrameRecord:
    pcm: bytes
    probability: float
    start_sample: int
    voiced: bool


@dataclass(frozen=True)
class InputSegment:
    pcm: bytes
    start_sample: int
    end_sample: int
    voiced_ms: int
    voiced_ratio: float
    consecutive_ms: int
    end_reason: str
    stream_generation: int


def _validate_stream_generation(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("stream_generation must be a non-negative int")
    if value < 0:
        raise ValueError("stream_generation must be a non-negative int")
    return value


def _validate_frame_pcm(pcm: bytes) -> Optional[str]:
    if type(pcm) is not bytes:
        return "pcm_invalid_type"
    if len(pcm) != FRAME_BYTES:
        return "frame_geometry"
    return None


def _validate_probability(value: float) -> Optional[str]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "probability_invalid"
    prob = float(value)
    if not math.isfinite(prob) or prob < 0.0 or prob > 1.0:
        return "probability_invalid"
    return None


class BoundedInputSegmenter:
    """有界 preroll + 当前段；每帧在发射时间轴上恰好出现一次。"""

    def __init__(
        self,
        *,
        stream_generation: int = 0,
        admission: Optional[SegmenterAdmissionConfig] = None,
    ) -> None:
        self._stream_generation = _validate_stream_generation(stream_generation)
        self._admission = admission or SegmenterAdmissionConfig()
        self._sample_cursor = 0
        self._preroll: Deque[SegmentFrameRecord] = deque(maxlen=PREROLL_FRAMES)
        self._segment_frames: List[SegmentFrameRecord] = []
        self._in_segment = False
        self._start_confirm = 0
        self._quiet_frames = 0

    @property
    def stream_generation(self) -> int:
        return self._stream_generation

    def reset(self, *, stream_generation: Optional[int] = None) -> None:
        if stream_generation is not None:
            self._stream_generation = _validate_stream_generation(stream_generation)
        self._sample_cursor = 0
        self._preroll.clear()
        self._segment_frames.clear()
        self._in_segment = False
        self._start_confirm = 0
        self._quiet_frames = 0

    def _admission_metrics(
        self, frames: List[SegmentFrameRecord]
    ) -> Tuple[int, float, int]:
        voiced_samples = 0
        total_samples = len(frames) * FRAME_SAMPLES
        consecutive = 0
        longest_consecutive = 0
        for record in frames:
            if record.voiced:
                voiced_samples += FRAME_SAMPLES
                consecutive += FRAME_SAMPLES
                longest_consecutive = max(longest_consecutive, consecutive)
            else:
                consecutive = 0
        if total_samples <= 0:
            return 0, 0.0, 0
        voiced_ms = voiced_samples * 1000 // SAMPLE_RATE_HZ
        ratio = voiced_samples / float(total_samples)
        consecutive_ms = longest_consecutive * 1000 // SAMPLE_RATE_HZ
        return voiced_ms, ratio, consecutive_ms

    def _admission_ok(self, frames: List[SegmentFrameRecord]) -> bool:
        voiced_ms, ratio, consecutive_ms = self._admission_metrics(frames)
        cfg = self._admission
        return (
            voiced_ms >= cfg.min_voiced_ms
            and consecutive_ms >= cfg.min_consecutive_ms
            and ratio >= cfg.min_voiced_ratio
        )

    def _build_segment(
        self, frames: List[SegmentFrameRecord], end_reason: str
    ) -> Optional[InputSegment]:
        if len(frames) > MAX_FRAMES_PER_SEGMENT:
            return None
        if not frames or not self._admission_ok(frames):
            return None
        pcm = b"".join(record.pcm for record in frames)
        total_samples = len(frames) * FRAME_SAMPLES
        voiced_ms, ratio, consecutive_ms = self._admission_metrics(frames)
        start = frames[0].start_sample
        end = start + total_samples
        return InputSegment(
            pcm=pcm,
            start_sample=start,
            end_sample=end,
            voiced_ms=voiced_ms,
            voiced_ratio=ratio,
            consecutive_ms=consecutive_ms,
            end_reason=end_reason,
            stream_generation=self._stream_generation,
        )

    def _clear_segment_state(self) -> None:
        self._segment_frames.clear()
        self._in_segment = False
        self._quiet_frames = 0
        self._start_confirm = 0

    def _close_segment(self, end_reason: str) -> Optional[InputSegment]:
        frames = list(self._segment_frames)
        self._clear_segment_state()
        self._preroll.clear()
        return self._build_segment(frames, end_reason)

    def _begin_continuation(self, frame: SegmentFrameRecord) -> None:
        self._in_segment = True
        self._segment_frames = [frame]
        if frame.probability < self._admission.end_threshold:
            self._quiet_frames = 1
        else:
            self._quiet_frames = 0
        self._start_confirm = 0

    def feed_frame(
        self,
        frame_pcm: bytes,
        probability: float,
    ) -> Optional[InputSegment]:
        pcm_err = _validate_frame_pcm(frame_pcm)
        if pcm_err is not None:
            self.reset(stream_generation=self._stream_generation)
            return None
        prob_err = _validate_probability(probability)
        if prob_err is not None:
            self.reset(stream_generation=self._stream_generation)
            return None

        start_sample = self._sample_cursor
        self._sample_cursor += FRAME_SAMPLES
        voiced = probability >= self._admission.start_threshold
        record = SegmentFrameRecord(
            pcm=frame_pcm,
            probability=float(probability),
            start_sample=start_sample,
            voiced=voiced,
        )

        if self._in_segment:
            emitted: Optional[InputSegment] = None
            if len(self._segment_frames) >= MAX_FRAMES_PER_SEGMENT:
                emitted = self._close_segment("max_duration")
                self._begin_continuation(record)
                return emitted

            self._segment_frames.append(record)
            if probability < self._admission.end_threshold:
                self._quiet_frames += 1
                if self._quiet_frames >= END_QUIET_FRAMES:
                    return self._close_segment("trailing_silence")
            else:
                self._quiet_frames = 0
            return None

        self._preroll.append(record)
        if probability >= self._admission.start_threshold:
            self._start_confirm += 1
            if self._start_confirm >= START_CONFIRM_FRAMES:
                self._in_segment = True
                self._segment_frames = list(self._preroll)
                self._quiet_frames = 0
                self._start_confirm = 0
        else:
            self._start_confirm = 0
        return None

    def finish(self) -> Optional[InputSegment]:
        if not self._in_segment:
            return None
        return self._close_segment("explicit_finish")
