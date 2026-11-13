# -*- coding: utf-8 -*-
"""ContinuousInputCapture（fake 源/VAD/NS；无真实 PyAudio/ORT）。"""
from __future__ import annotations

import struct
import threading
import time
import unittest
from types import SimpleNamespace
from typing import Any, Callable, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from src.voice.input_audio import InputAudioSettings
from src.voice.input_capture import (
    CAPTURE_STALE_SEC,
    ContinuousInputCapture,
    MAX_OVERFLOW_RESETS,
    _PyAudioCaptureSource,
)
from src.voice.input_segments import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    SegmenterAdmissionConfig,
)
from src.voice.silero_vad_stream import SileroStreamFrameScores

_SAVED_PYAUDIO = None
_SAVED_MIC_INIT = None
_SAVED_MIC_ENTER = None
_SAVED_ORT = None


def _frame(tag: int = 1) -> bytes:
    return struct.pack("<h", tag) * FRAME_SAMPLES


class FakeCaptureSource:
    opens = 0
    closes = 0

    def __init__(
        self,
        frames: List[Any],
        *,
        block: Optional[threading.Event] = None,
        release: Optional[threading.Event] = None,
        end_block: Optional[threading.Event] = None,
        close_ok_after: int = 1,
        close_result: Any = True,
    ) -> None:
        self._frames = list(frames)
        self._block = block
        self._release = release
        self._end_block = end_block
        self._close_attempts = 0
        self._close_ok_after = close_ok_after
        self._close_result = close_result

    def open(self) -> Optional[str]:
        type(self).opens += 1
        return None

    def read_frame(self) -> Tuple[Optional[bytes], Optional[str]]:
        if self._block is not None:
            self._block.set()
            if self._release is not None and not self._release.wait(2.0):
                return None, "device_read_failed"
        if not self._frames:
            if self._end_block is not None:
                self._end_block.wait()
            return None, "device_read_empty"
        item = self._frames.pop(0)
        if item == "OVERFLOW":
            return None, "overflow"
        return item, None

    def close(self) -> bool:
        type(self).closes += 1
        self._close_attempts += 1
        if self._close_attempts < self._close_ok_after:
            if self._close_result is True:
                return False
            return self._close_result
        return True


