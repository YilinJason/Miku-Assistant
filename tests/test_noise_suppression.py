# -*- coding: utf-8 -*-
"""WebRTC NS utterance 预处理（fake AudioProcessor；无真实 native/Mic/ORT）。"""
from __future__ import annotations

import builtins
import importlib
import sys
import threading
import unittest
from types import SimpleNamespace
from typing import Any, Callable, List
from unittest.mock import MagicMock, patch

import src.voice.noise_suppression as ns_module
from src.core.safety import SafetyPolicy
from src.voice.input_audio import (
    FRAME_BYTES,
    MAX_PCM_BYTES,
    InputAudioSettings,
    acoustic_guard_not_ready_hint,
    resolve_acoustic_input_readiness,
)
from src.voice.noise_suppression import (
    NOISE_SUPPRESSION_NONE,
    NOISE_SUPPRESSION_WEBRTC,
    WEBRTC_NS_INSTALL_HINT,
    NoiseSuppressionResult,
    probe_webrtc_noise_suppression_readiness,
    suppress_utterance_pcm,
)
from src.voice.recognition import RecognitionStatus
from src.voice.stt import STTEngine

_SAVED_AUDIO_PROCESSOR: Any = None
_SAVED_ORT_SESSION: Any = None
_SAVED_MIC_INIT: Any = None
_SAVED_MIC_ENTER: Any = None

_BLOCKED_ROOTS = frozenset({"numpy", "pywebrtc_audio", "onnxruntime"})


