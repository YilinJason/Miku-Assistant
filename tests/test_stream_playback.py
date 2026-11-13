"""
增量分句、播报队列与流式助手路径的离线测试。

不启动真实 HTTP/音频/浏览器；用 Event 做可控同步（不用轮询 sleep 冒充证据）。
"""
from __future__ import annotations

import io
import threading
import unittest
from contextlib import redirect_stdout
from typing import Any, Iterator, List, Optional
from unittest.mock import patch

from src.cli import text_session
from src.core.assistant import MikuAssistant
from src.core.dispatch_outcome import DispatchOutcome, ReplyStage
from src.core.router_factory import build_intent_router
from src.core.sentence_splitter import IncrementalSentenceSplitter
from src.core.speech_queue import TurnSpeechPlayer
from src.llm.generate_result import GenerateResult, GenerateStatus
from src.llm.ollama_client import OllamaClient
from src.llm.stream_events import StreamEvent
from tests.test_assistant_orchestration import (
    FakeCharacter,
    FakeTTS,
    MemoryConfig,
)
from tests.test_generate_stream import ControllableStreamResponse, _ndjson
from tests.test_intent_router import FakeController, FakeLLM, FakeSearcher


def _join_thread(thread: threading.Thread, timeout: float = 2.0) -> None:
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise AssertionError(f"thread {thread.name} did not finish within {timeout}s")


class SentenceSplitterTests(unittest.TestCase):
    def test_chinese_cross_chunk_and_tail(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=500)
        self.assertEqual(sp.push("你好"), [])
        self.assertEqual(sp.push("世界。下一"), ["你好世界。"])
        self.assertEqual(sp.push("句！\n尾"), ["下一句！"])
        self.assertEqual(sp.finish(), ["尾"])
        self.assertEqual(sp.finish(), [])

    def test_english_period_cross_chunk(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=500)
        self.assertEqual(sp.push("Hello"), [])
        self.assertEqual(sp.push(" world. Next"), ["Hello world."])
        self.assertEqual(sp.push(" one!"), ["Next one!"])
        self.assertEqual(sp.finish(), [])

    def test_discard_drops_tail(self) -> None:
        sp = IncrementalSentenceSplitter()
        sp.push("未完")
        sp.discard()
        self.assertEqual(sp.finish(), [])

    def test_max_buffer_continuous_split_no_drop_no_dup(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=4)
        raw = "abcdefghijklmnop"  # 16 chars, no punctuation
        out = sp.push(raw)
        # push 应连续切开，缓冲残留 < max_buffer
        self.assertTrue(out)
        self.assertLess(sp.buffered_len, 4)
        joined = "".join(out) + (sp._buf if sp._buf else "")
        # finish 再刷出残留且继续切大尾
        out2 = sp.finish()
        full = "".join(out) + "".join(out2)
        self.assertEqual(full.replace(" ", ""), raw)
        self.assertEqual(len(full), len(raw))
        self.assertEqual(sp.buffered_len, 0)
        for piece in out + out2:
            self.assertLessEqual(len(piece), 4)

    def test_finish_splits_huge_tail(self) -> None:
        sp = IncrementalSentenceSplitter(max_buffer=5)
        sp.push("xy")
        # 模拟修复前「残留远超上限」场景：finish 必须连续切分而非一次刷出
        sp._buf = "1234567890ABCDE"  # noqa: SLF001
        parts = sp.finish()
        self.assertTrue(parts)
        self.assertEqual("".join(parts), "1234567890ABCDE")
        for p in parts:
            self.assertLessEqual(len(p), 5)
        self.assertEqual(sp.buffered_len, 0)


