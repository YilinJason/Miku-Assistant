# -*- coding: utf-8 -*-
"""Silero 概率 VAD 与 STTEngine 集成（全 fake；无真实 Mic/ORT/模型）。"""
from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional, Tuple
from unittest.mock import MagicMock, patch

from src.core.safety import SafetyPolicy
from src.voice.input_audio import (
    INPUT_AUDIO_BACKEND_SILERO,
    INPUT_AUDIO_BACKEND_WEBRTC,
    InputAudioSettings,
    analyze_utterance_pcm,
    analyze_utterance_pcm_silero,
    resolve_acoustic_input_readiness,
    resolve_input_audio_settings_from_stt_config,
)
from src.voice.recognition import RecognitionStatus
from src.voice.silero_vad import FRAME_SAMPLES
from src.voice.stt import STTEngine

_SAVED_ORT_SESSION = None
_SAVED_MIC_INIT = None
_SAVED_MIC_ENTER = None


def setUpModule() -> None:
    global _SAVED_ORT_SESSION, _SAVED_MIC_INIT, _SAVED_MIC_ENTER
    try:
        import onnxruntime as ort
    except ImportError:
        ort = None  # type: ignore[assignment]
    if ort is not None:
        _SAVED_ORT_SESSION = ort.InferenceSession

        def _blocked_ort(*args, **kwargs):
            raise AssertionError(
                "offline test must not construct real onnxruntime.InferenceSession"
            )

        ort.InferenceSession = _blocked_ort  # type: ignore[misc, assignment]

    try:
        import speech_recognition as sr
    except ImportError:
        return
    _SAVED_MIC_INIT = sr.Microphone.__init__
    _SAVED_MIC_ENTER = sr.Microphone.__enter__

    def _blocked_mic_init(self, *args, **kwargs) -> None:
        raise AssertionError(
            "offline test must not construct speech_recognition.Microphone"
        )

    def _blocked_mic_enter(self, *args, **kwargs):
        raise AssertionError(
            "offline test must not enter speech_recognition.Microphone context"
        )

    sr.Microphone.__init__ = _blocked_mic_init  # type: ignore[method-assign]
    sr.Microphone.__enter__ = _blocked_mic_enter  # type: ignore[method-assign]


def tearDownModule() -> None:
    global _SAVED_ORT_SESSION, _SAVED_MIC_INIT, _SAVED_MIC_ENTER
    try:
        import onnxruntime as ort
    except ImportError:
        ort = None  # type: ignore[assignment]
    if ort is not None and _SAVED_ORT_SESSION is not None:
        ort.InferenceSession = _SAVED_ORT_SESSION  # type: ignore[misc, assignment]
    _SAVED_ORT_SESSION = None

    try:
        import speech_recognition as sr
    except ImportError:
        return
    if _SAVED_MIC_INIT is not None:
        sr.Microphone.__init__ = _SAVED_MIC_INIT  # type: ignore[method-assign]
    if _SAVED_MIC_ENTER is not None:
        sr.Microphone.__enter__ = _SAVED_MIC_ENTER  # type: ignore[method-assign]
    _SAVED_MIC_INIT = None
    _SAVED_MIC_ENTER = None


def _pcm_from_samples(count: int, *, byte: int = 5) -> bytes:
    return bytes([byte, 0]) * count


@dataclass
class _FakeVadScores:
    ok: bool
    reason: str
    probabilities: Tuple[float, ...] = ()
    valid_samples: Tuple[int, ...] = ()
    elapsed_ms: float = 0.0