def _pcm_frames(frames: int, *, byte: int = 7) -> bytes:
    return bytes([byte, 0]) * (FRAME_BYTES // 2 * frames)


def _passthrough_factory() -> Callable[[], Any]:
    class _Processor:
        calls = 0
        released = False

        def process(self, chunk: Any) -> Any:
            type(self).calls += 1
            return chunk

    return _Processor


def setUpModule() -> None:
    global _SAVED_AUDIO_PROCESSOR, _SAVED_ORT_SESSION, _SAVED_MIC_INIT, _SAVED_MIC_ENTER

    try:
        import pywebrtc_audio
    except ImportError:
        pywebrtc_audio = None  # type: ignore[assignment]
    if pywebrtc_audio is not None:
        _SAVED_AUDIO_PROCESSOR = pywebrtc_audio.AudioProcessor

        def _blocked_audio_processor(*_a: Any, **_k: Any) -> None:
            raise AssertionError(
                "offline test must not construct real pywebrtc_audio.AudioProcessor"
            )

        pywebrtc_audio.AudioProcessor = _blocked_audio_processor  # type: ignore[misc, assignment]

    try:
        import onnxruntime as ort
    except ImportError:
        ort = None  # type: ignore[assignment]
    if ort is not None:
        _SAVED_ORT_SESSION = ort.InferenceSession

        def _blocked_ort(*_a: Any, **_k: Any) -> None:
            raise AssertionError(
                "offline test must not construct real onnxruntime.InferenceSession"
            )

        ort.InferenceSession = _blocked_ort  # type: ignore[misc, assignment]

    try:
        import speech_recognition as sr
    except ImportError:
        sr = None  # type: ignore[assignment]
    if sr is not None:
        _SAVED_MIC_INIT = sr.Microphone.__init__
        _SAVED_MIC_ENTER = sr.Microphone.__enter__

        def _blocked_mic_init(self, *_a: Any, **_k: Any) -> None:
            raise AssertionError(
                "offline test must not construct speech_recognition.Microphone"
            )

        def _blocked_mic_enter(self, *_a: Any, **_k: Any) -> Any:
            raise AssertionError(
                "offline test must not enter speech_recognition.Microphone context"
            )

        sr.Microphone.__init__ = _blocked_mic_init  # type: ignore[method-assign]
        sr.Microphone.__enter__ = _blocked_mic_enter  # type: ignore[method-assign]


def tearDownModule() -> None:
    global _SAVED_AUDIO_PROCESSOR, _SAVED_ORT_SESSION, _SAVED_MIC_INIT, _SAVED_MIC_ENTER

    try:
        import pywebrtc_audio
    except ImportError:
        pywebrtc_audio = None  # type: ignore[assignment]
    if pywebrtc_audio is not None and _SAVED_AUDIO_PROCESSOR is not None:
        pywebrtc_audio.AudioProcessor = _SAVED_AUDIO_PROCESSOR  # type: ignore[misc, assignment]
    _SAVED_AUDIO_PROCESSOR = None

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


class NoiseSuppressionConfigTests(unittest.TestCase):
    def test_default_mapping_selects_webrtc(self) -> None:
        settings = InputAudioSettings.from_mapping({})
        self.assertEqual(settings.noise_suppression, NOISE_SUPPRESSION_WEBRTC)

    def test_rejects_explicit_null(self) -> None:
        with self.assertRaises(ValueError):
            InputAudioSettings.from_mapping({"noise_suppression": None})

    def test_rejects_bool_numeric_list_unknown(self) -> None:
        for bad in (True, 0, 1, [], "cloud"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    InputAudioSettings.from_mapping({"noise_suppression": bad})

    def test_missing_key_keeps_webrtc_default(self) -> None:
        settings = InputAudioSettings.from_mapping({"enabled": True})
        self.assertEqual(settings.noise_suppression, NOISE_SUPPRESSION_WEBRTC)


class ModuleImportIsolationTests(unittest.TestCase):
    def test_cold_import_does_not_touch_native_deps(self) -> None:
        real_import = builtins.__import__
        hits: list[str] = []

        def guarded_import(
            name: str,
            globals: Any = None,
            locals: Any = None,
            fromlist: Any = (),
            level: int = 0,
        ) -> Any:
            root = name.partition(".")[0]
            if root in _BLOCKED_ROOTS:
                hits.append(name)
                raise ImportError(f"blocked:{name}")
            return real_import(name, globals, locals, fromlist, level)

        saved = sys.modules.pop("src.voice.noise_suppression", None)
        try:
            with patch.object(builtins, "__import__", guarded_import):
                importlib.import_module("src.voice.noise_suppression")
            self.assertEqual(hits, [])
        finally:
            if saved is not None:
                sys.modules["src.voice.noise_suppression"] = saved
            else:
                importlib.import_module("src.voice.noise_suppression")


class FakeProcessorTests(unittest.TestCase):
    def test_suppress_success_same_length(self) -> None:
        pcm = _pcm_frames(3)
        factory = _passthrough_factory()
        with patch.object(
            ns_module, "_import_pywebrtc_audio", return_value=(object(), None)
        ):
            result = suppress_utterance_pcm(
                pcm,
                processor_factory=factory,
            )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.pcm), len(pcm))

    def test_final_frame_tail_bytes_preserved(self) -> None:
        tail = bytes([9, 0] * 50)
        pcm = _pcm_frames(2) + tail
        captured: List[bytes] = []

        class _TailProbe:
            def process(self, chunk: Any) -> Any:
                captured.append(chunk.tobytes())
                return chunk

        with patch.object(
            ns_module, "_import_pywebrtc_audio", return_value=(object(), None)
        ):
            result = suppress_utterance_pcm(
                pcm, processor_factory=lambda: _TailProbe()
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.pcm[-len(tail) :], tail)
        self.assertEqual(len(result.pcm), len(pcm))

    def test_rejects_before_factory(self) -> None:
        factory = _passthrough_factory()
        factory_calls: list[int] = []

        def counting_factory() -> Any:
            factory_calls.append(1)
            return factory()

        cases = [
            (b"", "pcm_empty"),
            (bytearray(b"\x00\x01"), "pcm_invalid_type"),
            (b"\x00\x01\x02", "pcm_misaligned"),
            (b"\x00" * (MAX_PCM_BYTES + 2), "pcm_oversized"),
        ]
        for pcm, reason in cases:
            with self.subTest(reason=reason):
                factory_calls.clear()
                with patch.object(
                    ns_module,
                    "_import_pywebrtc_audio",
                    side_effect=AssertionError("native"),
                ):
                    result = suppress_utterance_pcm(
                        pcm, processor_factory=counting_factory
                    )
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.pcm, b"")
                self.assertEqual(factory_calls, [])

    def test_bad_native_output_fail_closed(self) -> None:
        pcm = _pcm_frames(1)

        class _BadOutput:
            def process(self, chunk: Any) -> Any:
                return None

        with patch.object(
            ns_module, "_import_pywebrtc_audio", return_value=(object(), None)
        ):
            result = suppress_utterance_pcm(
                pcm, processor_factory=lambda: _BadOutput()
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "ns_output_invalid")
        self.assertEqual(result.pcm, b"")

    def test_native_exception_fail_closed(self) -> None:
        pcm = _pcm_frames(1)

        class _Boom:
            def process(self, chunk: Any) -> Any:
                raise RuntimeError("boom")

        with patch.object(
            ns_module, "_import_pywebrtc_audio", return_value=(object(), None)
        ):
            result = suppress_utterance_pcm(pcm, processor_factory=lambda: _Boom())
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "ns_processing_failed")
        self.assertEqual(result.pcm, b"")

    def test_cancel_during_factory_releases_without_success(self) -> None:
        pcm = _pcm_frames(1)
        cancel = threading.Event()
        constructed: list[str] = []

        class _Processor:
            def process(self, chunk: Any) -> Any:
                return chunk

        def factory() -> _Processor:
            constructed.append("built")
            cancel.set()
            return _Processor()

        with patch.object(
            ns_module, "_import_pywebrtc_audio", return_value=(object(), None)
        ):
            result = suppress_utterance_pcm(
                pcm, cancel_event=cancel, processor_factory=factory
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "cancelled")
        self.assertEqual(result.pcm, b"")
        self.assertEqual(constructed, ["built"])

    def test_cancel_in_final_process_frame(self) -> None:
        pcm = _pcm_frames(2) + bytes([1, 0] * 40)
        cancel = threading.Event()
        process_calls = 0

        class _CancelOnLast:
            def process(self, chunk: Any) -> Any:
                nonlocal process_calls
                process_calls += 1
                if process_calls >= 3:
                    cancel.set()
                return chunk

        with patch.object(
            ns_module, "_import_pywebrtc_audio", return_value=(object(), None)
        ):
            result = suppress_utterance_pcm(
                pcm,
                cancel_event=cancel,
                processor_factory=lambda: _CancelOnLast(),
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "cancelled")
        self.assertEqual(result.pcm, b"")

    def test_wrong_dtype_fail_closed(self) -> None:
        pcm = _pcm_frames(1)

        class _FloatOutput:
            def process(self, chunk: Any) -> Any:
                import numpy as np

                return np.zeros(160, dtype=np.float32)

        with patch.object(
            ns_module, "_import_pywebrtc_audio", return_value=(object(), None)
        ):
            result = suppress_utterance_pcm(
                pcm, processor_factory=lambda: _FloatOutput()
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "ns_output_invalid")
        self.assertEqual(result.pcm, b"")

    def test_readiness_imports_without_processor(self) -> None:
        with patch.object(
            ns_module, "_import_pywebrtc_audio", return_value=(object(), None)
        ), patch.object(ns_module, "_import_numpy", return_value=(object(), None)):
            ok, hint = probe_webrtc_noise_suppression_readiness()
        self.assertTrue(ok)
        self.assertEqual(hint, "")


