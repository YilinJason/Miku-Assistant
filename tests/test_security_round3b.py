# -*- coding: utf-8 -*-
"""Round 3B：网络出口、远端模型端点与云 STT/TTS 许可——纯离线行为测试。"""
from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.assembly import assemble_default
from src.core.network_endpoint import (
    ModelEndpointKind,
    authorize_model_endpoint,
    classify_and_normalize_model_url,
    reject_redirect_status,
)
from src.core.safety import Operation, SafetyPolicy
from src.llm.ollama_client import OllamaClient
from src.llm.generate_result import GenerateStatus
from src.memory.errors import MemoryExtractionTransportError, MemoryValidationError
from src.memory.ollama_extractor import RequestsOllamaChatTransport
from src.voice.recognition import RecognitionStatus
from src.voice.stt import STTEngine
from src.voice.tts.engine import TTSEngine


def _clear_dry_run():
    return patch.dict(os.environ, {"MIKU_DRY_RUN": ""}, clear=False)


def _remote_allow_policy(**extra) -> SafetyPolicy:
    perms = {
        "remote_model_endpoint": True,
        "insecure_remote_model_http": True,
        "cloud_stt": True,
        "cloud_tts": True,
        "file_delete": True,
    }
    perms.update(extra)
    return SafetyPolicy({"permissions": perms})


class _Resp:
    def __init__(self, status_code: int = 200, payload=None, *, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {"done": True, "message": {"content": "ok"}}
        self.text = ""
        self.content = b""

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"status={self.status_code}")

    def json(self):
        return self._json

    def iter_lines(self):
        return iter([])