class _FakeSileroVad:
    instances: list["_FakeSileroVad"] = []
    factory_calls = 0
    construct_enter: threading.Event | None = None
    construct_release: threading.Event | None = None
    close_enter: threading.Event | None = None
    close_release: threading.Event | None = None

    def __init__(self, **kwargs: Any) -> None:
        _FakeSileroVad.factory_calls += 1
        if _FakeSileroVad.construct_enter is not None:
            _FakeSileroVad.construct_enter.set()
        if _FakeSileroVad.construct_release is not None:
            if not _FakeSileroVad.construct_release.wait(timeout=2.0):
                raise TimeoutError("factory blocked too long")
        self.closed = False
        self.kwargs = kwargs
        _FakeSileroVad.instances.append(self)

    def score_pcm(self, pcm: bytes, *, cancel_event=None) -> _FakeVadScores:
        if cancel_event is not None and cancel_event.is_set():
            return _FakeVadScores(ok=False, reason="cancelled")
        frames = max(1, len(pcm) // (2 * FRAME_SAMPLES))
        return _FakeVadScores(
            ok=True,
            reason="ok",
            probabilities=(0.9,) * frames,
            valid_samples=(FRAME_SAMPLES,) * frames,
        )

    def close(self, *, timeout_sec=None) -> bool:
        if _FakeSileroVad.close_enter is not None:
            _FakeSileroVad.close_enter.set()
        if _FakeSileroVad.close_release is not None:
            if not _FakeSileroVad.close_release.wait(timeout=2.0):
                raise TimeoutError("close blocked too long")
        self.closed = True
        return True


class SileroSettingsResolverTests(unittest.TestCase):
    def test_missing_inner_mapping_defaults_silero(self) -> None:
        settings = resolve_input_audio_settings_from_stt_config({})
        self.assertEqual(settings.backend, INPUT_AUDIO_BACKEND_SILERO)

    def test_explicit_webrtc_backend(self) -> None:
        settings = resolve_input_audio_settings_from_stt_config(
            {"input_audio": {"backend": "webrtc"}}
        )
        self.assertEqual(settings.backend, INPUT_AUDIO_BACKEND_WEBRTC)


class SileroAnalyzeTests(unittest.TestCase):
    def test_disabled_silero_without_scorer_returns_disabled_not_unavailable(self) -> None:
        pcm = _pcm_from_samples(FRAME_SAMPLES)
        settings = InputAudioSettings(
            backend=INPUT_AUDIO_BACKEND_SILERO,
            enabled=False,
        )
        verdict = analyze_utterance_pcm(pcm, settings, silero_vad=None)
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.reason, "disabled")

    def test_cancel_before_backend_without_scorer(self) -> None:
        cancel = threading.Event()
        cancel.set()
        pcm = _pcm_from_samples(FRAME_SAMPLES)
        settings = InputAudioSettings(backend=INPUT_AUDIO_BACKEND_SILERO)
        verdict = analyze_utterance_pcm(
            pcm,
            settings,
            silero_vad=None,
            cancel_event=cancel,
        )
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, "cancelled")

    def test_tail_valid_samples_only_for_voiced_ms(self) -> None:
        vad = SimpleNamespace(
            score_pcm=lambda pcm, cancel_event=None: _FakeVadScores(
                ok=True,
                reason="ok",
                probabilities=(0.9,) * 7,
                valid_samples=(FRAME_SAMPLES,) * 6 + (100,),
            )
        )
        pcm = _pcm_from_samples(FRAME_SAMPLES * 6 + 100)
        settings = InputAudioSettings(
            backend=INPUT_AUDIO_BACKEND_SILERO,
            min_voiced_ms=180,
            min_consecutive_ms=90,
            min_voiced_ratio=0.12,
        )
        verdict = analyze_utterance_pcm_silero(pcm, settings, vad)
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.voiced_ms, 198)

    def test_low_probability_rejected_without_client(self) -> None:
        vad = SimpleNamespace(
            score_pcm=lambda pcm, cancel_event=None: _FakeVadScores(
                ok=True,
                reason="ok",
                probabilities=(0.1,) * 4,
                valid_samples=(FRAME_SAMPLES,) * 4,
            )
        )
        pcm = _pcm_from_samples(FRAME_SAMPLES * 4)
        settings = InputAudioSettings(backend=INPUT_AUDIO_BACKEND_SILERO)
        verdict = analyze_utterance_pcm(pcm, settings, silero_vad=vad)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, "insufficient_voiced_ms")


class STTEngineSileroIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeSileroVad.instances.clear()
        _FakeSileroVad.factory_calls = 0
        _FakeSileroVad.construct_enter = None
        _FakeSileroVad.construct_release = None
        _FakeSileroVad.close_enter = None
        _FakeSileroVad.close_release = None

    def _engine(self, **kwargs: Any) -> STTEngine:
        settings = kwargs.pop(
            "input_audio_settings",
            InputAudioSettings(
                backend=INPUT_AUDIO_BACKEND_SILERO,
                noise_suppression="none",
            ),
        )
        return STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=settings,
            **kwargs,
        )

    def test_guard_rejects_before_paraformer_client(self) -> None:
        engine = self._engine()
        pcm = _pcm_from_samples(FRAME_SAMPLES * 2)
        with patch(
            "src.voice.silero_vad.SileroOnnxVAD",
            _FakeSileroVad,
        ), patch(
            "src.voice.input_audio.analyze_utterance_pcm_silero",
            return_value=SimpleNamespace(
                accepted=False,
                reason="insufficient_voiced_ms",
                voiced_ms=0,
                voiced_ratio=0.0,
                frames_analyzed=2,
            ),
        ):
            result = engine._transcribe_paraformer_pcm(pcm, apply_acoustic_guard=True)
        self.assertEqual(result.status, RecognitionStatus.UNRECOGNIZED)
        self.assertIsNone(engine._paraformer_client)

    def test_shutdown_closes_vad_without_paraformer_client(self) -> None:
        engine = self._engine()
        with patch("src.voice.silero_vad.SileroOnnxVAD", _FakeSileroVad):
            engine._ensure_silero_vad()
        self.assertEqual(len(_FakeSileroVad.instances), 1)
        self.assertTrue(engine.shutdown())
        self.assertIsNone(engine._silero_vad)
        self.assertTrue(engine.is_shutdown_complete())

    def test_ensure_silero_warms_session_on_publish(self) -> None:
        engine = self._engine()
        with patch("src.voice.silero_vad.SileroOnnxVAD", _FakeSileroVad):
            vad, err = engine._ensure_silero_vad()
        self.assertIsNone(err)
        self.assertIsNotNone(vad)
        self.assertIs(engine._silero_vad, vad)
        self.assertTrue(getattr(vad, "closed", False) is False)

    def test_ensure_silero_warm_failure_unpublishes(self) -> None:
        class _ColdVad(_FakeSileroVad):
            def score_pcm(self, pcm: bytes, *, cancel_event=None) -> _FakeVadScores:
                return _FakeVadScores(ok=False, reason="model_load_failed")

        engine = self._engine()
        with patch("src.voice.silero_vad.SileroOnnxVAD", _ColdVad):
            vad, err = engine._ensure_silero_vad()
        self.assertIsNone(vad)
        self.assertEqual(err, "model_load_failed")
        self.assertIsNone(engine._silero_vad)
        self.assertEqual(len(_ColdVad.instances), 1)
        self.assertTrue(_ColdVad.instances[0].closed)

    def test_readiness_uses_light_probe_not_session(self) -> None:
        settings = InputAudioSettings(
            backend=INPUT_AUDIO_BACKEND_SILERO,
            noise_suppression="none",
        )
        with patch(
            "src.voice.silero_vad.probe_silero_install_readiness",
            return_value=(False, "missing"),
        ) as probe:
            ready, hint = resolve_acoustic_input_readiness(settings)
        probe.assert_called_once()
        self.assertFalse(ready)
        self.assertEqual(hint, "missing")

    def test_webrtc_backend_still_supported(self) -> None:
        settings = InputAudioSettings(
            backend=INPUT_AUDIO_BACKEND_WEBRTC,
            noise_suppression="none",
        )
        with patch(
            "src.voice.input_audio.resolve_vad_backend",
            return_value=(True, "", object()),
        ):
            ready, hint = resolve_acoustic_input_readiness(settings)
        self.assertTrue(ready)
        self.assertEqual(hint, "")

    def test_close_timeout_on_vad_keeps_shutdown_pending(self) -> None:
        engine = self._engine()

        class _StickyVad(_FakeSileroVad):
            def close(self, *, timeout_sec=None) -> bool:
                return False

        with patch("src.voice.silero_vad.SileroOnnxVAD", _StickyVad):
            engine._ensure_silero_vad()
        self.assertFalse(engine.shutdown())
        self.assertIsNotNone(engine._silero_vad)
        self.assertFalse(engine.get_shutdown_status()["complete"])
        self.assertFalse(engine.is_shutdown_complete())

    def test_close_truthy_nonbool_keeps_owner_until_true(self) -> None:
        engine = self._engine()

        class _TruthyCloseVad(_FakeSileroVad):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._closes = 0

            def close(self, *, timeout_sec=None):
                self._closes += 1
                if self._closes == 1:
                    return 1
                self.closed = True
                return True

        with patch("src.voice.silero_vad.SileroOnnxVAD", _TruthyCloseVad):
            engine._ensure_silero_vad()
        self.assertFalse(engine.shutdown())
        self.assertIsNotNone(engine._silero_vad)
        self.assertTrue(engine.shutdown())
        self.assertIsNone(engine._silero_vad)
        self.assertTrue(engine.is_shutdown_complete())

    def test_close_exception_retains_owner_and_retry(self) -> None:
        engine = self._engine()
        attempts = {"count": 0}

        class _FlakyCloseVad(_FakeSileroVad):
            def close(self, *, timeout_sec=None) -> bool:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("close failed")
                self.closed = True
                return True

        with patch("src.voice.silero_vad.SileroOnnxVAD", _FlakyCloseVad):
            engine._ensure_silero_vad()
        self.assertFalse(engine.shutdown())
        self.assertIsNotNone(engine._silero_vad)
        self.assertTrue(engine.shutdown())
        self.assertIsNone(engine._silero_vad)

    def test_shutdown_during_vad_construction_never_publishes(self) -> None:
        engine = self._engine()
        construct_enter = threading.Event()
        construct_release = threading.Event()
        _FakeSileroVad.construct_enter = construct_enter
        _FakeSileroVad.construct_release = construct_release
        errors: list[BaseException] = []
        ready: list[tuple[Any, Optional[str]]] = []

        def worker() -> None:
            try:
                ready.append(engine._ensure_silero_vad())
            except BaseException as exc:
                errors.append(exc)

        with patch("src.voice.silero_vad.SileroOnnxVAD", _FakeSileroVad):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(construct_enter.wait(timeout=2.0))
            self.assertFalse(engine.shutdown())
            self.assertFalse(engine.get_shutdown_status()["complete"])
            construct_release.set()
            thread.join(timeout=2.0)
        self.assertFalse(errors)
        self.assertEqual(len(ready), 1)
        self.assertIsNone(ready[0][0])
        self.assertEqual(ready[0][1], "closed")
        self.assertEqual(_FakeSileroVad.factory_calls, 1)
        self.assertIsNone(engine._silero_vad)
        self.assertFalse(engine._permanently_closed)
        self.assertTrue(engine.shutdown())
        self.assertTrue(engine.is_shutdown_complete())

    def test_shutdown_while_retire_close_in_flight(self) -> None:
        engine = self._engine()
        factory_enter = threading.Event()
        factory_release = threading.Event()
        close_enter = threading.Event()
        close_release = threading.Event()
        _FakeSileroVad.construct_enter = factory_enter
        _FakeSileroVad.construct_release = factory_release
        _FakeSileroVad.close_enter = close_enter
        _FakeSileroVad.close_release = close_release
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                engine._ensure_silero_vad()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        try:
            with patch("src.voice.silero_vad.SileroOnnxVAD", _FakeSileroVad):
                thread.start()
                self.assertTrue(factory_enter.wait(timeout=2.0))
                self.assertFalse(engine.shutdown())
                factory_release.set()
                self.assertTrue(close_enter.wait(timeout=2.0))
                self.assertFalse(engine.shutdown())
                self.assertFalse(engine.get_shutdown_status()["complete"])
                close_release.set()
                thread.join(timeout=2.0)
        finally:
            factory_release.set()
            close_release.set()
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(engine._silero_vad)
        self.assertTrue(engine.shutdown())

    def test_concurrent_ensure_single_factory_second_busy(self) -> None:
        engine = self._engine()
        construct_enter = threading.Event()
        construct_release = threading.Event()
        _FakeSileroVad.construct_enter = construct_enter
        _FakeSileroVad.construct_release = construct_release
        results: list[tuple[Any, Optional[str]]] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(engine._ensure_silero_vad())
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        try:
            with patch("src.voice.silero_vad.SileroOnnxVAD", _FakeSileroVad):
                t1.start()
                self.assertTrue(construct_enter.wait(timeout=2.0))
                t2.start()
                t2.join(timeout=2.0)
                construct_release.set()
                t1.join(timeout=2.0)
        finally:
            construct_release.set()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)
            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
        self.assertFalse(errors)
        self.assertEqual(_FakeSileroVad.factory_calls, 1)
        self.assertEqual(len(results), 2)
        reasons = {item[1] for item in results}
        self.assertIn("busy", reasons)
        owners = [item[0] for item in results if item[0] is not None]
        self.assertEqual(len(owners), 1)

    def test_retiring_unpublished_truthy_close_blocks_shutdown_retry(self) -> None:
        engine = self._engine()
        construct_enter = threading.Event()
        construct_release = threading.Event()

        class _TruthyRetireVad(_FakeSileroVad):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._close_attempts = 0

            def close(self, *, timeout_sec=None):
                self._close_attempts += 1
                if self._close_attempts < 3:
                    return 1
                self.closed = True
                return True

        _FakeSileroVad.construct_enter = construct_enter
        _FakeSileroVad.construct_release = construct_release

        def worker() -> None:
            engine._ensure_silero_vad()

        with patch("src.voice.silero_vad.SileroOnnxVAD", _TruthyRetireVad):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(construct_enter.wait(timeout=2.0))
            engine._shutdown_cancel.set()
            construct_release.set()
            thread.join(timeout=2.0)
        self.assertIsNotNone(engine._silero_vad_retiring)
        self.assertIsNone(engine._silero_vad)
        self.assertFalse(engine.shutdown())
        self.assertTrue(engine.shutdown())
        self.assertIsNone(engine._silero_vad_retiring)

    def test_factory_exception_propagates_and_clears_creating(self) -> None:
        engine = self._engine()

        class _BoomFactory:
            def __init__(self, **kwargs: Any) -> None:
                raise RuntimeError("factory boom")

        with patch("src.voice.silero_vad.SileroOnnxVAD", _BoomFactory):
            with self.assertRaises(RuntimeError):
                engine._ensure_silero_vad()
        self.assertFalse(engine._silero_vad_creating)

    def test_cancel_combined_with_shutdown(self) -> None:
        engine = self._engine()
        client = MagicMock()
        engine._paraformer_client = client
        cancel = threading.Event()
        pcm = _pcm_from_samples(FRAME_SAMPLES * 2)
        with patch("src.voice.silero_vad.SileroOnnxVAD", _FakeSileroVad):
            engine._shutdown_cancel.set()
            result = engine._transcribe_paraformer_pcm(
                pcm,
                cancel_event=cancel,
                apply_acoustic_guard=True,
            )
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)


if __name__ == "__main__":
    unittest.main()
