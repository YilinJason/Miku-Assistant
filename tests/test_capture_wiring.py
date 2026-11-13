# -*- coding: utf-8 -*-
"""STT/Assistant/VoiceSession 连续采集接线（全 fake；无麦克风/模型）。"""
from __future__ import annotations

import logging
import struct
import threading
import unittest
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock, Mock, patch

from src.cli.voice_session import VoiceSession
from src.core.assistant import MikuAssistant
from src.core.safety import SafetyPolicy
from src.voice.input_audio import (
    CAPTURE_MODE_CONTINUOUS,
    CAPTURE_MODE_UTTERANCE,
    InputAudioSettings,
)
from src.voice.input_capture import FRAME_SAMPLES
from src.voice.input_segments import FRAME_BYTES
from src.voice.recognition import RecognitionResult, RecognitionStatus
from src.voice.stt import STTEngine

_SAVED_PYAUDIO = None
_SAVED_ORT = None


def _frame(tag: int = 1) -> bytes:
    return struct.pack("<h", tag) * FRAME_SAMPLES


def _sealed_policy() -> SafetyPolicy:
    policy = SafetyPolicy(
        {
            "permissions": {
                "local_asr_worker": True,
                "cloud_stt": False,
            }
        }
    )
    policy._local_asr_profile_sealed = True
    return policy


def _continuous_settings(**kwargs: Any) -> InputAudioSettings:
    base = dict(
        enabled=True,
        capture_mode=CAPTURE_MODE_CONTINUOUS,
        noise_suppression="none",
    )
    base.update(kwargs)
    return InputAudioSettings(**base)


class FakeCaptureController:
    instances: List["FakeCaptureController"] = []

    def __init__(self, **_: Any) -> None:
        type(self).instances.append(self)
        self._running = False
        self._complete = False
        self._pending = False
        self._generation = 1
        self._session_id = 1
        self._stop = threading.Event()
        self._segments: List[Any] = []

    @property
    def stop_cancel_view(self) -> SimpleNamespace:
        return SimpleNamespace(is_set=self._stop.is_set)

    def start(self) -> Tuple[bool, str]:
        self._running = True
        return True, ""

    def close(self) -> bool:
        self._stop.set()
        self._running = False
        self._complete = True
        return True

    def status(self) -> Any:
        return SimpleNamespace(
            running=self._running,
            complete=self._complete,
            pending_cleanup=self._pending,
            generation=self._generation,
            session_id=self._session_id,
        )

    def queue_segment(self, pcm: bytes, *, start: float, end: float) -> None:
        self._segments.append(
            SimpleNamespace(
                pcm=pcm,
                capture_start_mono=start,
                capture_end_mono=end,
                stream_generation=self._generation,
                session_id=self._session_id,
            )
        )

    def read_segment(
        self,
        timeout_sec: float,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> Tuple[Any, str]:
        if cancel_event is not None and cancel_event.is_set():
            return None, "cancelled"
        if self._segments:
            return self._segments.pop(0), "ok"
        return None, "timeout"


def setUpModule() -> None:
    global _SAVED_PYAUDIO, _SAVED_ORT
    try:
        import pyaudio
    except ImportError:
        pyaudio = None  # type: ignore[assignment]
    if pyaudio is not None:
        _SAVED_PYAUDIO = pyaudio.PyAudio
        pyaudio.PyAudio = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[misc, assignment]
            AssertionError("real PyAudio blocked")
        )
    try:
        import onnxruntime as ort
    except ImportError:
        return
    _SAVED_ORT = ort.InferenceSession
    ort.InferenceSession = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[misc, assignment]
        AssertionError("ORT blocked")
    )


def tearDownModule() -> None:
    global _SAVED_PYAUDIO, _SAVED_ORT
    try:
        import pyaudio
    except ImportError:
        pyaudio = None  # type: ignore[assignment]
    if pyaudio is not None and _SAVED_PYAUDIO is not None:
        pyaudio.PyAudio = _SAVED_PYAUDIO  # type: ignore[misc, assignment]
    try:
        import onnxruntime as ort
    except ImportError:
        return
    if _SAVED_ORT is not None:
        ort.InferenceSession = _SAVED_ORT  # type: ignore[misc, assignment]


