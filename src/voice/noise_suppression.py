# -*- coding: utf-8 -*-
"""窄 WebRTC NS 预处理（utterance 批处理 + 有界流式 chunk）。"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from src.voice.input_audio import (
    NOISE_SUPPRESSION_NONE,
    NOISE_SUPPRESSION_WEBRTC,
    SAMPLE_RATE_HZ,
    validate_pcm_geometry,
)

FRAME_SAMPLES = 160
FRAME_BYTES = FRAME_SAMPLES * 2
MAX_CHUNK_BYTES = 4096
MAX_RESIDUAL_SAMPLES = FRAME_SAMPLES - 1
STREAM_CLOSE_BUDGET_SEC = 2.0

WEBRTC_NS_PACKAGE = "pywebrtc-audio==0.2.0"
WEBRTC_NS_INSTALL_HINT = (
    f"麦克风降噪不可用：请在主程序环境中安装 {WEBRTC_NS_PACKAGE} "
    "（Windows CPython 3.12 wheel；见 docs/VOICE.md）"
)

ProcessorFactory = Callable[[], Any]


def is_noise_suppression_not_ready_reason(reason: str) -> bool:
    if not reason:
        return False
    if reason.startswith("ns_"):
        return True
    return reason in ("numpy_unavailable", "pywebrtc_unavailable")


@dataclass(frozen=True)
class NoiseSuppressionResult:
    ok: bool
    reason: str
    pcm: bytes = b""
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class NoiseStreamChunkResult:
    ok: bool
    reason: str
    pcm: bytes = b""


def _import_numpy() -> Tuple[Any, Optional[str]]:
    try:
        import numpy as np
    except ImportError:
        return None, "numpy_unavailable"
    except OSError:
        return None, "numpy_unavailable"
    return np, None


def _import_pywebrtc_audio() -> Tuple[Any, Optional[str]]:
    try:
        import pywebrtc_audio  # type: ignore[import-untyped]
    except ImportError:
        return None, "pywebrtc_unavailable"
    except OSError:
        return None, "pywebrtc_unavailable"
    return pywebrtc_audio, None


def probe_webrtc_noise_suppression_readiness() -> Tuple[bool, str]:
    _, np_err = _import_numpy()
    if np_err is not None:
        return False, WEBRTC_NS_INSTALL_HINT
    _, ort_err = _import_pywebrtc_audio()
    if ort_err is not None:
        return False, WEBRTC_NS_INSTALL_HINT
    return True, ""


def default_webrtc_processor_factory() -> Any:
    module, err = _import_pywebrtc_audio()
    if module is None:
        raise OSError(err or "pywebrtc_unavailable")
    return module.AudioProcessor(
        sample_rate=SAMPLE_RATE_HZ,
        num_channels=1,
        noise_suppression=True,
        high_pass_filter=True,
        ns_level=1,
        auto_gain_control=False,
        echo_cancellation=False,
    )


def _stable_error(reason: str) -> NoiseSuppressionResult:
    return NoiseSuppressionResult(ok=False, reason=reason, pcm=b"")


def _stable_chunk_error(reason: str) -> NoiseStreamChunkResult:
    return NoiseStreamChunkResult(ok=False, reason=reason, pcm=b"")


def _validate_close_timeout(value: Any) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError("close timeout must be a finite number of seconds")
    budget = float(value)
    if not math.isfinite(budget) or budget < 0.0 or budget > STREAM_CLOSE_BUDGET_SEC:
        raise ValueError("close timeout must be between 0 and 2 seconds")
    return budget


def _process_ns_frame(
    processor: Any,
    np: Any,
    chunk: Any,
    *,
    should_abort: Callable[[], Optional[str]],
) -> Tuple[Optional[Any], Optional[str]]:
    if (reason := should_abort()) is not None:
        return None, reason
    try:
        processed = processor.process(chunk)
    except OSError:
        return None, "ns_native_error"
    except Exception:
        return None, "ns_processing_failed"
    if (reason := should_abort()) is not None:
        return None, reason
    if processed is None or not isinstance(processed, np.ndarray):
        return None, "ns_output_invalid"
    if processed.dtype != np.int16:
        return None, "ns_output_invalid"
    if processed.ndim != 1 or processed.shape[0] != FRAME_SAMPLES:
        return None, "ns_output_invalid"
    return processed, None


class WebRtcNoiseStream:
    """有界增量 NS：至多 159 残留样本；仅完整 160 样本帧进 native。"""

    def __init__(
        self,
        *,
        processor_factory: Optional[ProcessorFactory] = None,
    ) -> None:
        self._factory = processor_factory or default_webrtc_processor_factory
        self._processor: Any = None
        self._np: Any = None
        self._residual = b""
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._abort_requested = threading.Event()
        self._closed = False
        self._invalidated = False
        self._finished = False
        self._active_ops = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def invalidated(self) -> bool:
        return self._invalidated

    def _should_abort(
        self, cancel_event: Optional[threading.Event]
    ) -> Optional[str]:
        if self._invalidated or self._abort_requested.is_set():
            return "invalidated"
        if self._finished:
            return "finished"
        if self._closed or self._closing.is_set():
            return "closed"
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        return None

    def _apply_invalidation_locked(self) -> None:
        self._invalidated = True
        self._residual = b""
        self._abort_requested.clear()

    def _request_invalidate_or_apply(self) -> None:
        if self._lock.acquire(blocking=False):
            try:
                self._apply_invalidation_locked()
            finally:
                self._lock.release()
        else:
            self._abort_requested.set()

    def _fail_chunk(
        self,
        reason: str,
        *,
        under_lock: bool = False,
    ) -> NoiseStreamChunkResult:
        if under_lock:
            self._apply_invalidation_locked()
        else:
            self._request_invalidate_or_apply()
        return _stable_chunk_error(reason)

    def _ensure_processor(
        self,
        cancel_event: Optional[threading.Event],
    ) -> Optional[str]:
        if (reason := self._should_abort(cancel_event)) is not None:
            return reason
        if self._processor is not None:
            return None
        np, np_err = _import_numpy()
        if np is None:
            return np_err or "numpy_unavailable"
        _, webrtc_err = _import_pywebrtc_audio()
        if webrtc_err is not None:
            return "ns_unavailable"
        if (reason := self._should_abort(cancel_event)) is not None:
            return reason
        try:
            self._processor = self._factory()
        except OSError:
            return "ns_unavailable"
        except Exception:
            return "ns_native_error"
        if (reason := self._should_abort(cancel_event)) is not None:
            self._processor = None
            return reason
        self._np = np
        return None

    def process_chunk(
        self,
        pcm: bytes,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> NoiseStreamChunkResult:
        if (reason := self._should_abort(cancel_event)) is not None:
            return self._fail_chunk(reason)
        if type(pcm) is not bytes:
            return self._fail_chunk("pcm_invalid_type")
        if not pcm:
            return NoiseStreamChunkResult(ok=True, reason="ok", pcm=b"")
        if len(pcm) > MAX_CHUNK_BYTES:
            return self._fail_chunk("pcm_chunk_oversized")
        if len(pcm) % 2 != 0:
            return self._fail_chunk("pcm_misaligned")

        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return _stable_chunk_error("busy")
        try:
            self._active_ops += 1
            if (reason := self._should_abort(cancel_event)) is not None:
                return self._fail_chunk(reason, under_lock=True)
            err = self._ensure_processor(cancel_event)
            if err is not None:
                return self._fail_chunk(err, under_lock=True)

            assert self._np is not None and self._processor is not None
            np = self._np
            combined = self._residual + pcm
            self._residual = b""
            samples = np.frombuffer(combined, dtype=np.int16)
            out_parts: list[bytes] = []
            offset = 0
            total = len(samples)
            abort = lambda: self._should_abort(cancel_event)
            while offset + FRAME_SAMPLES <= total:
                frame = samples[offset : offset + FRAME_SAMPLES]
                processed, frame_err = _process_ns_frame(
                    self._processor, np, frame, should_abort=abort
                )
                if frame_err is not None:
                    return self._fail_chunk(frame_err, under_lock=True)
                out_parts.append(processed.tobytes())
                offset += FRAME_SAMPLES
            tail = samples[offset:]
            if len(tail) > 0:
                if len(tail) > MAX_RESIDUAL_SAMPLES:
                    return self._fail_chunk("residual_overflow", under_lock=True)
                if (reason := self._should_abort(cancel_event)) is not None:
                    return self._fail_chunk(reason, under_lock=True)
                self._residual = tail.tobytes()
            if (reason := self._should_abort(cancel_event)) is not None:
                return self._fail_chunk(reason, under_lock=True)
            return NoiseStreamChunkResult(
                ok=True, reason="ok", pcm=b"".join(out_parts)
            )
        finally:
            self._active_ops -= 1
            self._lock.release()

    def finish(
        self,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> NoiseStreamChunkResult:
        if (reason := self._should_abort(cancel_event)) is not None:
            return self._fail_chunk(reason)
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return _stable_chunk_error("busy")
        try:
            self._active_ops += 1
            if (reason := self._should_abort(cancel_event)) is not None:
                return self._fail_chunk(reason, under_lock=True)
            if self._finished:
                return NoiseStreamChunkResult(ok=True, reason="ok", pcm=b"")
            if not self._residual:
                if (reason := self._should_abort(cancel_event)) is not None:
                    return self._fail_chunk(reason, under_lock=True)
                self._finished = True
                return NoiseStreamChunkResult(ok=True, reason="ok", pcm=b"")
            err = self._ensure_processor(cancel_event)
            if err is not None:
                return self._fail_chunk(err, under_lock=True)
            assert self._np is not None and self._processor is not None
            np = self._np
            tail = np.frombuffer(self._residual, dtype=np.int16)
            valid = len(tail)
            padded = np.pad(tail, (0, FRAME_SAMPLES - valid))
            if (reason := self._should_abort(cancel_event)) is not None:
                return self._fail_chunk(reason, under_lock=True)
            processed, frame_err = _process_ns_frame(
                self._processor,
                np,
                padded,
                should_abort=lambda: self._should_abort(cancel_event),
            )
            if frame_err is not None:
                return self._fail_chunk(frame_err, under_lock=True)
            if (reason := self._should_abort(cancel_event)) is not None:
                return self._fail_chunk(reason, under_lock=True)
            self._residual = b""
            self._finished = True
            return NoiseStreamChunkResult(
                ok=True, reason="ok", pcm=processed[:valid].tobytes()
            )
        finally:
            self._active_ops -= 1
            self._lock.release()

    def close(self, *, timeout_sec: Optional[float] = None) -> bool:
        raw = STREAM_CLOSE_BUDGET_SEC if timeout_sec is None else timeout_sec
        budget = _validate_close_timeout(raw)
        self._closing.set()
        acquired = self._lock.acquire(timeout=budget)
        if not acquired:
            return False
        try:
            if self._active_ops > 0:
                return False
            self._closed = True
            self._residual = b""
            if self._processor is not None:
                processor = self._processor
                self._processor = None
                del processor
            self._np = None
            return True
        finally:
            self._lock.release()


def suppress_utterance_pcm(
    pcm: bytes,
    *,
    cancel_event: Optional[threading.Event] = None,
    processor_factory: Optional[ProcessorFactory] = None,
) -> NoiseSuppressionResult:
    started = time.perf_counter()
    if cancel_event is not None and cancel_event.is_set():
        return _stable_error("cancelled")
    if type(pcm) is not bytes:
        return _stable_error("pcm_invalid_type")
    geom = validate_pcm_geometry(pcm)
    if geom is not None:
        return _stable_error(geom)

    stream = WebRtcNoiseStream(processor_factory=processor_factory)
    out_parts: list[bytes] = []
    offset = 0
    try:
        while offset < len(pcm):
            if cancel_event is not None and cancel_event.is_set():
                return _stable_error("cancelled")
            chunk = pcm[offset : offset + MAX_CHUNK_BYTES]
            result = stream.process_chunk(chunk, cancel_event=cancel_event)
            if not result.ok:
                return _stable_error(result.reason)
            out_parts.append(result.pcm)
            offset += len(chunk)
        if cancel_event is not None and cancel_event.is_set():
            return _stable_error("cancelled")
        tail = stream.finish(cancel_event=cancel_event)
        if not tail.ok:
            return _stable_error(tail.reason)
        if cancel_event is not None and cancel_event.is_set():
            return _stable_error("cancelled")
        out_parts.append(tail.pcm)
        cleaned = b"".join(out_parts)
        if len(cleaned) != len(pcm):
            return _stable_error("ns_length_mismatch")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not math.isfinite(elapsed_ms):
            elapsed_ms = 0.0
        return NoiseSuppressionResult(
            ok=True,
            reason="ok",
            pcm=cleaned,
            elapsed_ms=elapsed_ms,
        )
    finally:
        stream.close()
