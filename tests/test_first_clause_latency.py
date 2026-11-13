"""
首个自然分句提前切分：离线行为与 fake 流式集成。

不启动真实模型/播放器/正式 DB；不吞异常、不遗留线程。
"""
from __future__ import annotations

import threading
import unittest
from typing import Any, Iterator, List

from src.core.assistant import MikuAssistant
from src.core.dispatch_outcome import ReplyStage
from src.core.router_factory import build_intent_router
from src.core.sentence_splitter import IncrementalSentenceSplitter
from src.llm.generate_result import GenerateResult
from src.llm.stream_events import StreamEvent
from tests.test_assistant_orchestration import (
    FakeCharacter,
    MemoryConfig,
    _shutdown_assistant_with_player,
)
from tests.test_intent_router import FakeController, FakeSearcher
from tests.test_stream_playback import FakeStreamLLM

# 固定前缀：strip 后长度 >= 12（与 assistant 首段阈值一致）
LONG_PREFIX = "今天如果你觉得有一点点疲惫"


def _assert_long_prefix(self: unittest.TestCase) -> None:
    self.assertGreaterEqual(len(LONG_PREFIX.strip()), 12)


def _push_all(sp: IncrementalSentenceSplitter, text: str) -> List[str]:
    out: List[str] = []
    for ch in text:
        out.extend(sp.push(ch))
    out.extend(sp.finish())
    return out