class SpeechQueueTests(unittest.TestCase):
    def test_order_and_stale_turn_with_blocked_consumer(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        spoken: List[str] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                entered.set()
                if not release.wait(timeout=3.0):
                    raise TimeoutError("release not set")

        player = TurnSpeechPlayer(GatedTTS(), maxsize=4, poll_interval=0.02)
        try:
            tid = player.begin_turn()
            self.assertTrue(player.enqueue(tid, "一"))
            self.assertTrue(entered.wait(timeout=2.0), "consumer should enter first speak")
            self.assertTrue(player.enqueue(tid, "二"))
            tid2 = player.begin_turn()
            self.assertTrue(player.enqueue(tid2, "新"))
            release.set()
            done = threading.Event()

            def _wait_new() -> None:
                for _ in range(100):
                    if "新" in spoken:
                        done.set()
                        return
                    threading.Event().wait(0.02)

            waiter = threading.Thread(target=_wait_new, name="wait-new", daemon=True)
            waiter.start()
            self.assertTrue(done.wait(timeout=2.0), "new turn item should play")
            _join_thread(waiter)
            self.assertIn("新", spoken)
            self.assertNotIn("二", spoken)
        finally:
            release.set()
            player.shutdown(timeout=2.0)

    def test_backpressure_waits_without_silent_drop(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        spoken: List[str] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                if text == "hold":
                    entered.set()
                    if not release.wait(timeout=3.0):
                        raise TimeoutError("release not set")

        player = TurnSpeechPlayer(GatedTTS(), maxsize=2, poll_interval=0.02)
        cancel = threading.Event()
        blocked_entered = threading.Event()
        blocked_done = threading.Event()
        blocked_result: List[bool] = []
        worker: Optional[threading.Thread] = None
        try:
            tid = player.begin_turn()
            self.assertTrue(player.enqueue(tid, "hold"))
            self.assertTrue(entered.wait(timeout=2.0))
            self.assertTrue(player.enqueue(tid, "q1"))
            self.assertTrue(player.enqueue(tid, "q2"))

            def _blocked_enqueue() -> None:
                try:
                    blocked_entered.set()
                    ok = player.enqueue(tid, "q3", cancel_event=cancel)
                    blocked_result.append(ok)
                finally:
                    blocked_done.set()

            worker = threading.Thread(
                target=_blocked_enqueue, name="blocked-enqueue", daemon=True
            )
            worker.start()
            self.assertTrue(blocked_entered.wait(timeout=2.0))
            # 仍阻塞：尚未完成
            self.assertFalse(blocked_done.wait(timeout=0.15))
            release.set()
            self.assertTrue(blocked_done.wait(timeout=2.0))
            self.assertEqual(blocked_result, [True])
            # 全部句子最终播出，无静默丢弃
            deadline = threading.Event()
            for _ in range(100):
                if set(spoken) >= {"hold", "q1", "q2", "q3"}:
                    deadline.set()
                    break
                threading.Event().wait(0.02)
            self.assertTrue(deadline.is_set(), f"spoken={spoken}")
        finally:
            release.set()
            cancel.set()
            if worker is not None:
                _join_thread(worker)
            player.shutdown(timeout=2.0)

    def test_backpressure_cancel_unblocks(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class GatedTTS:
            def speak(self, text: str) -> None:
                entered.set()
                release.wait(timeout=3.0)

        player = TurnSpeechPlayer(GatedTTS(), maxsize=1, poll_interval=0.02)
        cancel = threading.Event()
        done = threading.Event()
        result: List[bool] = []
        worker: Optional[threading.Thread] = None
        try:
            tid = player.begin_turn()
            self.assertTrue(player.enqueue(tid, "hold"))
            self.assertTrue(entered.wait(timeout=2.0))
            self.assertTrue(player.enqueue(tid, "full"))

            def _blocked() -> None:
                try:
                    result.append(player.enqueue(tid, "wait", cancel_event=cancel))
                finally:
                    done.set()

            worker = threading.Thread(target=_blocked, name="cancel-enqueue", daemon=True)
            worker.start()
            self.assertFalse(done.wait(timeout=0.15))
            cancel.set()
            self.assertTrue(done.wait(timeout=2.0))
            self.assertEqual(result, [False])
        finally:
            release.set()
            cancel.set()
            if worker is not None:
                _join_thread(worker)
            player.shutdown(timeout=2.0)

    def test_shutdown_discards_queued_not_yet_spoken(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        spoken: List[str] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                entered.set()
                release.wait(timeout=3.0)

        player = TurnSpeechPlayer(GatedTTS(), maxsize=8, poll_interval=0.02)
        try:
            tid = player.begin_turn()
            self.assertTrue(player.enqueue(tid, "playing"))
            self.assertTrue(entered.wait(timeout=2.0))
            self.assertTrue(player.enqueue(tid, "queued-a"))
            self.assertTrue(player.enqueue(tid, "queued-b"))
            player.stop_accepting()
            release.set()
            self.assertTrue(player.shutdown(timeout=2.0))
            self.assertEqual(spoken, ["playing"])
            self.assertNotIn("queued-a", spoken)
            self.assertNotIn("queued-b", spoken)
        finally:
            release.set()

    def test_consumer_failure_drains_same_turn_queue(self) -> None:
        entered = threading.Event()
        failed_done = threading.Event()
        spoken: List[str] = []

        class BoomThenOk:
            def __init__(self) -> None:
                self.calls = 0

            def speak(self, text: str) -> None:
                self.calls += 1
                spoken.append(text)
                entered.set()
                raise RuntimeError("boom")

        tts = BoomThenOk()
        player = TurnSpeechPlayer(tts, maxsize=4, poll_interval=0.02)
        try:
            tid = player.begin_turn()
            self.assertTrue(player.enqueue(tid, "first"))
            self.assertTrue(player.enqueue(tid, "second"))
            self.assertTrue(entered.wait(timeout=2.0))
            for _ in range(100):
                if player.consumer_failed:
                    failed_done.set()
                    break
                threading.Event().wait(0.02)
            self.assertTrue(failed_done.wait(timeout=2.0))
            self.assertFalse(player.enqueue(tid, "third"))
            self.assertEqual(spoken, ["first"])
            # 新轮次清除失败标志
            tid2 = player.begin_turn()
            self.assertFalse(player.consumer_failed)
            ok_tts_spoken = threading.Event()

            class OkTTS:
                def speak(self, text: str) -> None:
                    spoken.append(text)
                    ok_tts_spoken.set()

            player._tts = OkTTS()  # noqa: SLF001 — 测试替身切换
            self.assertTrue(player.enqueue(tid2, "fresh"))
            self.assertTrue(ok_tts_spoken.wait(timeout=2.0))
            self.assertIn("fresh", spoken)
            self.assertNotIn("second", spoken)
            self.assertNotIn("third", spoken)
        finally:
            player.shutdown(timeout=2.0)

    def test_stale_speak_failure_does_not_pollute_new_turn(self) -> None:
        """旧轮 speak 阻塞期间 begin_turn；旧 speak 抛错不得污染新轮。"""
        entered = threading.Event()
        release = threading.Event()
        new_spoken = threading.Event()
        spoken: List[str] = []

        class BoomAfterHold:
            def speak(self, text: str) -> None:
                if text == "old":
                    entered.set()
                    if not release.wait(timeout=3.0):
                        raise TimeoutError("release not set")
                    raise RuntimeError("old-boom")
                spoken.append(text)
                new_spoken.set()

        player = TurnSpeechPlayer(BoomAfterHold(), maxsize=4, poll_interval=0.02)
        try:
            old_tid = player.begin_turn()
            self.assertTrue(player.enqueue(old_tid, "old"))
            self.assertTrue(entered.wait(timeout=2.0))
            new_tid = player.begin_turn()
            self.assertTrue(player.enqueue(new_tid, "new"))
            release.set()
            self.assertTrue(new_spoken.wait(timeout=2.0))
            self.assertFalse(player.consumer_failed)
            self.assertIn("new", spoken)
            self.assertNotIn("old", spoken)
        finally:
            release.set()
            player.shutdown(timeout=2.0)

    def test_compare_and_invalidate_does_not_clear_new_turn(self) -> None:
        """屏障交错：旧轮 CAS 清理不得清掉新轮已入队项。"""
        entered = threading.Event()
        release = threading.Event()
        new_spoken = threading.Event()
        spoken: List[str] = []
        stale_ready = threading.Event()
        stale_go = threading.Event()
        cas_result: List[bool] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                if text == "hold-old":
                    entered.set()
                    release.wait(timeout=3.0)
                    return
                spoken.append(text)
                new_spoken.set()

        player = TurnSpeechPlayer(GatedTTS(), maxsize=4, poll_interval=0.02)
        worker: Optional[threading.Thread] = None
        try:
            old_tid = player.begin_turn()
            self.assertTrue(player.enqueue(old_tid, "hold-old"))
            self.assertTrue(entered.wait(timeout=2.0))

            def _stale_cas() -> None:
                try:
                    stale_ready.set()
                    stale_go.wait(timeout=3.0)
                    cas_result.append(player.compare_and_invalidate(old_tid))
                finally:
                    pass

            worker = threading.Thread(target=_stale_cas, name="stale-cas", daemon=True)
            worker.start()
            self.assertTrue(stale_ready.wait(timeout=2.0))

            new_tid = player.begin_turn()
            self.assertTrue(player.enqueue(new_tid, "keep-new"))
            stale_go.set()
            _join_thread(worker)
            self.assertEqual(cas_result, [False])
            self.assertEqual(player.turn_id, new_tid)

            release.set()
            self.assertTrue(new_spoken.wait(timeout=2.0))
            self.assertIn("keep-new", spoken)
        finally:
            release.set()
            stale_go.set()
            if worker is not None and worker.is_alive():
                _join_thread(worker)
            player.shutdown(timeout=2.0)


class FakeStreamLLM:
    """可控 generate_stream；记录调用次数。"""

    def __init__(self, events: List[StreamEvent]):
        self.events = events
        self.calls: List[str] = []
        self.closed = False

    def generate_stream(self, prompt: str, system=None):
        self.calls.append(prompt)
        events = list(self.events)

        def gen() -> Iterator[StreamEvent]:
            try:
                for ev in events:
                    yield ev
            finally:
                self.closed = True

        return gen()

    def generate_result(self, text: str, stream: bool = False, system=None):
        self.calls.append(text)
        return GenerateResult.success("fallback")


class StreamingAssistantTests(unittest.TestCase):
    def _make(
        self,
        *,
        llm: Any,
        tts: Optional[Any] = None,
        character: Optional[FakeCharacter] = None,
        controller: Optional[Any] = None,
        searcher: Optional[Any] = None,
        queue_size: int = 8,
    ) -> MikuAssistant:
        router = build_intent_router(
            windows_controller=controller or FakeController(None),  # type: ignore[arg-type]
            chrome_searcher=searcher or FakeSearcher(query=None),  # type: ignore[arg-type]
            llm_client=llm,  # type: ignore[arg-type]
        )
        return MikuAssistant.from_dependencies(
            config=MemoryConfig(
                {
                    "assistant": {
                        "auto_tts": True,
                        "stream_sentence_max_buffer": 500,
                        "stream_speech_queue_size": queue_size,
                        "stream_speech_poll_interval": 0.02,
                    }
                }
            ),
            router=router,
            llm_client=llm,
            tts_engine=tts,
            character_interface=character,
        )

    def test_first_sentence_reaches_tts_before_completed(self) -> None:
        first_speak = threading.Event()
        spoken: List[str] = []

        class MarkTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                first_speak.set()

            def shutdown(self) -> None:
                return None

        class SeqLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("你好")
                        yield StreamEvent.text_delta("世界。")
                        # 完成事件交付前，首句必须已进入 TTS
                        if not first_speak.wait(timeout=2.0):
                            raise AssertionError(
                                "first sentence must reach TTS before COMPLETED"
                            )
                        yield StreamEvent.text_delta("尾")
                        yield StreamEvent.completed("你好世界。尾")
                    finally:
                        self.closed = True

                return gen()

        llm = SeqLLM([])
        character = FakeCharacter(initialized=True)
        assistant = self._make(llm=llm, tts=MarkTTS(), character=character)
        try:
            deltas: List[str] = []
            outcome = assistant.process_text_input_streaming(
                "hi", use_tts=True, on_delta=deltas.append
            )
            self.assertEqual(deltas[0], "你好")
            self.assertTrue(outcome.allow_tts)
            self.assertEqual(outcome.text, "你好世界。尾")
            self.assertEqual(character.messages, ["你好世界。尾"])
            self.assertTrue(llm.closed)
            self.assertTrue(any("你好世界。" in s for s in spoken))
        finally:
            assistant.shutdown()

    def test_cancel_aborts_and_closes_stream(self) -> None:
        cancel = threading.Event()
        mid = threading.Event()

        class SlowLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("Hello. ")
                        mid.set()
                        # 等待外部取消
                        for _ in range(200):
                            if cancel.is_set():
                                break
                            threading.Event().wait(0.01)
                        yield StreamEvent.text_delta("More. ")
                        yield StreamEvent.completed("Hello. More. ")
                    finally:
                        self.closed = True

                return gen()

        llm = SlowLLM([])
        assistant = self._make(llm=llm, tts=FakeTTS())
        outcome_box: List[DispatchOutcome] = []
        done = threading.Event()

        def _run() -> None:
            try:
                outcome_box.append(
                    assistant.process_text_input_streaming(
                        "q", use_tts=True, cancel_event=cancel
                    )
                )
            finally:
                done.set()

        worker = threading.Thread(target=_run, name="stream-cancel", daemon=True)
        try:
            worker.start()
            self.assertTrue(mid.wait(timeout=2.0))
            cancel.set()
            self.assertTrue(done.wait(timeout=2.0))
            _join_thread(worker)
            self.assertTrue(llm.closed)
            self.assertTrue(outcome_box)
            self.assertFalse(outcome_box[0].allow_tts)
        finally:
            cancel.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()

    def test_consumer_failure_stops_turn(self) -> None:
        boom = threading.Event()

        class BoomTTS:
            def speak(self, text: str) -> None:
                boom.set()
                raise RuntimeError("tts-down")

            def shutdown(self) -> None:
                return None

        class LongLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("One. ")
                        if not boom.wait(timeout=2.0):
                            raise AssertionError("tts boom not reached")
                        for _ in range(50):
                            threading.Event().wait(0.02)
                        yield StreamEvent.text_delta("Two. ")
                        yield StreamEvent.completed("One. Two. ")
                    finally:
                        self.closed = True

                return gen()

        llm = LongLLM([])
        assistant = self._make(llm=llm, tts=BoomTTS(), queue_size=2)
        try:
            outcome = assistant.process_text_input_streaming("q", use_tts=True)
            self.assertFalse(outcome.allow_tts)
            self.assertIn("播报", outcome.text)
            self.assertTrue(llm.closed)
        finally:
            assistant.shutdown()

    def test_shutdown_sets_flag_and_skips_queued_playback(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        spoken: List[str] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                entered.set()
                release.wait(timeout=3.0)

            def shutdown(self) -> None:
                return None

        class HoldLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("Aaa. ")
                        yield StreamEvent.text_delta("Bbb. ")
                        yield StreamEvent.text_delta("Ccc. ")
                        if not entered.wait(timeout=2.0):
                            raise AssertionError("first speak not entered")
                        yield StreamEvent.completed("Aaa. Bbb. Ccc. ")
                    finally:
                        self.closed = True

                return gen()

        llm = HoldLLM([])
        assistant = self._make(llm=llm, tts=GatedTTS(), queue_size=8)
        try:
            outcome = assistant.process_text_input_streaming("q", use_tts=True)
            self.assertTrue(outcome.allow_tts)
            self.assertTrue(entered.is_set())

            shut_done = threading.Event()

            def _shutdown() -> None:
                try:
                    assistant.shutdown()
                finally:
                    shut_done.set()

            shut_worker = threading.Thread(
                target=_shutdown, name="assist-shutdown", daemon=True
            )
            shut_worker.start()
            # stop_accepting 丢弃待播后，释放当前 speak 以便有界 join 完成
            threading.Event().wait(0.05)
            release.set()
            self.assertTrue(shut_done.wait(timeout=3.0))
            _join_thread(shut_worker)
            self.assertTrue(assistant._shutting_down)  # noqa: SLF001
            self.assertIsNone(assistant._speech_player)  # noqa: SLF001
            self.assertLessEqual(len(spoken), 1)
            blocked = assistant.process_text_input_streaming("again", use_tts=True)
            self.assertIn("关闭", blocked.text)
        finally:
            release.set()
            if not assistant._shutdown_flow_done:  # noqa: SLF001
                assistant.shutdown()

    def test_new_input_invalidates_prior_queue_even_without_tts(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        spoken: List[str] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                entered.set()
                release.wait(timeout=3.0)

            def shutdown(self) -> None:
                return None

        llm = FakeStreamLLM(
            [
                StreamEvent.text_delta("Old one. "),
                StreamEvent.completed("Old one. "),
            ]
        )
        assistant = self._make(llm=llm, tts=GatedTTS())
        try:
            assistant.process_text_input_streaming("a", use_tts=True)
            self.assertTrue(entered.wait(timeout=2.0))
            # 新输入关闭 TTS：仍须失效旧队列
            with patch("src.core.assistant.threading.Thread"):
                assistant.process_text_input("搜索跳过", use_tts=False)
            release.set()
            # 给消费者退出当前 speak
            threading.Event().wait(0.05)
            # 旧队列中若有未播项不应再追加（仅可能有正在播的 Old）
            self.assertTrue(all("Old" in s or s == "Old one." for s in spoken))
        finally:
            release.set()
            assistant.shutdown()

    def test_error_prefix_success_still_speaks(self) -> None:
        first_speak = threading.Event()

        class MarkTTS(FakeTTS):
            def speak(self, text: str) -> None:
                super().speak(text)
                first_speak.set()

        llm = FakeStreamLLM(
            [
                StreamEvent.text_delta("错误: 台词。"),
                StreamEvent.completed("错误: 台词。"),
            ]
        )
        character = FakeCharacter(initialized=True)
        assistant = self._make(llm=llm, tts=MarkTTS(), character=character)
        try:
            outcome = assistant.process_text_input_streaming("x", use_tts=True)
            self.assertTrue(outcome.allow_tts)
            self.assertEqual(outcome.text, "错误: 台词。")
            self.assertEqual(character.messages, ["错误: 台词。"])
            self.assertTrue(first_speak.wait(timeout=2.0))
        finally:
            assistant.shutdown()

    def test_failure_no_tts_no_character_keeps_history_snapshot(self) -> None:
        class HistLLM(FakeStreamLLM):
            def __init__(self):
                super().__init__(
                    [
                        StreamEvent.text_delta("半"),
                        StreamEvent.failed(
                            GenerateStatus.CONNECTION_ERROR, "cut"
                        ),
                    ]
                )
                from src.llm.conversation_history import ConversationHistory

                self._history = ConversationHistory()

            @property
            def conversation_history(self):
                return self._history.snapshot()

            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("半")
                        yield StreamEvent.failed(
                            GenerateStatus.CONNECTION_ERROR, "cut"
                        )
                    finally:
                        self.closed = True

                return gen()

        llm = HistLLM()
        llm._history.commit_turn("seed", "ok")
        before = llm.conversation_history
        tts = FakeTTS()
        character = FakeCharacter(initialized=True)
        assistant = self._make(llm=llm, tts=tts, character=character)
        try:
            outcome = assistant.process_text_input_streaming("q", use_tts=True)
            self.assertFalse(outcome.allow_tts)
            self.assertFalse(outcome.allow_character)
            self.assertTrue(outcome.text.startswith("错误:"))
            self.assertEqual(character.messages, [])
            self.assertEqual(llm.conversation_history, before)
            self.assertTrue(llm.closed)
            self.assertEqual(tts.speak_calls, [])
        finally:
            assistant.shutdown()

    def test_system_priority_single_execution(self) -> None:
        controller = FakeController("sys-ok")
        llm = FakeStreamLLM([StreamEvent.completed("nope")])
        assistant = self._make(llm=llm, controller=controller)
        try:
            outcome = assistant.process_text_input_streaming("关机", use_tts=False)
            self.assertEqual(outcome.text, "sys-ok")
            self.assertEqual(controller.calls, ["关机"])
            self.assertEqual(llm.calls, [])
        finally:
            assistant.shutdown()

    def test_search_single_execution_with_stream_fallback(self) -> None:
        """无 generate_stream 时回退 handle_outcome，搜索仍只执行一次。"""
        searcher = FakeSearcher(query="python")
        llm = FakeLLM("should-not-run")
        assistant = self._make(llm=llm, searcher=searcher)
        try:
            outcome = assistant.process_text_input_streaming(
                "搜索 python", use_tts=False
            )
            self.assertEqual(outcome.text, "search-ok")
            self.assertEqual(searcher.extract_calls, ["搜索 python"])
            self.assertEqual(searcher.search_calls, ["python"])
            self.assertEqual(llm.calls, [])
        finally:
            assistant.shutdown()

    def test_llm_compat_fallback_single_handler_pass(self) -> None:
        """无 generate_stream 的聊天路径只走一次 LLM handler。"""
        llm = FakeLLM("plain")
        assistant = self._make(llm=llm)
        try:
            with patch("src.core.assistant.threading.Thread"):
                outcome = assistant.process_text_input_streaming("hello", use_tts=False)
            self.assertEqual(outcome.text, "plain")
            self.assertEqual(llm.calls, ["hello"])
        finally:
            assistant.shutdown()

    def test_nonstream_compat_unchanged(self) -> None:
        llm = FakeLLM("plain")
        assistant = self._make(llm=llm)
        try:
            with patch("src.core.assistant.threading.Thread"):
                self.assertEqual(
                    assistant.process_text_input("a", use_tts=False), "plain"
                )
            self.assertEqual(llm.calls, ["a"])
        finally:
            assistant.shutdown()

    def test_old_stream_cannot_display_forward_or_invalidate_new(self) -> None:
        """两可控假流：旧轮恢复后不得 on_delta/角色转发，也不得清空新轮。"""
        old_gate = threading.Event()
        old_resume = threading.Event()
        new_started = threading.Event()
        old_deltas: List[str] = []
        new_deltas: List[str] = []
        new_spoken = threading.Event()

        class MarkTTS(FakeTTS):
            def speak(self, text: str) -> None:
                super().speak(text)
                if "新句" in text:
                    new_spoken.set()

        class DualLLM:
            def __init__(self) -> None:
                self.calls: List[str] = []
                self.phase = 0

            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)
                self.phase += 1
                phase = self.phase

                def gen():
                    try:
                        if phase == 1:
                            yield StreamEvent.text_delta("旧半")
                            old_gate.set()
                            if not old_resume.wait(timeout=3.0):
                                raise TimeoutError("old resume")
                            # 新轮已开始后旧流继续产出
                            if not new_started.wait(timeout=3.0):
                                raise TimeoutError("new not started")
                            yield StreamEvent.text_delta("旧续。")
                            yield StreamEvent.completed("旧半旧续。")
                        else:
                            new_started.set()
                            yield StreamEvent.text_delta("新句。")
                            yield StreamEvent.completed("新句。")
                    finally:
                        pass

                return gen()

        llm = DualLLM()
        character = FakeCharacter(initialized=True)
        assistant = self._make(llm=llm, tts=MarkTTS(), character=character)
        old_done = threading.Event()
        old_outcome: List[DispatchOutcome] = []

        def _old() -> None:
            try:
                old_outcome.append(
                    assistant.process_text_input_streaming(
                        "old", use_tts=False, on_delta=old_deltas.append
                    )
                )
            finally:
                old_done.set()

        old_worker = threading.Thread(target=_old, name="old-stream", daemon=True)
        try:
            old_worker.start()
            self.assertTrue(old_gate.wait(timeout=2.0))
            new_outcome = assistant.process_text_input_streaming(
                "new", use_tts=True, on_delta=new_deltas.append
            )
            old_resume.set()
            self.assertTrue(old_done.wait(timeout=3.0))
            _join_thread(old_worker)
            self.assertTrue(new_outcome.allow_tts)
            self.assertEqual(new_outcome.text, "新句。")
            self.assertEqual(new_deltas, ["新句。"])
            # 旧轮在恢复后不得追加显示
            self.assertEqual(old_deltas, ["旧半"])
            self.assertNotIn("旧续。", old_deltas)
            # 角色仅新轮全文
            self.assertEqual(character.messages, ["新句。"])
            self.assertTrue(new_spoken.wait(timeout=2.0))
            # 旧轮 abort 不得导致新轮播报失败
            self.assertTrue(
                any("新句" in s for s in assistant.tts_engine.speak_calls)  # type: ignore[union-attr]
            )
        finally:
            old_resume.set()
            new_started.set()
            if old_worker.is_alive():
                _join_thread(old_worker)
            assistant.shutdown()

    def test_pre_cancelled_skips_all_handlers(self) -> None:
        cancel = threading.Event()
        cancel.set()
        controller = FakeController("sys-ok")
        searcher = FakeSearcher(query="kw")
        llm = FakeStreamLLM([StreamEvent.completed("nope")])
        assistant = self._make(
            llm=llm, controller=controller, searcher=searcher, tts=FakeTTS()
        )
        try:
            out_sys = assistant.process_text_input_streaming(
                "关机", use_tts=False, cancel_event=cancel
            )
            self.assertIn("取消", out_sys.text)
            self.assertEqual(controller.calls, [])

            out_search = assistant.process_text_input_streaming(
                "搜索 原神", use_tts=False, cancel_event=cancel
            )
            self.assertIn("取消", out_search.text)
            self.assertEqual(searcher.extract_calls, [])
            self.assertEqual(searcher.search_calls, [])

            out_llm = assistant.process_text_input_streaming(
                "hello", use_tts=False, cancel_event=cancel
            )
            self.assertIn("取消", out_llm.text)
            self.assertEqual(llm.calls, [])
        finally:
            assistant.shutdown()

    def test_shutdown_rejects_streaming_and_nonstream(self) -> None:
        controller = FakeController("sys-ok")
        searcher = FakeSearcher(query="kw")
        llm = FakeStreamLLM([StreamEvent.completed("nope")])
        assistant = self._make(llm=llm, controller=controller, searcher=searcher)
        assistant.shutdown()
        stream_out = assistant.process_text_input_streaming("关机", use_tts=False)
        self.assertIn("关闭", stream_out.text)
        self.assertEqual(controller.calls, [])
        self.assertEqual(searcher.extract_calls, [])
        self.assertEqual(llm.calls, [])
        plain = assistant.process_text_input("关机", use_tts=False)
        self.assertIn("关闭", plain)
        self.assertEqual(controller.calls, [])

    def test_bc_entry_interleave_does_not_clear_c_speech(self) -> None:
        """B 在 claim 前暂停，C 完成归属后 B 恢复不得清掉 C 的待播与身份。"""
        entered_c = threading.Event()
        release_c = threading.Event()
        spoken: List[str] = []
        b_at_claim = threading.Event()
        c_claimed = threading.Event()
        b_done = threading.Event()

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                if "C句" in text:
                    entered_c.set()
                    release_c.wait(timeout=3.0)

            def shutdown(self) -> None:
                return None

        class DualLLM:
            def __init__(self) -> None:
                self.calls: List[str] = []

            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    if prompt == "b":
                        yield StreamEvent.text_delta("B半。")
                        yield StreamEvent.completed("B半。")
                    else:
                        yield StreamEvent.text_delta("C句。")
                        yield StreamEvent.completed("C句。")

                return gen()

        llm = DualLLM()
        assistant = self._make(llm=llm, tts=GatedTTS(), queue_size=8)
        real_claim = assistant._claim_speech_for_turn
        claim_n = {"n": 0}

        def _claim_hook(turn, external_cancel=None):
            claim_n["n"] += 1
            if claim_n["n"] == 1:
                b_at_claim.set()
                if not c_claimed.wait(timeout=3.0):
                    raise TimeoutError("C did not claim")
            return real_claim(turn, external_cancel)

        assistant._claim_speech_for_turn = _claim_hook  # type: ignore[method-assign]
        b_outcome: List[DispatchOutcome] = []

        def _run_b() -> None:
            try:
                b_outcome.append(
                    assistant.process_text_input_streaming("b", use_tts=True)
                )
            finally:
                b_done.set()

        worker = threading.Thread(target=_run_b, name="entry-B", daemon=True)
        try:
            worker.start()
            self.assertTrue(b_at_claim.wait(timeout=2.0))
            c_out = assistant.process_text_input_streaming("c", use_tts=True)
            c_claimed.set()
            self.assertTrue(b_done.wait(timeout=3.0))
            _join_thread(worker)

            self.assertEqual(c_out.text, "C句。")
            self.assertTrue(c_out.allow_tts)
            self.assertTrue(entered_c.wait(timeout=2.0))
            player = assistant._speech_player  # noqa: SLF001
            self.assertIsNotNone(player)
            assert player is not None
            # C 仍拥有当前播报轮；B 恢复后的 CAS/无条件 invalidate 不得抬高轮次清队
            c_speech = player.turn_id
            self.assertGreater(c_speech, 0)
            release_c.set()
            # 仅 C 的句子应被消费（B 在 claim 前已失去所有权）
            for _ in range(50):
                if "C句。" in spoken or any("C句" in s for s in spoken):
                    break
                threading.Event().wait(0.02)
            self.assertTrue(any("C句" in s for s in spoken))
            self.assertFalse(any("B半" in s for s in spoken))
            self.assertEqual(player.turn_id, c_speech)
            self.assertTrue(b_outcome)
            self.assertEqual(b_outcome[0].stage, ReplyStage.CANCELLED)
        finally:
            c_claimed.set()
            release_c.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()

    def test_playback_failed_after_complete_real_client(self) -> None:
        """真实客户端 COMPLETED 后尾句入队失败：stage=PLAYBACK_FAILED，历史提交一次。"""
        client = OllamaClient(base_url="http://127.0.0.1:9", model="test")
        client._history.commit_turn("seed", "ok")  # noqa: SLF001
        before_len = len(client.conversation_history)
        # 增量无句末，COMPLETED 时 finish 才刷尾句 → 入队失败在生成提交之后
        resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "已经生成的完整回答"}, "done": False},
                {"message": {"content": ""}, "done": True},
            )
        )

        class RejectEnqueuePlayer:
            def __init__(self) -> None:
                self.turn_id = 1
                self.consumer_failed = False
                self.stopped = False

            def begin_turn(self) -> int:
                self.turn_id += 1
                return self.turn_id

            def compare_and_invalidate(self, expected: int) -> bool:
                if self.turn_id != expected:
                    return False
                self.turn_id += 1
                return True

            def enqueue(self, turn_id, text, **kwargs):
                return False

            def stop_accepting(self) -> None:
                return None

            def shutdown(self, timeout: float = 2.0) -> bool:
                return True

        assistant = self._make(llm=client, tts=FakeTTS())
        fake_player = RejectEnqueuePlayer()
        assistant._speech_player = fake_player  # noqa: SLF001
        real_claim = assistant._claim_speech_for_turn

        def _claim(turn, external_cancel=None):
            with assistant._turn_lock:  # noqa: SLF001
                if assistant._active_turn is not turn:  # noqa: SLF001
                    return None
                turn.speech_turn_id = fake_player.begin_turn()
                return fake_player  # type: ignore[return-value]

        assistant._claim_speech_for_turn = _claim  # type: ignore[method-assign]
        try:
            with patch("src.llm.ollama_client.requests.post", return_value=resp):
                outcome = assistant.process_text_input_streaming(
                    "q", use_tts=True
                )
            self.assertEqual(outcome.stage, ReplyStage.PLAYBACK_FAILED)
            self.assertFalse(outcome.allow_tts)
            self.assertIn("已经生成的完整回答", outcome.text)
            hist = client.conversation_history
            self.assertEqual(len(hist), before_len + 2)
            self.assertEqual(hist[-1]["content"], "已经生成的完整回答")
            self.assertTrue(resp.closed)
        finally:
            assistant._claim_speech_for_turn = real_claim  # type: ignore[method-assign]
            assistant.shutdown()


    def test_cancel_on_delta_does_not_commit_real_client_history(self) -> None:
        """真实 OllamaClient：on_delta 取消后不得再 next 触发 commit_turn。"""
        client = OllamaClient(base_url="http://127.0.0.1:9", model="test")
        client._history.commit_turn("seed", "ok")  # noqa: SLF001
        before = client.conversation_history
        # 单行 content+done=True：第一次 next 出 delta，第二次 next 才会 _complete
        resp = ControllableStreamResponse(
            _ndjson({"message": {"content": "半句"}, "done": True})
        )
        cancel = threading.Event()

        def _on_delta(_delta: str) -> None:
            cancel.set()

        assistant = self._make(llm=client, tts=FakeTTS())
        try:
            with patch("src.llm.ollama_client.requests.post", return_value=resp):
                outcome = assistant.process_text_input_streaming(
                    "q",
                    use_tts=False,
                    on_delta=_on_delta,
                    cancel_event=cancel,
                )
            self.assertIn("取消", outcome.text)
            self.assertEqual(outcome.stage, ReplyStage.CANCELLED)
            self.assertFalse(outcome.allow_tts)
            self.assertEqual(client.conversation_history, before)
            self.assertTrue(resp.closed)
        finally:
            assistant.shutdown()

    def test_two_deltas_then_done_cancelled_by_new_turn(self) -> None:
        """两行 delta 后 done；旧轮被新轮替代时不得再 next，历史不得提交。"""
        client = OllamaClient(base_url="http://127.0.0.1:9", model="test")
        client._history.commit_turn("seed", "ok")  # noqa: SLF001
        before = list(client.conversation_history)

        after_first = threading.Event()
        new_started = threading.Event()
        old_done = threading.Event()

        resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "一"}, "done": False},
                {"message": {"content": "二"}, "done": False},
                {"message": {"content": ""}, "done": True},
            )
        )
        new_resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "新。"}, "done": False},
                {"message": {"content": ""}, "done": True},
            )
        )
        post_calls = {"n": 0}

        def _post(*_a, **_k):
            post_calls["n"] += 1
            return resp if post_calls["n"] == 1 else new_resp

        def _on_delta(_delta: str) -> None:
            after_first.set()
            # 在 on_delta 内等待新轮取消本轮，返回后再检 cancel，避免二次 next
            if not new_started.wait(timeout=3.0):
                raise TimeoutError("new turn did not start")

        assistant = self._make(llm=client, tts=FakeTTS())
        old_outcome: List[DispatchOutcome] = []

        def _old() -> None:
            try:
                old_outcome.append(
                    assistant.process_text_input_streaming(
                        "old", use_tts=False, on_delta=_on_delta
                    )
                )
            finally:
                old_done.set()

        worker = threading.Thread(target=_old, name="old-real-client", daemon=True)
        try:
            with patch("src.llm.ollama_client.requests.post", side_effect=_post):
                worker.start()
                self.assertTrue(after_first.wait(timeout=2.0))
                new_out = assistant.process_text_input_streaming(
                    "new", use_tts=False
                )
                new_started.set()
                self.assertTrue(old_done.wait(timeout=3.0))
                _join_thread(worker)

            self.assertTrue(old_outcome)
            self.assertIn("取消", old_outcome[0].text)
            self.assertEqual(old_outcome[0].stage, ReplyStage.CANCELLED)
            self.assertEqual(new_out.text, "新。")
            self.assertTrue(new_out.allow_tts)
            hist = client.conversation_history
            self.assertEqual(hist[0], before[0])
            self.assertEqual(hist[1], before[1])
            self.assertEqual(len(before), 2)
            self.assertEqual(hist[-2], {"role": "user", "content": "new"})
            self.assertEqual(hist[-1], {"role": "assistant", "content": "新。"})
            self.assertTrue(all(m.get("content") not in ("一", "一二") for m in hist))
            self.assertTrue(resp.closed)
            self.assertTrue(new_resp.closed)
            # 取消后未再拉取第二行
            self.assertEqual(resp.lines_read, 1)
        finally:
            new_started.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()