class AcousticHintTests(unittest.TestCase):
    def test_ns_failures_return_install_hint_without_reprobe(self) -> None:
        settings = InputAudioSettings(noise_suppression=NOISE_SUPPRESSION_WEBRTC)
        for reason in ("ns_output_invalid", "ns_length_mismatch"):
            with self.subTest(reason=reason):
                with patch(
                    "src.voice.silero_vad.probe_silero_install_readiness",
                    side_effect=AssertionError("must not reprobe silero"),
                ), patch(
                    "src.voice.noise_suppression.probe_webrtc_noise_suppression_readiness",
                    side_effect=AssertionError("must not reprobe ns"),
                ):
                    hint = acoustic_guard_not_ready_hint(settings, reason=reason)
                self.assertEqual(hint, WEBRTC_NS_INSTALL_HINT)


class STTEngineNoiseSuppressionTests(unittest.TestCase):
    def _engine(self, **kwargs: Any) -> STTEngine:
        settings = kwargs.pop(
            "input_audio_settings",
            InputAudioSettings(
                enabled=True,
                noise_suppression=NOISE_SUPPRESSION_WEBRTC,
            ),
        )
        return STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=settings,
            **kwargs,
        )

    def test_guard_passes_cleaned_pcm_to_vad_and_asr(self) -> None:
        engine = self._engine()
        client = MagicMock()
        client.transcribe.return_value = SimpleNamespace(
            ok=True, text="hi", empty=False, error_label=""
        )
        engine._paraformer_client = client
        pcm = _pcm_frames(6)
        cleaned = _pcm_frames(6, byte=3)

        class _FakeSileroVad:
            def __init__(self, **_k: Any) -> None:
                pass

        with patch(
            "src.voice.noise_suppression.suppress_utterance_pcm",
            return_value=NoiseSuppressionResult(
                ok=True, reason="ok", pcm=cleaned
            ),
        ) as suppress, patch(
            "src.voice.silero_vad.SileroOnnxVAD",
            _FakeSileroVad,
        ), patch(
            "src.voice.input_audio.analyze_utterance_pcm",
            return_value=SimpleNamespace(
                accepted=True,
                reason="accepted",
                voiced_ms=180,
                voiced_ratio=1.0,
                frames_analyzed=6,
            ),
        ) as analyze:
            result = engine._transcribe_paraformer_pcm(
                pcm, apply_acoustic_guard=True
            )
        suppress.assert_called_once()
        analyze.assert_called_once()
        passed_pcm = analyze.call_args[0][0]
        self.assertIs(passed_pcm, cleaned)
        client.transcribe.assert_called_once()
        self.assertIs(client.transcribe.call_args[0][0], cleaned)
        self.assertEqual(result.status, RecognitionStatus.SUCCESS)

    def test_ns_not_ready_never_transcribes(self) -> None:
        engine = self._engine()
        client = MagicMock()
        engine._paraformer_client = client
        pcm = _pcm_frames(4)
        with patch(
            "src.voice.noise_suppression.suppress_utterance_pcm",
            return_value=NoiseSuppressionResult(
                ok=False, reason="ns_output_invalid", pcm=b""
            ),
        ), patch(
            "src.voice.input_audio.analyze_utterance_pcm",
        ) as analyze:
            result = engine._transcribe_paraformer_pcm(
                pcm, apply_acoustic_guard=True
            )
        analyze.assert_not_called()
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)
        self.assertEqual(result.error_detail, WEBRTC_NS_INSTALL_HINT)

    def test_caller_cancel_during_ns_skips_vad_and_asr(self) -> None:
        engine = self._engine()
        client = MagicMock()
        engine._paraformer_client = client
        pcm = _pcm_frames(3)
        cancel = threading.Event()

        def _ns_cancel(*_a: Any, **_k: Any) -> NoiseSuppressionResult:
            cancel.set()
            return NoiseSuppressionResult(ok=False, reason="cancelled", pcm=b"")

        with patch(
            "src.voice.noise_suppression.suppress_utterance_pcm",
            side_effect=_ns_cancel,
        ), patch(
            "src.voice.input_audio.analyze_utterance_pcm",
        ) as analyze:
            result = engine._transcribe_paraformer_pcm(
                pcm, cancel_event=cancel, apply_acoustic_guard=True
            )
        analyze.assert_not_called()
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.CANCELLED)

    def test_shutdown_during_ns_skips_vad_and_asr(self) -> None:
        engine = self._engine()
        client = MagicMock()
        engine._paraformer_client = client
        pcm = _pcm_frames(3)

        def _ns_shutdown(*_a: Any, **_k: Any) -> NoiseSuppressionResult:
            engine._shutdown_cancel.set()
            return NoiseSuppressionResult(ok=False, reason="cancelled", pcm=b"")

        with patch(
            "src.voice.noise_suppression.suppress_utterance_pcm",
            side_effect=_ns_shutdown,
        ), patch(
            "src.voice.input_audio.analyze_utterance_pcm",
        ) as analyze:
            result = engine._transcribe_paraformer_pcm(
                pcm, apply_acoustic_guard=True
            )
        analyze.assert_not_called()
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)

    def test_direct_audio_bypass_skips_ns(self) -> None:
        settings = InputAudioSettings(
            enabled=True,
            noise_suppression=NOISE_SUPPRESSION_WEBRTC,
        )
        engine = self._engine(input_audio_settings=settings)
        client = MagicMock()
        client.transcribe.return_value = SimpleNamespace(
            ok=True, text="hi", empty=False, error_label=""
        )
        engine._paraformer_client = client
        audio = MagicMock()
        audio.get_raw_data.return_value = _pcm_frames(4)
        with patch(
            "src.voice.noise_suppression.suppress_utterance_pcm",
        ) as suppress, patch(
            "src.voice.input_audio.analyze_utterance_pcm",
        ) as analyze:
            engine._recognize_paraformer_result(audio)
        suppress.assert_not_called()
        analyze.assert_not_called()
        client.transcribe.assert_called_once()

    def test_explicit_none_skips_ns(self) -> None:
        settings = InputAudioSettings(
            enabled=True,
            noise_suppression=NOISE_SUPPRESSION_NONE,
        )
        engine = self._engine(input_audio_settings=settings)
        client = MagicMock()
        engine._paraformer_client = client
        pcm = _pcm_frames(3)
        class _FakeSileroVad:
            def __init__(self, **_k: Any) -> None:
                pass

        with patch(
            "src.voice.noise_suppression.suppress_utterance_pcm",
        ) as suppress, patch(
            "src.voice.silero_vad.SileroOnnxVAD",
            _FakeSileroVad,
        ), patch(
            "src.voice.input_audio.analyze_utterance_pcm",
            return_value=SimpleNamespace(
                accepted=False,
                reason="insufficient_voiced_ms",
                voiced_ms=0,
                voiced_ratio=0.0,
                frames_analyzed=3,
            ),
        ):
            engine._transcribe_paraformer_pcm(pcm, apply_acoustic_guard=True)
        suppress.assert_not_called()

    def test_acoustic_readiness_includes_ns(self) -> None:
        settings = InputAudioSettings(noise_suppression=NOISE_SUPPRESSION_WEBRTC)
        with patch(
            "src.voice.noise_suppression.probe_webrtc_noise_suppression_readiness",
            return_value=(False, "missing-ns"),
        ) as probe, patch(
            "src.voice.silero_vad.probe_silero_install_readiness",
            return_value=(True, ""),
        ):
            ready, hint = resolve_acoustic_input_readiness(settings)
        probe.assert_called_once()
        self.assertFalse(ready)
        self.assertEqual(hint, "missing-ns")


if __name__ == "__main__":
    unittest.main()