class FirstClauseSplitterTests(unittest.TestCase):
    def setUp(self) -> None:
        _assert_long_prefix(self)

    def test_default_compatible_no_soft_split(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=500)
        long_clause = (
            LONG_PREFIX + "，后面还有同样长的尾巴内容没有句号"
        )
        self.assertEqual(sp.push(long_clause), [])
        self.assertEqual(sp.finish(), [long_clause.strip()])

    def test_threshold_strict_type_and_range(self) -> None:
        with self.assertRaises(TypeError):
            IncrementalSentenceSplitter(max_buffer=10, first_clause_min_chars=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            IncrementalSentenceSplitter(max_buffer=10, first_clause_min_chars=0)
        with self.assertRaises(ValueError):
            IncrementalSentenceSplitter(max_buffer=10, first_clause_min_chars=11)

    def test_early_soft_comma_before_late_period_whole_and_char(self) -> None:
        text = LONG_PREFIX + "，但后面还有一个更长的句子在这里结束。"
        expected = [LONG_PREFIX + "，", "但后面还有一个更长的句子在这里结束。"]
        sp_whole = IncrementalSentenceSplitter(
            max_buffer=500, first_clause_min_chars=12
        )
        self.assertEqual(sp_whole.push(text) + sp_whole.finish(), expected)
        sp_char = IncrementalSentenceSplitter(
            max_buffer=500, first_clause_min_chars=12
        )
        self.assertEqual(_push_all(sp_char, text), expected)

    def test_char_by_char_matches_whole_delta(self) -> None:
        text = LONG_PREFIX + "，我们一起去公园散步吧。"
        expected = [LONG_PREFIX + "，", "我们一起去公园散步吧。"]
        sp1 = IncrementalSentenceSplitter(max_buffer=500, first_clause_min_chars=12)
        sp2 = IncrementalSentenceSplitter(max_buffer=500, first_clause_min_chars=12)
        self.assertEqual(sp1.push(text) + sp1.finish(), expected)
        self.assertEqual(_push_all(sp2, text), expected)

    def test_early_hard_end_before_comma(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=500, first_clause_min_chars=12)
        out = sp.push(LONG_PREFIX + "。后面，不会软切")
        self.assertEqual(out, [LONG_PREFIX + "。"])
        rest = sp.finish()
        self.assertEqual(rest, ["后面，不会软切"])

    def test_prefix_too_short_skips_comma(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=500, first_clause_min_chars=12)
        short = "短，" + LONG_PREFIX + "后面仍然没有句末标点"
        self.assertEqual(sp.push(short), [])
        tail = sp.finish()
        self.assertEqual(tail, [short.strip()])

    def test_only_first_segment_soft_split(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=500, first_clause_min_chars=12)
        body = LONG_PREFIX + "，" + LONG_PREFIX + "，第三段继续没有句号"
        first = sp.push(body)
        self.assertEqual(first, [LONG_PREFIX + "，"])
        second = sp.push("")
        self.assertEqual(second, [])
        done = sp.finish()
        self.assertEqual("".join(first) + "".join(second) + "".join(done), body)

    def test_english_comma_with_space_not_thousands(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=500, first_clause_min_chars=12)
        out = sp.push("Sales hit 1,000 units, then grew again today")
        self.assertEqual(len(out), 1)
        self.assertIn("1,000", out[0])
        self.assertTrue(out[0].endswith("units,"))
        self.assertEqual(sp.finish(), ["then grew again today"])

    def test_english_comma_space_soft_split(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=500, first_clause_min_chars=12)
        out = sp.push("Hello wonderful world, and then more text here")
        self.assertEqual(out, ["Hello wonderful world,"])
        self.assertEqual(sp.finish(), ["and then more text here"])

    def test_overflow_consumes_first_clause_slot(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=8, first_clause_min_chars=4)
        raw = "abcdefghij，klmnop"
        out = sp.push(raw)
        self.assertTrue(out)
        self.assertLess(sp.buffered_len, 8)
        rest = sp.finish()
        full = "".join(out) + "".join(rest)
        self.assertEqual(full.replace(" ", ""), raw.replace(" ", ""))
        self.assertFalse(sp._first_clause_eligible)  # noqa: SLF001

    def test_discard_resets_first_clause_state(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=500, first_clause_min_chars=12)
        sp.push(LONG_PREFIX + "，")
        sp.discard()
        self.assertTrue(sp._first_clause_eligible)  # noqa: SLF001
        out = sp.push(LONG_PREFIX + "，尾巴")
        self.assertEqual(out, [LONG_PREFIX + "，"])
        self.assertEqual(sp.finish(), ["尾巴"])

    def test_finish_no_duplicate_flush(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=500, first_clause_min_chars=12)
        sp.push(LONG_PREFIX + "，剩余")
        self.assertEqual(sp.finish(), ["剩余"])
        self.assertEqual(sp.finish(), [])

    def test_no_drop_no_dup_join(self) -> None:
        text = LONG_PREFIX + "，" + LONG_PREFIX + "，结尾部分"
        sp = IncrementalSentenceSplitter(max_buffer=500, first_clause_min_chars=12)
        parts = _push_all(sp, text)
        self.assertEqual("".join(parts), text)


class FirstClauseStreamingIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _assert_long_prefix(self)

    def _make(self, *, llm: Any, tts: Any) -> MikuAssistant:
        router = build_intent_router(
            windows_controller=FakeController(None),  # type: ignore[arg-type]
            chrome_searcher=FakeSearcher(query=None),  # type: ignore[arg-type]
            llm_client=llm,  # type: ignore[arg-type]
        )
        return MikuAssistant.from_dependencies(
            config=MemoryConfig(
                {
                    "assistant": {
                        "auto_tts": True,
                        "stream_sentence_max_buffer": 500,
                        "stream_speech_queue_size": 4,
                        "stream_speech_poll_interval": 0.02,
                    }
                }
            ),
            router=router,
            llm_client=llm,
            tts_engine=tts,
            character_interface=FakeCharacter(initialized=True),
        )

    def test_first_clause_reaches_tts_before_completed(self) -> None:
        first_speak = threading.Event()
        spoken: List[str] = []

        class MarkTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                first_speak.set()

            def shutdown(self) -> None:
                return None

        clause = LONG_PREFIX + "，"
        tail = "我们一起去公园散步然后再聊别的。"

        class ClauseLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen() -> Iterator[StreamEvent]:
                    try:
                        for ch in clause:
                            yield StreamEvent.text_delta(ch)
                        if not first_speak.wait(timeout=2.0):
                            raise AssertionError(
                                "first clause must reach TTS before stream completes"
                            )
                        for ch in tail:
                            yield StreamEvent.text_delta(ch)
                        full = clause + tail
                        yield StreamEvent.completed(full)
                    finally:
                        self.closed = True

                return gen()

            def generate_result(self, text: str, stream: bool = False, system=None):
                return GenerateResult.success("fallback")

        llm = ClauseLLM([])
        assistant = self._make(llm=llm, tts=MarkTTS())
        try:
            outcome = assistant.process_text_input_streaming("hi", use_tts=True)
            full = clause + tail
            self.assertEqual(outcome.stage, ReplyStage.OK)
            self.assertEqual(outcome.text, full)
            self.assertTrue(llm.closed)
            self.assertTrue(
                any(LONG_PREFIX in s for s in spoken),
                msg=f"spoken={spoken!r}",
            )
        finally:
            _shutdown_assistant_with_player(assistant)


if __name__ == "__main__":
    unittest.main()