class RecognitionTimingTests(unittest.TestCase):
    def test_success_accepts_capture_pair(self) -> None:
        result = RecognitionResult.success(
            "你好",
            capture_start_mono=1.0,
            capture_end_mono=1.5,
        )
        self.assertEqual(result.capture_start_mono, 1.0)

    def test_coerce_rejects_invalid_pair_on_success(self) -> None:
        broken = RecognitionResult.__new__(RecognitionResult)
        object.__setattr__(broken, "status", RecognitionStatus.SUCCESS)
        object.__setattr__(broken, "text", "x")
        object.__setattr__(broken, "error_detail", None)
        object.__setattr__(broken, "capture_start_mono", 2.0)
        object.__setattr__(broken, "capture_end_mono", 1.0)
        out = RecognitionResult.coerce(broken)
        self.assertEqual(out.status, RecognitionStatus.INTERNAL_ERROR)

    def test_failure_rejects_capture_timestamps(self) -> None:
        with self.assertRaises(ValueError):
            RecognitionResult(
                status=RecognitionStatus.NOT_READY,
                error_detail="x",
                capture_start_mono=0.0,
                capture_end_mono=0.1,
            )


class ConfigCaptureModeTests(unittest.TestCase):
    def test_default_continuous(self) -> None:
        settings = InputAudioSettings.from_mapping({})
        self.assertEqual(settings.capture_mode, CAPTURE_MODE_CONTINUOUS)

    def test_explicit_utterance(self) -> None:
        settings = InputAudioSettings.from_mapping(
            {"capture_mode": CAPTURE_MODE_UTTERANCE}
        )
        self.assertEqual(settings.capture_mode, CAPTURE_MODE_UTTERANCE)

    def test_reject_invalid_capture_mode(self) -> None:
        with self.assertRaises(ValueError):
            InputAudioSettings.from_mapping({"capture_mode": "invalid"})


class STTContinuousWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeCaptureController.instances.clear()

    def _engine(self, **audio_kw: Any) -> STTEngine:
        return STTEngine(
            engine="paraformer",
            safety_policy=_sealed_policy(),
            input_audio_settings=_continuous_settings(**audio_kw),
        )

    def test_utterance_mode_start_is_noop_success(self) -> None:
        engine = self._engine(capture_mode=CAPTURE_MODE_UTTERANCE)
        ok, _ = engine.start_continuous_input()
        self.assertTrue(ok)
        self.assertIsNone(engine._continuous_capture)

    def test_continuous_recognize_without_owned_capture_uses_legacy(self) -> None:
        engine = self._engine()
        with patch.object(
            engine,
            "_recognize_with_microphone",
            return_value=RecognitionResult.failure(
                RecognitionStatus.SILENCE_TIMEOUT
            ),
        ) as legacy:
            result = engine.recognize(continuous=True)
        legacy.assert_called_once()
        self.assertEqual(result.status, RecognitionStatus.SILENCE_TIMEOUT)

    @patch("src.voice.input_capture.ContinuousInputCapture", FakeCaptureController)
    def test_start_publishes_controller_before_thread(self) -> None:
        engine = self._engine()
        with patch.object(engine, "_ensure_silero_vad", return_value=(object(), None)):
            with patch.object(engine, "_paraformer_runtime_ready", return_value=True):
                ok, _ = engine.start_continuous_input()
        self.assertTrue(ok)
        self.assertIs(engine._continuous_capture, FakeCaptureController.instances[0])

    @patch("src.voice.input_capture.ContinuousInputCapture", FakeCaptureController)
    def test_continuous_consumes_segment_pcm_once(self) -> None:
        engine = self._engine()
        pcm = _frame(1)
        with patch.object(engine, "_ensure_silero_vad", return_value=(object(), None)):
            with patch.object(engine, "_paraformer_runtime_ready", return_value=True):
                self.assertTrue(engine.start_continuous_input()[0])
        capture = engine._continuous_capture
        assert isinstance(capture, FakeCaptureController)
        capture.queue_segment(pcm, start=10.0, end=10.5)
        with patch.object(
            engine,
            "_transcribe_paraformer_pcm",
            return_value=RecognitionResult.success("识别"),
        ) as transcribe:
            result = engine.recognize(continuous=True, timeout=0.1)
        transcribe.assert_called_once()
        self.assertIs(transcribe.call_args.kwargs.get("apply_acoustic_guard"), False)
        self.assertEqual(transcribe.call_args.args[0], pcm)
        self.assertEqual(result.status, RecognitionStatus.SUCCESS)
        self.assertEqual(result.capture_start_mono, 10.0)
        self.assertEqual(result.capture_end_mono, 10.5)

    @patch("src.voice.input_capture.ContinuousInputCapture", FakeCaptureController)
    def test_shutdown_closes_capture_before_vad(self) -> None:
        engine = self._engine()
        calls: List[str] = []

        def close_capture() -> bool:
            calls.append("capture")
            return True

        def close_vad() -> bool:
            calls.append("vad")
            return True

        with patch.object(engine, "_ensure_silero_vad", return_value=(object(), None)):
            with patch.object(engine, "_paraformer_runtime_ready", return_value=True):
                engine.start_continuous_input()
        with patch.object(engine, "_shutdown_continuous_capture", side_effect=close_capture):
            with patch.object(engine, "_shutdown_owned_silero_vad", side_effect=close_vad):
                with patch.object(engine, "_shutdown_owned_temp_files", return_value=True):
                    engine.shutdown()
        self.assertEqual(calls, ["capture", "vad"])

    @patch("src.voice.input_capture.ContinuousInputCapture", FakeCaptureController)
    def test_stop_failure_blocks_restart(self) -> None:
        engine = self._engine()
        with patch.object(engine, "_ensure_silero_vad", return_value=(object(), None)):
            with patch.object(engine, "_paraformer_runtime_ready", return_value=True):
                engine.start_continuous_input()
        capture = engine._continuous_capture
        assert capture is not None
        with patch.object(capture, "close", return_value=False):
            self.assertFalse(engine.stop_continuous_input())
        ok, reason = engine.start_continuous_input()
        self.assertFalse(ok)
        self.assertEqual(reason, "capture_stop_pending")

    @patch("src.voice.input_capture.ContinuousInputCapture", FakeCaptureController)
    def test_shutdown_during_preparation_blocks_late_start(self) -> None:
        engine = self._engine()
        entered = threading.Event()
        release = threading.Event()
        starts: List[int] = []
        errors: List[BaseException] = []

        class Capture(FakeCaptureController):
            def start(self) -> Tuple[bool, str]:
                starts.append(1)
                return super().start()

        def ensure() -> Tuple[object, None]:
            entered.set()
            if not release.wait(2):
                raise TimeoutError("preparation release")
            return object(), None

        def starter() -> None:
            try:
                engine.start_continuous_input()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=starter)
        with (
            patch.object(engine, "_paraformer_runtime_ready", return_value=True),
            patch.object(engine, "_ensure_silero_vad", side_effect=ensure),
            patch("src.voice.input_capture.ContinuousInputCapture", Capture),
        ):
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(engine.shutdown())
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(starts, [])
        self.assertIsNone(engine._continuous_capture)
        engine.stop_continuous_input()

    @patch("src.voice.input_capture.ContinuousInputCapture", FakeCaptureController)
    def test_concurrent_prepare_second_starter_idempotent(self) -> None:
        engine = self._engine()
        in_prepare_a = threading.Event()
        in_prepare_b = threading.Event()
        prepare_barrier = threading.Barrier(2, timeout=2)
        allow_a = threading.Event()
        allow_b = threading.Event()
        start_counts: List[int] = []
        close_counts: List[int] = []
        errors: List[BaseException] = []

        class RacingCapture(FakeCaptureController):
            def start(self) -> Tuple[bool, str]:
                start_counts.append(1)
                return super().start()

            def close(self) -> bool:
                close_counts.append(1)
                return super().close()

        def ensure() -> Tuple[object, None]:
            if threading.current_thread().name == "capture-starter-a":
                in_prepare_a.set()
            else:
                in_prepare_b.set()
            try:
                prepare_barrier.wait()
            except threading.BrokenBarrierError as exc:
                raise TimeoutError("prepare barrier") from exc
            if threading.current_thread().name == "capture-starter-a":
                if not allow_a.wait(2):
                    raise TimeoutError("allow_a")
            elif not allow_b.wait(2):
                raise TimeoutError("allow_b")
            return object(), None

        def run_a() -> None:
            try:
                engine.start_continuous_input()
            except BaseException as exc:
                errors.append(exc)

        def run_b() -> None:
            try:
                engine.start_continuous_input()
            except BaseException as exc:
                errors.append(exc)

        ta = threading.Thread(target=run_a, name="capture-starter-a", daemon=True)
        tb = threading.Thread(target=run_b, name="capture-starter-b", daemon=True)
        RacingCapture.instances.clear()
        try:
            with (
                patch.object(engine, "_paraformer_runtime_ready", return_value=True),
                patch.object(engine, "_ensure_silero_vad", side_effect=ensure),
                patch("src.voice.input_capture.ContinuousInputCapture", RacingCapture),
            ):
                ta.start()
                tb.start()
                self.assertTrue(in_prepare_a.wait(2))
                self.assertTrue(in_prepare_b.wait(2))
                allow_a.set()
                ta.join(2)
                self.assertFalse(ta.is_alive())
                self.assertEqual(start_counts, [1])
                allow_b.set()
                tb.join(2)
                self.assertFalse(tb.is_alive())
        finally:
            allow_a.set()
            allow_b.set()
            ta.join(2)
            tb.join(2)
        self.assertFalse(errors, errors)
        self.assertEqual(start_counts, [1])
        self.assertEqual(close_counts, [])
        capture = engine._continuous_capture
        self.assertIsNotNone(capture)
        assert isinstance(capture, RacingCapture)
        self.assertTrue(capture.status().running)
        self.assertEqual(len(RacingCapture.instances), 1)
        engine.stop_continuous_input()

    @patch("src.voice.input_capture.ContinuousInputCapture", FakeCaptureController)
    def test_late_asr_drops_stale_generation(self) -> None:
        engine = self._engine()
        pcm = _frame(2)
        with patch.object(engine, "_ensure_silero_vad", return_value=(object(), None)):
            with patch.object(engine, "_paraformer_runtime_ready", return_value=True):
                self.assertTrue(engine.start_continuous_input()[0])
        capture = engine._continuous_capture
        assert isinstance(capture, FakeCaptureController)
        capture.queue_segment(pcm, start=1.0, end=1.2)

        def stale_success(*_a: Any, **_k: Any) -> RecognitionResult:
            capture._generation = 2
            return RecognitionResult.success("迟到")

        with patch.object(
            engine, "_transcribe_paraformer_pcm", side_effect=stale_success
        ):
            result = engine.recognize(continuous=True, timeout=0.1)
        self.assertEqual(result.status, RecognitionStatus.CANCELLED)


