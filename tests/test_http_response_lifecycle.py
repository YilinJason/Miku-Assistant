"""HTTP 响应显式回收：离线计数 fake；不连真实 Ollama / memory DB。"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any, Iterator, List, Optional
from unittest.mock import patch

import requests

from src.core.network_endpoint import authorize_model_endpoint
from src.llm.generate_result import GenerateStatus
from src.llm.ollama_client import OllamaClient
from src.llm.stream_events import StreamEventKind
from src.memory.errors import MemoryExtractionResponseError, MemoryExtractionTransportError
from src.memory.ollama_extractor import RequestsOllamaChatTransport


class CountingResponse:
    """记录 ``close`` 次数；可选模拟 close 失败或无 close 方法。"""

    instances: List["CountingResponse"] = []

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: Any = None,
        lines: Optional[List[Any]] = None,
        close_error: Optional[BaseException] = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self._lines = lines or []
        self._close_error = close_error
        self.close_count = 0
        CountingResponse.instances.append(self)

    @classmethod
    def reset_instances(cls) -> None:
        cls.instances.clear()

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code}", response=self
            )

    def json(self) -> Any:
        if isinstance(self._json, BaseException):
            raise self._json
        if self._json is None:
            raise requests.exceptions.JSONDecodeError("bad", "{}", 0)
        return self._json

    def iter_lines(self) -> Iterator[bytes]:
        for line in self._lines:
            if isinstance(line, bytes):
                yield line
            elif isinstance(line, str):
                yield line.encode("utf-8")
            else:
                yield json.dumps(line, ensure_ascii=False).encode("utf-8")

    def close(self) -> None:
        self.close_count += 1
        if self._close_error is not None:
            raise self._close_error


class OllamaClientResponseLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        CountingResponse.reset_instances()

    def _client(self) -> OllamaClient:
        return OllamaClient(base_url="http://127.0.0.1:9", model="test-model")

    def _last_resp(self) -> CountingResponse:
        self.assertTrue(CountingResponse.instances)
        return CountingResponse.instances[-1]

    def test_warm_up_success_closes_once_and_touches(self) -> None:
        client = self._client()

        def post(url: str, **kwargs: Any) -> CountingResponse:
            return CountingResponse(json_data={"done": True})

        with patch.object(client, "_http_post", side_effect=post, create=True):
            self.assertTrue(client.warm_up(timeout=1))
        resp = self._last_resp()
        self.assertEqual(resp.close_count, 1)
        self.assertTrue(client.model_touch_succeeded())

    def test_warm_up_http_error_closes(self) -> None:
        client = self._client()

        def post(url: str, **kwargs: Any) -> CountingResponse:
            return CountingResponse(status_code=500, json_data={})

        with patch.object(client, "_http_post", side_effect=post, create=True):
            self.assertFalse(client.warm_up(timeout=1))
        self.assertEqual(self._last_resp().close_count, 1)
        self.assertFalse(client.model_touch_succeeded())

    def test_warm_up_invalid_done_closes(self) -> None:
        client = self._client()

        def post(url: str, **kwargs: Any) -> CountingResponse:
            return CountingResponse(json_data={"done": False})

        with patch.object(client, "_http_post", side_effect=post, create=True):
            self.assertFalse(client.warm_up(timeout=1))
        self.assertEqual(self._last_resp().close_count, 1)

    def test_generate_result_success_non_stream_closes_and_commits(self) -> None:
        client = self._client()
        resp = CountingResponse(
            json_data={"message": {"content": "hi"}, "done": True}
        )

        with patch.object(client, "_http_post", return_value=resp, create=True):
            result = client.generate_result("u")
        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(resp.close_count, 1)
        self.assertEqual(len(client.conversation_history), 2)

    def test_generate_result_http_error_no_commit(self) -> None:
        client = self._client()
        resp = CountingResponse(status_code=503, json_data={})

        with patch.object(client, "_http_post", return_value=resp, create=True):
            result = client.generate_result("u")
        self.assertEqual(result.status, GenerateStatus.HTTP_ERROR)
        self.assertEqual(resp.close_count, 1)
        self.assertEqual(client.conversation_history, [])

    def test_generate_result_json_error_closes(self) -> None:
        client = self._client()
        resp = CountingResponse(
            json_data=requests.exceptions.JSONDecodeError("x", "y", 0)
        )

        with patch.object(client, "_http_post", return_value=resp, create=True):
            result = client.generate_result("u")
        self.assertEqual(result.status, GenerateStatus.RESPONSE_ERROR)
        self.assertEqual(resp.close_count, 1)

    def test_generate_result_stream_aggregate_closes(self) -> None:
        client = self._client()
        lines = [
            {"message": {"content": "a"}, "done": False},
            {"message": {"content": "b"}, "done": True},
        ]
        resp = CountingResponse(lines=lines)

        with patch.object(client, "_http_post", return_value=resp, create=True):
            result = client.generate_result("u", stream=True)
        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.text, "ab")
        self.assertEqual(resp.close_count, 1)

    def test_generate_result_truncated_aggregate_stream_still_closes(self) -> None:
        client = self._client()
        resp = CountingResponse(
            lines=[{"message": {"content": "part"}, "done": False}]
        )

        with patch.object(client, "_http_post", return_value=resp, create=True):
            result = client.generate_result("u", stream=True)
        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.text, "part")
        self.assertEqual(resp.close_count, 1)

    def test_generate_result_redirect_closes(self) -> None:
        client = self._client()
        resp = CountingResponse(status_code=302, json_data={})

        with patch.object(client, "_http_post", return_value=resp, create=True):
            result = client.generate_result("u")
        self.assertEqual(result.status, GenerateStatus.HTTP_ERROR)
        self.assertEqual(resp.close_count, 1)

    def test_post_before_response_no_close(self) -> None:
        client = self._client()

        def boom(url: str, **kwargs: Any) -> CountingResponse:
            raise requests.exceptions.ConnectionError("down")

        with patch.object(client, "_http_post", side_effect=boom, create=True):
            result = client.generate_result("u")
        self.assertEqual(result.status, GenerateStatus.CONNECTION_ERROR)
        self.assertEqual(CountingResponse.instances, [])

    def test_close_failure_does_not_change_success(self) -> None:
        client = self._client()
        resp = CountingResponse(
            json_data={"message": {"content": "ok"}},
            close_error=OSError("disk"),
        )

        with patch.object(client, "_http_post", return_value=resp, create=True):
            result = client.generate_result("u")
        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(resp.close_count, 1)

    def test_no_close_method_still_succeeds(self) -> None:
        client = self._client()
        resp = SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"message": {"content": "ok"}},
        )

        with patch.object(client, "_http_post", return_value=resp, create=True):
            result = client.generate_result("u")
        self.assertEqual(result.status, GenerateStatus.SUCCESS)

    def test_response_validation_fatal_preserved(self) -> None:
        client = self._client()
        resp = CountingResponse(
            json_data={"message": {"content": 123}},
        )

        with patch.object(client, "_http_post", return_value=resp, create=True):
            result = client.generate_result("u")
        self.assertEqual(result.status, GenerateStatus.RESPONSE_ERROR)
        self.assertIn("str", result.error_detail)
        self.assertEqual(resp.close_count, 1)

    def test_generate_result_json_keyerror_closes(self) -> None:
        client = self._client()
        fatal = KeyError("missing")
        resp = CountingResponse(json_data=fatal)

        with patch.object(client, "_http_post", return_value=resp, create=True):
            result = client.generate_result("u")
        self.assertEqual(result.status, GenerateStatus.RESPONSE_ERROR)
        self.assertEqual(resp.close_count, 1)

    def test_generate_result_json_systemexit_propagates_same_object(self) -> None:
        client = self._client()
        fatal = SystemExit(42)
        resp = CountingResponse(json_data=fatal)

        with patch.object(client, "_http_post", return_value=resp, create=True):
            with self.assertRaises(SystemExit) as ctx:
                client.generate_result("u")
        self.assertIs(ctx.exception, fatal)
        self.assertEqual(resp.close_count, 1)

    def test_generate_result_json_generator_exit_propagates_same_object(self) -> None:
        client = self._client()
        fatal = GeneratorExit()
        resp = CountingResponse(json_data=fatal)

        with patch.object(client, "_http_post", return_value=resp, create=True):
            with self.assertRaises(GeneratorExit) as ctx:
                client.generate_result("u")
        self.assertIs(ctx.exception, fatal)
        self.assertEqual(resp.close_count, 1)

    def test_generate_result_json_custom_baseexception_propagates(self) -> None:
        class _CustomFatal(BaseException):
            pass

        client = self._client()
        fatal = _CustomFatal("boom")
        resp = CountingResponse(json_data=fatal)

        with patch.object(client, "_http_post", return_value=resp, create=True):
            with self.assertRaises(_CustomFatal) as ctx:
                client.generate_result("u")
        self.assertIs(ctx.exception, fatal)
        self.assertEqual(resp.close_count, 1)

    def test_generate_stream_early_close_exactly_once(self) -> None:
        client = self._client()
        lines = [
            {"message": {"content": "x"}, "done": False},
            {"message": {"content": ""}, "done": True},
        ]
        resp = CountingResponse(lines=lines)

        with patch.object(client, "_http_post", return_value=resp, create=True):
            it = client.generate_stream("u")
            ev = next(it)
            self.assertEqual(ev.kind, StreamEventKind.TEXT_DELTA)
            it.close()
        self.assertEqual(resp.close_count, 1)
        self.assertEqual(client.conversation_history, [])

    def test_release_owned_model_closes_when_touched(self) -> None:
        client = self._client()
        client._model_touch_succeeded = True
        resp = CountingResponse(json_data={"done": True})

        with patch.object(client, "_http_post", return_value=resp, create=True):
            self.assertTrue(client.release_owned_model())
        self.assertEqual(resp.close_count, 1)

    def test_tags_probes_close(self) -> None:
        client = self._client()
        tags = CountingResponse(
            json_data={"models": [{"name": "test-model"}, {"name": "other"}]}
        )

        with patch.object(client, "_http_get", return_value=tags, create=True):
            self.assertTrue(client.check_connection())
            self.assertTrue(client.check_model_available())
            self.assertEqual(client.list_models(), ["test-model", "other"])
        self.assertEqual(tags.close_count, 3)


class MemoryTransportResponseLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        CountingResponse.reset_instances()

    def _transport(self) -> RequestsOllamaChatTransport:
        sealed = authorize_model_endpoint(None, "http://127.0.0.1:9")
        transport = RequestsOllamaChatTransport(
            sealed.base_url, sealed_endpoint=sealed
        )
        return transport

    def _payload(self) -> dict[str, Any]:
        return {
            "model": "m:latest",
            "messages": [{"role": "user", "content": "{}"}],
            "stream": False,
        }

    def test_post_chat_success_closes(self) -> None:
        transport = self._transport()
        resp = CountingResponse(json_data={"done": True, "message": {"content": "[]"}})

        transport._session = SimpleNamespace(  # type: ignore[method-assign]
            post=lambda *a, **k: resp
        )
        data = transport.post_chat(self._payload())
        self.assertIsInstance(data, dict)
        self.assertEqual(resp.close_count, 1)

    def test_post_chat_http_error_closes(self) -> None:
        transport = self._transport()
        resp = CountingResponse(status_code=500, json_data={})
        transport._session = SimpleNamespace(post=lambda *a, **k: resp)  # type: ignore[method-assign]
        with self.assertRaises(MemoryExtractionTransportError) as ctx:
            transport.post_chat(self._payload())
        self.assertEqual(ctx.exception.error_kind, "http")
        self.assertEqual(resp.close_count, 1)

    def test_post_chat_json_error_closes(self) -> None:
        transport = self._transport()
        resp = CountingResponse(json_data=ValueError("not json"))
        transport._session = SimpleNamespace(post=lambda *a, **k: resp)  # type: ignore[method-assign]
        with self.assertRaises(MemoryExtractionResponseError):
            transport.post_chat(self._payload())
        self.assertEqual(resp.close_count, 1)

    def test_post_chat_redirect_closes(self) -> None:
        transport = self._transport()
        resp = CountingResponse(status_code=301, json_data={})
        transport._session = SimpleNamespace(post=lambda *a, **k: resp)  # type: ignore[method-assign]
        with self.assertRaises(MemoryExtractionTransportError) as ctx:
            transport.post_chat(self._payload())
        self.assertEqual(ctx.exception.error_kind, "http")
        self.assertEqual(resp.close_count, 1)

    def test_post_chat_json_keyerror_propagates_same_object(self) -> None:
        transport = self._transport()
        fatal = KeyError("k")
        resp = CountingResponse(json_data=fatal)
        transport._session = SimpleNamespace(post=lambda *a, **k: resp)  # type: ignore[method-assign]
        with self.assertRaises(KeyError) as ctx:
            transport.post_chat(self._payload())
        self.assertIs(ctx.exception, fatal)
        self.assertEqual(resp.close_count, 1)

    def test_post_chat_json_systemexit_propagates_same_object(self) -> None:
        transport = self._transport()
        fatal = SystemExit(7)
        resp = CountingResponse(json_data=fatal)
        transport._session = SimpleNamespace(post=lambda *a, **k: resp)  # type: ignore[method-assign]
        with self.assertRaises(SystemExit) as ctx:
            transport.post_chat(self._payload())
        self.assertIs(ctx.exception, fatal)
        self.assertEqual(resp.close_count, 1)

    def test_post_chat_json_generator_exit_propagates_same_object(self) -> None:
        transport = self._transport()
        fatal = GeneratorExit()
        resp = CountingResponse(json_data=fatal)
        transport._session = SimpleNamespace(post=lambda *a, **k: resp)  # type: ignore[method-assign]
        with self.assertRaises(GeneratorExit) as ctx:
            transport.post_chat(self._payload())
        self.assertIs(ctx.exception, fatal)
        self.assertEqual(resp.close_count, 1)

    def test_post_chat_json_custom_baseexception_propagates(self) -> None:
        class _CustomFatal(BaseException):
            pass

        transport = self._transport()
        fatal = _CustomFatal("x")
        resp = CountingResponse(json_data=fatal)
        transport._session = SimpleNamespace(post=lambda *a, **k: resp)  # type: ignore[method-assign]
        with self.assertRaises(_CustomFatal) as ctx:
            transport.post_chat(self._payload())
        self.assertIs(ctx.exception, fatal)
        self.assertEqual(resp.close_count, 1)

    def test_post_chat_connection_no_response(self) -> None:
        transport = self._transport()

        def fail(*a: Any, **k: Any) -> CountingResponse:
            raise requests.exceptions.ConnectionError("x")

        transport._session = SimpleNamespace(post=fail)  # type: ignore[method-assign]
        with self.assertRaises(MemoryExtractionTransportError) as ctx:
            transport.post_chat(self._payload())
        self.assertEqual(ctx.exception.error_kind, "connection")
        self.assertEqual(CountingResponse.instances, [])


if __name__ == "__main__":
    unittest.main()
