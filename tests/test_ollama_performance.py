"""Ollama 预热、模型保活与性能日志的纯离线契约测试。"""
from __future__ import annotations

import threading
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

import requests

from src.llm.ollama_client import OllamaClient


class _Response:
    def __init__(self, data: Any) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        if isinstance(self._data, BaseException):
            raise self._data
        return self._data


class OllamaWarmupTests(unittest.TestCase):
    def test_warmup_uses_empty_chat_keep_alive_and_preserves_history(self) -> None:
        client = OllamaClient(
            base_url="http://fake-ollama",
            model="test",
            timeout=9,
            keep_alive="30m",
        )
        before = client.conversation_history
        response = _Response({"done": True, "load_duration": 2_000_000})

        with patch(
            "src.llm.ollama_client.requests.post", return_value=response
        ) as post:
            self.assertTrue(client.warm_up(timeout=12))

        post.assert_called_once_with(
            "http://fake-ollama/api/chat",
            json={
                "model": "test",
                "messages": [],
                "stream": False,
                "keep_alive": "30m",
            },
            timeout=12,
        )
        self.assertEqual(client.conversation_history, before)

    def test_warmup_failure_is_fail_open_and_preserves_history(self) -> None:
        client = OllamaClient(base_url="http://fake-ollama", model="test")
        before = client.conversation_history
        with patch(
            "src.llm.ollama_client.requests.post",
            side_effect=requests.exceptions.ConnectionError("offline"),
        ):
            self.assertFalse(client.warm_up(timeout=1))
        self.assertEqual(client.conversation_history, before)

    def test_generation_payload_includes_configured_keep_alive(self) -> None:
        client = OllamaClient(
            base_url="http://fake-ollama",
            model="test",
            keep_alive="30m",
        )
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_Response({"message": {"content": "ok"}}),
        ) as post:
            self.assertEqual(client.generate("hi"), "ok")
        self.assertEqual(post.call_args.kwargs["json"]["keep_alive"], "30m")

    def test_default_client_omits_keep_alive_for_compatibility(self) -> None:
        client = OllamaClient(base_url="http://fake-ollama", model="test")
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_Response({"message": {"content": "ok"}}),
        ) as post:
            self.assertEqual(client.generate("hi"), "ok")
        self.assertNotIn("keep_alive", post.call_args.kwargs["json"])

    def test_background_warmup_is_idempotent_and_formal_request_waits(self) -> None:
        client = OllamaClient(base_url="http://fake-ollama", model="test")
        entered = threading.Event()
        release = threading.Event()

        def blocked_warmup(*, timeout: float) -> bool:
            self.assertEqual(timeout, 3)
            entered.set()
            release.wait(timeout=1)
            return True

        with patch.object(client, "warm_up", side_effect=blocked_warmup) as warm:
            self.assertTrue(client.start_warmup(timeout=3))
            self.assertTrue(entered.wait(timeout=1))
            self.assertFalse(client.start_warmup(timeout=3))
            release.set()
            client._wait_for_warmup()  # noqa: SLF001

        self.assertTrue(client._warmup_done.is_set())  # noqa: SLF001
        self.assertTrue(client._warmup_ok)  # noqa: SLF001
        warm.assert_called_once_with(timeout=3)

    def test_thread_start_failure_restores_non_waiting_retryable_state(self) -> None:
        client = OllamaClient(base_url="http://fake-ollama", model="test")
        thread = MagicMock()
        thread.start.side_effect = RuntimeError("thread unavailable")
        with patch("src.llm.ollama_client.threading.Thread", return_value=thread):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                client.start_warmup(timeout=3)
        self.assertIsNone(client._warmup_thread)  # noqa: SLF001
        self.assertFalse(client._warmup_done.is_set())  # noqa: SLF001
        self.assertFalse(client._warmup_ok)  # noqa: SLF001

    def test_metric_logger_tolerates_malformed_optional_fields(self) -> None:
        client = OllamaClient(base_url="http://fake-ollama", model="test")
        client.logger = MagicMock()
        client._log_response_metrics(  # noqa: SLF001
            {
                "total_duration": "bad",
                "load_duration": None,
                "eval_duration": 0,
                "eval_count": True,
            },
            stage="test",
        )
        client.logger.info.assert_called_once()
        self.assertIn("n/a", client.logger.info.call_args.args)


if __name__ == "__main__":
    unittest.main()
