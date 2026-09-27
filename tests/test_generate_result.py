"""
Ollama 结构化生成结果与兼容层的离线测试。

全部 mock HTTP；禁止连接真实 Ollama。不修改 Handler/TTS/CLI。
"""
from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import requests

from src.llm.generate_result import GenerateResult, GenerateStatus
from src.llm.ollama_client import OllamaClient


class FakeResponse:
    def __init__(
        self,
        *,
        json_data: Any = None,
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

    def json(self) -> Any:
        if isinstance(self._json, BaseException):
            raise self._json
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


def _ok(content: str) -> FakeResponse:
    return FakeResponse(json_data={"message": {"content": content}})


def _stream(chunks: List[str]) -> FakeResponse:
    lines: List[Dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        lines.append(
            {
                "message": {"content": chunk},
                "done": i == len(chunks) - 1,
            }
        )
    if not lines:
        lines.append({"message": {"content": ""}, "done": True})
    return FakeResponse(lines=lines)


class GenerateResultTypeTests(unittest.TestCase):
    def test_success_and_failure_invariants(self) -> None:
        ok = GenerateResult.success("错误: 这是模型台词")
        self.assertTrue(ok.is_success)
        self.assertFalse(ok.is_failure)
        self.assertEqual(ok.to_legacy_string(), "错误: 这是模型台词")

        empty = GenerateResult.empty()
        self.assertTrue(empty.is_empty)
        self.assertEqual(empty.to_legacy_string(), "")

        fail = GenerateResult.failure(
            GenerateStatus.CONNECTION_ERROR, "down"
        )
        self.assertTrue(fail.is_failure)
        self.assertEqual(
            fail.to_legacy_string(),
            "错误: 无法连接到Ollama服务 (down)",
        )
        with self.assertRaises(ValueError):
            GenerateResult(
                status=GenerateStatus.SUCCESS,
                text="x",
                error_detail="nope",
            )


class GenerateResultClientTests(unittest.TestCase):
    def _client(self) -> OllamaClient:
        return OllamaClient(base_url="http://fake-ollama", model="test")

    def _seed(self, client: OllamaClient) -> List[Dict[str, str]]:
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_ok("seeded"),
        ) as mock_post:
            result = client.generate_result("seed-user")
            self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        before = client.conversation_history
        self.assertEqual(
            before,
            [
                {"role": "user", "content": "seed-user"},
                {"role": "assistant", "content": "seeded"},
            ],
        )
        return before

    def test_success_including_error_prefix_model_text(self) -> None:
        client = self._client()
        text = "错误: 这是角色说的话"
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_ok(text),
        ) as mock_post:
            result = client.generate_result("hi")
            self.assertEqual(mock_post.call_count, 1)
            legacy = client.generate("hi2")
            self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.text, text)
        self.assertTrue(result.text.startswith("错误:"))
        self.assertEqual(legacy, text)
        self.assertEqual(len(client.conversation_history), 4)

    def test_empty_reply(self) -> None:
        client = self._client()
        before = self._seed(client)
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_ok(""),
        ) as mock_post:
            result = client.generate_result("empty-please")
            self.assertEqual(mock_post.call_count, 1)
            legacy = client.generate("empty-legacy")
            self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(result.status, GenerateStatus.EMPTY)
        self.assertEqual(legacy, "")
        self.assertEqual(client.conversation_history, before)

    def test_connection_timeout_http_errors(self) -> None:
        cases = [
            (
                requests.exceptions.ConnectionError("down"),
                GenerateStatus.CONNECTION_ERROR,
                "无法连接到Ollama服务",
            ),
            (
                requests.exceptions.Timeout("slow"),
                GenerateStatus.TIMEOUT,
                "无法连接到Ollama服务",
            ),
            (
                FakeResponse(status_code=500),
                GenerateStatus.HTTP_ERROR,
                "无法连接到Ollama服务",
            ),
        ]
        for exc_or_resp, status, legacy_part in cases:
            with self.subTest(status=status):
                client = self._client()
                before = self._seed(client)
                side = (
                    exc_or_resp
                    if isinstance(exc_or_resp, BaseException)
                    else None
                )
                ret = None if side else exc_or_resp
                with patch(
                    "src.llm.ollama_client.requests.post",
                    side_effect=side,
                    return_value=ret,
                ) as mock_post:
                    result = client.generate_result("x")
                    self.assertEqual(mock_post.call_count, 1)
                    legacy = client.generate("y")
                    self.assertEqual(mock_post.call_count, 2)
                self.assertEqual(result.status, status)
                self.assertTrue(result.is_failure)
                self.assertTrue(legacy.startswith("错误:"))
                self.assertIn(legacy_part, legacy)
                self.assertEqual(client.conversation_history, before)

    def test_invalid_json_structure_and_non_string_content(self) -> None:
        payloads = [
            FakeResponse(json_data=ValueError("broken json body")),
            FakeResponse(json_data=["not", "an", "object"]),
            FakeResponse(json_data={"message": "plain-string"}),
            FakeResponse(json_data={"message": {"content": 123}}),
            FakeResponse(json_data={"message": {"content": ["a", "b"]}}),
        ]
        for resp in payloads:
            with self.subTest(resp=resp._json):
                client = self._client()
                before = self._seed(client)
                with patch(
                    "src.llm.ollama_client.requests.post",
                    return_value=resp,
                ) as mock_post:
                    result = client.generate_result("bad")
                    self.assertEqual(mock_post.call_count, 1)
                    legacy = client.generate("bad2")
                    self.assertEqual(mock_post.call_count, 2)
                self.assertEqual(result.status, GenerateStatus.RESPONSE_ERROR)
                self.assertTrue(legacy.startswith("错误:"))
                self.assertFalse(legacy.startswith("错误: 无法连接到Ollama服务"))
                self.assertEqual(client.conversation_history, before)

    def test_stream_partial_then_parse_or_connection_failure(self) -> None:
        class CutOff:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def iter_lines(self):
                yield json.dumps(
                    {"message": {"content": "partial"}, "done": False}
                ).encode("utf-8")
                raise requests.exceptions.ConnectionError("stream cut")

        scenarios = [
            (
                FakeResponse(
                    lines=[
                        {"message": {"content": "partial"}, "done": False},
                        b"{not-json",
                    ]
                ),
                GenerateStatus.RESPONSE_ERROR,
                False,
            ),
            (
                CutOff(),
                GenerateStatus.CONNECTION_ERROR,
                True,
            ),
            (
                FakeResponse(
                    lines=[
                        {"message": {"content": "partial"}, "done": False},
                        {"message": {"content": 99}, "done": True},
                    ]
                ),
                GenerateStatus.RESPONSE_ERROR,
                False,
            ),
        ]
        for resp, status, conn_legacy in scenarios:
            with self.subTest(status=status, conn=conn_legacy):
                client = self._client()
                before = self._seed(client)
                with patch(
                    "src.llm.ollama_client.requests.post",
                    return_value=resp,
                ):
                    result = client.generate_result("s", stream=True)
                    legacy = client.generate("s2", stream=True)
                self.assertEqual(result.status, status)
                self.assertTrue(legacy.startswith("错误:"))
                if conn_legacy:
                    self.assertIn("无法连接到Ollama服务", legacy)
                self.assertEqual(client.conversation_history, before)

    def test_generate_delegates_once_no_double_commit(self) -> None:
        client = self._client()
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_ok("once"),
        ) as mock_post:
            legacy = client.generate("only-once")
            self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(legacy, "once")
        self.assertEqual(
            client.conversation_history,
            [
                {"role": "user", "content": "only-once"},
                {"role": "assistant", "content": "once"},
            ],
        )

    def test_empty_timeout_and_connection_error_have_stable_detail(self) -> None:
        """无参 Timeout()/ConnectionError() 不得让 failure 再次抛错。"""
        cases = [
            (requests.exceptions.Timeout(), GenerateStatus.TIMEOUT, "请求超时"),
            (
                requests.exceptions.ConnectionError(),
                GenerateStatus.CONNECTION_ERROR,
                "连接失败",
            ),
        ]
        for exc, status, fallback in cases:
            with self.subTest(status=status):
                client = self._client()
                before = self._seed(client)
                with patch(
                    "src.llm.ollama_client.requests.post",
                    side_effect=exc,
                ) as mock_post:
                    result = client.generate_result("x")
                    self.assertEqual(mock_post.call_count, 1)
                self.assertEqual(result.status, status)
                self.assertTrue(result.error_detail)
                self.assertEqual(result.error_detail, fallback)
                self.assertEqual(client.conversation_history, before)

                with patch(
                    "src.llm.ollama_client.requests.post",
                    side_effect=type(exc)(),
                ) as mock_post:
                    legacy = client.generate("y")
                    self.assertEqual(mock_post.call_count, 1)
                self.assertTrue(legacy.startswith("错误: 无法连接到Ollama服务"))
                self.assertIn(fallback, legacy)
                self.assertEqual(client.conversation_history, before)

    def test_requests_json_decode_error_is_response_error(self) -> None:
        """
        requests.exceptions.JSONDecodeError 须为 RESPONSE_ERROR。

        兼容文案变化：第六小步前因继承 RequestException 会变成
        「错误: 无法连接到Ollama服务 (…)」；现为「错误: {detail}」。
        """
        client = self._client()
        before = self._seed(client)
        decode_exc = requests.exceptions.JSONDecodeError(
            "Expecting value", "doc", 0
        )
        resp = FakeResponse(json_data=decode_exc)

        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=resp,
        ) as mock_post:
            result = client.generate_result("bad-json")
            self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(result.status, GenerateStatus.RESPONSE_ERROR)
        self.assertTrue(result.error_detail)
        self.assertEqual(client.conversation_history, before)

        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=FakeResponse(json_data=decode_exc),
        ) as mock_post:
            legacy = client.generate("bad-json-2")
            self.assertEqual(mock_post.call_count, 1)
        self.assertTrue(legacy.startswith("错误:"))
        self.assertFalse(
            legacy.startswith("错误: 无法连接到Ollama服务"),
            msg="JSONDecodeError 兼容串不应再走连接失败文案",
        )
        self.assertEqual(client.conversation_history, before)

    def test_stream_non_dict_message_after_partial_is_response_error(self) -> None:
        """流式已有部分内容后 message 非 dict → RESPONSE_ERROR，不提交部分回复。"""
        client = self._client()
        before = self._seed(client)
        bad = FakeResponse(
            lines=[
                {"message": {"content": "partial-ok"}, "done": False},
                {"message": "not-an-object", "done": True},
            ]
        )
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=bad,
        ) as mock_post:
            result = client.generate_result("s", stream=True)
            self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(result.status, GenerateStatus.RESPONSE_ERROR)
        self.assertIn("message 必须为对象", result.error_detail or "")
        self.assertEqual(client.conversation_history, before)

        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=FakeResponse(
                lines=[
                    {"message": {"content": "partial-ok"}, "done": False},
                    {"message": "not-an-object", "done": True},
                ]
            ),
        ) as mock_post:
            legacy = client.generate("s2", stream=True)
            self.assertEqual(mock_post.call_count, 1)
        self.assertTrue(legacy.startswith("错误:"))
        self.assertIn("message 必须为对象", legacy)
        self.assertEqual(client.conversation_history, before)

    def test_failure_empty_detail_factory_fallback(self) -> None:
        result = GenerateResult.failure(GenerateStatus.TIMEOUT, "")
        self.assertEqual(result.error_detail, "timeout")
        self.assertEqual(
            result.to_legacy_string(),
            "错误: 无法连接到Ollama服务 (timeout)",
        )

    def test_stream_success_commits_once(self) -> None:
        client = self._client()
        with patch(
            "src.llm.ollama_client.requests.post",
            return_value=_stream(["你", "好"]),
        ) as mock_post:
            result = client.generate_result("s", stream=True)
            self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.text, "你好")
        self.assertEqual(len(client.conversation_history), 2)


if __name__ == "__main__":
    unittest.main()
