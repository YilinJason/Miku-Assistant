# -*- coding: utf-8 -*-
"""Round 3B Codex Review：F1–F5 反例与回归（纯离线）。"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src.core.network_endpoint import (
    ModelEndpointKind,
    SealedModelEndpoint,
    authorize_model_endpoint,
    classify_and_normalize_model_url,
    reseal_model_endpoint,
)
from src.core.safety import SafetyPolicy
from src.llm.generate_result import GenerateStatus
from src.llm.ollama_client import OllamaClient
from src.llm.stream_events import StreamEventKind
from src.memory.errors import MemoryExtractionTransportError, MemoryValidationError
from src.memory.ollama_extractor import RequestsOllamaChatTransport
from src.memory.diagnostic import (
    run_diagnostic,
    run_dry_run,
    run_execute,
)
from src.memory.runtime import MemoryRuntime, MemorySettings


def _clear_dry_run():
    return patch.dict(os.environ, {"MIKU_DRY_RUN": ""}, clear=False)


def _remote_https_policy() -> SafetyPolicy:
    return SafetyPolicy(
        {
            "permissions": {
                "remote_model_endpoint": True,
                "insecure_remote_model_http": False,
                "cloud_stt": False,
                "cloud_tts": False,
            }
        }
    )


class _Cfg:
    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        cur: object = self._data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur if cur is not None else default


class F1ForgedSealedEndpointTests(unittest.TestCase):
    def test_forged_loopback_kind_rejected_by_ollama_client(self) -> None:
        forged = SealedModelEndpoint(
            "https://evil.example", ModelEndpointKind.LOOPBACK
        )
        with self.assertRaises(PermissionError):
            OllamaClient(model="m", sealed_endpoint=forged)
        with self.assertRaises(PermissionError):
            OllamaClient(
                model="m",
                sealed_endpoint=forged,
                safety_policy=SafetyPolicy(),
            )

    def test_forged_loopback_kind_rejected_by_memory_transport(self) -> None:
        forged = SealedModelEndpoint(
            "https://evil.example", ModelEndpointKind.LOOPBACK
        )
        with self.assertRaises(MemoryValidationError):
            RequestsOllamaChatTransport(
                "http://localhost", sealed_endpoint=forged
            )
        with self.assertRaises(MemoryValidationError):
            RequestsOllamaChatTransport(
                "http://127.0.0.1:9",
                sealed_endpoint=forged,
                safety_policy=SafetyPolicy(),
            )

    def test_reseal_rejects_kind_mismatch_even_with_remote_permission(self) -> None:
        forged = SealedModelEndpoint(
            "https://evil.example", ModelEndpointKind.LOOPBACK
        )
        policy = _remote_https_policy()
        with self.assertRaises(PermissionError):
            reseal_model_endpoint(policy, forged, context="test")
        # 正当封存可通过
        ok = authorize_model_endpoint(policy, "https://evil.example")
        resealed = reseal_model_endpoint(policy, ok, context="test")
        self.assertEqual(resealed.kind, ModelEndpointKind.REMOTE_HTTPS)

    def test_memory_runtime_forged_sealed_fails_before_http(self) -> None:
        forged = SealedModelEndpoint(
            "https://evil.example", ModelEndpointKind.LOOPBACK
        )
        posts = []

        class BoomSess:
            def post(self, *a, **k):
                posts.append(1)
                raise AssertionError("must not HTTP")

        with TemporaryDirectory() as tmp:
            settings = MemorySettings(
                enabled=True,
                db_path=Path(tmp) / "m.sqlite",
                max_entries=8,
                max_chars=200,
                candidate_limit=20,
                queue_capacity=8,
                max_extractions_per_turn=4,
                close_drain=True,
                close_timeout=1.0,
                extractor_base_url="http://127.0.0.1:9",
                extractor_model="m",
                extractor_timeout=1.0,
                extractor_keep_alive="1m",
            )
            with self.assertRaises((PermissionError, MemoryValidationError)):
                MemoryRuntime.create_from_settings(
                    settings,
                    safety_policy=SafetyPolicy(),
                    sealed_endpoint=forged,
                )
            self.assertEqual(posts, [])


class F2NoTypeErrorRedirectRetryTests(unittest.TestCase):
    def test_post_internal_typeerror_called_once(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        calls = []

        def boom_post(url, **kwargs):
            calls.append(dict(kwargs))
            self.assertIs(kwargs.get("allow_redirects"), False)
            raise TypeError("adapter internal boom")

        client._http_post = boom_post  # type: ignore[attr-defined]
        self.assertFalse(client.warm_up())
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].get("allow_redirects"), False)

    def test_get_internal_typeerror_called_once(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        calls = []

        def boom_get(url, **kwargs):
            calls.append(dict(kwargs))
            self.assertIs(kwargs.get("allow_redirects"), False)
            raise TypeError("adapter internal boom")

        client._http_get = boom_get  # type: ignore[attr-defined]
        self.assertFalse(client.check_connection())
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].get("allow_redirects"), False)


class F3DiagnosticSharedPolicyTests(unittest.TestCase):
    def _cfg(self, *, remote: bool = False, insecure: bool = False, url: str = "http://127.0.0.1:9"):
        return _Cfg(
            {
                "ollama": {
                    "base_url": url,
                    "model": "m",
                    "timeout": 1,
                    "keep_alive": "30m",
                },
                "memory": {
                    "enabled": False,
                    "extractor": {
                        "base_url": None,
                        "model": None,
                        "timeout": None,
                        "keep_alive": None,
                        "num_ctx": None,
                        "num_predict": 64,
                    },
                },
                "security": {
                    "permissions": {
                        "remote_model_endpoint": remote,
                        "insecure_remote_model_http": insecure,
                        "cloud_stt": False,
                        "cloud_tts": False,
                        "rvc_worker": False,
                        "rvc_infer": False,
                        "rvc_probe": False,
                        "file_delete": True,
                    }
                },
            }
        )

    def test_from_app_config_allows_remote_https_dry_run(self) -> None:
        with _clear_dry_run():
            cfg = self._cfg(remote=True, url="https://models.example")
            report = run_dry_run(cfg)
            self.assertEqual(report.endpoint["host"], "models.example")
            self.assertIsNone(report.endpoint.get("port"))

    def test_remote_denied_before_temp_or_http(self) -> None:
        with _clear_dry_run():
            cfg = self._cfg(remote=False, url="https://models.example")
            side = {"temp": 0, "http": 0}

            def boom_tmp():
                side["temp"] += 1
                raise AssertionError("temp must not be created")

            def boom_transport():
                side["http"] += 1
                raise AssertionError("http must not start")

            with self.assertRaises(MemoryValidationError):
                run_execute(
                    cfg,
                    temp_dir_factory=boom_tmp,
                    transport_factory=boom_transport,
                )
            self.assertEqual(side, {"temp": 0, "http": 0})

            with self.assertRaises(MemoryValidationError):
                run_diagnostic(cfg, execute=False, base_url="https://evil.example")

    def test_remote_http_requires_dual_permission(self) -> None:
        with _clear_dry_run():
            only_remote = self._cfg(
                remote=True, insecure=False, url="http://models.example:8080"
            )
            with self.assertRaises(MemoryValidationError):
                run_dry_run(only_remote)
            dual = self._cfg(
                remote=True, insecure=True, url="http://models.example:8080"
            )
            report = run_dry_run(dual)
            self.assertEqual(report.endpoint["host"], "models.example")
            self.assertEqual(report.endpoint["port"], 8080)

    def test_execute_shares_policy_with_fake_transport_and_llm(self) -> None:
        with _clear_dry_run(), TemporaryDirectory() as tmp:
            cfg = self._cfg(remote=True, url="https://models.example")
            policy = SafetyPolicy.from_app_config(cfg)
            self.assertTrue(policy.is_allowed("remote_model_endpoint"))
            seen = {"transport": 0}

            class FakeTransport:
                def post_chat(self, payload):
                    seen["transport"] += 1
                    return {"message": {"content": "[]"}, "done": True}

            from src.memory import diagnostic as diag

            captured_default: dict = {}

            class FakeLLM:
                def __init__(self, **kwargs):
                    pass

                def generate_result(self, *a, **k):
                    from src.llm.generate_result import GenerateResult

                    return GenerateResult.success("ok")

                def generate(self, *a, **k):
                    return "ok"

            def tracking_default(endpoint, *, safety_policy=None):
                captured_default["policy"] = safety_policy
                captured_default["url"] = endpoint.base_url
                return FakeLLM()

            def make_tmp():
                p = Path(tmp) / "diag"
                p.mkdir(parents=True, exist_ok=True)
                return p

            with patch.object(diag, "_default_llm_factory", tracking_default), patch.object(
                diag, "_run_wrapper_roundtrip"
            ) as wrap, patch.object(diag, "_run_grounding_session_check") as ground, patch.object(
                diag, "_run_extractor_scenario"
            ) as scen:
                wrap.return_value = SimpleNamespace(
                    to_public_dict=lambda: {"status": "ok"}
                )
                ground.return_value = SimpleNamespace(
                    to_public_dict=lambda: {"status": "ok"}
                )
                scen.return_value = SimpleNamespace(
                    to_public_dict=lambda: {"scenario_id": "x", "status": "ok"}
                )
                report = run_execute(
                    cfg,
                    safety_policy=policy,
                    temp_dir_factory=make_tmp,
                    transport_factory=lambda: FakeTransport(),
                )
            self.assertIs(captured_default.get("policy"), policy)
            self.assertEqual(captured_default.get("url"), "https://models.example")
            self.assertFalse(report.production_db_path_touched)
            self.assertTrue(report.temp_db_used)


class F4EmptyPortRejectionTests(unittest.TestCase):
    def test_explicit_empty_port_table(self) -> None:
        bad = [
            "http://localhost:",
            "http://127.0.0.1:",
            "http://[::1]:",
            "HTTPS://LOCALHOST:",
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    classify_and_normalize_model_url(raw)

        # 合法：无端口 / 有端口 / IPv6 带端口（内部冒号不得误拒）
        ok = [
            ("http://localhost", "http://localhost"),
            ("http://localhost:11434", "http://localhost:11434"),
            ("http://[::1]", "http://[::1]"),
            ("http://[::1]:11434", "http://[::1]:11434"),
            ("http://127.0.0.1:9", "http://127.0.0.1:9"),
        ]
        for raw, expected in ok:
            with self.subTest(raw=raw):
                norm, kind = classify_and_normalize_model_url(raw)
                self.assertEqual(norm, expected)
                self.assertEqual(kind, ModelEndpointKind.LOOPBACK)


class _Closeable302:
    def __init__(self, *, close_error: BaseException | None = None):
        self.status_code = 302
        self.close_calls = 0
        self._close_error = close_error
        self.closed = False

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self._close_error is not None:
            raise self._close_error

    def raise_for_status(self) -> None:
        raise AssertionError("redirect must not reach raise_for_status")

    def iter_lines(self):
        raise AssertionError("redirect must not be read")


class F5RedirectClosesResponseTests(unittest.TestCase):
    def test_generate_stream_302_closes_once_no_body_no_history(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        client._history.commit_turn("u0", "a0")
        before = list(client.conversation_history)
        resp = _Closeable302()
        posts = []

        def fake_post(url, **kwargs):
            posts.append(kwargs)
            self.assertIs(kwargs.get("allow_redirects"), False)
            self.assertTrue(kwargs.get("stream"))
            return resp

        client._http_post = fake_post  # type: ignore[attr-defined]
        events = list(client.generate_stream("hi"))
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, StreamEventKind.FAILED)
        self.assertEqual(events[0].status, GenerateStatus.HTTP_ERROR)
        self.assertFalse(events[0].text)
        self.assertFalse(events[0].delta)
        self.assertEqual(client.conversation_history, before)
        self.assertEqual(resp.close_calls, 1)
        self.assertTrue(resp.closed)

    def test_get_302_closes_before_false_single_request(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        resp = _Closeable302()
        gets = []

        def fake_get(url, **kwargs):
            gets.append(kwargs)
            self.assertIs(kwargs.get("allow_redirects"), False)
            return resp

        client._http_get = fake_get  # type: ignore[attr-defined]
        self.assertFalse(client.check_connection())
        self.assertEqual(len(gets), 1)
        self.assertEqual(resp.close_calls, 1)

    def test_memory_transport_302_closes_before_error_single_post(self) -> None:
        posts = []
        resp = _Closeable302()
        case = self

        class Sess:
            def post(self, url, **kwargs):
                posts.append(kwargs)
                case.assertIs(kwargs.get("allow_redirects"), False)
                return resp

        transport = RequestsOllamaChatTransport(
            "http://127.0.0.1:9", timeout=1.0, session=Sess()
        )
        with self.assertRaises(MemoryExtractionTransportError) as ctx:
            transport.post_chat({"model": "m", "messages": []})
        self.assertEqual(ctx.exception.error_kind, "http")
        self.assertIn("redirect", str(ctx.exception).lower())
        self.assertEqual(len(posts), 1)
        self.assertEqual(resp.close_calls, 1)

    def test_close_failure_preserves_redirect_rejection_no_second_http(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        resp = _Closeable302(close_error=RuntimeError("close-boom"))
        calls = []

        def fake_post(url, **kwargs):
            calls.append(1)
            return resp

        client._http_post = fake_post  # type: ignore[attr-defined]
        with self.assertRaises(PermissionError) as ctx:
            client._request_post("http://127.0.0.1:9/api/chat", json={})
        self.assertIn("redirect 302", str(ctx.exception))
        self.assertEqual(len(calls), 1)
        self.assertEqual(resp.close_calls, 1)

        # memory：close 抛错仍映射为 redirect transport error，无第二次 POST
        posts = []
        resp2 = _Closeable302(close_error=RuntimeError("close-boom"))

        class Sess:
            def post(self, url, **kwargs):
                posts.append(1)
                return resp2

        transport = RequestsOllamaChatTransport(
            "http://127.0.0.1:9", timeout=1.0, session=Sess()
        )
        with self.assertRaises(MemoryExtractionTransportError) as ctx2:
            transport.post_chat({"model": "m", "messages": []})
        self.assertEqual(ctx2.exception.error_kind, "http")
        self.assertEqual(len(posts), 1)
        self.assertEqual(resp2.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
