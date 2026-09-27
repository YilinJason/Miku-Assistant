"""
第七小步：Handler / 路由 / 助手消费结构化生成结果的离线测试。

fake/mock；不连真实 Ollama；不启动真实设备。
不覆盖 src/voice/stt.py（独立语音修复，不在本轮验收范围）。
"""
from __future__ import annotations

import unittest
from typing import Any, List, Optional
from unittest.mock import patch

from src.core.assistant import MikuAssistant
from src.core.dispatch_outcome import DispatchOutcome, ReplyStage
from src.core.handlers import (
    LLMIntentHandler,
    SearchIntentHandler,
    SystemIntentHandler,
)
from src.core.router import IntentRouter
from src.core.router_factory import build_intent_router
from src.llm.generate_result import GenerateResult, GenerateStatus
from tests.test_assistant_orchestration import (
    FakeCharacter,
    FakeTTS,
    MemoryConfig,
    _immediate_thread_factory,
)
from tests.test_intent_router import FakeController, FakeLLM, FakeSearcher


class StructuredFakeLLM:
    """仅暴露 generate_result；generate 委托一次，便于断言调用次数。"""

    def __init__(self, result: GenerateResult):
        self.result = result
        self.generate_result_calls: List[str] = []
        self.generate_calls: List[str] = []

    def generate_result(self, text: str, stream: bool = False, system=None) -> GenerateResult:
        self.generate_result_calls.append(text)
        return self.result

    def generate(self, text: str, stream: bool = False, system=None) -> str:
        self.generate_calls.append(text)
        return self.generate_result(text, stream=stream, system=system).to_legacy_string()


class StructuredDispatchUnitTests(unittest.TestCase):
    def test_from_generate_result_mapping(self) -> None:
        ok = DispatchOutcome.from_generate_result(
            GenerateResult.success("错误: 模型台词")
        )
        self.assertEqual(ok.text, "错误: 模型台词")
        self.assertTrue(ok.allow_tts)
        self.assertTrue(ok.allow_character)
        self.assertEqual(ok.stage, ReplyStage.OK)

        empty = DispatchOutcome.from_generate_result(GenerateResult.empty())
        self.assertEqual(empty.text, "")
        self.assertFalse(empty.allow_tts)
        self.assertFalse(empty.allow_character)
        self.assertEqual(empty.stage, ReplyStage.GENERATION_FAILED)

        fail = DispatchOutcome.from_generate_result(
            GenerateResult.failure(GenerateStatus.CONNECTION_ERROR, "down")
        )
        self.assertTrue(fail.text.startswith("错误:"))
        self.assertFalse(fail.allow_tts)
        self.assertFalse(fail.allow_character)
        self.assertEqual(fail.stage, ReplyStage.GENERATION_FAILED)


class LLMHandlerStructuredTests(unittest.TestCase):
    def test_success_and_error_prefix_success_allow_side_effects(self) -> None:
        llm = StructuredFakeLLM(GenerateResult.success("错误: 角色说的"))
        handler = LLMIntentHandler(llm)  # type: ignore[arg-type]
        outcome = handler.handle_outcome("hi")
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.text, "错误: 角色说的")
        self.assertTrue(outcome.allow_tts)
        self.assertTrue(outcome.allow_character)
        self.assertEqual(llm.generate_result_calls, ["hi"])
        # handle 委托同一路径，再调一次会第二次请求
        text = handler.handle("hi2")
        self.assertEqual(text, "错误: 角色说的")
        self.assertEqual(llm.generate_result_calls, ["hi", "hi2"])

    def test_failure_and_empty_disallow_side_effects(self) -> None:
        cases = [
            GenerateResult.failure(GenerateStatus.TIMEOUT, "slow"),
            GenerateResult.failure(GenerateStatus.HTTP_ERROR, "500"),
            GenerateResult.failure(GenerateStatus.RESPONSE_ERROR, "bad"),
            GenerateResult.empty(),
        ]
        for result in cases:
            with self.subTest(status=result.status):
                llm = StructuredFakeLLM(result)
                handler = LLMIntentHandler(llm)  # type: ignore[arg-type]
                outcome = handler.handle_outcome("x")
                assert outcome is not None
                self.assertFalse(outcome.allow_tts)
                self.assertFalse(outcome.allow_character)
                self.assertEqual(len(llm.generate_result_calls), 1)
                if result.is_empty:
                    self.assertEqual(outcome.text, "")
                else:
                    self.assertEqual(outcome.text, result.to_legacy_string())

    def test_string_only_client_single_generate(self) -> None:
        llm = FakeLLM("plain")
        handler = LLMIntentHandler(llm)  # type: ignore[arg-type]
        outcome = handler.handle_outcome("q")
        assert outcome is not None
        self.assertTrue(outcome.allow_tts)
        self.assertEqual(llm.calls, ["q"])
        self.assertEqual(handler.handle("q2"), "plain")
        self.assertEqual(llm.calls, ["q", "q2"])


