"""
Ollama 增量输出接口离线测试。

可控 HTTP 流；禁止连接真实 Ollama。不接入 Handler/CLI/TTS。
"""
from __future__ import annotations

import json
import unittest
from typing import Any, Iterator, List, Optional
from unittest.mock import patch

import requests

from src.llm.generate_result import GenerateStatus
from src.llm.ollama_client import OllamaClient
from src.llm.stream_events import StreamEvent, StreamEventKind


class ControllableStreamResponse:
    """可统计读行次数、支持中途抛错，并跟踪 close。"""

    def __init__(
        self,
        lines: Optional[List[Any]] = None,
        *,
        line_iter: Optional[Iterator[Any]] = None,
        status_code: int = 200,
        raise_on_nth: Optional[int] = None,
        raise_exc: Optional[BaseException] = None,
    ) -> None:
        self._lines = lines
        self._line_iter = line_iter
        self.status_code = status_code
        self.raise_on_nth = raise_on_nth
        self.raise_exc = raise_exc or requests.exceptions.ConnectionError("cut")
        self.closed = False
        self.lines_read = 0

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code}", response=self
            )

    def iter_lines(self):
        source: Iterator[Any]
        if self._line_iter is not None:
            source = self._line_iter
        else:
            source = iter(self._lines or [])
        for item in source:
            self.lines_read += 1
            if self.raise_on_nth is not None and self.lines_read >= self.raise_on_nth:
                raise self.raise_exc
            if isinstance(item, bytes):
                yield item
            elif isinstance(item, str):
                yield item.encode("utf-8")
            else:
                yield json.dumps(item).encode("utf-8")

    def close(self) -> None:
        self.closed = True


def _ndjson(*objs: Any) -> List[Any]:
    return list(objs)


