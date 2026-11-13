# -*- coding: utf-8 -*-
"""Phase A 声学准入与有界能量门限（离线 fake；R1/R3 契约）。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from src.core.assembly import build_stt_engine, validate_assembly_voice_stt_config
from src.core.safety import SafetyPolicy
from src.voice.input_audio import (
    FRAME_BYTES,
    MAX_PCM_BYTES,
    InputAudioSettings,
    analyze_utterance_pcm,
    compute_paraformer_listen_phrase_limit_sec,
    create_bounded_recognizer,
    reset_vad_backend_state_for_tests,
    resolve_input_audio_settings_from_stt_config,
    validate_paraformer_pcm_bytes,
    validate_pcm_geometry,
    validate_stt_energy_threshold_value,
)
from src.voice.recognition import RecognitionStatus
from src.voice.stt import STTEngine

_SAVED_MIC_INIT = None
_SAVED_MIC_ENTER = None


def setUpModule() -> None:
    global _SAVED_MIC_INIT, _SAVED_MIC_ENTER
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
    global _SAVED_MIC_INIT, _SAVED_MIC_ENTER
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


def _nonzero_pcm(frames: int, *, byte: int = 5) -> bytes:
    return bytes([byte, 0]) * (FRAME_BYTES // 2 * frames)


def _webrtc_settings(**kwargs: Any) -> InputAudioSettings:
    kwargs.setdefault("backend", "webrtc")
    kwargs.setdefault("noise_suppression", "none")
    return InputAudioSettings(**kwargs)


def _frame_virtual_source_class(
    sr_module: Any,
    *,
    sample_rate: int,
    chunk: int,
    sample_width: int,
    silence_sec: float = 0.6,
    speech_sec: float = 1.0,
) -> type:
    """SpeechRecognition 约定：stream.read(n) 的 n 为采样帧数，返回 n * width 字节。"""

    quiet_sample = b"\x01" if sample_width == 1 else b"\x01\x00"
    loud_sample = b"\xff" if sample_width == 1 else b"\xff\x7f"
    silence = quiet_sample * int(sample_rate * silence_sec)
    speech = loud_sample * int(sample_rate * speech_sec)
    payload = silence + speech

    class FrameVirtualSource(sr_module.AudioSource):
        CHUNK = chunk
        SAMPLE_RATE = sample_rate
        SAMPLE_WIDTH = sample_width

        def __init__(self) -> None:
            self._payload = payload
            self._offset = 0
            self.stream = self

        def read(self, frame_count: int) -> bytes:
            byte_count = frame_count * sample_width
            if self._offset >= len(self._payload):
                return quiet_sample * frame_count
            end = min(self._offset + byte_count, len(self._payload))
            chunk_bytes = self._payload[self._offset : end]
            self._offset = end
            if len(chunk_bytes) < byte_count:
                pad_frames = (byte_count - len(chunk_bytes)) // sample_width
                chunk_bytes += quiet_sample * pad_frames
            return chunk_bytes

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    return FrameVirtualSource


class _FakeMicrophoneFactory:
    """可追踪的 ``speech_recognition.Microphone`` 替身，禁止真实设备 I/O。"""

    def __init__(self, names: list[str] | None = None) -> None:
        self.names = list(names or ["fake-mic-0"])
        self.list_calls = 0
        self.instances: list[dict[str, object]] = []

    def build(self) -> type:
        factory = self

        class FakeMicrophone:
            CHUNK = 1024
            SAMPLE_RATE = 16000
            SAMPLE_WIDTH = 2

            def __init__(self, device_index=None) -> None:
                factory.instances.append({"device_index": device_index})
                self.device_index = device_index

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            @staticmethod
            def list_microphone_names() -> list[str]:
                factory.list_calls += 1
                return list(factory.names)

        return FakeMicrophone


class OfflineMicrophoneTestCase(unittest.TestCase):
    """凡可能触及 ``Microphone`` 的用例必须继承本类。"""

    _mic_factory: _FakeMicrophoneFactory

    def setUp(self) -> None:
        super().setUp()
        self._mic_factory = _FakeMicrophoneFactory()
        self._mic_patch = patch(
            "speech_recognition.Microphone", self._mic_factory.build()
        )
        self._mic_patch.start()
        self.addCleanup(self._mic_patch.stop)


class InputAudioSettingsValidationTests(unittest.TestCase):
    def test_default_mapping_uses_silero_backend(self) -> None:
        settings = InputAudioSettings.from_mapping({})
        self.assertEqual(settings.backend, "silero_onnx")
        self.assertEqual(settings.speech_probability_threshold, 0.6)
        self.assertEqual(settings.noise_suppression, "webrtc")

    def test_rejects_invalid_backend(self) -> None:
        with self.assertRaises(ValueError):
            InputAudioSettings.from_mapping({"backend": "google"})

    def test_rejects_unknown_key(self) -> None:
        with self.assertRaises(ValueError):
            InputAudioSettings.from_mapping({"enabled": True, "extra": 1})

    def test_direct_construct_validates_invariants(self) -> None:
        with self.assertRaises(ValueError):
            InputAudioSettings(energy_floor=9000, energy_ceiling=1000)

    def test_direct_construct_rejects_invalid_typed_fields(self) -> None:
        base = dict(
            enabled=True,
            backend="silero_onnx",
            speech_probability_threshold=0.6,
            energy_floor=300,
            energy_ceiling=12000,
            vad_mode=2,
            min_voiced_ms=180,
            min_consecutive_ms=90,
            min_voiced_ratio=0.12,
            microphone_device_index=None,
        )
        cases = [
            {"enabled": 1},
            {"energy_floor": True},
            {"energy_floor": -1},
            {"vad_mode": 9},
            {"vad_mode": False},
            {"min_voiced_ratio": float("nan")},
            {"min_voiced_ratio": True},
            {"noise_suppression": "cloud"},
            {"noise_suppression": 0},
            {"microphone_device_index": True},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                payload = dict(base)
                payload.update(overrides)
                with self.assertRaises(ValueError):
                    InputAudioSettings(**payload)

    def test_assembly_rejects_malformed_voice_stt_shape(self) -> None:
        class _Cfg:
            def __init__(self, payload):
                self._payload = payload

            def get(self, key: str, default=None):
                if key == "voice.stt":
                    return self._payload
                return default

        for bad in (False, 0, []):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_assembly_voice_stt_config(_Cfg(bad))

    def test_energy_threshold_bool_rejected_in_resolver(self) -> None:
        with self.assertRaises(ValueError):
            resolve_input_audio_settings_from_stt_config(
                {"energy_threshold": True, "input_audio": {}}
            )

    def test_stt_mapping_type_enforced(self) -> None:
        with self.assertRaises(ValueError):
            resolve_input_audio_settings_from_stt_config([])  # type: ignore[arg-type]

    def test_assembly_validates_bad_vad_mode(self) -> None:
        class _Cfg:
            def get(self, key: str, default=None):
                if key == "voice.stt":
                    return {"engine": "paraformer", "input_audio": {"vad_mode": 9}}
                return default

        with self.assertRaises(ValueError):
            validate_assembly_voice_stt_config(_Cfg())


class BoundedEnergyRecognizerTests(unittest.TestCase):
    def test_create_bounded_subclass_clamps_assignments(self) -> None:
        settings = _webrtc_settings(enabled=True, energy_floor=300, energy_ceiling=12000)
        rec = create_bounded_recognizer(settings, initial_energy=4000)
        rec.energy_threshold = 50
        self.assertEqual(rec.energy_threshold, 300.0)
        rec.energy_threshold = 99999
        self.assertEqual(rec.energy_threshold, 12000.0)

    def test_real_sr_adjust_for_ambient_noise_stays_bounded(self) -> None:
        try:
            import speech_recognition as sr
        except ImportError:
            self.skipTest("SpeechRecognition not installed")
        settings = _webrtc_settings(enabled=True, energy_floor=500, energy_ceiling=8000)
        rec = create_bounded_recognizer(settings, initial_energy=4000)
        rec.dynamic_energy_threshold = True
        rec.pause_threshold = 1.5

        sample_rate = 16000
        sample_width = 2
        duration_sec = 3.0
        quiet = b"\x02\x00" * int(sample_rate * duration_sec)
        loud = b"\xff\x7f" * int(sample_rate * duration_sec)

        class _VirtualSource(sr.AudioSource):
            CHUNK = 1024
            SAMPLE_RATE = sample_rate
            SAMPLE_WIDTH = sample_width

            def __init__(self, payload: bytes) -> None:
                self._payload = payload
                self._offset = 0
                self.stream = self

            def read(self, frame_count: int) -> bytes:
                byte_count = frame_count * sample_width
                if self._offset >= len(self._payload):
                    return b"\x02\x00" * frame_count
                end = min(self._offset + byte_count, len(self._payload))
                chunk = self._payload[self._offset : end]
                self._offset = end
                if len(chunk) < byte_count:
                    pad = (byte_count - len(chunk)) // sample_width
                    chunk += b"\x02\x00" * pad
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        with _VirtualSource(quiet) as quiet_source:
            rec.adjust_for_ambient_noise(quiet_source, duration=duration_sec)
        self.assertGreaterEqual(rec.energy_threshold, 500.0)
        self.assertLessEqual(rec.energy_threshold, 8000.0)

        rec.energy_threshold = 4000
        with _VirtualSource(loud) as loud_source:
            rec.adjust_for_ambient_noise(loud_source, duration=duration_sec)
        self.assertGreaterEqual(rec.energy_threshold, 500.0)
        self.assertLessEqual(rec.energy_threshold, 8000.0)
        rec.energy_threshold = 10
        self.assertEqual(rec.energy_threshold, 500.0)

    def test_real_sr_listen_fake_stream_timeout_and_loud_bounds(self) -> None:
        try:
            import speech_recognition as sr
        except ImportError:
            self.skipTest("SpeechRecognition not installed")
        settings = _webrtc_settings(enabled=True, energy_floor=400, energy_ceiling=9000)
        rec = create_bounded_recognizer(settings, initial_energy=4000)
        rec.dynamic_energy_threshold = False
        rec.pause_threshold = 0.4
        rec.non_speaking_duration = 0.2

        sample_rate = 16000
        sample_width = 2
        quiet = b"\x01\x00" * (sample_rate * 4)
        loud = b"\xff\x7f" * (sample_rate * 4)

        class _VirtualSource(sr.AudioSource):
            CHUNK = 1024
            SAMPLE_RATE = sample_rate
            SAMPLE_WIDTH = sample_width

            def __init__(self, payload: bytes) -> None:
                self._payload = payload
                self._offset = 0
                self.stream = self

            def read(self, frame_count: int) -> bytes:
                byte_count = frame_count * sample_width
                if self._offset >= len(self._payload):
                    return b"\x02\x00" * frame_count
                end = min(self._offset + byte_count, len(self._payload))
                chunk = self._payload[self._offset : end]
                self._offset = end
                if len(chunk) < byte_count:
                    pad = (byte_count - len(chunk)) // sample_width
                    chunk += b"\x02\x00" * pad
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        rec.energy_threshold = 50000
        with _VirtualSource(quiet) as source:
            with self.assertRaises(sr.WaitTimeoutError):
                rec.listen(source, timeout=0.3, phrase_time_limit=0.6)

        rec.energy_threshold = 4000
        with _VirtualSource(loud) as source:
            audio = rec.listen(source, timeout=2.0, phrase_time_limit=0.6)
        self.assertGreater(len(audio.frame_data), 0)
        self.assertGreaterEqual(rec.energy_threshold, 400.0)
        self.assertLessEqual(rec.energy_threshold, 9000.0)

    def test_paraformer_stt_engine_initializes_recognizer_with_defaults(self) -> None:
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy(),
            input_audio_settings=InputAudioSettings(enabled=True),
        )
        self.assertIsNotNone(engine._recognizer)
        self.assertTrue(engine._recognizer.dynamic_energy_threshold)
        self.assertEqual(engine._recognizer.pause_threshold, 1.5)


class AnalyzeUtterancePcmTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_vad_backend_state_for_tests()

    def test_zero_pcm_geometry(self) -> None:
        self.assertEqual(validate_pcm_geometry(b"\x00" * FRAME_BYTES), "pcm_zero")

    def test_silence_rejected_with_explicit_decisions(self) -> None:
        pcm = _nonzero_pcm(10)
        settings = _webrtc_settings(enabled=True)
        verdict = analyze_utterance_pcm(
            pcm,
            settings,
            frame_is_speech=lambda _f: False,
        )
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, "insufficient_voiced_ms")

    def test_short_burst_rejected(self) -> None:
        pcm = _nonzero_pcm(10)
        settings = _webrtc_settings(enabled=True, min_voiced_ms=180)
        position = [0]

        def _decide(_frame: bytes) -> bool:
            index = position[0]
            position[0] += 1
            return index < 2

        verdict = analyze_utterance_pcm(
            pcm,
            settings,
            frame_is_speech=_decide,
        )
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, "insufficient_voiced_ms")
        self.assertEqual(verdict.voiced_ms, 60)

    def test_at_threshold_voiced_ms_accepted(self) -> None:
        pcm = _nonzero_pcm(6)
        settings = _webrtc_settings(
            enabled=True,
            min_voiced_ms=180,
            min_consecutive_ms=90,
            min_voiced_ratio=0.12,
        )
        verdict = analyze_utterance_pcm(pcm, settings, frame_is_speech=lambda _f: True)
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.voiced_ms, 180)

    def test_normal_speech_accepted(self) -> None:
        pcm = _nonzero_pcm(12)
        settings = _webrtc_settings(enabled=True)
        verdict = analyze_utterance_pcm(
            pcm,
            settings,
            frame_is_speech=lambda _f: True,
        )
        self.assertTrue(verdict.accepted)

    def test_vad_unavailable_with_nonzero_pcm(self) -> None:
        pcm = _nonzero_pcm(6)
        settings = _webrtc_settings(enabled=True)
        with patch("src.voice.input_audio.resolve_vad_backend", return_value=(False, "hint", None)):
            verdict = analyze_utterance_pcm(pcm, settings)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, "vad_unavailable")

    def test_cancel_during_analysis(self) -> None:
        import threading

        pcm = _nonzero_pcm(8)
        settings = _webrtc_settings(enabled=True)
        cancel = threading.Event()
        cancel.set()
        verdict = analyze_utterance_pcm(
            pcm,
            settings,
            frame_is_speech=lambda _f: True,
            cancel_event=cancel,
        )
        self.assertEqual(verdict.reason, "cancelled")

    def test_fresh_vad_instance_per_call(self) -> None:
        pcm = _nonzero_pcm(8)
        settings = _webrtc_settings(enabled=True)
        created: list[int] = []

        class _FakeVad:
            def __init__(self, mode: int) -> None:
                created.append(mode)

            def is_speech(self, frame: bytes, rate: int) -> bool:
                return True

        class _FakeModule:
            Vad = _FakeVad

        with patch(
            "src.voice.input_audio.resolve_vad_backend",
            return_value=(True, "", _FakeModule()),
        ):
            analyze_utterance_pcm(pcm, settings)
            analyze_utterance_pcm(pcm, settings)
        self.assertEqual(len(created), 2)


class STTEngineGuardScopeTests(OfflineMicrophoneTestCase):
    def test_direct_audio_default_skips_guard(self) -> None:
        settings = _webrtc_settings(enabled=True)
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=settings,
        )
        client = MagicMock()
        client.transcribe.return_value = SimpleNamespace(
            ok=True, text="hi", empty=False, error_label=""
        )
        engine._paraformer_client = client
        audio = MagicMock()
        audio.get_raw_data.return_value = _nonzero_pcm(8)
        with patch(
            "src.voice.input_audio.analyze_utterance_pcm",
        ) as analyze:
            engine._recognize_paraformer_result(audio)
        analyze.assert_not_called()
        client.transcribe.assert_called_once()

    def test_microphone_path_applies_guard_before_transcribe(self) -> None:
        settings = _webrtc_settings(enabled=True)
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=settings,
        )
        client = MagicMock()
        engine._paraformer_client = client
        pcm = _nonzero_pcm(6)
        with patch(
            "src.voice.input_audio.analyze_utterance_pcm",
            return_value=SimpleNamespace(
                accepted=False,
                reason="insufficient_voiced_ms",
                voiced_ms=0,
                voiced_ratio=0.0,
                frames_analyzed=6,
            ),
        ):
            result = engine._transcribe_paraformer_pcm(
                pcm, apply_acoustic_guard=True
            )
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.UNRECOGNIZED)

    def test_cancel_after_guard_before_transcribe(self) -> None:
        import threading

        settings = _webrtc_settings(enabled=True)
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=settings,
        )
        client = MagicMock()
        engine._paraformer_client = client
        cancel = threading.Event()
        pcm = _nonzero_pcm(8)

        def _analyze(*_a, **_k):
            cancel.set()
            return SimpleNamespace(
                accepted=True,
                reason="accepted",
                voiced_ms=240,
                voiced_ratio=1.0,
                frames_analyzed=8,
            )

        with patch("src.voice.input_audio.analyze_utterance_pcm", side_effect=_analyze):
            result = engine._transcribe_paraformer_pcm(
                pcm, cancel_event=cancel, apply_acoustic_guard=True
            )
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.CANCELLED)

    def test_mic_preflight_lists_once(self) -> None:
        settings = _webrtc_settings(enabled=True, microphone_device_index=0)
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=settings,
        )
        engine._recognizer = object()
        ok1, _ = engine.check_microphone()
        ok2, _ = engine.check_microphone()
        self.assertTrue(ok1 and ok2)
        self.assertEqual(self._mic_factory.list_calls, 1)
        self.assertEqual(
            [item["device_index"] for item in self._mic_factory.instances],
            [0, 0],
        )

    def test_single_check_microphone_lists_at_most_once(self) -> None:
        settings = _webrtc_settings(enabled=True)
        engine = STTEngine(
            engine="paraformer",
            input_audio_settings=settings,
            safety_policy=SafetyPolicy(),
        )
        engine._recognizer = object()
        engine.check_microphone()
        self.assertEqual(self._mic_factory.list_calls, 1)
        self.assertEqual(len(self._mic_factory.instances), 1)

    def test_invalid_device_index_at_preflight(self) -> None:
        self._mic_factory.names = ["only-one"]
        settings = _webrtc_settings(enabled=True, microphone_device_index=3)
        engine = STTEngine(
            engine="paraformer",
            input_audio_settings=settings,
            safety_policy=SafetyPolicy(),
        )
        ok, detail = engine.check_microphone()
        self.assertFalse(ok)
        self.assertIn("device_index=3", detail or "")

    def test_disabled_guard_uses_legacy_duration_gate(self) -> None:
        settings = _webrtc_settings(enabled=False)
        self.assertFalse(
            STTEngine(
                engine="paraformer",
                input_audio_settings=settings,
                safety_policy=SafetyPolicy(),
            )._paraformer_input_guard_enabled()
        )

    def test_recognize_public_path_rejects_guard_without_worker(self) -> None:
        settings = _webrtc_settings(enabled=True)
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=settings,
        )
        pcm = _nonzero_pcm(8)
        audio = MagicMock()
        audio.frame_data = pcm
        audio.sample_rate = 16000
        audio.sample_width = 2
        audio.get_raw_data.return_value = pcm
        engine._recognizer = MagicMock()
        engine._recognizer.non_speaking_duration = 0.5
        engine._recognizer.pause_threshold = 1.5
        engine._recognizer.listen.return_value = audio
        client = MagicMock()
        engine._paraformer_client = client

        mic_cm = MagicMock()
        fake_source = MagicMock()
        fake_source.CHUNK = 1024
        fake_source.SAMPLE_RATE = 16000
        fake_source.SAMPLE_WIDTH = 2
        mic_cm.__enter__ = MagicMock(return_value=fake_source)
        mic_cm.__exit__ = MagicMock(return_value=False)

        with patch.object(
            engine, "_paraformer_runtime_ready", return_value=True
        ), patch.object(engine, "_calibrate_ambient_noise"), patch.object(
            engine, "_open_microphone", return_value=mic_cm
        ), patch(
            "src.voice.input_audio.analyze_utterance_pcm",
            return_value=SimpleNamespace(
                accepted=False,
                reason="insufficient_voiced_ms",
                voiced_ms=30,
                voiced_ratio=0.05,
                frames_analyzed=8,
            ),
        ), patch.object(
            engine, "_ensure_paraformer_client"
        ) as ensure_client:
            result = engine.recognize()

        ensure_client.assert_not_called()
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.UNRECOGNIZED)

    def test_recognize_public_path_accepts_guard_and_transcribes(self) -> None:
        settings = _webrtc_settings(enabled=True)
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=settings,
        )
        pcm = _nonzero_pcm(10)
        audio = MagicMock()
        audio.frame_data = pcm
        audio.sample_rate = 16000
        audio.sample_width = 2
        audio.get_raw_data.return_value = pcm
        engine._recognizer = MagicMock()
        engine._recognizer.non_speaking_duration = 0.5
        engine._recognizer.pause_threshold = 1.5
        engine._recognizer.listen.return_value = audio
        client = MagicMock()
        client.transcribe.return_value = SimpleNamespace(
            ok=True, text="你好", empty=False, error_label=""
        )
        engine._paraformer_client = client

        mic_cm = MagicMock()
        fake_source = MagicMock()
        fake_source.CHUNK = 1024
        fake_source.SAMPLE_RATE = 16000
        fake_source.SAMPLE_WIDTH = 2
        mic_cm.__enter__ = MagicMock(return_value=fake_source)
        mic_cm.__exit__ = MagicMock(return_value=False)

        with patch.object(
            engine, "_paraformer_runtime_ready", return_value=True
        ), patch.object(engine, "_calibrate_ambient_noise"), patch.object(
            engine, "_open_microphone", return_value=mic_cm
        ), patch(
            "src.voice.input_audio.analyze_utterance_pcm",
            return_value=SimpleNamespace(
                accepted=True,
                reason="accepted",
                voiced_ms=300,
                voiced_ratio=0.5,
                frames_analyzed=10,
            ),
        ), patch.object(engine, "_ensure_paraformer_client", return_value=True):
            result = engine.recognize()

        client.transcribe.assert_called_once()
        self.assertEqual(result.status, RecognitionStatus.SUCCESS)
        self.assertEqual(result.text, "你好")

    def test_stt_rejects_non_settings_input_audio(self) -> None:
        with self.assertRaises(TypeError):
            STTEngine(
                engine="paraformer",
                safety_policy=SafetyPolicy(),
                input_audio_settings={"enabled": True},  # type: ignore[arg-type]
            )

    def test_mocked_runtime_ready_cannot_hide_missing_recognizer(self) -> None:
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=InputAudioSettings(enabled=True),
        )
        engine._recognizer = None
        with patch.object(engine, "_paraformer_runtime_ready", return_value=True):
            result = engine.recognize()
        self.assertEqual(len(self._mic_factory.instances), 0)
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)

    def test_vad_unavailable_recognize_no_mic_no_worker(self) -> None:
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=InputAudioSettings(enabled=True),
        )
        engine._recognizer = MagicMock()
        with patch.object(engine, "_paraformer_runtime_ready", return_value=False), patch.object(
            engine, "_ensure_paraformer_client"
        ) as ensure:
            result = engine.recognize()
        self.assertEqual(len(self._mic_factory.instances), 0)
        ensure.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)

    def test_vad_native_error_on_guard_blocks_transcribe(self) -> None:
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=InputAudioSettings(
                enabled=True, noise_suppression="none"
            ),
        )
        client = MagicMock()
        engine._paraformer_client = client
        pcm = _nonzero_pcm(8)
        with patch(
            "src.voice.input_audio.analyze_utterance_pcm",
            return_value=SimpleNamespace(
                accepted=False,
                reason="vad_native_error",
                voiced_ms=0,
                voiced_ratio=0.0,
                frames_analyzed=0,
            ),
        ):
            result = engine._transcribe_paraformer_pcm(
                pcm, apply_acoustic_guard=True
            )
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)

    def test_oversized_pcm_rejected_with_guard_enabled(self) -> None:
        pcm = _nonzero_pcm(MAX_PCM_BYTES // FRAME_BYTES + 1)
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=InputAudioSettings(
                enabled=True, noise_suppression="none"
            ),
        )
        client = MagicMock()
        engine._paraformer_client = client
        result = engine._transcribe_paraformer_pcm(pcm, apply_acoustic_guard=True)
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.INTERNAL_ERROR)

    def test_oversized_pcm_rejected_without_transcribe(self) -> None:
        oversized = _nonzero_pcm(MAX_PCM_BYTES // FRAME_BYTES + 1)
        self.assertGreater(len(oversized), MAX_PCM_BYTES)
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=InputAudioSettings(enabled=False),
        )
        client = MagicMock()
        engine._paraformer_client = client
        with patch.object(engine, "_ensure_paraformer_client", return_value=True):
            result = engine._transcribe_paraformer_pcm(oversized)
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.INTERNAL_ERROR)

    def test_odd_length_pcm_rejected_direct_path(self) -> None:
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy(),
            input_audio_settings=InputAudioSettings(enabled=False),
        )
        client = MagicMock()
        engine._paraformer_client = client
        odd = b"\x01\x02\x03"
        result = engine._transcribe_paraformer_pcm(odd)
        client.transcribe.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.INTERNAL_ERROR)

    def test_max_pcm_accepted_guard_disabled(self) -> None:
        pcm = _nonzero_pcm(MAX_PCM_BYTES // FRAME_BYTES)
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
            input_audio_settings=InputAudioSettings(enabled=False),
        )
        client = MagicMock()
        client.transcribe.return_value = SimpleNamespace(
            ok=True, text="ok", empty=False, error_label=""
        )
        engine._paraformer_client = client
        with patch.object(engine, "_ensure_paraformer_client", return_value=True):
            result = engine._transcribe_paraformer_pcm(pcm)
        client.transcribe.assert_called_once()
        self.assertEqual(result.status, RecognitionStatus.SUCCESS)


class ParaformerListenPhraseLimitTests(unittest.TestCase):
    def test_phrase_limit_sample_width_invariant(self) -> None:
        recognizer = SimpleNamespace(non_speaking_duration=0.5)
        narrow = SimpleNamespace(CHUNK=1024, SAMPLE_RATE=16000, SAMPLE_WIDTH=1)
        wide = SimpleNamespace(CHUNK=1024, SAMPLE_RATE=16000, SAMPLE_WIDTH=2)
        limit_n = compute_paraformer_listen_phrase_limit_sec(
            narrow, recognizer, caller_phrase_limit=15.0
        )
        limit_w = compute_paraformer_listen_phrase_limit_sec(
            wide, recognizer, caller_phrase_limit=15.0
        )
        self.assertEqual(limit_n, limit_w)

    def test_phrase_limit_reserves_non_speaking_for_sample_rates(self) -> None:
        recognizer = SimpleNamespace(non_speaking_duration=0.5)
        for rate, chunk in ((8000, 512), (16000, 1024), (44100, 2048), (48000, 2048)):
            with self.subTest(rate=rate, chunk=chunk):
                source = SimpleNamespace(
                    CHUNK=chunk,
                    SAMPLE_RATE=rate,
                    SAMPLE_WIDTH=2,
                )
                limit = compute_paraformer_listen_phrase_limit_sec(
                    source, recognizer, caller_phrase_limit=15.0
                )
                self.assertLess(limit, 15.0)
                self.assertGreater(limit, 0.0)

    def test_invalid_non_speaking_duration_rejected(self) -> None:
        source = SimpleNamespace(CHUNK=1024, SAMPLE_RATE=16000, SAMPLE_WIDTH=2)
        with self.assertRaises(ValueError):
            compute_paraformer_listen_phrase_limit_sec(
                source,
                SimpleNamespace(non_speaking_duration=float("nan")),
                10.0,
            )


class ParaformerListenCaptureBudgetTests(unittest.TestCase):
    def test_sr_listen_virtual_stream_within_protocol_pcm(self) -> None:
        try:
            import speech_recognition as sr
        except ImportError:
            self.skipTest("SpeechRecognition not installed")
        for rate, chunk in ((8000, 512), (16000, 1024), (44100, 2048), (48000, 2048)):
            with self.subTest(rate=rate, chunk=chunk):
                rec = sr.Recognizer()
                rec.energy_threshold = 350
                rec.dynamic_energy_threshold = False
                rec.pause_threshold = 0.8
                rec.non_speaking_duration = 0.5
                source_cls = _frame_virtual_source_class(
                    sr,
                    sample_rate=rate,
                    chunk=chunk,
                    sample_width=2,
                    silence_sec=0.6,
                    speech_sec=1.0,
                )
                with source_cls() as source:
                    phrase = compute_paraformer_listen_phrase_limit_sec(
                        source, rec, caller_phrase_limit=12.0
                    )
                    audio = rec.listen(
                        source, timeout=5.0, phrase_time_limit=phrase
                    )
                pcm = audio.get_raw_data(convert_rate=16000, convert_width=2)
                self.assertLessEqual(len(pcm), MAX_PCM_BYTES)
                self.assertGreater(len(pcm), 0)


class PcmGeometryBoundaryTests(unittest.TestCase):
    def test_exact_max_pcm_bytes_passes_geometry(self) -> None:
        pcm = _nonzero_pcm(MAX_PCM_BYTES // FRAME_BYTES)
        self.assertEqual(len(pcm), MAX_PCM_BYTES)
        self.assertIsNone(validate_pcm_geometry(pcm))

    def test_oversized_pcm_rejected_before_vad(self) -> None:
        pcm = _nonzero_pcm(MAX_PCM_BYTES // FRAME_BYTES + 2)
        settings = _webrtc_settings(enabled=True)
        verdict = analyze_utterance_pcm(pcm, settings, frame_is_speech=lambda _f: True)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, "pcm_oversized")

    def test_validate_paraformer_pcm_helper(self) -> None:
        self.assertIsNone(validate_paraformer_pcm_bytes(_nonzero_pcm(4)))
        self.assertEqual(validate_paraformer_pcm_bytes(b""), "pcm_empty")
        self.assertEqual(validate_paraformer_pcm_bytes(b"\x01"), "pcm_misaligned")
        self.assertEqual(validate_paraformer_pcm_bytes(None), "pcm_invalid_type")
        self.assertEqual(validate_paraformer_pcm_bytes("pcm"), "pcm_invalid_type")
        self.assertEqual(validate_paraformer_pcm_bytes(bytearray(b"\x00\x01")), "pcm_invalid_type")

    def test_invalid_pcm_type_rejected_without_worker(self) -> None:
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy(),
            input_audio_settings=InputAudioSettings(enabled=False),
        )
        client = MagicMock()
        engine._paraformer_client = client
        for bad in (None, bytearray(b"\x00\x01"), "text"):
            with self.subTest(bad=bad):
                result = engine._transcribe_paraformer_pcm(bad)  # type: ignore[arg-type]
                client.transcribe.assert_not_called()
                self.assertEqual(result.status, RecognitionStatus.INTERNAL_ERROR)
            client.reset_mock()


class AssemblyEnergyThresholdTests(unittest.TestCase):
    def test_build_stt_rejects_nonfinite_energy(self) -> None:
        class _Cfg:
            def get(self, key: str, default=None):
                if key == "voice.stt":
                    return {"engine": "paraformer", "energy_threshold": float("nan")}
                return default

        with self.assertRaises(ValueError):
            build_stt_engine(_Cfg(), safety_policy=SafetyPolicy({}))


class VoiceSessionDispatchGuardTests(unittest.TestCase):
    def test_unrecognized_guard_does_not_dispatch_turn(self) -> None:
        from src.cli.voice_session import VoiceSession
        from src.voice.recognition import RecognitionResult

        assistant = MagicMock()
        assistant.config.get.side_effect = lambda key, default=None: {
            "assistant.stream_reply": False,
            "assistant.duplex_listening": False,
        }.get(key, default)
        session = VoiceSession(assistant)
        sequence = iter(
            [
                RecognitionResult.failure(
                    RecognitionStatus.UNRECOGNIZED, "未检测到足够人声"
                ),
                RecognitionResult.cancelled("退出"),
            ]
        )

        def _once(**_kwargs):
            return next(sequence)

        with patch.object(session, "_recognize_once", side_effect=_once), patch.object(
            session, "_handle_nonstreaming_turn"
        ) as handle_turn, patch.object(
            session, "_handle_streaming_turn"
        ) as handle_stream:
            session.run()

        handle_turn.assert_not_called()
        handle_stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
