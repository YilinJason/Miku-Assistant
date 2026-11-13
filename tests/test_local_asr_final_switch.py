# -*- coding: utf-8 -*-
"""Final default switch to local Paraformer (fake-only; no worker/models)."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from src.core.assembly import build_stt_engine
from src.core.config import Config
from src.core.safety import SafetyPolicy
from src.core.voice_activation import stt_shutdown_reports_complete
from src.voice.recognition import RecognitionResult, RecognitionStatus
from src.voice.stt import STTEngine


class ConfigDefaultParityTests(unittest.TestCase):
    def test_fresh_config_defaults_paraformer_and_permissions(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            cfg = Config(str(path))
            self.assertEqual(cfg.get("voice.stt.engine"), "paraformer")
            self.assertTrue(cfg.get("security.permissions.local_asr_worker"))
            self.assertFalse(cfg.get("security.permissions.cloud_stt"))
            inner = cfg.get("voice.stt.paraformer") or {}
            self.assertEqual(inner.get("cpu_threads"), 2)
            self.assertEqual(inner.get("startup_deadline_sec"), 45)
            self.assertEqual(inner.get("inference_deadline_sec"), 15)
            self.assertEqual(inner.get("close_budget_sec"), 2)

    def test_repo_yaml_stt_engine_is_paraformer(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        cfg = Config(str(repo_root / "config" / "config.yaml"))
        self.assertEqual(cfg.get("voice.stt.engine"), "paraformer")
        self.assertFalse(cfg.get("security.permissions.cloud_stt"))
        self.assertTrue(cfg.get("security.permissions.local_asr_worker"))


class AssemblySttDefaultTests(unittest.TestCase):
    def test_build_stt_engine_fallback_paraformer(self) -> None:
        class _Cfg:
            def get(self, key: str, default=None):
                if key == "voice.stt":
                    return {"language": "zh-CN", "energy_threshold": 4000}
                return default

        captured: list[dict] = []

        def fake_stt(**kwargs):
            captured.append(kwargs)
            return MagicMock()

        build_stt_engine(
            _Cfg(),
            stt_engine_cls=fake_stt,
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["engine"], "paraformer")
        self.assertIn("paraformer_settings", captured[0])


class ParaformerReadinessTests(unittest.TestCase):
    def test_default_not_ready_without_sealed_profile(self) -> None:
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        self.assertIsNotNone(engine._recognizer)
        self.assertFalse(engine.is_ready())

    def test_recognize_never_opens_mic_when_not_ready(self) -> None:
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        with patch("speech_recognition.Microphone") as mic:
            result = engine.recognize()
        mic.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)
        self.assertIn("Paraformer", result.error_detail or "")

    def test_shutdown_after_not_ready_reports_complete(self) -> None:
        engine = STTEngine(engine="paraformer", safety_policy=SafetyPolicy())
        self.assertTrue(engine.shutdown())
        self.assertTrue(stt_shutdown_reports_complete(engine))
        with patch("speech_recognition.Microphone") as mic:
            result = engine.recognize()
        mic.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)

    def test_shutdown_incomplete_not_ready_and_no_mic(self) -> None:
        class _FakeClient:
            def shutdown(self, *, budget_sec=None):
                return False

        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        engine._recognizer = object()
        engine._paraformer_client = _FakeClient()
        self.assertFalse(engine.shutdown())
        self.assertTrue(engine._shutdown_cancel.is_set())
        self.assertFalse(engine._permanently_closed)
        self.assertFalse(engine.is_ready())
        with patch("speech_recognition.Microphone") as mic:
            result = engine.recognize()
        mic.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)
        self.assertIsInstance(engine._paraformer_client, _FakeClient)


class LegacyExplicitEngineTests(unittest.TestCase):
    def test_speech_recognition_still_blocks_mic_without_cloud_stt(self) -> None:
        policy = SafetyPolicy({"permissions": {"cloud_stt": False, "file_delete": True}})
        engine = STTEngine(engine="speech_recognition", safety_policy=policy)
        engine._recognizer = object()
        with patch("speech_recognition.Microphone") as mic:
            result = engine.recognize()
        mic.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)
        self.assertIn("cloud_stt", result.error_detail or "")

    def test_injected_provider_skips_microphone_path(self) -> None:
        provider = MagicMock()
        provider.is_ready.return_value = True
        provider.recognize.return_value = RecognitionResult.success("ok")
        engine = STTEngine(provider=provider, safety_policy=SafetyPolicy())
        with patch("speech_recognition.Microphone") as mic:
            result = engine.recognize()
        mic.assert_not_called()
        provider.recognize.assert_called_once()
        self.assertEqual(result.status, RecognitionStatus.SUCCESS)


if __name__ == "__main__":
    unittest.main()