class VoicedSileroCtx:
    """PCM 标签非 0=voiced、0=quiet。"""

    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    def process_frame(
        self,
        pcm: bytes,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> SileroStreamFrameScores:
        if cancel_event is not None and cancel_event.is_set():
            return SileroStreamFrameScores(ok=False, reason="cancelled")
        tag = struct.unpack("<h", pcm[:2])[0] if len(pcm) >= 2 else 0
        prob = 0.92 if tag != 0 else 0.05
        return SileroStreamFrameScores(ok=True, reason="ok", probability=prob)

    def reset(self) -> bool:
        return True

    def close(self, *, timeout_sec: Optional[float] = None) -> bool:
        self.close_calls += 1
        self.closed = True
        return True


class PassthroughNs:
    def process_chunk(
        self,
        pcm: bytes,
        *,
        cancel_event: Optional[threading.Event] = None,
        **_: Any,
    ) -> Any:
        from src.voice.noise_suppression import NoiseStreamChunkResult

        if cancel_event is not None and cancel_event.is_set():
            return NoiseStreamChunkResult(ok=False, reason="cancelled", pcm=b"")
        return NoiseStreamChunkResult(ok=True, reason="ok", pcm=pcm)

    def close(self, *, timeout_sec: Optional[float] = None, **_: Any) -> bool:
        return True


def _owner() -> MagicMock:
    owner = MagicMock()
    owner.score_pcm.return_value = SimpleNamespace(ok=True, reason="ok")
    return owner


def _settings(**kwargs: Any) -> InputAudioSettings:
    base = dict(
        enabled=True,
        microphone_device_index=0,
        noise_suppression="none",
    )
    base.update(kwargs)
    return InputAudioSettings(**base)


def _utterance_pattern(
    *,
    voiced_frames: int = 6,
    quiet_frames: int = 20,
) -> List[bytes]:
    return [_frame(1)] * voiced_frames + [_frame(0)] * quiet_frames


def setUpModule() -> None:
    global _SAVED_PYAUDIO, _SAVED_MIC_INIT, _SAVED_MIC_ENTER, _SAVED_ORT
    try:
        import pyaudio
    except ImportError:
        pyaudio = None  # type: ignore[assignment]
    if pyaudio is not None:
        _SAVED_PYAUDIO = pyaudio.PyAudio

        def _blocked(*_a: Any, **_k: Any) -> None:
            raise AssertionError("real PyAudio blocked")

        pyaudio.PyAudio = _blocked  # type: ignore[misc, assignment]

    try:
        import speech_recognition as sr
    except ImportError:
        sr = None  # type: ignore[assignment]
    if sr is not None:
        _SAVED_MIC_INIT = sr.Microphone.__init__
        _SAVED_MIC_ENTER = sr.Microphone.__enter__

        def _mic_block(self, *_a: Any, **_k: Any) -> None:
            raise AssertionError("Mic blocked")

        def _mic_enter(self, *_a: Any, **_k: Any) -> Any:
            raise AssertionError("Mic blocked")

        sr.Microphone.__init__ = _mic_block  # type: ignore[method-assign]
        sr.Microphone.__enter__ = _mic_enter  # type: ignore[method-assign]

    try:
        import onnxruntime as ort
    except ImportError:
        return
    _SAVED_ORT = ort.InferenceSession

    def _ort_blocked(*_a: Any, **_k: Any) -> None:
        raise AssertionError("ORT blocked")

    ort.InferenceSession = _ort_blocked  # type: ignore[misc, assignment]


def tearDownModule() -> None:
    global _SAVED_PYAUDIO, _SAVED_MIC_INIT, _SAVED_MIC_ENTER, _SAVED_ORT
    try:
        import pyaudio
    except ImportError:
        pyaudio = None  # type: ignore[assignment]
    if pyaudio is not None and _SAVED_PYAUDIO is not None:
        pyaudio.PyAudio = _SAVED_PYAUDIO  # type: ignore[misc, assignment]
    try:
        import speech_recognition as sr
    except ImportError:
        sr = None  # type: ignore[assignment]
    if sr is not None:
        if _SAVED_MIC_INIT is not None:
            sr.Microphone.__init__ = _SAVED_MIC_INIT  # type: ignore[method-assign]
        if _SAVED_MIC_ENTER is not None:
            sr.Microphone.__enter__ = _SAVED_MIC_ENTER  # type: ignore[method-assign]
    try:
        import onnxruntime as ort
    except ImportError:
        return
    if _SAVED_ORT is not None:
        ort.InferenceSession = _SAVED_ORT  # type: ignore[misc, assignment]


class CaptureControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeCaptureSource.opens = 0
        FakeCaptureSource.closes = 0

    def _release(self, ctl: ContinuousInputCapture, end_block: threading.Event) -> None:
        end_block.set()
        self.assertTrue(ctl.close())

    def _controller(
        self,
        frames: List[Any],
        *,
        clock: Optional[Callable[[], float]] = None,
        end_block: Optional[threading.Event] = None,
        **settings_kw: Any,
    ) -> Tuple[ContinuousInputCapture, threading.Event]:
        block = end_block or threading.Event()
        ctl = ContinuousInputCapture(
            settings=_settings(**settings_kw),
            silero_vad_owner=_owner(),
            source_factory=lambda: FakeCaptureSource(
                frames,
                end_block=block,
            ),
            clock=clock or time.monotonic,
            noise_stream_factory=lambda: PassthroughNs(),
            silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
        )
        return ctl, block

    def test_disabled_settings_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ContinuousInputCapture(
                settings=_settings(enabled=False),
                silero_vad_owner=_owner(),
            )

    def test_default_microphone_none_constructor_and_close(self) -> None:
        ctl = ContinuousInputCapture(
            settings=_settings(microphone_device_index=None),
            silero_vad_owner=_owner(),
            source_factory=lambda: FakeCaptureSource([]),
            silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
        )
        self.assertTrue(ctl.close())

    def test_single_flight_start(self) -> None:
        ctl, block = self._controller(_utterance_pattern() * 2)
        try:
            self.assertTrue(ctl.start()[0])
            self.assertFalse(ctl.start()[0])
        finally:
            self._release(ctl, block)

    def test_one_open_multiple_segments(self) -> None:
        pattern: List[Any] = []
        for _ in range(2):
            pattern.extend(_utterance_pattern())
        ctl, block = self._controller(pattern)
        try:
            self.assertTrue(ctl.start()[0])
            segments = 0
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and segments < 2:
                seg, reason = ctl.read_segment(0.05)
                if seg is not None:
                    self.assertEqual(reason, "ok")
                    segments += 1
            self.assertEqual(segments, 2)
            self.assertEqual(FakeCaptureSource.opens, 1)
        finally:
            self._release(ctl, block)

    def test_queue_drops_oldest(self) -> None:
        frames: List[Any] = []
        for _ in range(5):
            frames.extend(_utterance_pattern(voiced_frames=35))
        ctl, block = self._controller(frames)
        try:
            self.assertTrue(ctl.start()[0])
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if ctl.status().dropped_segments >= 1:
                    break
                ctl.read_segment(0.02)
            self.assertGreaterEqual(ctl.status().dropped_segments, 1)
        finally:
            self._release(ctl, block)

    def test_stale_segment_discarded(self) -> None:
        t = {"now": 1000.0}

        def clock() -> float:
            return t["now"]

        ctl, block = self._controller(_utterance_pattern(), clock=clock)
        try:
            self.assertTrue(ctl.start()[0])
            for _ in range(40):
                seg, _ = ctl.read_segment(0.02)
                if seg is not None:
                    t["now"] = seg.capture_end_mono + CAPTURE_STALE_SEC + 0.1
                    break
            seg, reason = ctl.read_segment(0.05)
            self.assertIsNone(seg)
            self.assertEqual(reason, "timeout")
        finally:
            self._release(ctl, block)

    def test_overflow_reset_limit(self) -> None:
        frames: List[Any] = ["OVERFLOW"] * (MAX_OVERFLOW_RESETS + 2)
        frames.extend(_utterance_pattern(voiced_frames=10, quiet_frames=5))
        ctl, block = self._controller(frames)
        try:
            self.assertTrue(ctl.start()[0])
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if ctl.status().overflow_resets >= MAX_OVERFLOW_RESETS:
                    break
                ctl.read_segment(0.02)
            status = ctl.status()
            self.assertGreaterEqual(status.overflow_resets, MAX_OVERFLOW_RESETS)
            seg, reason = ctl.read_segment(0.05)
            self.assertIsNone(seg)
            self.assertEqual(reason, "overflow_limit")
        finally:
            self._release(ctl, block)

    def test_close_while_blocked_read(self) -> None:
        block = threading.Event()
        release = threading.Event()
        frames = [_frame()] * 500
        source = FakeCaptureSource(frames, block=block, release=release)
        ctl = ContinuousInputCapture(
            settings=_settings(),
            silero_vad_owner=_owner(),
            source_factory=lambda: source,
            silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
            noise_stream_factory=lambda: PassthroughNs(),
        )
        errors: list[BaseException] = []

        def closer() -> None:
            try:
                block.wait(2.0)
                self.assertFalse(ctl.close(timeout_sec=0))
                release.set()
                self.assertTrue(ctl.close())
            except BaseException as exc:
                errors.append(exc)

        try:
            self.assertTrue(ctl.start()[0])
            thread = threading.Thread(target=closer)
            thread.start()
            thread.join(timeout=3.0)
            self.assertFalse(errors)
            self.assertFalse(thread.is_alive())
        finally:
            self.assertTrue(ctl.close())

    def test_idle_frames_no_segment(self) -> None:
        frames = [_frame(0)] * 200
        ctl, block = self._controller(frames)
        try:
            self.assertTrue(ctl.start()[0])
            seg, _ = ctl.read_segment(0.1)
            self.assertIsNone(seg)
        finally:
            self._release(ctl, block)

    def test_cleanup_fail_same_worker_retry(self) -> None:
        attempted = threading.Event()
        close_threads: List[int] = []

        class Source:
            attempts = 0

            def open(self) -> Optional[str]:
                return None

            def read_frame(self) -> Tuple[Optional[bytes], Optional[str]]:
                return None, "device_lost"

            def close(self) -> bool:
                self.attempts += 1
                close_threads.append(threading.get_ident())
                attempted.set()
                return self.attempts >= 2

        source = Source()
        ctl = ContinuousInputCapture(
            settings=_settings(),
            silero_vad_owner=_owner(),
            source_factory=lambda: source,
            silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
        )
        try:
            self.assertTrue(ctl.start()[0])
            self.assertTrue(attempted.wait(2.0))
            thread = ctl._thread
            assert thread is not None
            thread.join(0.05)
            self.assertTrue(thread.is_alive())
            self.assertFalse(ctl.status().complete)
            self.assertTrue(ctl.close())
            self.assertTrue(ctl.status().complete)
            self.assertEqual(len(set(close_threads)), 1)
        finally:
            ctl.close()

    def test_truthy_close_not_confirmed(self) -> None:
        for bogus in (1, None, {"closed": True}):
            with self.subTest(bogus=bogus):
                attempted = threading.Event()

                class Source:
                    attempts = 0

                    def open(self) -> Optional[str]:
                        return None

                    def read_frame(self) -> Tuple[Optional[bytes], Optional[str]]:
                        return None, "device_lost"

                    def close(self) -> Any:
                        self.attempts += 1
                        attempted.set()
                        if self.attempts == 1:
                            return bogus
                        return True

                source = Source()
                ctl = ContinuousInputCapture(
                    settings=_settings(),
                    silero_vad_owner=_owner(),
                    source_factory=lambda: source,
                    silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
                )
                try:
                    self.assertTrue(ctl.start()[0])
                    self.assertTrue(attempted.wait(2.0))
                    thread = ctl._thread
                    assert thread is not None
                    thread.join(0.05)
                    self.assertTrue(thread.is_alive())
                    self.assertTrue(ctl.close())
                    self.assertGreaterEqual(source.attempts, 2)
                finally:
                    ctl.close()

    def test_close_during_thread_start_pending(self) -> None:
        start_enter, start_release = threading.Event(), threading.Event()
        real_start = threading.Thread.start
        errors: list[BaseException] = []
        ctl = ContinuousInputCapture(
            settings=_settings(),
            silero_vad_owner=_owner(),
            source_factory=lambda: FakeCaptureSource(
                [_frame(0)] * 4,
                end_block=threading.Event(),
            ),
            silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
        )

        def guarded_start(thread: threading.Thread) -> None:
            if thread.name == "continuous-input-capture":
                start_enter.set()
                if not start_release.wait(2.0):
                    raise TimeoutError("start release")
            return real_start(thread)

        def starter() -> None:
            try:
                ctl.start()
            except BaseException as exc:
                errors.append(exc)

        outer = threading.Thread(target=starter)
        try:
            with patch.object(threading.Thread, "start", guarded_start):
                outer.start()
                self.assertTrue(start_enter.wait(2.0))
                self.assertFalse(ctl.close(timeout_sec=0))
                self.assertTrue(ctl.status().pending_cleanup)
                self.assertFalse(ctl.start()[0])
        finally:
            start_release.set()
            outer.join(2.0)
            self.assertTrue(ctl.close())
            self.assertTrue(ctl.status().complete)
        self.assertFalse(errors)

    def test_start_timeout_cancels_late_open(self) -> None:
        open_enter = threading.Event()
        open_release = threading.Event()
        source_closed = threading.Event()
        calls: List[str] = []

        class SlowOpenSource:
            def open(self) -> Optional[str]:
                open_enter.set()
                if not open_release.wait(2.0):
                    return "open_blocked"
                return None

            def read_frame(self) -> Tuple[Optional[bytes], Optional[str]]:
                calls.append("read")
                return None, "device_lost"

            def close(self) -> bool:
                source_closed.set()
                return True

        ctl = ContinuousInputCapture(
            settings=_settings(),
            silero_vad_owner=_owner(),
            source_factory=lambda: SlowOpenSource(),
            silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
        )
        try:
            with patch("src.voice.input_capture.CAPTURE_JOIN_TIMEOUT_SEC", 0.05):
                ok, reason = ctl.start()
            self.assertTrue(open_enter.is_set())
            self.assertFalse(ok)
            self.assertEqual(reason, "startup_timeout")
            open_release.set()
            self.assertTrue(source_closed.wait(2.0))
            self.assertEqual(calls, [])
            self.assertTrue(ctl.close())
        finally:
            open_release.set()
            ctl.close()

    def test_read_segment_uses_wall_clock_not_frozen_capture_clock(self) -> None:
        frozen = {"t": 0.0}

        def clock() -> float:
            return frozen["t"]

        ctl, block = self._controller([_frame(0)] * 40, clock=clock)
        try:
            self.assertTrue(ctl.start()[0])
            t0 = time.monotonic()
            _seg, reason = ctl.read_segment(0.08)
            self.assertGreater(time.monotonic() - t0, 0.05)
            self.assertEqual(reason, "timeout")
        finally:
            self._release(ctl, block)

    def test_close_timeout_validation(self) -> None:
        ctl, block = self._controller([])
        try:
            with self.assertRaises(ValueError):
                ctl.close(timeout_sec=-0.1)
            with self.assertRaises(ValueError):
                ctl.close(timeout_sec=float("nan"))
            with self.assertRaises(ValueError):
                ctl.close(timeout_sec=True)  # type: ignore[arg-type]
            self.assertTrue(ctl.close(timeout_sec=0))
        finally:
            block.set()
            ctl.close()

    def test_stop_after_read_no_late_enqueue(self) -> None:
        read_done = threading.Event()
        frames = [_frame(1)] * 30 + [_frame(0)] * 25

        class GateSource(FakeCaptureSource):
            def read_frame(self) -> Tuple[Optional[bytes], Optional[str]]:
                out = super().read_frame()
                if self._frames and not read_done.is_set():
                    read_done.set()
                return out

        end_block = threading.Event()
        ctl = ContinuousInputCapture(
            settings=_settings(),
            silero_vad_owner=_owner(),
            source_factory=lambda: GateSource(frames, end_block=end_block),
            silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
        )
        try:
            self.assertTrue(ctl.start()[0])
            self.assertTrue(read_done.wait(2.0))
            ctl.close(timeout_sec=0)
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                seg, _ = ctl.read_segment(0.02)
                self.assertIsNone(seg)
        finally:
            end_block.set()
            ctl.close()

    def test_admission_configured_threshold(self) -> None:
        cfg = SegmenterAdmissionConfig(start_threshold=0.45, end_threshold=0.35)
        self.assertLess(cfg.end_threshold, cfg.start_threshold)
        with self.assertRaises(ValueError):
            SegmenterAdmissionConfig(
                start_threshold=0.4,
                end_threshold=0.5,
            )
        ctl, block = self._controller(
            _utterance_pattern(voiced_frames=8),
            speech_probability_threshold=0.45,
        )
        try:
            self.assertTrue(ctl.start()[0])
            seg, reason = ctl.read_segment(0.5)
            if seg is not None:
                self.assertEqual(reason, "ok")
        finally:
            self._release(ctl, block)

    def test_pyaudio_teardown_requires_literal_true(self) -> None:
        src = _PyAudioCaptureSource(0)
        src._stream = object()
        src._pa = object()
        with patch.object(
            src,
            "_safe_teardown",
            return_value=False,
        ):
            self.assertIs(src.close(), False)

    def test_close_retry_after_stale_pending_hint(self) -> None:
        ctl = ContinuousInputCapture(
            settings=_settings(),
            silero_vad_owner=_owner(),
            source_factory=lambda: FakeCaptureSource([]),
            silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
        )

        class FinishingThread:
            checks = 0

            def is_alive(self) -> bool:
                self.checks += 1
                return self.checks <= 2

            def join(self, timeout: float = 0) -> None:
                ctl._worker_cleanup_ok = True

        ctl._thread = FinishingThread()  # type: ignore[assignment]
        ctl._pending_cleanup = True
        try:
            self.assertFalse(ctl.close(timeout_sec=0))
            self.assertTrue(ctl.close(timeout_sec=0))
            self.assertTrue(ctl.status().complete)
        finally:
            ctl._thread = None
            ctl.close()

    def test_thread_start_failure_close_succeeds(self) -> None:
        ctl = ContinuousInputCapture(
            settings=_settings(),
            silero_vad_owner=_owner(),
            source_factory=lambda: FakeCaptureSource([]),
            silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
        )
        real_start = threading.Thread.start

        def boom(thread: threading.Thread) -> None:
            if thread.name == "continuous-input-capture":
                raise RuntimeError("start blocked")
            return real_start(thread)

        with patch.object(threading.Thread, "start", boom):
            self.assertFalse(ctl.start()[0])
        self.assertTrue(ctl.close())

    def test_overflow_does_not_return_pregeneration_queue_item(self) -> None:
        from src.voice.input_capture import CapturedSegment

        old_gen_segment = CapturedSegment(
            pcm=_frame(1),
            start_sample=0,
            end_sample=FRAME_SAMPLES,
            voiced_ms=32,
            voiced_ratio=0.5,
            consecutive_ms=32,
            end_reason="trailing_silence",
            stream_generation=0,
            session_id=1,
            capture_start_mono=0.0,
            capture_end_mono=0.032,
        )
        end_block = threading.Event()
        ctl = ContinuousInputCapture(
            settings=_settings(),
            silero_vad_owner=_owner(),
            source_factory=lambda: FakeCaptureSource(
                ["OVERFLOW"] + _utterance_pattern(),
                end_block=end_block,
            ),
            silero_context_factory=lambda _o: (VoicedSileroCtx(), None),
        )
        try:
            self.assertTrue(ctl.start()[0])
            with ctl._lifecycle_lock:
                with ctl._queue_lock:
                    ctl._segments.append(old_gen_segment)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                ctl.read_segment(0.02)
                if ctl.status().overflow_resets >= 1:
                    break
            gen = ctl.status().generation
            self.assertGreaterEqual(gen, 1)
            for _ in range(20):
                seg, _reason = ctl.read_segment(0.05)
                if seg is not None:
                    self.assertEqual(seg.stream_generation, gen)
                    return
            with ctl._lifecycle_lock:
                with ctl._queue_lock:
                    for item in ctl._segments:
                        self.assertEqual(item.stream_generation, gen)
        finally:
            end_block.set()
            self.assertTrue(ctl.close())

    def test_overflow_context_factory_failure_leaves_worker_owned(self) -> None:
        create_count = [0]

        def ctx_factory(_o: Any) -> Tuple[Any, Optional[str]]:
            create_count[0] += 1
            if create_count[0] > 1:
                raise RuntimeError("overflow_ctx_factory_failed")
            return VoicedSileroCtx(), None

        end_block = threading.Event()
        ctl = ContinuousInputCapture(
            settings=_settings(),
            silero_vad_owner=_owner(),
            source_factory=lambda: FakeCaptureSource(
                ["OVERFLOW", _frame(0)] * 4,
                end_block=end_block,
            ),
            silero_context_factory=ctx_factory,
        )
        try:
            self.assertTrue(ctl.start()[0])
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if ctl.status().device_error == "overflow_reset_failed":
                    break
                ctl.read_segment(0.02)
            self.assertEqual(ctl.status().device_error, "overflow_reset_failed")
            thread = ctl._thread
            if thread is not None:
                thread.join(timeout=2.0)
            self.assertTrue(ctl.close())
        finally:
            end_block.set()
            ctl.close()


if __name__ == "__main__":
    unittest.main()