class GenerateStreamTests(unittest.TestCase):
    def _client(self) -> OllamaClient:
        return OllamaClient(base_url="http://127.0.0.1:9", model="test")

    def _seed(self, client: OllamaClient) -> List[dict]:
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=type(
                "R",
                (),
                {
                    "raise_for_status": lambda self: None,
                    "json": lambda self: {
                        "message": {"content": "seeded"}
                    },
                },
            )(),
        ):
            client.generate_result("seed-user", stream=False)
        before = client.conversation_history
        self.assertEqual(len(before), 2)
        return before

    def test_first_delta_before_full_response_consumed(self) -> None:
        client = self._client()
        resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "你", "role": "assistant"}, "done": False},
                {"message": {"content": "好", "role": "assistant"}, "done": False},
                {"message": {"content": ""}, "done": True, "done_reason": "stop"},
            )
        )
        with patch(
            "src.llm.ollama_client.requests.post", return_value=resp
        ) as mock_post:
            it = client.generate_stream("hi")
            first = next(it)
            self.assertEqual(first.kind, StreamEventKind.TEXT_DELTA)
            self.assertEqual(first.delta, "你")
            # 首段已交付时只读过第一行
            self.assertEqual(resp.lines_read, 1)
            self.assertEqual(mock_post.call_args.kwargs.get("stream"), True)

            rest = list(it)
        self.assertEqual(
            [e.kind for e in rest],
            [StreamEventKind.TEXT_DELTA, StreamEventKind.COMPLETED],
        )
        self.assertEqual(rest[0].delta, "好")
        self.assertEqual(rest[1].text, "你好")
        self.assertTrue(rest[1].is_terminal)
        self.assertTrue(resp.closed)
        self.assertEqual(len(client.conversation_history), 2)

    def test_success_commits_once_and_single_terminal(self) -> None:
        client = self._client()
        before = self._seed(client)
        resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "A"}, "done": False},
                {"message": {"content": "B"}, "done": True},
            )
        )
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            events = list(client.generate_stream("q"))
        kinds = [e.kind for e in events]
        self.assertEqual(
            kinds,
            [
                StreamEventKind.TEXT_DELTA,
                StreamEventKind.TEXT_DELTA,
                StreamEventKind.COMPLETED,
            ],
        )
        self.assertEqual(sum(1 for k in kinds if k != StreamEventKind.TEXT_DELTA), 1)
        self.assertEqual(events[-1].text, "AB")
        hist = client.conversation_history
        self.assertEqual(len(hist), len(before) + 2)
        self.assertEqual(hist[-2:], [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "AB"},
        ])
        self.assertTrue(resp.closed)

    def test_missing_done_does_not_commit(self) -> None:
        client = self._client()
        before = self._seed(client)
        resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "partial"}, "done": False},
            )
        )
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            events = list(client.generate_stream("q"))
        self.assertEqual(events[0].kind, StreamEventKind.TEXT_DELTA)
        self.assertEqual(events[-1].kind, StreamEventKind.FAILED)
        self.assertEqual(events[-1].status, GenerateStatus.RESPONSE_ERROR)
        self.assertIn("done=true", events[-1].error_detail or "")
        self.assertEqual(client.conversation_history, before)
        self.assertTrue(resp.closed)

    def test_parse_error_mid_stream_no_commit(self) -> None:
        client = self._client()
        before = self._seed(client)
        resp = ControllableStreamResponse(
            [
                {"message": {"content": "ok"}, "done": False},
                b"{not-json",
            ]
        )
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            events = list(client.generate_stream("q"))
        self.assertEqual(events[-1].kind, StreamEventKind.FAILED)
        self.assertEqual(client.conversation_history, before)
        self.assertTrue(resp.closed)

    def test_connection_cut_mid_stream_no_commit(self) -> None:
        client = self._client()
        before = self._seed(client)
        resp = ControllableStreamResponse(
            _ndjson({"message": {"content": "x"}, "done": False}),
            raise_on_nth=2,
            raise_exc=requests.exceptions.ConnectionError("stream cut"),
        )
        # need a second line attempt - raise_on_nth=1 would fail before first yield content
        # After first line, next iteration increments to 2 and raises — but we only have 1 line.
        # Use line_iter that yields one then raises on second next.
        def line_iter():
            yield json.dumps(
                {"message": {"content": "x"}, "done": False}
            ).encode()
            raise requests.exceptions.ConnectionError("stream cut")

        resp = ControllableStreamResponse(line_iter=line_iter())
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            events = list(client.generate_stream("q"))
        self.assertEqual(events[0].kind, StreamEventKind.TEXT_DELTA)
        self.assertEqual(events[-1].kind, StreamEventKind.FAILED)
        self.assertEqual(events[-1].status, GenerateStatus.CONNECTION_ERROR)
        self.assertEqual(client.conversation_history, before)
        self.assertTrue(resp.closed)

    def test_service_error_object_no_commit(self) -> None:
        client = self._client()
        before = self._seed(client)
        resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "hi"}, "done": False},
                {"error": "model not found"},
            )
        )
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            events = list(client.generate_stream("q"))
        self.assertEqual(events[-1].kind, StreamEventKind.FAILED)
        self.assertIn("model not found", events[-1].error_detail or "")
        self.assertEqual(client.conversation_history, before)
        self.assertTrue(resp.closed)

    def test_early_close_releases_response_no_commit(self) -> None:
        client = self._client()
        before = self._seed(client)
        resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "一"}, "done": False},
                {"message": {"content": "二"}, "done": False},
                {"done": True},
            )
        )
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            it = client.generate_stream("q")
            first = next(it)
            self.assertEqual(first.delta, "一")
            it.close()
        self.assertTrue(resp.closed)
        self.assertEqual(client.conversation_history, before)
        self.assertEqual(resp.lines_read, 1)

    def test_http_error_before_body_releases_and_fails(self) -> None:
        client = self._client()
        before = self._seed(client)
        resp = ControllableStreamResponse(status_code=500)
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            events = list(client.generate_stream("q"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, StreamEventKind.FAILED)
        self.assertEqual(events[0].status, GenerateStatus.HTTP_ERROR)
        self.assertEqual(client.conversation_history, before)
        self.assertTrue(resp.closed)

    def test_non_dict_message_fails_no_partial_commit(self) -> None:
        client = self._client()
        before = self._seed(client)
        resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "p"}, "done": False},
                {"message": "bad", "done": True},
            )
        )
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            events = list(client.generate_stream("q"))
        self.assertEqual(events[-1].kind, StreamEventKind.FAILED)
        self.assertEqual(client.conversation_history, before)
        self.assertTrue(resp.closed)

    def test_empty_completed_does_not_commit(self) -> None:
        client = self._client()
        before = self._seed(client)
        resp = ControllableStreamResponse(_ndjson({"message": {"content": ""}, "done": True}))
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            events = list(client.generate_stream("q"))
        self.assertEqual(events, [
            StreamEvent.completed(""),
        ])
        self.assertEqual(events[0].status, GenerateStatus.EMPTY)
        self.assertEqual(client.conversation_history, before)
        self.assertTrue(resp.closed)

    def test_done_must_be_bool_parameterized(self) -> None:
        """非法 done 类型不得 COMPLETED/提交；仅 bool True 完成。"""
        illegal = (
            ("false", "false"),
            ("true", "true"),
            (0, 0),
            (1, 1),
            (None, None),
            ([], []),
            ({}, {}),
        )
        for label, done_val in illegal:
            with self.subTest(done=label):
                client = self._client()
                before = self._seed(client)
                resp = ControllableStreamResponse(
                    _ndjson(
                        {"message": {"content": "段"}, "done": False},
                        {
                            "message": {"content": "续"},
                            "done": done_val,
                        },
                    )
                )
                with patch(
                    "src.llm.ollama_client.requests.post", return_value=resp
                ):
                    events = list(client.generate_stream("q"))
                self.assertEqual(events[0].kind, StreamEventKind.TEXT_DELTA)
                self.assertEqual(events[0].delta, "段")
                # 第二行在交付「续」之前因 done 类型失败 → 仅一个失败终结，无成功
                terminals = [e for e in events if e.is_terminal]
                self.assertEqual(len(terminals), 1)
                self.assertEqual(terminals[0].kind, StreamEventKind.FAILED)
                self.assertEqual(terminals[0].status, GenerateStatus.RESPONSE_ERROR)
                self.assertIn("done 必须为 bool", terminals[0].error_detail or "")
                self.assertFalse(
                    any(e.kind is StreamEventKind.COMPLETED for e in events)
                )
                self.assertFalse(
                    any(
                        e.kind is StreamEventKind.TEXT_DELTA and e.delta == "续"
                        for e in events
                    )
                )
                self.assertEqual(client.conversation_history, before)
                self.assertTrue(resp.closed)

        # 合法 False 继续，随后 True 完成
        client = self._client()
        before = self._seed(client)
        resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "A"}, "done": False},
                {"message": {"content": "B"}, "done": True},
            )
        )
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            events = list(client.generate_stream("q"))
        self.assertEqual(events[-1].kind, StreamEventKind.COMPLETED)
        self.assertEqual(events[-1].text, "AB")
        self.assertEqual(len(client.conversation_history), len(before) + 2)


if __name__ == "__main__":
    unittest.main()