class RouterAndAssistantStructuredTests(unittest.TestCase):
    def _assistant(
        self,
        *,
        router: Any,
        tts: Optional[FakeTTS] = None,
        character: Optional[FakeCharacter] = None,
    ) -> MikuAssistant:
        return MikuAssistant.from_dependencies(
            config=MemoryConfig({"assistant": {"auto_tts": True}}),
            router=router,
            tts_engine=tts,
            character_interface=character,
        )

    def test_system_and_search_priority_unchanged(self) -> None:
        controller = FakeController("sys-ok")
        searcher = FakeSearcher(query="kw", search_result="search-ok")
        llm = StructuredFakeLLM(GenerateResult.success("should-not-run"))
        router = IntentRouter(
            [
                SystemIntentHandler(controller),  # type: ignore[arg-type]
                SearchIntentHandler(searcher),  # type: ignore[arg-type]
                LLMIntentHandler(llm),  # type: ignore[arg-type]
            ]
        )
        self.assertEqual(router.dispatch("关机"), "sys-ok")
        self.assertEqual(llm.generate_result_calls, [])

        controller2 = FakeController(None)
        searcher2 = FakeSearcher(query="py", search_result="opened")
        llm2 = StructuredFakeLLM(GenerateResult.success("no"))
        router2 = IntentRouter(
            [
                SystemIntentHandler(controller2),  # type: ignore[arg-type]
                SearchIntentHandler(searcher2),  # type: ignore[arg-type]
                LLMIntentHandler(llm2),  # type: ignore[arg-type]
            ]
        )
        outcome = router2.dispatch_outcome("搜索 py")
        assert outcome is not None
        self.assertEqual(outcome.text, "opened")
        self.assertTrue(outcome.allow_tts)
        self.assertTrue(outcome.allow_character)
        self.assertEqual(llm2.generate_result_calls, [])

    def test_assistant_success_starts_thread_and_forwards_character(self) -> None:
        llm = StructuredFakeLLM(GenerateResult.success("错误: 台词"))
        router = build_intent_router(
            windows_controller=FakeController(None),  # type: ignore[arg-type]
            chrome_searcher=FakeSearcher(query=None),  # type: ignore[arg-type]
            llm_client=llm,  # type: ignore[arg-type]
        )
        tts = FakeTTS()
        character = FakeCharacter(initialized=True)
        assistant = self._assistant(router=router, tts=tts, character=character)
        factory = _immediate_thread_factory()
        with patch("src.core.assistant.threading.Thread", side_effect=factory) as thread_cls:
            reply = assistant.process_text_input("hello", use_tts=True)
        self.assertEqual(reply, "错误: 台词")
        self.assertEqual(llm.generate_result_calls, ["hello"])
        self.assertEqual(llm.generate_calls, [])
        self.assertEqual(thread_cls.call_count, 1)
        self.assertEqual(tts.speak_calls, ["错误: 台词"])
        self.assertEqual(character.messages, ["错误: 台词"])

    def test_assistant_failure_no_tts_no_character(self) -> None:
        llm = StructuredFakeLLM(
            GenerateResult.failure(GenerateStatus.CONNECTION_ERROR, "down")
        )
        router = build_intent_router(
            windows_controller=FakeController(None),  # type: ignore[arg-type]
            chrome_searcher=FakeSearcher(query=None),  # type: ignore[arg-type]
            llm_client=llm,  # type: ignore[arg-type]
        )
        tts = FakeTTS()
        character = FakeCharacter(initialized=True)
        assistant = self._assistant(router=router, tts=tts, character=character)
        with patch("src.core.assistant.threading.Thread") as thread_cls:
            reply = assistant.process_text_input("x", use_tts=True)
        self.assertTrue(reply.startswith("错误:"))
        self.assertIn("无法连接到Ollama服务", reply)
        self.assertEqual(llm.generate_result_calls, ["x"])
        self.assertEqual(thread_cls.call_count, 0)
        self.assertEqual(tts.speak_calls, [])
        self.assertEqual(character.messages, [])

    def test_assistant_empty_no_tts_no_character(self) -> None:
        llm = StructuredFakeLLM(GenerateResult.empty())
        router = build_intent_router(
            windows_controller=FakeController(None),  # type: ignore[arg-type]
            chrome_searcher=FakeSearcher(query=None),  # type: ignore[arg-type]
            llm_client=llm,  # type: ignore[arg-type]
        )
        tts = FakeTTS()
        character = FakeCharacter(initialized=True)
        assistant = self._assistant(router=router, tts=tts, character=character)
        with patch("src.core.assistant.threading.Thread") as thread_cls:
            reply = assistant.process_text_input("x", use_tts=True)
        self.assertEqual(reply, "")
        self.assertEqual(thread_cls.call_count, 0)
        self.assertEqual(tts.speak_calls, [])
        self.assertEqual(character.messages, [])
        self.assertEqual(llm.generate_result_calls, ["x"])

    def test_dispatch_and_dispatch_outcome_share_single_model_call(self) -> None:
        llm = StructuredFakeLLM(GenerateResult.success("once"))
        router = IntentRouter([LLMIntentHandler(llm)])  # type: ignore[arg-type]
        self.assertEqual(router.dispatch("a"), "once")
        self.assertEqual(llm.generate_result_calls, ["a"])
        outcome = router.dispatch_outcome("b")
        assert outcome is not None
        self.assertEqual(outcome.text, "once")
        self.assertEqual(llm.generate_result_calls, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