class CliStreamingPrintTests(unittest.TestCase):
    def test_cli_does_not_reprint_full_text_after_deltas(self) -> None:
        llm = FakeStreamLLM(
            [
                StreamEvent.text_delta("甲"),
                StreamEvent.text_delta("乙。"),
                StreamEvent.completed("甲乙。"),
            ]
        )
        assistant = StreamingAssistantTests()._make(llm=llm, tts=FakeTTS())
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                text_session._print_reply_streaming(assistant, "q", use_tts=False)
            out = buf.getvalue()
            self.assertIn("甲", out)
            self.assertIn("乙。", out)
            self.assertEqual(out.count("甲乙。"), 1)
            self.assertNotIn("[生成失败]", out)
            self.assertNotIn("[播报失败]", out)
        finally:
            assistant.shutdown()

    def test_cli_playback_failed_short_tip_no_reprint(self) -> None:
        """播放失败不得标成生成失败，也不得重复全文。"""
        full = "已经生成的完整回答。"

        class FakeAssist:
            def process_text_input_streaming(self, user_input, use_tts=None, on_delta=None):
                if on_delta:
                    on_delta(full)
                return DispatchOutcome.playback_failed(full)

        buf = io.StringIO()
        with redirect_stdout(buf):
            text_session._print_reply_streaming(FakeAssist(), "q", use_tts=True)  # type: ignore[arg-type]
        out = buf.getvalue()
        self.assertEqual(out.count(full), 1)
        self.assertIn("[播报失败]", out)
        self.assertNotIn("[生成失败]", out)

    def test_cli_generation_failed_distinct_from_ok_prefix(self) -> None:
        llm = FakeStreamLLM(
            [
                StreamEvent.text_delta("半"),
                StreamEvent.failed(GenerateStatus.CONNECTION_ERROR, "cut"),
            ]
        )
        assistant = StreamingAssistantTests()._make(llm=llm, tts=FakeTTS())
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                text_session._print_reply_streaming(assistant, "q", use_tts=False)
            out = buf.getvalue()
            self.assertIn("[生成失败]", out)
            self.assertNotIn("[播报失败]", out)
        finally:
            assistant.shutdown()

    def test_cli_error_prefix_success_not_marked_failed(self) -> None:
        llm = FakeStreamLLM(
            [
                StreamEvent.text_delta("错误: 台词。"),
                StreamEvent.completed("错误: 台词。"),
            ]
        )
        assistant = StreamingAssistantTests()._make(llm=llm, tts=FakeTTS())
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                text_session._print_reply_streaming(assistant, "q", use_tts=False)
            out = buf.getvalue()
            self.assertIn("错误: 台词。", out)
            self.assertNotIn("[生成失败]", out)
            self.assertEqual(out.count("错误: 台词。"), 1)
        finally:
            assistant.shutdown()

    def test_cli_search_executes_once(self) -> None:
        searcher = FakeSearcher(query="原神")
        llm = FakeStreamLLM([StreamEvent.completed("nope")])
        assistant = StreamingAssistantTests()._make(llm=llm, searcher=searcher)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                text_session._print_reply_streaming(
                    assistant, "搜索 原神", use_tts=False
                )
            self.assertEqual(searcher.extract_calls, ["搜索 原神"])
            self.assertEqual(searcher.search_calls, ["原神"])
            self.assertEqual(llm.calls, [])
        finally:
            assistant.shutdown()


if __name__ == "__main__":
    unittest.main()