class UrlClassifyAuthorizeTests(unittest.TestCase):
    def test_loopback_variants_and_normalize(self) -> None:
        cases = [
            ("http://localhost:11434", "http://localhost:11434", ModelEndpointKind.LOOPBACK),
            ("HTTP://LOCALHOST:11434/", "http://localhost:11434", ModelEndpointKind.LOOPBACK),
            ("http://127.0.0.1:11434/", "http://127.0.0.1:11434", ModelEndpointKind.LOOPBACK),
            ("http://[::1]:11434", "http://[::1]:11434", ModelEndpointKind.LOOPBACK),
        ]
        for raw, expected, kind in cases:
            with self.subTest(raw=raw):
                norm, k = classify_and_normalize_model_url(raw)
                self.assertEqual(norm, expected)
                self.assertEqual(k, kind)
                sealed = authorize_model_endpoint(None, raw)
                self.assertEqual(sealed.base_url, expected)
                self.assertEqual(sealed.kind, kind)

    def test_remote_https_requires_permission(self) -> None:
        raw = "https://models.example.com"
        norm, kind = classify_and_normalize_model_url(raw)
        self.assertEqual(kind, ModelEndpointKind.REMOTE_HTTPS)
        with self.assertRaises(PermissionError):
            authorize_model_endpoint(None, raw)
        with self.assertRaises(PermissionError):
            authorize_model_endpoint(SafetyPolicy(), raw)
        sealed = authorize_model_endpoint(
            SafetyPolicy({"permissions": {"remote_model_endpoint": True}}), raw
        )
        self.assertEqual(sealed.base_url, norm)
        self.assertEqual(sealed.kind, ModelEndpointKind.REMOTE_HTTPS)

    def test_remote_http_needs_dual_permission(self) -> None:
        raw = "http://models.example.com:8080"
        with self.assertRaises(PermissionError):
            authorize_model_endpoint(
                SafetyPolicy({"permissions": {"remote_model_endpoint": True}}), raw
            )
        sealed = authorize_model_endpoint(_remote_allow_policy(), raw)
        self.assertEqual(sealed.kind, ModelEndpointKind.REMOTE_HTTP)

    def test_reject_userinfo_query_fragment_path_port_scheme(self) -> None:
        bad = [
            "http://user:pass@localhost:11434",
            "http://localhost:11434?token=x",
            "http://localhost:11434#frag",
            "http://localhost:11434/v1",
            "http://localhost:notaport",
            "http://localhost:",
            "http://127.0.0.1:",
            "http://[::1]:",
            "ftp://localhost:11434",
            "file:///tmp",
            "",
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    classify_and_normalize_model_url(raw)


class RedirectNoFollowTests(unittest.TestCase):
    def test_reject_redirect_helper(self) -> None:
        reject_redirect_status(200)
        reject_redirect_status(404)
        with self.assertRaises(PermissionError):
            reject_redirect_status(302)

    def _assert_post_no_redirect(self, call_kwargs) -> None:
        self.assertIn("allow_redirects", call_kwargs)
        self.assertIs(call_kwargs["allow_redirects"], False)

    def test_ollama_paths_302_no_second_request(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        posts = []

        def fake_post(url, **kwargs):
            posts.append({"url": url, **kwargs})
            return _Resp(302)

        client._http_post = fake_post  # type: ignore[attr-defined]

        self.assertFalse(client.warm_up())
        result = client.generate_result("hi")
        self.assertEqual(result.status, GenerateStatus.HTTP_ERROR)
        client._model_touch_succeeded = True
        self.assertFalse(client.release_owned_model())
        events = list(client.generate_stream("hi"))
        self.assertTrue(events)
        self.assertEqual(events[-1].status, GenerateStatus.HTTP_ERROR)

        for item in posts:
            self._assert_post_no_redirect(item)
        # 每次调用仅一次 POST，无跟随二次请求
        self.assertEqual(len(posts), 4)

    def test_tags_get_allow_redirects_false(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        gets = []

        def fake_get(url, **kwargs):
            gets.append(kwargs)
            return _Resp(302)

        client._http_get = fake_get  # type: ignore[attr-defined]
        self.assertFalse(client.check_connection())
        self.assertFalse(client.check_model_available())
        self.assertEqual(client.list_models(), [])
        for item in gets:
            self.assertIs(item.get("allow_redirects"), False)

    def test_memory_extractor_302_no_follow(self) -> None:
        posts = []

        class Sess:
            def post(self, url, **kwargs):
                posts.append(kwargs)
                return _Resp(302)

        transport = RequestsOllamaChatTransport(
            "http://127.0.0.1:9", timeout=1.0, session=Sess()
        )
        with self.assertRaises(MemoryExtractionTransportError):
            transport.post_chat({"model": "m", "messages": []})
        self.assertEqual(len(posts), 1)
        self.assertIs(posts[0].get("allow_redirects"), False)


class SharedPolicyAssembleTests(unittest.TestCase):
    def test_assemble_shares_policy_and_rejects_remote_before_http(self) -> None:
        posts = []

        class BoomClient:
            def __init__(self, **kwargs):
                posts.append("constructed")
                self.kwargs = kwargs

            def start_warmup(self, **kwargs):
                return False

            def wait_for_warmup(self, **kwargs):
                return True

            def model_touch_succeeded(self):
                return False

        yaml = (
            "ollama:\n"
            "  base_url: https://remote.example\n"
            "  model: m\n"
            "  timeout: 1\n"
            "security:\n"
            "  permissions:\n"
            "    remote_model_endpoint: false\n"
            "    cloud_stt: false\n"
            "    cloud_tts: false\n"
            "memory:\n"
            "  enabled: false\n"
            "resource:\n"
            "  profile: balanced\n"
            "  ollama:\n"
            "    warmup_on_start: false\n"
            "    unload_on_exit: false\n"
            "  rvc:\n"
            "    warmup_on_start: false\n"
        )
        with TemporaryDirectory() as tmp, _clear_dry_run():
            cfg = Path(tmp) / "c.yaml"
            cfg.write_text(yaml, encoding="utf-8")
            with self.assertRaises(PermissionError):
                assemble_default(
                    str(cfg),
                    startup_mode="text",
                    ollama_client_cls=BoomClient,
                    start_cleaner=False,
                )
            self.assertEqual(posts, [])

    def test_memory_override_cannot_bypass_remote(self) -> None:
        yaml = (
            "ollama:\n"
            "  base_url: http://127.0.0.1:9\n"
            "  model: m\n"
            "  timeout: 1\n"
            "security:\n"
            "  permissions:\n"
            "    remote_model_endpoint: false\n"
            "memory:\n"
            "  enabled: true\n"
            "  db_path: mem.sqlite\n"
            "  extractor:\n"
            "    base_url: https://evil.example\n"
            "resource:\n"
            "  profile: balanced\n"
            "  ollama:\n"
            "    warmup_on_start: false\n"
            "  rvc:\n"
            "    warmup_on_start: false\n"
        )
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            cfg = root / "c.yaml"
            cfg.write_text(yaml, encoding="utf-8")
            # rewrite db into tmp via resolve — use absolute in yaml
            cfg.write_text(
                yaml.replace("db_path: mem.sqlite", f"db_path: {root / 'mem.sqlite'}"),
                encoding="utf-8",
            )

            class FakeOllama:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs

                def start_warmup(self, **kwargs):
                    return False

                def wait_for_warmup(self, **kwargs):
                    return True

                def model_touch_succeeded(self):
                    return False

            with self.assertRaises(PermissionError):
                assemble_default(
                    str(cfg),
                    startup_mode="text",
                    ollama_client_cls=FakeOllama,
                    start_cleaner=False,
                )

    def test_audit_summary_omits_secrets_and_text_mode_skips_voice(self) -> None:
        logs: list[str] = []

        class Handler(logging.Handler):
            def emit(self, record):
                logs.append(record.getMessage())

        handler = Handler()
        logger = logging.getLogger("src.core.assembly")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            yaml = (
                "ollama:\n"
                "  base_url: http://127.0.0.1:9\n"
                "  model: secret-model\n"
                "  timeout: 1\n"
                "security:\n"
                "  permissions:\n"
                "    cloud_stt: true\n"
                "    cloud_tts: false\n"
                "memory:\n"
                "  enabled: false\n"
                "voice:\n"
                "  stt:\n"
                "    engine: speech_recognition\n"
                "  tts:\n"
                "    engine: edge-tts\n"
                "resource:\n"
                "  profile: balanced\n"
                "  ollama:\n"
                "    warmup_on_start: false\n"
                "  rvc:\n"
                "    warmup_on_start: false\n"
            )
            with TemporaryDirectory() as tmp, _clear_dry_run():
                cfg = Path(tmp) / "c.yaml"
                cfg.write_text(yaml, encoding="utf-8")

                class FakeOllama:
                    def __init__(self, **kwargs):
                        self.kwargs = kwargs

                    def start_warmup(self, **kwargs):
                        return False

                    def wait_for_warmup(self, **kwargs):
                        return True

                    def model_touch_succeeded(self):
                        return False

                voice_built = {"n": 0}

                def boom_tts(*a, **k):
                    voice_built["n"] += 1
                    raise AssertionError("text mode must not build TTS")

                def boom_stt(*a, **k):
                    voice_built["n"] += 1
                    raise AssertionError("text mode must not build STT")

                parts = assemble_default(
                    str(cfg),
                    startup_mode="text",
                    ollama_client_cls=FakeOllama,
                    tts_engine_cls=boom_tts,
                    stt_engine_cls=boom_stt,
                    start_cleaner=False,
                )
                self.assertIsNone(parts.tts_engine)
                self.assertIsNone(parts.stt_engine)
                self.assertEqual(voice_built["n"], 0)
                joined = "\n".join(logs)
                self.assertIn("endpoint_kind=loopback", joined)
                self.assertIn("cloud_stt=True", joined)
                self.assertIn("cloud_tts=False", joined)
                self.assertNotIn("127.0.0.1:9", joined)
                self.assertNotIn("http://", joined)
                parts.temp_file_manager.shutdown(timeout=0.2)
        finally:
            logger.removeHandler(handler)


class CloudSttGateTests(unittest.TestCase):
    def test_deny_before_microphone_and_google(self) -> None:
        policy = SafetyPolicy({"permissions": {"cloud_stt": False}})
        with patch.object(STTEngine, "_initialize_engine"):
            engine = STTEngine(
                engine="speech_recognition", safety_policy=policy
            )
        engine._recognizer = MagicMock()
        mic_calls = []

        class BoomMic:
            def __init__(self, *a, **k):
                mic_calls.append("open")
                raise AssertionError("mic must not open")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        sr_mod = SimpleNamespace(
            Microphone=BoomMic,
            Recognizer=MagicMock,
            WaitTimeoutError=type("W", (Exception,), {}),
            UnknownValueError=type("U", (Exception,), {}),
            RequestError=type("R", (Exception,), {}),
        )
        with patch.dict(sys.modules, {"speech_recognition": sr_mod}):
            result = engine.recognize()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)
        self.assertEqual(mic_calls, [])
        engine._recognizer.recognize_google.assert_not_called()

    def test_allow_calls_fake_google(self) -> None:
        policy = SafetyPolicy({"permissions": {"cloud_stt": True}})
        with patch.object(STTEngine, "_initialize_engine"):
            engine = STTEngine(
                engine="speech_recognition", safety_policy=policy
            )

        class Audio:
            frame_data = b"x" * 32000
            sample_rate = 16000
            sample_width = 2

            def get_wav_data(self):
                return b"RIFF"

        class Mic:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        recognizer = MagicMock()
        recognizer.listen.return_value = Audio()
        recognizer.recognize_google.return_value = "你好"
        engine._recognizer = recognizer
        engine._noise_adjusted = True
        sr_mod = SimpleNamespace(
            Microphone=lambda: Mic(),
            WaitTimeoutError=type("W", (Exception,), {}),
            UnknownValueError=type("U", (Exception,), {}),
            RequestError=type("R", (Exception,), {}),
        )
        with patch.dict(sys.modules, {"speech_recognition": sr_mod}):
            result = engine.recognize()
        self.assertEqual(result.status, RecognitionStatus.SUCCESS)
        recognizer.recognize_google.assert_called_once()

    def test_whisper_import_fail_no_fallback_without_cloud(self) -> None:
        policy = SafetyPolicy({"permissions": {"cloud_stt": False}})
        real_import = __import__

        def fake_import(name, *a, **k):
            if name == "whisper" or name.startswith("whisper."):
                raise ImportError("no whisper")
            if name == "speech_recognition":
                raise AssertionError("must not import sr for google fallback")
            return real_import(name, *a, **k)

        with patch("builtins.__import__", side_effect=fake_import):
            engine = STTEngine(engine="whisper", safety_policy=policy)
        self.assertIsNone(engine._recognizer)
        self.assertEqual(engine.engine_type, "whisper")
        self.assertFalse(engine.is_ready())

    def test_whisper_import_fail_fallback_when_cloud_allowed(self) -> None:
        policy = SafetyPolicy({"permissions": {"cloud_stt": True}})
        real_import = __import__

        class FakeSR:
            class Recognizer:
                def __init__(self):
                    self.energy_threshold = 0
                    self.dynamic_energy_threshold = False
                    self.pause_threshold = 0
                    self.non_speaking_duration = 0

        def fake_import(name, *a, **k):
            if name == "whisper" or name.startswith("whisper."):
                raise ImportError("no whisper")
            if name == "speech_recognition":
                return FakeSR
            return real_import(name, *a, **k)

        with patch("builtins.__import__", side_effect=fake_import):
            engine = STTEngine(engine="whisper", safety_policy=policy)
        self.assertEqual(engine.engine_type, "speech_recognition")
        self.assertIsNotNone(engine._recognizer)


class CloudTtsGateTests(unittest.TestCase):
    def _engine(self, policy: SafetyPolicy, **kwargs) -> TTSEngine:
        with patch("src.voice.tts.engine.AudioPlayer"), patch(
            "src.voice.rvc.infer.stop_rvc_worker"
        ), patch(
            "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
        ), patch.object(TTSEngine, "_initialize_engine"):
            engine = TTSEngine(
                engine="edge-tts",
                fallback_enabled=kwargs.get("fallback_enabled", True),
                pipeline_enabled=False,
                rvc_config={},
                safety_policy=policy,
                warm_rvc_on_init=False,
            )
        engine.engine_type = "edge-tts"
        return engine

    def test_edge_allowed_calls_communicate(self) -> None:
        policy = SafetyPolicy(
            {"permissions": {"cloud_tts": True, "file_delete": True}}
        )
        with TemporaryDirectory() as tmp:
            policy.register_file_delete_dir(tmp)
            engine = self._engine(policy)
            engine._temp_files = __import__(
                "src.voice.audio.temp_files", fromlist=["TempFileManager"]
            ).TempFileManager(safety_policy=policy, default_dir=Path(tmp))

            async def ok_save(path):
                Path(path).write_bytes(b"mp3")

            with patch("edge_tts.Communicate") as comm:
                inst = MagicMock()
                inst.save = ok_save
                comm.return_value = inst
                import asyncio

                path = asyncio.run(engine._edge_tts_to_mp3("hi"))
                self.assertTrue(Path(path).is_file())
                self.assertEqual(comm.call_count, 1)
                engine._temp_files.remove(path)
            self.assertEqual(engine._temp_files.owned_count(), 0)
            engine._temp_files.shutdown(timeout=0.2)

    def test_deny_with_local_fallback_zero_communicate(self) -> None:
        policy = SafetyPolicy(
            {"permissions": {"cloud_tts": False, "file_delete": True}}
        )
        engine = self._engine(policy, fallback_enabled=True)
        py_calls = []

        def fake_pyttsx(text, async_mode=False):
            py_calls.append(text)

        engine._speak_pyttsx3 = fake_pyttsx  # type: ignore[method-assign]
        engine._ensure_pyttsx3_fallback = lambda: True  # type: ignore[method-assign]
        with patch("edge_tts.Communicate") as comm:
            engine.speak("hello cloud denied")
            self.assertEqual(comm.call_count, 0)
        self.assertEqual(py_calls, ["hello cloud denied"])

    def test_deny_without_fallback_zero_communicate(self) -> None:
        policy = SafetyPolicy(
            {"permissions": {"cloud_tts": False, "file_delete": True}}
        )
        engine = self._engine(policy, fallback_enabled=False)
        with patch("edge_tts.Communicate") as comm:
            engine.speak("no fallback")
            self.assertEqual(comm.call_count, 0)

    def test_save_to_file_denied_without_fallback(self) -> None:
        policy = SafetyPolicy({"permissions": {"cloud_tts": False}})
        engine = self._engine(policy, fallback_enabled=False)
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.mp3"
            with patch("edge_tts.Communicate") as comm:
                with self.assertRaises(PermissionError):
                    engine.save_to_file("x", str(out))
                self.assertEqual(comm.call_count, 0)

    def test_pipeline_denied_zero_communicate_and_temp_zero(self) -> None:
        policy = SafetyPolicy(
            {"permissions": {"cloud_tts": False, "file_delete": True}}
        )
        with TemporaryDirectory() as tmp:
            policy.register_file_delete_dir(tmp)
            engine = self._engine(policy, fallback_enabled=False)
            from src.voice.audio.temp_files import TempFileManager

            mgr = TempFileManager(safety_policy=policy, default_dir=Path(tmp))
            engine._temp_files = mgr
            engine._owns_temp_files = False
            engine.pipeline_enabled = True
            with patch("edge_tts.Communicate") as comm:
                engine.speak("第一句。第二句。第三句。")
                self.assertEqual(comm.call_count, 0)
            self.assertEqual(mgr.owned_count(), 0)
            mgr.shutdown(timeout=0.2)


class DryRunNetworkTests(unittest.TestCase):
    def test_dry_run_denies_remote_and_cloud(self) -> None:
        with patch.dict(os.environ, {"MIKU_DRY_RUN": "1"}, clear=False):
            policy = SafetyPolicy(
                {
                    "permissions": {
                        "remote_model_endpoint": True,
                        "insecure_remote_model_http": True,
                        "cloud_stt": True,
                        "cloud_tts": True,
                    }
                }
            )
            self.assertFalse(policy.is_allowed(Operation.REMOTE_MODEL_ENDPOINT))
            self.assertFalse(policy.is_allowed(Operation.INSECURE_REMOTE_MODEL_HTTP))
            self.assertFalse(policy.is_allowed(Operation.CLOUD_STT))
            self.assertFalse(policy.is_allowed(Operation.CLOUD_TTS))
            # loopback 仍可用
            sealed = authorize_model_endpoint(policy, "http://localhost:11434")
            self.assertEqual(sealed.kind, ModelEndpointKind.LOOPBACK)
            with self.assertRaises(PermissionError):
                authorize_model_endpoint(policy, "https://remote.example")

    def test_bare_policy_defaults_deny_cloud_and_remote(self) -> None:
        bare = SafetyPolicy()
        self.assertFalse(bare.is_allowed(Operation.CLOUD_STT))
        self.assertFalse(bare.is_allowed(Operation.CLOUD_TTS))
        self.assertFalse(bare.is_allowed(Operation.REMOTE_MODEL_ENDPOINT))
        app = SafetyPolicy.from_app_config(
            SimpleNamespace(get=lambda key, default=None: default)
        )
        self.assertFalse(app.is_allowed(Operation.CLOUD_STT))
        self.assertTrue(app.is_allowed(Operation.CLOUD_TTS))
        self.assertTrue(app.is_allowed(Operation.LOCAL_ASR_WORKER))
        self.assertFalse(app.is_allowed(Operation.REMOTE_MODEL_ENDPOINT))


class DirectConstructFailClosedTests(unittest.TestCase):
    def test_ollama_remote_without_policy_fails(self) -> None:
        with self.assertRaises(PermissionError):
            OllamaClient(base_url="https://remote.example", model="m")

    def test_transport_remote_without_policy_fails(self) -> None:
        with self.assertRaises(MemoryValidationError):
            RequestsOllamaChatTransport("http://evil.example", timeout=1.0)


if __name__ == "__main__":
    unittest.main()
