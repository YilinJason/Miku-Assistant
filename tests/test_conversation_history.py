"""
会话历史组件与 OllamaClient 历史提交规则的离线契约测试。

全部使用 fake HTTP；禁止连接真实 Ollama。
"""
from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import requests

from src.llm.conversation_history import (
    DEFAULT_REQUEST_WINDOW,
    ConversationHistory,
)
from src.llm.ollama_client import OllamaClient


class FakeResponse:
    """最小 requests.Response 替身。"""

    def __init__(
        self,
        *,
        json_data: Optional[Dict[str, Any]] = None,
        lines: Optional[List[Any]] = None,
        status_code: int = 200,
    ) -> None:
        self._json = json_data
        self._lines = lines or []
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code}", response=self
            )

    def json(self) -> Dict[str, Any]:
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_lines(self):
        for line in self._lines:
            if isinstance(line, bytes):
                yield line
            elif isinstance(line, str):
                yield line.encode("utf-8")
            else:
                yield json.dumps(line).encode("utf-8")


def _non_stream_ok(content: str) -> FakeResponse:
    return FakeResponse(json_data={"message": {"content": content}})


def _stream_ok(chunks: List[str]) -> FakeResponse:
    lines: List[Dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        done = i == len(chunks) - 1
        lines.append({"message": {"content": chunk}, "done": done})
    if not lines:
        lines.append({"message": {"content": ""}, "done": True})
    return FakeResponse(lines=lines)


class ConversationHistoryUnitTests(unittest.TestCase):
    def test_first_turn_and_multi_turn_order(self) -> None:
        hist = ConversationHistory()
        self.assertEqual(
            hist.build_messages("你好"),
            [{"role": "user", "content": "你好"}],
        )
        hist.commit_turn("你好", "嗨")
        hist.commit_turn("天气呢", "晴")
        self.assertEqual(
            hist.snapshot(),
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "嗨"},
                {"role": "user", "content": "天气呢"},
                {"role": "assistant", "content": "晴"},
            ],
        )
        built = hist.build_messages("继续")
        self.assertEqual(built[-1], {"role": "user", "content": "继续"})
        self.assertEqual(built[:-1], hist.snapshot())

    def test_request_window_selects_last_ten_plus_current(self) -> None:
        hist = ConversationHistory(request_window=DEFAULT_REQUEST_WINDOW)
        for i in range(7):
            hist.commit_turn(f"u{i}", f"a{i}")
        self.assertEqual(len(hist), 14)
        built = hist.build_messages("current")
        # 最近 10 条历史 + 当前输入
        self.assertEqual(len(built), 11)
        self.assertEqual(built[0], {"role": "user", "content": "u2"})
        self.assertEqual(built[-2], {"role": "assistant", "content": "a6"})
        self.assertEqual(built[-1], {"role": "user", "content": "current"})
        # 存储未因窗口截断
        self.assertEqual(len(hist), 14)

    def test_snapshot_mutation_does_not_affect_internal(self) -> None:
        hist = ConversationHistory()
        hist.commit_turn("u", "a")
        snap = hist.snapshot()
        snap.append({"role": "user", "content": "injected"})
        snap[0]["content"] = "mutated"
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist.snapshot()[0]["content"], "u")

    def test_build_messages_mutation_does_not_affect_internal(self) -> None:
        hist = ConversationHistory()
        hist.commit_turn("u", "a")
        built = hist.build_messages("now")
        built[0]["content"] = "x"
        built.append({"role": "assistant", "content": "leak"})
        self.assertEqual(hist.snapshot(), [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
        ])

    def test_clear_and_instance_isolation(self) -> None:
        a = ConversationHistory()
        b = ConversationHistory()
        a.commit_turn("a1", "r1")
        b.commit_turn("b1", "r2")
        a.clear()
        self.assertEqual(len(a), 0)
        self.assertEqual(len(b), 2)
        self.assertEqual(b.snapshot()[0]["content"], "b1")

    def test_max_stored_turns_trims_complete_pairs_only(self) -> None:
        hist = ConversationHistory(max_stored_turns=3)
        for i in range(5):
            hist.commit_turn(f"u{i}", f"a{i}")
        self.assertEqual(hist.turn_count, 3)
        self.assertEqual(len(hist), 6)
        snap = hist.snapshot()
        self.assertEqual(snap[0]["content"], "u2")
        self.assertEqual(snap[-1]["content"], "a4")
        # 无半轮
        self.assertEqual(len(snap) % 2, 0)
        for i in range(0, len(snap), 2):
            self.assertEqual(snap[i]["role"], "user")
            self.assertEqual(snap[i + 1]["role"], "assistant")

    def test_ten_thousand_turns_stay_within_cap(self) -> None:
        cap = 50
        hist = ConversationHistory(request_window=10, max_stored_turns=cap)
        for i in range(10_000):
            hist.commit_turn(f"u{i}", f"a{i}")
        self.assertEqual(hist.turn_count, cap)
        self.assertEqual(len(hist), cap * 2)
        snap = hist.snapshot()
        self.assertEqual(snap[0]["content"], "u9950")
        self.assertEqual(snap[-1]["content"], "a9999")
        built = hist.build_messages("now")
        self.assertEqual(len(built), 11)
        self.assertEqual(built[0]["content"], "u9995")
        self.assertEqual(built[-1], {"role": "user", "content": "now"})
    """generate 路径：fake HTTP，验证窗口、提交规则、流式聚合分支。"""

    def _client(self, **kwargs: Any) -> OllamaClient:
        return OllamaClient(base_url="http://fake-ollama", model="test", **kwargs)

    def test_first_and_multi_turn_payload_order_non_stream(self) -> None:
        client = self._client()
        captured: List[Any] = []

        def fake_post(url: str, json: Any = None, timeout: Any = None) -> FakeResponse:
            captured.append(json)
            n = len(captured)
            return _non_stream_ok(f"reply{n}")

        with patch("src.llm.ollama_client.requests.post", side_effect=fake_post):
            r1 = client.generate("hello")
            r2 = client.generate("again")
        self.assertEqual(r1, "reply1")
        self.assertEqual(r2, "reply2")
        self.assertEqual(
            captured[0]["messages"],
            [{"role": "user", "content": "hello"}],
        )
        self.assertEqual(
            captured[1]["messages"],
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "reply1"},
                {"role": "user", "content": "again"},
            ],
        )
        self.assertEqual(len(client.conversation_history), 4)

    def test_window_only_last_ten_history_plus_current(self) -> None:
        client = self._client()
        # 预填 12 条消息（6 轮），窗口应取后 10 条
        for i in range(6):
            client._history.commit_turn(f"u{i}", f"a{i}")
        captured: List[Any] = []

        def fake_post(url: str, json: Any = None, timeout: Any = None) -> FakeResponse:
            captured.append(json)
            return _non_stream_ok("ok")

        with patch("src.llm.ollama_client.requests.post", side_effect=fake_post):
            client.generate("now")
        msgs = captured[0]["messages"]
        self.assertEqual(len(msgs), 11)
        self.assertEqual(msgs[0]["content"], "u1")
        self.assertEqual(msgs[-1], {"role": "user", "content": "now"})
        # 存储仍保留全部 12 + 本轮 2 = 14
        self.assertEqual(len(client.conversation_history), 14)

    def test_failure_and_empty_do_not_commit(self) -> None:
        client = self._client()

        with patch(
            "src.llm.ollama_client.requests.post",
            side_effect=requests.exceptions.ConnectionError("down"),
        ):
            err = client.generate("x")
        self.assertTrue(err.startswith("错误:"))
        self.assertEqual(client.conversation_history, [])

        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_non_stream_ok(""),
        ):
            empty = client.generate("y")
        self.assertEqual(empty, "")
        self.assertEqual(client.conversation_history, [])

        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_non_stream_ok("ok"),
        ):
            self.assertEqual(client.generate("z"), "ok")
        self.assertEqual(len(client.conversation_history), 2)

    def test_stream_and_non_stream_both_follow_commit_rules(self) -> None:
        client = self._client()

        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_stream_ok(["你", "好"]),
        ):
            self.assertEqual(client.generate("s", stream=True), "你好")
        self.assertEqual(
            client.conversation_history,
            [
                {"role": "user", "content": "s"},
                {"role": "assistant", "content": "你好"},
            ],
        )

        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_stream_ok([""]),
        ):
            self.assertEqual(client.generate("empty-stream", stream=True), "")
        # 空流式聚合不追加
        self.assertEqual(len(client.conversation_history), 2)

        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_non_stream_ok("ns"),
        ):
            self.assertEqual(client.generate("t", stream=False), "ns")
        self.assertEqual(len(client.conversation_history), 4)

    def test_clear_history_and_client_isolation(self) -> None:
        a = self._client()
        b = self._client()
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_non_stream_ok("ra"),
        ):
            a.generate("ua")
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_non_stream_ok("rb"),
        ):
            b.generate("ub")
        a.clear_history()
        self.assertEqual(a.conversation_history, [])
        self.assertEqual(
            b.conversation_history,
            [
                {"role": "user", "content": "ub"},
                {"role": "assistant", "content": "rb"},
            ],
        )

    def test_public_conversation_history_is_snapshot(self) -> None:
        client = self._client()
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_non_stream_ok("r"),
        ):
            client.generate("u")
        snap = client.conversation_history
        snap.append({"role": "user", "content": "hack"})
        snap[0]["content"] = "mutated"
        self.assertEqual(
            client.conversation_history,
            [
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": "r"},
            ],
        )

    def _seed_successful_turn(self, client: OllamaClient) -> List[Dict[str, str]]:
        """先写入一轮成功对话，返回失败前完整快照。"""
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_non_stream_ok("seeded-reply"),
        ):
            self.assertEqual(client.generate("seed-user"), "seeded-reply")
        before = client.conversation_history
        self.assertEqual(
            before,
            [
                {"role": "user", "content": "seed-user"},
                {"role": "assistant", "content": "seeded-reply"},
            ],
        )
        return before

    def test_non_stream_parse_error_preserves_history(self) -> None:
        client = self._client()
        before = self._seed_successful_turn(client)

        class BadJsonResponse(FakeResponse):
            def json(self) -> Dict[str, Any]:
                raise ValueError("broken json body")

        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=BadJsonResponse(json_data={}),
        ):
            result = client.generate("after-seed", stream=False)
        self.assertTrue(result.startswith("错误:"))
        self.assertIn("broken json body", result)
        self.assertEqual(client.conversation_history, before)

    def test_stream_invalid_json_after_content_does_not_commit_partial(self) -> None:
        client = self._client()
        before = self._seed_successful_turn(client)
        bad_stream = FakeResponse(
            lines=[
                {"message": {"content": "partial-ok"}, "done": False},
                b"{not-valid-json",
            ]
        )
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=bad_stream,
        ):
            result = client.generate("stream-bad-json", stream=True)
        self.assertTrue(result.startswith("错误:"))
        self.assertEqual(client.conversation_history, before)

    def test_stream_connection_error_after_content_does_not_commit_partial(self) -> None:
        client = self._client()
        before = self._seed_successful_turn(client)

        class CutOffStreamResponse:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def iter_lines(self):
                yield json.dumps(
                    {"message": {"content": "partial-ok"}, "done": False}
                ).encode("utf-8")
                raise requests.exceptions.ConnectionError("stream cut")

        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=CutOffStreamResponse(),
        ):
            result = client.generate("stream-cut", stream=True)
        self.assertTrue(result.startswith("错误:"))
        self.assertIn("无法连接到Ollama服务", result)
        self.assertEqual(client.conversation_history, before)


if __name__ == "__main__":
    unittest.main()