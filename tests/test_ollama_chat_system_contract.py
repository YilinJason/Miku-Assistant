"""Ollama /api/chat system 消息契约：检查最终 HTTP JSON，不只包装器参数。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import requests

from src.llm.generate_result import GenerateStatus
from src.llm.ollama_client import OllamaClient
from src.llm.stream_events import StreamEvent, StreamEventKind
from src.memory import (
    MemoryAwareLLMClient,
    MemoryCategory,
    MemoryQueryService,
    MemorySource,
    NewMemory,
    SqliteMemoryStore,
)
from src.memory.service_models import MemoryTurnEvent


class _NonStreamResponse:
    def __init__(self, text: str = "ok", status_code: int = 200) -> None:
        self.status_code = status_code
        self._text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)

    def json(self) -> dict:
        return {"message": {"role": "assistant", "content": self._text}}


class _StreamResponse:
    def __init__(self, chunks: Optional[List[str]] = None) -> None:
        self.status_code = 200
        self.closed = False
        self._chunks = chunks if chunks is not None else ["回", "复"]

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self):
        for part in self._chunks:
            yield json.dumps(
                {"message": {"role": "assistant", "content": part}, "done": False},
                ensure_ascii=False,
            ).encode("utf-8")
        yield json.dumps(
            {"message": {"role": "assistant", "content": ""}, "done": True},
            ensure_ascii=False,
        ).encode("utf-8")

    def close(self) -> None:
        self.closed = True


class _NoopIngestion:
    def __init__(self) -> None:
        self.events: List[MemoryTurnEvent] = []

    def submit(self, event: object) -> bool:
        if isinstance(event, MemoryTurnEvent):
            self.events.append(event)
        return True


class ChatSystemContractTests(unittest.TestCase):
    def _client(self) -> OllamaClient:
        return OllamaClient(base_url="http://127.0.0.1:9", model="test-model")

    def test_non_stream_system_is_first_message_no_top_level_system(self) -> None:
        client = self._client()
        client._history.commit_turn("prev-u", "prev-a")
        captured: List[dict] = []

        def fake_post(url: str, json: Any = None, timeout: Any = None, **_kw: Any):
            captured.append(json)
            return _NonStreamResponse("reply")

        with patch("src.llm.ollama_client.requests.post", side_effect=fake_post):
            result = client.generate_result("current", system="SYS-BLOCK")
        self.assertTrue(result.is_success)
        payload = captured[0]
        self.assertNotIn("system", payload)
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": "SYS-BLOCK"},
                {"role": "user", "content": "prev-u"},
                {"role": "assistant", "content": "prev-a"},
                {"role": "user", "content": "current"},
            ],
        )
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][-1]["role"], "user")

    def test_stream_same_message_order_no_top_level_system(self) -> None:
        client = self._client()
        client._history.commit_turn("prev-u", "prev-a")
        captured: List[dict] = []

        def fake_post(
            url: str, json: Any = None, timeout: Any = None, stream: bool = False, **_kw: Any
        ):
            captured.append(json)
            self.assertTrue(stream)
            return _StreamResponse(["流", "式"])

        with patch("src.llm.ollama_client.requests.post", side_effect=fake_post):
            events = list(client.generate_stream("current", system="SYS-BLOCK"))
        self.assertEqual(events[-1].kind, StreamEventKind.COMPLETED)
        payload = captured[0]
        self.assertNotIn("system", payload)
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": "SYS-BLOCK"},
                {"role": "user", "content": "prev-u"},
                {"role": "assistant", "content": "prev-a"},
                {"role": "user", "content": "current"},
            ],
        )

    def test_no_system_omits_system_message(self) -> None:
        client = self._client()
        client._history.commit_turn("u0", "a0")
        captured: List[dict] = []

        def fake_post(url: str, json: Any = None, timeout: Any = None, **_kw: Any):
            captured.append(json)
            return _NonStreamResponse("ok")

        with patch("src.llm.ollama_client.requests.post", side_effect=fake_post):
            client.generate_result("now", system=None)
            client.generate_result("again", system="")
        for payload in captured:
            self.assertNotIn("system", payload)
            roles = [m["role"] for m in payload["messages"]]
            self.assertNotIn("system", roles)
            self.assertEqual(payload["messages"][-1]["role"], "user")

    def test_payload_mutation_does_not_pollute_history(self) -> None:
        client = self._client()
        client._history.commit_turn("keep-u", "keep-a")
        before = client.conversation_history
        captured: List[dict] = []

        def fake_post(url: str, json: Any = None, timeout: Any = None, **_kw: Any):
            captured.append(json)
            # 污染捕获到的 payload，不得反向写入 ConversationHistory
            json["messages"][0]["content"] = "MUTATED-SYSTEM"
            json["messages"][-1]["content"] = "MUTATED-USER"
            json["messages"].append({"role": "user", "content": "ghost"})
            return _NonStreamResponse("ok")

        with patch("src.llm.ollama_client.requests.post", side_effect=fake_post):
            client.generate_result("ask", system="original-system")
        self.assertEqual(client.conversation_history[:2], before)
        # 成功提交后仅追加本轮原文，不含 MUTATED / ghost / system
        hist = client.conversation_history
        self.assertEqual(hist[-2:], [{"role": "user", "content": "ask"}, {"role": "assistant", "content": "ok"}])
        for msg in hist:
            self.assertNotEqual(msg.get("role"), "system")
            self.assertNotIn("MUTATED", msg["content"])
            self.assertNotEqual(msg["content"], "ghost")
        self.assertIn("system", captured[0]["messages"][0]["role"])

    def test_memory_aware_http_system_contains_grounding_facts(self) -> None:
        question = "这是一个全新的会话。你知道我的测试代号和最喜欢的饮品吗？"
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")
            try:
                store.add(
                    NewMemory(
                        text='用户的测试代号是“银杏4729”',
                        category=MemoryCategory.USER_PROFILE,
                        source=MemorySource.MODEL_INFERRED,
                    )
                )
                store.add(
                    NewMemory(
                        text="用户最喜欢的饮品是桂花乌龙",
                        category=MemoryCategory.PREFERENCE,
                        source=MemorySource.MODEL_INFERRED,
                    )
                )
                query = MemoryQueryService(store)
                inner = OllamaClient(base_url="http://127.0.0.1:9", model="test-model")
                aware = MemoryAwareLLMClient(
                    inner, query, _NoopIngestion(), max_entries=8, max_chars=2000
                )
                captured: List[dict] = []

                def fake_post(
                    url: str, json: Any = None, timeout: Any = None, **_kw: Any
                ):
                    captured.append(json)
                    return _NonStreamResponse("记得你的代号和饮品。")

                with patch(
                    "src.llm.ollama_client.requests.post", side_effect=fake_post
                ):
                    result = aware.generate_result(question, system="你是助手。")
                self.assertTrue(result.is_success)
                self.assertEqual(len(captured), 1)
                payload = captured[0]
                self.assertNotIn("system", payload)
                messages = payload["messages"]
                self.assertEqual(messages[0]["role"], "system")
                system_content = messages[0]["content"]
                self.assertIn("银杏4729", system_content)
                self.assertIn("桂花乌龙", system_content)
                self.assertIn("subject=user", system_content)
                self.assertEqual(messages[-1], {"role": "user", "content": question})
                # 记忆不得复制进 user prompt
                self.assertNotIn("银杏4729", messages[-1]["content"])
            finally:
                store.close()

    def test_stream_and_non_stream_share_builder_shape(self) -> None:
        """流式与非流式共用构造边界：同输入同 messages 形状。"""
        client = self._client()
        client._history.commit_turn("h-u", "h-a")
        shapes: List[List[Dict[str, str]]] = []

        def capture_non_stream(
            url: str, json: Any = None, timeout: Any = None, **_kw: Any
        ):
            shapes.append([dict(m) for m in json["messages"]])
            return _NonStreamResponse("n")

        def capture_stream(
            url: str, json: Any = None, timeout: Any = None, stream: bool = False, **_kw: Any
        ):
            shapes.append([dict(m) for m in json["messages"]])
            return _StreamResponse(["s"])

        with patch("src.llm.ollama_client.requests.post", side_effect=capture_non_stream):
            client.generate_result("q", system="S")
        # 清空成功提交后再比流式，避免历史漂移；改用独立客户端
        client2 = self._client()
        client2._history.commit_turn("h-u", "h-a")
        with patch("src.llm.ollama_client.requests.post", side_effect=capture_stream):
            list(client2.generate_stream("q", system="S"))
        self.assertEqual(shapes[0], shapes[1])
        for shape in shapes:
            self.assertEqual(shape[0]["role"], "system")
            self.assertEqual(shape[-1]["content"], "q")


class ChatSystemFailureNoDoubleCommitTests(unittest.TestCase):
    """既有失败/提前 close 语义：不因 system 契约改动产生双提交。"""

    def test_non_stream_failure_does_not_commit(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m")
        with patch(
            "src.llm.ollama_client.requests.post",
            side_effect=requests.exceptions.ConnectionError("down"),
        ):
            result = client.generate_result("x", system="SYS")
        self.assertEqual(result.status, GenerateStatus.CONNECTION_ERROR)
        self.assertEqual(client.conversation_history, [])

    def test_stream_early_close_does_not_commit(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m")
        resp = _StreamResponse(["半"])
        with patch("src.llm.ollama_client.requests.post", return_value=resp):
            it = client.generate_stream("early", system="SYS")
            first = next(it)
            self.assertEqual(first.kind, StreamEventKind.TEXT_DELTA)
            it.close()
        self.assertEqual(client.conversation_history, [])
        self.assertTrue(resp.closed)


if __name__ == "__main__":
    unittest.main()