class VoiceSessionCliWiringTests(unittest.TestCase):
    @staticmethod
    def _session(
        begin: Any,
        end: Any,
    ) -> VoiceSession:
        assistant = SimpleNamespace(
            config={},
            logger=logging.getLogger("test.capture_wiring"),
            begin_voice_input_session=begin,
            end_voice_input_session=end,
            check_mode_switch=lambda _text: None,
        )
        return VoiceSession(
            assistant,
            duplex_listening=True,
            stream_reply=False,
            stop_keywords=("退出",),
        )

    def test_end_failure_promotes_unsafe_pause(self) -> None:
        end = Mock(return_value=False)
        session = self._session(lambda: (True, ""), end)
        with patch.object(
            session,
            "_recognize_once",
            return_value=RecognitionResult.success("退出"),
        ):
            self.assertEqual(session.run(), "unsafe_pause")
        end.assert_called_once()

    def test_begin_failure_still_calls_end(self) -> None:
        end = Mock(return_value=True)
        session = self._session(lambda: (False, "partial"), end)
        self.assertEqual(session.run(), "stt_error")
        end.assert_called_once()

    def test_keyboard_interrupt_still_ends_capture(self) -> None:
        end = Mock(return_value=True)
        session = self._session(lambda: (True, ""), end)

        def raise_keyboard(*_a: Any, **_k: Any) -> RecognitionResult:
            raise KeyboardInterrupt

        with patch.object(session, "_recognize_once", side_effect=raise_keyboard):
            self.assertEqual(session.run(), "exit")
        end.assert_called_once()


class CaptureCancelViewTests(unittest.TestCase):
    def test_stop_cancel_view_exposes_only_is_set(self) -> None:
        from src.voice.input_capture import ContinuousInputCapture

        capture = ContinuousInputCapture(
            settings=_continuous_settings(),
            silero_vad_owner=object(),
        )
        try:
            view = capture.stop_cancel_view
            self.assertTrue(callable(view.is_set))
            self.assertFalse(hasattr(view, "set"))
            self.assertFalse(hasattr(view, "clear"))
        finally:
            self.assertTrue(capture.close())


class AssistantWiringTests(unittest.TestCase):
    def test_missing_stt_methods_compatible(self) -> None:
        assistant = MikuAssistant.from_dependencies(
            config={"assistant": {}},
            safety_policy=_sealed_policy(),
            stt_engine=SimpleNamespace(),
        )
        self.assertTrue(assistant.begin_voice_input_session()[0])
        self.assertTrue(assistant.end_voice_input_session())

    @patch("src.voice.input_capture.ContinuousInputCapture", FakeCaptureController)
    def test_assistant_delegates_begin_end(self) -> None:
        engine = STTEngine(
            engine="paraformer",
            safety_policy=_sealed_policy(),
            input_audio_settings=_continuous_settings(),
        )
        assistant = MikuAssistant.from_dependencies(
            config={"assistant": {}},
            safety_policy=_sealed_policy(),
            stt_engine=engine,
        )
        with patch.object(engine, "_ensure_silero_vad", return_value=(object(), None)):
            with patch.object(engine, "_paraformer_runtime_ready", return_value=True):
                self.assertTrue(assistant.begin_voice_input_session()[0])
        self.assertIsNotNone(engine._continuous_capture)
        self.assertTrue(assistant.end_voice_input_session())


if __name__ == "__main__":
    unittest.main()
