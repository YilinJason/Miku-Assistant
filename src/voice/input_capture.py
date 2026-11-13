# -*- coding: utf-8 -*-
"""
有界连续输入采集控制器（单工作线程；本切片不接入 STT/CLI）。

模块导入面仅 stdlib；PyAudio/numpy/ORT 仅在工作线程内懒加载。
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Optional, Protocol, Tuple

from src.voice.input_audio import (
    INPUT_AUDIO_BACKEND_SILERO,
    NOISE_SUPPRESSION_NONE,
    NOISE_SUPPRESSION_WEBRTC,
    InputAudioSettings,
)
from src.voice.input_segments import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    InputSegment,
    admission_config_from_input_audio,
    BoundedInputSegmenter,
)

CAPTURE_QUEUE_CAPACITY = 2
CAPTURE_JOIN_TIMEOUT_SEC = 2.0
CAPTURE_STALE_SEC = 3.0
CAPTURE_READ_SLICE_SEC = 0.05
MAX_OVERFLOW_RESETS = 3
SAMPLE_RATE_HZ = 16000
FRAME_DURATION_SEC = FRAME_SAMPLES / SAMPLE_RATE_HZ


class _StopCancelView:
    """只读取消观测；不可 set/clear。"""

    __slots__ = ("_event",)

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    def is_set(self) -> bool:
        return self._event.is_set()


class CaptureSource(Protocol):
    def open(self) -> Optional[str]: ...
    def read_frame(self) -> Tuple[Optional[bytes], Optional[str]]: ...
    def close(self) -> bool: ...


ClockFn = Callable[[], float]
SourceFactory = Callable[[], CaptureSource]
NoiseStreamFactory = Callable[[], Any]
SileroContextFactory = Callable[[Any], Tuple[Any, Optional[str]]]


@dataclass(frozen=True)
class CapturedSegment:
    pcm: bytes
    start_sample: int
    end_sample: int
    voiced_ms: int
    voiced_ratio: float
    consecutive_ms: int
    end_reason: str
    stream_generation: int
    session_id: int
    capture_start_mono: float
    capture_end_mono: float


@dataclass(frozen=True)
class CaptureStatus:
    running: bool
    complete: bool
    pending_cleanup: bool
    device_error: str
    overflow_resets: int
    dropped_segments: int
    session_id: int
    generation: int


def _validate_read_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a positive finite number")
    out = float(value)
    if not math.isfinite(out) or out <= 0.0:
        raise ValueError("timeout must be a positive finite number")
    return out


def _validate_close_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_sec must be a finite number in [0, 2]")
    out = float(value)
    if not math.isfinite(out) or out < 0.0 or out > CAPTURE_JOIN_TIMEOUT_SEC:
        raise ValueError("timeout_sec must be a finite number in [0, 2]")
    return out


def _segment_to_captured(
    segment: InputSegment,
    *,
    session_id: int,
    capture_start: float,
    capture_end: float,
) -> CapturedSegment:
    return CapturedSegment(
        pcm=segment.pcm,
        start_sample=segment.start_sample,
        end_sample=segment.end_sample,
        voiced_ms=segment.voiced_ms,
        voiced_ratio=segment.voiced_ratio,
        consecutive_ms=segment.consecutive_ms,
        end_reason=segment.end_reason,
        stream_generation=segment.stream_generation,
        session_id=session_id,
        capture_start_mono=capture_start,
        capture_end_mono=capture_end,
    )


def _close_capture_source(source: CaptureSource) -> bool:
    try:
        return source.close() is True
    except Exception:
        return False


def _close_noise_stream(ns_stream: Any) -> bool:
    try:
        return ns_stream.close(timeout_sec=None) is True
    except Exception:
        return False


def _close_silero_context(silero_ctx: Any) -> bool:
    try:
        return silero_ctx.close(timeout_sec=None) is True
    except Exception:
        return False


class ContinuousInputCapture:
    """单线程拥有采集、增量 NS、Silero 流上下文与分段器。"""

    def __init__(
        self,
        *,
        settings: InputAudioSettings,
        silero_vad_owner: Any,
        source_factory: Optional[SourceFactory] = None,
        clock: Optional[ClockFn] = None,
        noise_stream_factory: Optional[NoiseStreamFactory] = None,
        silero_context_factory: Optional[SileroContextFactory] = None,
    ) -> None:
        if not isinstance(settings, InputAudioSettings):
            raise TypeError("settings must be InputAudioSettings")
        if not settings.enabled:
            raise ValueError("capture requires enabled input audio")
        if settings.backend != INPUT_AUDIO_BACKEND_SILERO:
            raise ValueError("capture requires silero_onnx backend")
        device_index = settings.microphone_device_index
        self._settings = settings
        self._device_index = device_index
        self._silero_owner = silero_vad_owner
        self._source_factory = source_factory or (
            lambda: _default_pyaudio_source(device_index)
        )
        self._clock = clock or time.monotonic
        self._noise_stream_factory = noise_stream_factory
        self._silero_context_factory = silero_context_factory

        self._lifecycle_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._queue_notify = threading.Condition(self._queue_lock)
        self._stop = threading.Event()
        self._startup_ready = threading.Event()
        self._cleanup_retry = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._starting = False

        self._complete = False
        self._pending_cleanup = False
        self._running = False
        self._device_error = ""
        self._overflow_resets = 0
        self._dropped_segments = 0
        self._session_id = 0
        self._generation = 0
        self._worker_error = ""
        self._worker_cleanup_ok = True
        self._startup_succeeded = False

        self._segments: Deque[CapturedSegment] = deque(maxlen=CAPTURE_QUEUE_CAPACITY)

    def _can_complete_close(self, thread: Optional[threading.Thread]) -> bool:
        if self._starting:
            return False
        if thread is not None and thread.is_alive():
            return False
        return self._worker_cleanup_ok

    def _apply_close_success(self) -> None:
        self._complete = True
        self._pending_cleanup = False
        self._thread = None

    @property
    def stop_cancel_view(self) -> _StopCancelView:
        """只读取消视图；仅暴露 ``is_set``。"""
        return _StopCancelView(self._stop)

    def status(self) -> CaptureStatus:
        with self._lifecycle_lock:
            return CaptureStatus(
                running=self._running,
                complete=self._complete,
                pending_cleanup=self._pending_cleanup,
                device_error=self._device_error,
                overflow_resets=self._overflow_resets,
                dropped_segments=self._dropped_segments,
                session_id=self._session_id,
                generation=self._generation,
            )

    def start(self) -> Tuple[bool, str]:
        with self._lifecycle_lock:
            if self._complete:
                return False, "complete"
            if self._pending_cleanup:
                return False, "pending_cleanup"
            if self._starting:
                return False, "already_running"
            if self._thread is not None:
                if self._thread.is_alive():
                    return False, "already_running"
                if self._startup_succeeded or self._worker_error:
                    return False, "session_ended"
                return False, "session_ended"
            if self._stop.is_set():
                return False, "closing"
            self._stop.clear()
            self._startup_ready.clear()
            self._startup_succeeded = False
            self._worker_error = ""
            self._device_error = ""
            self._session_id += 1
            thread = threading.Thread(
                target=self._worker_main,
                name="continuous-input-capture",
                daemon=True,
            )
            self._thread = thread
            self._starting = True
        try:
            thread.start()
        except Exception:
            with self._lifecycle_lock:
                self._thread = None
                self._starting = False
            return False, "thread_start_failed"
        with self._lifecycle_lock:
            self._starting = False
        if not self._startup_ready.wait(timeout=CAPTURE_JOIN_TIMEOUT_SEC):
            self._stop.set()
            self._cleanup_retry.set()
            with self._queue_notify:
                self._queue_notify.notify_all()
            return False, "startup_timeout"
        with self._lifecycle_lock:
            if self._worker_error:
                if not self._thread or not self._thread.is_alive():
                    if self._worker_cleanup_ok:
                        self._pending_cleanup = False
                else:
                    self._pending_cleanup = True
                return False, self._worker_error
            if not self._startup_succeeded:
                if self._thread and self._thread.is_alive():
                    self._pending_cleanup = True
                reason = self._worker_error or "startup_failed"
                return False, reason
        return True, ""

    def close(self, *, timeout_sec: Optional[float] = None) -> bool:
        budget = (
            CAPTURE_JOIN_TIMEOUT_SEC
            if timeout_sec is None
            else _validate_close_timeout(timeout_sec)
        )
        self._stop.set()
        with self._lifecycle_lock:
            if self._starting:
                self._pending_cleanup = True
                self._running = False
            with self._queue_lock:
                self._segments.clear()
        with self._queue_notify:
            self._queue_notify.notify_all()
        self._cleanup_retry.set()
        thread = None
        launching = False
        with self._lifecycle_lock:
            launching = self._starting
            thread = self._thread
            self._running = False
        if launching:
            return False
        if thread is None:
            with self._lifecycle_lock:
                if self._can_complete_close(None):
                    self._apply_close_success()
                    return True
                self._pending_cleanup = not self._worker_cleanup_ok
            return False
        if not thread.is_alive():
            with self._lifecycle_lock:
                if self._can_complete_close(None):
                    self._apply_close_success()
                    return True
                self._pending_cleanup = True
            return False
        thread.join(timeout=budget)
        with self._lifecycle_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                self._pending_cleanup = True
                return False
            if self._can_complete_close(None):
                self._apply_close_success()
                return True
            self._pending_cleanup = not self._worker_cleanup_ok
            return False

    def read_segment(
        self,
        timeout_sec: float,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> Tuple[Optional[CapturedSegment], str]:
        timeout = _validate_read_timeout(timeout_sec)
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None, "cancelled"
            now_mono = time.monotonic()
            if now_mono >= deadline:
                return None, "timeout"
            with self._lifecycle_lock:
                if self._worker_error:
                    return None, self._worker_error
                if self._device_error:
                    return None, self._device_error
                if self._stop.is_set() and not self._running:
                    return None, "not_ready"
                if self._complete:
                    return None, "not_ready"
                generation = self._generation
                session_id = self._session_id
                capture_now = self._clock()
                with self._queue_lock:
                    if self._stop.is_set() and not self._running:
                        return None, "not_ready"
                    while self._segments:
                        item = self._segments.popleft()
                        if item.stream_generation != generation:
                            continue
                        if item.session_id != session_id:
                            continue
                        if capture_now - item.capture_end_mono > CAPTURE_STALE_SEC:
                            continue
                        if self._stop.is_set() and not self._running:
                            return None, "not_ready"
                        return item, "ok"
            remaining = min(CAPTURE_READ_SLICE_SEC, deadline - now_mono)
            if remaining <= 0:
                return None, "timeout"
            with self._queue_notify:
                self._queue_notify.wait(timeout=remaining)

    def _enqueue_segment(self, segment: CapturedSegment) -> None:
        with self._lifecycle_lock:
            if self._stop.is_set():
                return
            generation = self._generation
            session_id = self._session_id
            if segment.stream_generation != generation:
                return
            with self._queue_lock:
                if self._stop.is_set():
                    return
                if self._generation != generation or self._session_id != session_id:
                    return
                if len(self._segments) >= CAPTURE_QUEUE_CAPACITY:
                    self._segments.popleft()
                    self._dropped_segments += 1
                self._segments.append(segment)
        with self._queue_notify:
            self._queue_notify.notify_all()

    def _create_silero_context(
        self,
    ) -> Tuple[Optional[Any], Optional[str]]:
        if self._silero_context_factory is not None:
            return self._silero_context_factory(self._silero_owner)
        return self._silero_owner.create_stream_context(cancel_event=self._stop)

    def _warm_silero_owner(self) -> Optional[str]:
        warm = self._silero_owner.score_pcm(
            b"\x00" * FRAME_BYTES,
            cancel_event=self._stop,
        )
        if not getattr(warm, "ok", False):
            return getattr(warm, "reason", "vad_warm_failed") or "vad_warm_failed"
        return None

    def _cleanup_resources(
        self,
        source: Optional[CaptureSource],
        ns_stream: Any,
        silero_ctx: Any,
    ) -> Tuple[bool, Optional[CaptureSource], Any, Any]:
        all_ok = True
        if silero_ctx is not None:
            if _close_silero_context(silero_ctx):
                silero_ctx = None
            else:
                all_ok = False
        if ns_stream is not None:
            if _close_noise_stream(ns_stream):
                ns_stream = None
            else:
                all_ok = False
        if source is not None:
            if _close_capture_source(source):
                source = None
            else:
                all_ok = False
        return all_ok, source, ns_stream, silero_ctx

    def _worker_main(self) -> None:
        source: Optional[CaptureSource] = None
        ns_stream: Any = None
        silero_ctx: Any = None
        segmenter: Optional[BoundedInputSegmenter] = None
        cleaned_pending = b""
        capture_anchor: Optional[float] = None

        def _should_stop() -> bool:
            return self._stop.is_set()

        try:
            if _should_stop():
                return
            if self._settings.noise_suppression == NOISE_SUPPRESSION_WEBRTC:
                ns_stream = (
                    self._noise_stream_factory()
                    if self._noise_stream_factory is not None
                    else _lazy_noise_stream()
                )
            if _should_stop():
                return
            warm_err = self._warm_silero_owner()
            if warm_err is not None:
                self._worker_error = warm_err
                return
            if _should_stop():
                return
            silero_ctx, err = self._create_silero_context()
            if err is not None or silero_ctx is None:
                self._worker_error = err or "vad_unavailable"
                return
            if _should_stop():
                return

            source = self._source_factory()
            open_err = source.open()
            if _should_stop():
                return
            if open_err is not None:
                self._worker_error = open_err
                return
            if _should_stop():
                return

            segmenter = BoundedInputSegmenter(
                stream_generation=self._generation,
                admission=admission_config_from_input_audio(self._settings),
            )
            with self._lifecycle_lock:
                if _should_stop():
                    return
                self._running = True
                self._startup_succeeded = True
            self._startup_ready.set()

            outer_active = True
            while outer_active and not _should_stop():
                raw, read_err = source.read_frame()
                if _should_stop():
                    break
                if read_err == "overflow":
                    ok, ns_stream, silero_ctx, segmenter, cleaned_pending = (
                        self._handle_overflow(
                            segmenter,
                            ns_stream,
                            silero_ctx,
                        )
                    )
                    capture_anchor = None
                    if not ok:
                        self._device_error = (
                            self._device_error or "overflow_reset_failed"
                        )
                        break
                    if self._device_error == "overflow_limit":
                        break
                    continue
                if read_err is not None:
                    self._device_error = read_err
                    break
                if not raw:
                    self._device_error = "device_read_empty"
                    break
                if len(raw) != FRAME_BYTES:
                    self._device_error = "frame_geometry"
                    break

                if capture_anchor is None:
                    capture_anchor = self._clock() - FRAME_DURATION_SEC

                cleaned_chunk = raw
                if ns_stream is not None:
                    ns_out = ns_stream.process_chunk(
                        raw,
                        cancel_event=self._stop,
                    )
                    if _should_stop():
                        break
                    if not ns_out.ok:
                        self._device_error = ns_out.reason
                        break
                    cleaned_chunk = ns_out.pcm
                cleaned_pending += cleaned_chunk
                vad_failed = False
                while len(cleaned_pending) >= FRAME_BYTES:
                    if _should_stop():
                        outer_active = False
                        break
                    frame = cleaned_pending[:FRAME_BYTES]
                    cleaned_pending = cleaned_pending[FRAME_BYTES:]
                    vad = silero_ctx.process_frame(
                        frame,
                        cancel_event=self._stop,
                    )
                    if not vad.ok:
                        if vad.reason in ("closed", "closing", "cancelled"):
                            outer_active = False
                            break
                        self._device_error = vad.reason
                        vad_failed = True
                        break
                    emitted = segmenter.feed_frame(frame, vad.probability)
                    if emitted is not None and capture_anchor is not None:
                        if _should_stop():
                            outer_active = False
                            break
                        cap_start = capture_anchor + (
                            emitted.start_sample / SAMPLE_RATE_HZ
                        )
                        cap_end = capture_anchor + (
                            emitted.end_sample / SAMPLE_RATE_HZ
                        )
                        with self._lifecycle_lock:
                            session_id = self._session_id
                        self._enqueue_segment(
                            _segment_to_captured(
                                emitted,
                                session_id=session_id,
                                capture_start=cap_start,
                                capture_end=cap_end,
                            )
                        )
                if vad_failed or self._device_error or not outer_active:
                    break
        except BaseException as exc:
            self._worker_error = type(exc).__name__
        finally:
            with self._lifecycle_lock:
                self._running = False
            if not self._startup_ready.is_set():
                self._startup_ready.set()

            pending_source: Optional[CaptureSource] = source
            pending_ns = ns_stream
            pending_ctx = silero_ctx
            while True:
                self._cleanup_retry.clear()
                ok, pending_source, pending_ns, pending_ctx = self._cleanup_resources(
                    pending_source,
                    pending_ns,
                    pending_ctx,
                )
                if (
                    ok
                    and pending_source is None
                    and pending_ns is None
                    and pending_ctx is None
                ):
                    with self._lifecycle_lock:
                        self._worker_cleanup_ok = True
                        self._pending_cleanup = False
                    break
                with self._lifecycle_lock:
                    self._worker_cleanup_ok = False
                    self._pending_cleanup = True
                if self._cleanup_retry.is_set():
                    continue
                self._cleanup_retry.wait()

    def _handle_overflow(
        self,
        segmenter: BoundedInputSegmenter,
        ns_stream: Any,
        silero_ctx: Any,
    ) -> Tuple[bool, Any, Any, BoundedInputSegmenter, bytes]:
        cleaned_pending = b""
        with self._lifecycle_lock:
            self._overflow_resets += 1
            self._generation += 1
            generation = self._generation
            with self._queue_lock:
                self._segments.clear()
        segmenter.reset(stream_generation=generation)
        with self._queue_notify:
            self._queue_notify.notify_all()

        if self._overflow_resets >= MAX_OVERFLOW_RESETS:
            self._device_error = "overflow_limit"
            self._stop.set()
            return True, ns_stream, silero_ctx, segmenter, cleaned_pending

        if ns_stream is not None:
            if not _close_noise_stream(ns_stream):
                return False, ns_stream, silero_ctx, segmenter, cleaned_pending
            ns_stream = None
            try:
                ns_stream = (
                    self._noise_stream_factory()
                    if self._noise_stream_factory is not None
                    else _lazy_noise_stream()
                )
            except Exception:
                return False, None, silero_ctx, segmenter, cleaned_pending

        if not _close_silero_context(silero_ctx):
            return False, ns_stream, silero_ctx, segmenter, cleaned_pending
        silero_ctx = None
        try:
            silero_ctx, err = self._create_silero_context()
            if err is not None or silero_ctx is None:
                return False, ns_stream, None, segmenter, cleaned_pending
        except Exception:
            return False, ns_stream, None, segmenter, cleaned_pending

        return True, ns_stream, silero_ctx, segmenter, cleaned_pending


def _lazy_noise_stream() -> Any:
    from src.voice.noise_suppression import WebRtcNoiseStream

    return WebRtcNoiseStream()


def _default_pyaudio_source(device_index: Optional[int]) -> CaptureSource:
    return _PyAudioCaptureSource(device_index)


class _PyAudioCaptureSource:
    def __init__(self, device_index: Optional[int]) -> None:
        self._device_index = device_index
        self._pa: Any = None
        self._stream: Any = None

    def open(self) -> Optional[str]:
        try:
            import pyaudio
        except ImportError:
            return "pyaudio_unavailable"
        except OSError:
            return "pyaudio_unavailable"
        self._pa = pyaudio.PyAudio()
        try:
            open_kwargs: dict[str, Any] = {
                "format": pyaudio.paInt16,
                "channels": 1,
                "rate": SAMPLE_RATE_HZ,
                "input": True,
                "frames_per_buffer": FRAME_SAMPLES,
            }
            if self._device_index is not None:
                open_kwargs["input_device_index"] = self._device_index
            self._stream = self._pa.open(**open_kwargs)
        except Exception:
            self._safe_teardown()
            return "device_open_failed"
        return None

    def read_frame(self) -> Tuple[Optional[bytes], Optional[str]]:
        if self._stream is None:
            return None, "not_open"
        try:
            import pyaudio

            data = self._stream.read(
                FRAME_SAMPLES,
                exception_on_overflow=True,
            )
        except OSError as exc:
            errno = getattr(exc, "errno", None)
            try:
                import pyaudio as pa_mod

                if errno == pa_mod.paInputOverflowed:
                    return None, "overflow"
            except Exception:
                pass
            return None, "device_read_failed"
        except Exception:
            return None, "device_read_failed"
        if type(data) is not bytes or len(data) != FRAME_BYTES:
            return None, "frame_geometry"
        return data, None

    def close(self) -> bool:
        if self._safe_teardown():
            return True
        return False

    def _safe_teardown(self) -> bool:
        stream_cleared = self._stream is None
        pa_cleared = self._pa is None
        if self._stream is not None:
            try:
                self._stream.stop_stream()
            except Exception:
                pass
            try:
                self._stream.close()
                self._stream = None
                stream_cleared = True
            except Exception:
                stream_cleared = False
        if stream_cleared and self._pa is not None:
            try:
                self._pa.terminate()
                self._pa = None
                pa_cleared = True
            except Exception:
                pa_cleared = False
        return stream_cleared and pa_cleared
