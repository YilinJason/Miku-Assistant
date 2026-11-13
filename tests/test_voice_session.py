"""
语音会话增量回复与播报完成契约的离线测试。

全部 fake；不启真实麦克风、扬声器、RVC、Ollama 或系统控制。
"""
from __future__ import annotations

import io
import threading
import unittest
from contextlib import redirect_stdout
from typing import Any, Iterator, List, Optional
from unittest.mock import patch

from src.cli import voice_session as voice_mod
from src.cli.voice_preflight import VoicePreflightReport
from src.cli.voice_session import VoiceSession, run_voice_session
from src.core.assistant import MikuAssistant
from src.core.dispatch_outcome import DispatchOutcome, ReplyStage
from src.core.router_factory import build_intent_router
from src.core.speech_queue import PlaybackStatus, TurnSpeechPlayer
from src.llm.generate_result import GenerateResult, GenerateStatus
from src.llm.ollama_client import OllamaClient
from src.llm.stream_events import StreamEvent
from src.voice.recognition import RecognitionResult, RecognitionStatus
from tests.test_assistant_orchestration import FakeCharacter, FakeTTS, MemoryConfig
from tests.test_generate_stream import ControllableStreamResponse, _ndjson
from tests.test_intent_router import FakeController, FakeLLM, FakeSearcher
from tests.test_stream_playback import FakeStreamLLM, _join_thread


def _bind_fake_recognition(assistant: MikuAssistant, listener) -> None:
    """把既有字符串 STT fake 适配到新的结构化助手入口。"""

    def recognize(continuous=False, *, cancel_event=None):
        text = listener(continuous=continuous)
        if text:
            return RecognitionResult.success(text)
        return RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED)

    assistant.recognize_voice_input = recognize  # type: ignore[method-assign]


class PlaybackWaitContractTests(unittest.TestCase):
    def test_wait_not_completed_while_speak_blocks_despite_empty_queue(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        done = threading.Event()
        result: List[PlaybackStatus] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                entered.set()
                release.wait(timeout=3.0)

        player = TurnSpeechPlayer(GatedTTS(), maxsize=4, poll_interval=0.02)
        try:
            tid = player.begin_turn()
            self.assertTrue(player.enqueue(tid, "only"))
            player.mark_production_finished(tid)

            def _wait() -> None:
                try:
                    result.append(
                        player.wait_for_turn(tid, timeout=2.0)
                    )
                finally:
                    done.set()

            worker = threading.Thread(target=_wait, name="wait-speak", daemon=True)
            worker.start()
            self.assertTrue(entered.wait(timeout=2.0))
            # 队列已空但仍在 speak —— 不得 COMPLETED
            self.assertFalse(done.wait(timeout=0.15))
            release.set()
            self.assertTrue(done.wait(timeout=2.0))
            _join_thread(worker)
            self.assertEqual(result, [PlaybackStatus.COMPLETED])
        finally:
            release.set()
            player.shutdown(timeout=2.0)

    def test_failed_invalidate_shutdown_timeout_wake_waiters(self) -> None:
        # FAILED
        boom_entered = threading.Event()

        class BoomTTS:
            def speak(self, text: str) -> None:
                boom_entered.set()
                raise RuntimeError("boom")

        p1 = TurnSpeechPlayer(BoomTTS(), maxsize=2, poll_interval=0.02)
        try:
            tid = p1.begin_turn()
            self.assertTrue(p1.enqueue(tid, "x"))
            p1.mark_production_finished(tid)
            status = p1.wait_for_turn(tid, timeout=2.0)
            self.assertTrue(boom_entered.wait(timeout=2.0))
            self.assertEqual(status, PlaybackStatus.FAILED)
        finally:
            p1.shutdown(timeout=2.0)

        # INVALIDATED via begin_turn
        hold = threading.Event()
        release = threading.Event()

        class HoldTTS:
            def speak(self, text: str) -> None:
                hold.set()
                release.wait(timeout=3.0)

        p2 = TurnSpeechPlayer(HoldTTS(), maxsize=2, poll_interval=0.02)
        waiter_done = threading.Event()
        got: List[PlaybackStatus] = []
        try:
            tid = p2.begin_turn()
            self.assertTrue(p2.enqueue(tid, "a"))
            p2.mark_production_finished(tid)

            def _w() -> None:
                try:
                    got.append(p2.wait_for_turn(tid, timeout=3.0))
                finally:
                    waiter_done.set()

            t = threading.Thread(target=_w, daemon=True)
            t.start()
            self.assertTrue(hold.wait(timeout=2.0))
            p2.begin_turn()  # invalidate old
            self.assertTrue(waiter_done.wait(timeout=2.0))
            _join_thread(t)
            self.assertEqual(got, [PlaybackStatus.INVALIDATED])
        finally:
            release.set()
            p2.shutdown(timeout=2.0)

        # TIMEOUT
        block = threading.Event()

        class SlowTTS:
            def speak(self, text: str) -> None:
                block.wait(timeout=5.0)

        p3 = TurnSpeechPlayer(SlowTTS(), maxsize=2, poll_interval=0.02)
        try:
            tid = p3.begin_turn()
            self.assertTrue(p3.enqueue(tid, "slow"))
            p3.mark_production_finished(tid)
            st = p3.wait_for_turn(tid, timeout=0.2)
            self.assertEqual(st, PlaybackStatus.TIMEOUT)
        finally:
            block.set()
            p3.shutdown(timeout=2.0)

        # STOPPED
        p4 = TurnSpeechPlayer(FakeTTS(), maxsize=2, poll_interval=0.02)
        tid = p4.begin_turn()
        # 故意不 mark_production_finished，使等待阻塞直至 shutdown
        waiter_done2 = threading.Event()
        got2: List[PlaybackStatus] = []

        def _wait_stop() -> None:
            try:
                got2.append(p4.wait_for_turn(tid, timeout=3.0))
            finally:
                waiter_done2.set()

        tw = threading.Thread(target=_wait_stop, daemon=True)
        tw.start()
        self.assertFalse(waiter_done2.wait(timeout=0.15))
        p4.shutdown(timeout=1.0)
        self.assertTrue(waiter_done2.wait(timeout=2.0))
        _join_thread(tw)
        self.assertEqual(got2, [PlaybackStatus.STOPPED])


class VoiceStreamTurnTests(unittest.TestCase):
    def _make(
        self,
        *,
        llm: Any,
        tts: Any = None,
        controller: Any = None,
        searcher: Any = None,
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
                        "stream_reply": True,
                        "stream_speech_queue_size": 8,
                        "stream_speech_poll_interval": 0.02,
                        "stream_playback_wait_timeout": 5.0,
                    }
                }
            ),
            router=router,
            llm_client=llm,
            tts_engine=tts,
        )

    def test_next_stt_after_last_speak_with_early_sentence(self) -> None:
        """首句在 COMPLETED 前已 speak；下一轮 STT 仅在最后一句 speak 返回后。"""
        first_speak = threading.Event()
        second_entered = threading.Event()
        second_release = threading.Event()
        completed_pulled = threading.Event()
        speak_order: List[str] = []
        stt_times: List[str] = []

        class SeqTTS:
            def speak(self, text: str) -> None:
                speak_order.append(text)
                if "第一句" in text:
                    first_speak.set()
                if "第二句" in text:
                    second_entered.set()
                    second_release.wait(timeout=3.0)

            def shutdown(self) -> None:
                return None

        class SeqLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("第一句。")
                        if not first_speak.wait(timeout=2.0):
                            raise AssertionError("first sentence not spoken before more")
                        yield StreamEvent.text_delta("第二句。")
                        completed_pulled.set()
                        yield StreamEvent.completed("第一句。第二句。")
                    finally:
                        self.closed = True

                return gen()

        llm = SeqLLM([])
        assistant = self._make(llm=llm, tts=SeqTTS())
        stt_calls = {"n": 0}

        def fake_voice(continuous=False):
            stt_calls["n"] += 1
            if stt_calls["n"] == 1:
                return "用户一句"
            # 第二次 STT：必须在第二句 speak 已进入之后由 wait 释放前不能发生
            stt_times.append("stt2")
            if not second_entered.is_set():
                raise AssertionError("STT resumed before last speak started")
            return ""

        _bind_fake_recognition(assistant, fake_voice)
        session = VoiceSession(
            assistant, use_tts=True, stream_reply=True, stop_keywords=("退出x",)
        )

        # 限制循环：第二轮空识别后靠停止——改用直接测助手 wait
        outcome_box: List[DispatchOutcome] = []
        done = threading.Event()

        def _run() -> None:
            try:
                outcome_box.append(
                    assistant.process_text_input_streaming(
                        "用户一句",
                        use_tts=True,
                        wait_for_playback=True,
                        playback_timeout=5.0,
                    )
                )
            finally:
                done.set()

        worker = threading.Thread(target=_run, name="voice-wait", daemon=True)
        try:
            worker.start()
            self.assertTrue(first_speak.wait(timeout=2.0))
            self.assertTrue(completed_pulled.wait(timeout=2.0))
            self.assertTrue(second_entered.wait(timeout=2.0))
            self.assertFalse(done.wait(timeout=0.15), "must wait until last speak returns")
            second_release.set()
            self.assertTrue(done.wait(timeout=2.0))
            _join_thread(worker)
            self.assertTrue(outcome_box)
            self.assertEqual(outcome_box[0].stage, ReplyStage.OK)
            self.assertEqual(
                outcome_box[0].playback_status, PlaybackStatus.COMPLETED
            )
            self.assertEqual(speak_order[0], "第一句。")
        finally:
            second_release.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()

    def test_system_search_tracked_tts_single_exec(self) -> None:
        speak_done = threading.Event()

        class MarkTTS(FakeTTS):
            def speak(self, text: str) -> None:
                super().speak(text)
                speak_done.set()

        controller = FakeController("sys-ok")
        llm = FakeStreamLLM([StreamEvent.completed("nope")])
        assistant = self._make(llm=llm, tts=MarkTTS(), controller=controller)
        try:
            deltas = []
            out = assistant.process_text_input_streaming(
                "关机",
                use_tts=True,
                wait_for_playback=True,
                on_delta=deltas.append,
            )
            self.assertEqual(out.text, "sys-ok")
            self.assertEqual(deltas, ["sys-ok"])
            self.assertEqual(controller.calls, ["关机"])
            self.assertEqual(llm.calls, [])
            self.assertTrue(speak_done.wait(timeout=2.0))
            self.assertEqual(out.playback_status, PlaybackStatus.COMPLETED)

            searcher = FakeSearcher(query="kw")
            assistant2 = self._make(
                llm=FakeLLM("x"), tts=MarkTTS(), searcher=searcher
            )
            out2 = assistant2.process_text_input_streaming(
                "搜索 kw", use_tts=True, wait_for_playback=True
            )
            self.assertEqual(searcher.extract_calls, ["搜索 kw"])
            self.assertEqual(searcher.search_calls, ["kw"])
            self.assertTrue(out2.playback_status is PlaybackStatus.COMPLETED)
            assistant2.shutdown()
        finally:
            assistant.shutdown()

    def test_playback_timeout_keeps_history(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="test")
        client._history.commit_turn("seed", "ok")  # noqa: SLF001
        before = list(client.conversation_history)
        resp = ControllableStreamResponse(
            _ndjson(
                {"message": {"content": "短。"}, "done": False},
                {"message": {"content": ""}, "done": True},
            )
        )
        block = threading.Event()

        class SlowTTS:
            def speak(self, text: str) -> None:
                block.wait(timeout=5.0)

            def shutdown(self) -> None:
                return None

        assistant = self._make(llm=client, tts=SlowTTS())
        try:
            with patch("src.llm.ollama_client.requests.post", return_value=resp):
                out = assistant.process_text_input_streaming(
                    "q",
                    use_tts=True,
                    wait_for_playback=True,
                    playback_timeout=0.2,
                )
            self.assertEqual(out.stage, ReplyStage.PLAYBACK_FAILED)
            self.assertEqual(out.playback_status, PlaybackStatus.TIMEOUT)
            hist = client.conversation_history
            self.assertEqual(len(hist), len(before) + 2)
            self.assertEqual(hist[-1]["content"], "短。")
        finally:
            block.set()
            assistant.shutdown()

    def test_stream_off_uses_tracked_backend_once(self) -> None:
        llm = FakeLLM("plain")
        assistant = self._make(llm=llm, tts=FakeTTS())
        _bind_fake_recognition(assistant, lambda continuous=False: "hi")
        legacy_calls = {"n": 0}
        tracked_calls: List[dict] = []
        real_legacy = assistant.process_text_input
        real_tracked = assistant.process_text_input_streaming

        def counted_legacy(text, use_tts=None):
            legacy_calls["n"] += 1
            return real_legacy(text, use_tts=use_tts)

        def counted_tracked(text, **kwargs):
            tracked_calls.append(dict(kwargs))
            return real_tracked(text, **kwargs)

        assistant.process_text_input = counted_legacy  # type: ignore[method-assign]
        assistant.process_text_input_streaming = counted_tracked  # type: ignore[method-assign]
        session = VoiceSession(
            assistant,
            use_tts=False,
            stream_reply=False,
            stop_keywords=("退出",),
        )
        # one utterance then stop
        seq = {"i": 0}

        def stt(continuous=False):
            seq["i"] += 1
            if seq["i"] == 1:
                return "hi"
            return "退出"

        _bind_fake_recognition(assistant, stt)
        try:
            with patch("src.core.assistant.threading.Thread"):
                self.assertEqual(session.run(), "exit")
            self.assertEqual(legacy_calls["n"], 0)
            self.assertEqual(len(tracked_calls), 1)
            self.assertTrue(tracked_calls[0]["wait_for_playback"])
            self.assertIsNone(tracked_calls[0]["on_delta"])
            self.assertEqual(llm.calls, ["hi"])
        finally:
            assistant.shutdown()

    def test_stream_off_does_not_reopen_stt_during_playback(self) -> None:
        speak_entered = threading.Event()
        release_speak = threading.Event()
        second_stt = threading.Event()

        class BlockingTTS:
            def speak(self, text: str) -> None:
                speak_entered.set()
                release_speak.wait(timeout=3.0)

            def shutdown(self) -> None:
                release_speak.set()

        assistant = self._make(llm=FakeLLM("plain"), tts=BlockingTTS())
        calls = {"n": 0}

        def stt(continuous=False):
            calls["n"] += 1
            if calls["n"] == 1:
                return "hi"
            second_stt.set()
            return "退出"

        _bind_fake_recognition(assistant, stt)
        session = VoiceSession(
            assistant,
            use_tts=True,
            stream_reply=False,
            stop_keywords=("退出",),
        )
        result: List[str] = []
        worker = threading.Thread(target=lambda: result.append(session.run()), daemon=True)
        try:
            worker.start()
            self.assertTrue(speak_entered.wait(timeout=2.0))
            self.assertFalse(
                second_stt.wait(timeout=0.15),
                "microphone reopened while assistant playback was still active",
            )
            release_speak.set()
            self.assertTrue(second_stt.wait(timeout=2.0))
            _join_thread(worker)
            self.assertEqual(result, ["exit"])
        finally:
            release_speak.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()

    def test_stream_off_exception_does_not_reopen_stt(self) -> None:
        assistant = self._make(llm=FakeLLM("plain"), tts=FakeTTS())
        calls = {"n": 0}

        def stt(continuous=False):
            calls["n"] += 1
            if calls["n"] > 1:
                raise AssertionError("microphone reopened after an uncertain playback error")
            return "hi"

        def fail_after_dispatch(text):
            raise RuntimeError("playback state unknown")

        _bind_fake_recognition(assistant, stt)
        session = VoiceSession(
            assistant,
            use_tts=True,
            stream_reply=False,
            stop_keywords=("退出",),
        )
        session._handle_nonstreaming_turn = fail_after_dispatch  # type: ignore[method-assign]
        try:
            self.assertEqual(session.run(), "unsafe_pause")
            self.assertEqual(calls["n"], 1)
        finally:
            assistant.shutdown()

    def test_stream_entry_flags_from_run_voice_session(self) -> None:
        captured: List[Optional[bool]] = []

        class CaptureSession(VoiceSession):
            def __init__(self, *a, **k):
                captured.append(k.get("stream_reply"))
                super().__init__(*a, **k)

            def run(self):
                return "exit"

        assistant = self._make(llm=FakeLLM("x"), tts=FakeTTS())
        try:
            with patch.object(voice_mod, "VoiceSession", CaptureSession), patch.object(
                voice_mod,
                "check_voice_preflight",
                return_value=VoicePreflightReport(ok=True),
            ):
                run_voice_session(assistant, stream_reply=True)
                run_voice_session(assistant, stream_reply=False)
                assistant.config.data.setdefault("assistant", {})["stream_reply"] = True
                run_voice_session(assistant, stream_reply=None)
            self.assertEqual(captured, [True, False, True])
        finally:
            assistant.shutdown()

    def test_voice_cli_stages_printable(self) -> None:
        full = "错误: 台词。"
        llm = FakeStreamLLM(
            [
                StreamEvent.text_delta(full),
                StreamEvent.completed(full),
            ]
        )
        assistant = self._make(llm=llm, tts=FakeTTS())
        session = VoiceSession(assistant, use_tts=True, stream_reply=True)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                session._handle_streaming_turn("q")
            out = buf.getvalue()
            self.assertIn(full, out)
            self.assertNotIn("[生成失败]", out)
            self.assertEqual(out.count(full), 1)
        finally:
            assistant.shutdown()


class VoiceSessionRunBoundaryTests(unittest.TestCase):
    """真实 VoiceSession.run() 循环边界：仅 COMPLETED / 无需播报才可再 STT。"""

    def _make(
        self,
        *,
        llm: Any,
        tts: Any = None,
        wait_timeout: float = 5.0,
    ) -> MikuAssistant:
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
                        "stream_reply": True,
                        "stream_speech_queue_size": 8,
                        "stream_speech_poll_interval": 0.02,
                        "stream_playback_wait_timeout": wait_timeout,
                    }
                }
            ),
            router=router,
            llm_client=llm,
            tts_engine=tts,
        )

    def test_run_order_second_stt_only_after_last_speak(self) -> None:
        first_speak = threading.Event()
        last_entered = threading.Event()
        last_release = threading.Event()
        completed_pulled = threading.Event()
        second_stt_gate = threading.Event()
        stt_log: List[str] = []
        result_box: List[str] = []
        run_done = threading.Event()

        class SeqTTS:
            def speak(self, text: str) -> None:
                if "第一句" in text:
                    first_speak.set()
                if "第二句" in text:
                    last_entered.set()
                    last_release.wait(timeout=3.0)

            def shutdown(self) -> None:
                return None

        class SeqLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("第一句。")
                        if not first_speak.wait(timeout=2.0):
                            raise AssertionError("first sentence not spoken")
                        yield StreamEvent.text_delta("第二句。")
                        completed_pulled.set()
                        yield StreamEvent.completed("第一句。第二句。")
                    finally:
                        self.closed = True

                return gen()

        llm = SeqLLM([])
        assistant = self._make(llm=llm, tts=SeqTTS())

        def stt(continuous=False):
            n = len(stt_log) + 1
            if n == 1:
                stt_log.append("u1")
                return "用户一句"
            stt_log.append("u2")
            # 第二次 STT 只能在末句 speak 返回之后
            if not last_release.is_set():
                raise AssertionError("second STT before last speak released")
            second_stt_gate.set()
            return "结束测试"

        _bind_fake_recognition(assistant, stt)
        session = VoiceSession(
            assistant,
            use_tts=True,
            stream_reply=True,
            stop_keywords=("结束测试",),
        )

        def _run() -> None:
            try:
                with redirect_stdout(io.StringIO()):
                    result_box.append(session.run())
            finally:
                run_done.set()

        worker = threading.Thread(target=_run, name="vs-run-order", daemon=True)
        try:
            worker.start()
            self.assertTrue(first_speak.wait(timeout=2.0))
            self.assertTrue(completed_pulled.wait(timeout=2.0))
            self.assertTrue(last_entered.wait(timeout=2.0))
            self.assertEqual(stt_log, ["u1"])
            self.assertFalse(run_done.wait(timeout=0.15))
            last_release.set()
            self.assertTrue(second_stt_gate.wait(timeout=2.0))
            self.assertTrue(run_done.wait(timeout=2.0))
            _join_thread(worker)
            self.assertEqual(stt_log, ["u1", "u2"])
            self.assertEqual(result_box, ["exit"])
        finally:
            last_release.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()

    def test_run_timeout_does_not_reopen_stt(self) -> None:
        speak_entered = threading.Event()
        speak_release = threading.Event()
        stt_log: List[str] = []
        result_box: List[str] = []
        run_done = threading.Event()

        class SlowTTS:
            def speak(self, text: str) -> None:
                speak_entered.set()
                speak_release.wait(timeout=5.0)

            def shutdown(self) -> None:
                return None

        llm = FakeStreamLLM(
            [
                StreamEvent.text_delta("慢句。"),
                StreamEvent.completed("慢句。"),
            ]
        )
        assistant = self._make(llm=llm, tts=SlowTTS(), wait_timeout=0.25)

        def stt(continuous=False):
            stt_log.append(f"s{len(stt_log) + 1}")
            if len(stt_log) == 1:
                return "问一句"
            raise AssertionError("STT must not reopen after playback timeout")

        _bind_fake_recognition(assistant, stt)
        session = VoiceSession(
            assistant,
            use_tts=True,
            stream_reply=True,
            stop_keywords=("退出语音",),
        )

        def _run() -> None:
            try:
                with redirect_stdout(io.StringIO()):
                    result_box.append(session.run())
            finally:
                run_done.set()

        worker = threading.Thread(target=_run, name="vs-timeout", daemon=True)
        try:
            worker.start()
            self.assertTrue(speak_entered.wait(timeout=2.0))
            self.assertTrue(run_done.wait(timeout=3.0))
            _join_thread(worker)
            self.assertEqual(stt_log, ["s1"])
            self.assertEqual(result_box, ["unsafe_pause"])
            self.assertTrue(speak_entered.is_set())
            self.assertFalse(speak_release.is_set())
        finally:
            speak_release.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()

    def test_run_partial_speech_then_gen_fail_no_second_stt(self) -> None:
        speak_entered = threading.Event()
        speak_release = threading.Event()
        fail_armed = threading.Event()
        stt_log: List[str] = []
        result_box: List[str] = []
        run_done = threading.Event()

        class BlockTTS:
            def speak(self, text: str) -> None:
                speak_entered.set()
                speak_release.wait(timeout=5.0)

            def shutdown(self) -> None:
                return None

        class FailAfterPartialLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("已切句。")
                        if not speak_entered.wait(timeout=2.0):
                            raise AssertionError("speak did not start")
                        fail_armed.set()
                        yield StreamEvent.failed(
                            GenerateStatus.RESPONSE_ERROR, "boom mid-stream"
                        )
                    finally:
                        self.closed = True

                return gen()

        llm = FailAfterPartialLLM([])
        assistant = self._make(llm=llm, tts=BlockTTS())

        def stt(continuous=False):
            stt_log.append(f"s{len(stt_log) + 1}")
            if len(stt_log) == 1:
                return "问一句"
            raise AssertionError("STT must not reopen after partial speech + gen fail")

        _bind_fake_recognition(assistant, stt)
        session = VoiceSession(
            assistant,
            use_tts=True,
            stream_reply=True,
            stop_keywords=("退出语音",),
        )

        def _run() -> None:
            try:
                with redirect_stdout(io.StringIO()):
                    result_box.append(session.run())
            finally:
                run_done.set()

        worker = threading.Thread(target=_run, name="vs-partial-fail", daemon=True)
        try:
            worker.start()
            self.assertTrue(speak_entered.wait(timeout=2.0))
            self.assertTrue(fail_armed.wait(timeout=2.0))
            self.assertTrue(run_done.wait(timeout=3.0))
            _join_thread(worker)
            self.assertEqual(stt_log, ["s1"])
            self.assertEqual(result_box, ["unsafe_pause"])
            self.assertFalse(speak_release.is_set())
        finally:
            speak_release.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()

    def test_wait_cancel_drains_unstarted_and_isolates_next_turn(self) -> None:
        first_entered = threading.Event()
        first_release = threading.Event()
        second_speak = threading.Event()
        spoken: List[str] = []
        cancel = threading.Event()
        wait_done = threading.Event()
        outcome_box: List[DispatchOutcome] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                if len(spoken) == 1:
                    first_entered.set()
                    first_release.wait(timeout=5.0)
                else:
                    second_speak.set()

            def shutdown(self) -> None:
                return None

        llm = FakeStreamLLM(
            [
                StreamEvent.text_delta("句一。句二。句三。"),
                StreamEvent.completed("句一。句二。句三。"),
            ]
        )
        assistant = self._make(llm=llm, tts=GatedTTS())

        def _wait_run() -> None:
            try:
                outcome_box.append(
                    assistant.process_text_input_streaming(
                        "长答",
                        use_tts=True,
                        wait_for_playback=True,
                        cancel_event=cancel,
                        playback_timeout=5.0,
                    )
                )
            finally:
                wait_done.set()

        worker = threading.Thread(target=_wait_run, name="wait-cancel", daemon=True)
        try:
            worker.start()
            self.assertTrue(first_entered.wait(timeout=2.0))
            self.assertFalse(wait_done.wait(timeout=0.15))
            cancel.set()
            self.assertTrue(wait_done.wait(timeout=2.0))
            _join_thread(worker)
            self.assertTrue(outcome_box)
            out = outcome_box[0]
            self.assertEqual(out.stage, ReplyStage.CANCELLED)
            self.assertEqual(out.playback_status, PlaybackStatus.INVALIDATED)
            self.assertFalse(out.safe_to_resume_listen)
            # 未开始的句子不得再播（取消 CAS 已 drain）
            self.assertEqual(spoken, ["句一。"])
            first_release.set()
            self.assertFalse(
                second_speak.wait(timeout=0.4),
                "queued sentences must be drained; second speak must not start",
            )
            self.assertEqual(spoken, ["句一。"])

            # 新轮不受影响
            llm2 = FakeStreamLLM(
                [
                    StreamEvent.text_delta("新轮。"),
                    StreamEvent.completed("新轮。"),
                ]
            )
            assistant.llm_client = llm2
            assistant.router = build_intent_router(
                windows_controller=FakeController(None),  # type: ignore[arg-type]
                chrome_searcher=FakeSearcher(query=None),  # type: ignore[arg-type]
                llm_client=llm2,  # type: ignore[arg-type]
            )
            out2 = assistant.process_text_input_streaming(
                "下一轮",
                use_tts=True,
                wait_for_playback=True,
                playback_timeout=3.0,
            )
            self.assertEqual(out2.stage, ReplyStage.OK)
            self.assertEqual(out2.playback_status, PlaybackStatus.COMPLETED)
            self.assertTrue(out2.safe_to_resume_listen)
            self.assertIn("新轮。", spoken)
        finally:
            first_release.set()
            cancel.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()

    def test_run_iterator_raise_after_speech_no_second_stt(self) -> None:
        """流已入队并 speak 阻塞后，迭代器直接抛异常 → unsafe_pause，不得再 STT。"""
        speak_entered = threading.Event()
        speak_release = threading.Event()
        raised = threading.Event()
        stt_log: List[str] = []
        result_box: List[str] = []
        run_done = threading.Event()

        class BlockTTS:
            def speak(self, text: str) -> None:
                speak_entered.set()
                speak_release.wait(timeout=5.0)

            def shutdown(self) -> None:
                return None

        class RaiseAfterPartialLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("已切句。")
                        if not speak_entered.wait(timeout=2.0):
                            raise AssertionError("speak did not start")
                        raised.set()
                        raise ConnectionError("stream iterator boom")
                    finally:
                        self.closed = True

                return gen()

        llm = RaiseAfterPartialLLM([])
        assistant = self._make(llm=llm, tts=BlockTTS())

        def stt(continuous=False):
            stt_log.append(f"s{len(stt_log) + 1}")
            if len(stt_log) == 1:
                return "问一句"
            raise AssertionError("STT must not reopen after stream iterator raise")

        _bind_fake_recognition(assistant, stt)
        session = VoiceSession(
            assistant,
            use_tts=True,
            stream_reply=True,
            stop_keywords=("结束测试",),
        )

        def _run() -> None:
            try:
                with redirect_stdout(io.StringIO()):
                    result_box.append(session.run())
            finally:
                run_done.set()

        worker = threading.Thread(target=_run, name="vs-iter-raise", daemon=True)
        try:
            worker.start()
            self.assertTrue(speak_entered.wait(timeout=2.0))
            self.assertTrue(raised.wait(timeout=2.0))
            self.assertTrue(run_done.wait(timeout=3.0))
            _join_thread(worker)
            self.assertEqual(stt_log, ["s1"])
            self.assertEqual(result_box, ["unsafe_pause"])
            self.assertFalse(speak_release.is_set())
        finally:
            speak_release.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()

    def test_no_tts_failed_and_cancel_remain_safe_to_listen(self) -> None:
        """无 TTS 时 no-op 入队不得标成已播报；失败/取消应可安全恢复监听。"""
        # FAILED after full sentence — no engine
        llm_fail = FakeStreamLLM(
            [
                StreamEvent.text_delta("完整一句。"),
                StreamEvent.failed(GenerateStatus.RESPONSE_ERROR, "no-tts-fail"),
            ]
        )
        assistant_fail = self._make(llm=llm_fail, tts=None)
        try:
            out_fail = assistant_fail.process_text_input_streaming(
                "问",
                use_tts=True,
                wait_for_playback=True,
            )
            self.assertEqual(out_fail.stage, ReplyStage.GENERATION_FAILED)
            self.assertTrue(out_fail.safe_to_resume_listen)
        finally:
            assistant_fail.shutdown()

        # 外部取消（生成中）— 无引擎
        cancel = threading.Event()

        class CancelMidLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("一句。")
                        cancel.set()
                        yield StreamEvent.text_delta("续。")
                        yield StreamEvent.completed("一句。续。")
                    finally:
                        self.closed = True

                return gen()

        llm_cancel = CancelMidLLM([])
        assistant_cancel = self._make(llm=llm_cancel, tts=None)
        try:
            out_cancel = assistant_cancel.process_text_input_streaming(
                "问",
                use_tts=True,
                wait_for_playback=True,
                cancel_event=cancel,
            )
            self.assertEqual(out_cancel.stage, ReplyStage.CANCELLED)
            self.assertTrue(out_cancel.safe_to_resume_listen)
        finally:
            assistant_cancel.shutdown()

        # 非成功 COMPLETED（空成功态）在无 TTS 下也安全
        llm_empty = FakeStreamLLM([StreamEvent.completed("")])
        assistant_empty = self._make(llm=llm_empty, tts=None)
        try:
            out_empty = assistant_empty.process_text_input_streaming(
                "问",
                use_tts=True,
                wait_for_playback=True,
            )
            self.assertEqual(out_empty.stage, ReplyStage.GENERATION_FAILED)
            self.assertTrue(out_empty.safe_to_resume_listen)
        finally:
            assistant_empty.shutdown()

    def test_run_stream_exhaust_without_terminal_no_second_stt(self) -> None:
        """部分播报后生成器直接 StopIteration（无 COMPLETED/FAILED）→ unsafe_pause。"""
        first_entered = threading.Event()
        first_release = threading.Event()
        second_speak = threading.Event()
        exhausted = threading.Event()
        spoken: List[str] = []
        stt_log: List[str] = []
        result_box: List[str] = []
        run_done = threading.Event()

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                if len(spoken) == 1:
                    first_entered.set()
                    first_release.wait(timeout=5.0)
                else:
                    second_speak.set()

            def shutdown(self) -> None:
                return None

        class ExhaustAfterPartialLLM(FakeStreamLLM):
            def generate_stream(self, prompt: str, system=None):
                self.calls.append(prompt)

                def gen():
                    try:
                        yield StreamEvent.text_delta("句一。句二。")
                        if not first_entered.wait(timeout=2.0):
                            raise AssertionError("first speak did not start")
                        exhausted.set()
                        # 无 COMPLETED / FAILED — 直接耗尽
                    finally:
                        self.closed = True

                return gen()

        llm = ExhaustAfterPartialLLM([])
        assistant = self._make(llm=llm, tts=GatedTTS())

        def stt(continuous=False):
            stt_log.append(f"s{len(stt_log) + 1}")
            if len(stt_log) == 1:
                return "问一句"
            raise AssertionError("STT must not reopen after unterminated stream exhaust")

        _bind_fake_recognition(assistant, stt)
        session = VoiceSession(
            assistant,
            use_tts=True,
            stream_reply=True,
            stop_keywords=("结束测试",),
        )

        def _run() -> None:
            try:
                with redirect_stdout(io.StringIO()):
                    result_box.append(session.run())
            finally:
                run_done.set()

        worker = threading.Thread(target=_run, name="vs-exhaust", daemon=True)
        try:
            worker.start()
            self.assertTrue(first_entered.wait(timeout=2.0))
            self.assertTrue(exhausted.wait(timeout=2.0))
            self.assertTrue(run_done.wait(timeout=3.0))
            _join_thread(worker)
            self.assertEqual(stt_log, ["s1"])
            self.assertEqual(result_box, ["unsafe_pause"])
            self.assertEqual(spoken, ["句一。"])
            first_release.set()
            self.assertFalse(
                second_speak.wait(timeout=0.4),
                "unstarted sentence must be drained after unterminated exhaust",
            )
            self.assertEqual(spoken, ["句一。"])
        finally:
            first_release.set()
            if worker.is_alive():
                _join_thread(worker)
            assistant.shutdown()

    def test_no_tts_stream_exhaust_without_terminal_safe_to_listen(self) -> None:
        """无 TTS：无终结事件耗尽 → GENERATION_FAILED 且可安全恢复监听。"""
        llm = FakeStreamLLM([StreamEvent.text_delta("完整一句。")])
        assistant = self._make(llm=llm, tts=None)
        try:
            out = assistant.process_text_input_streaming(
                "问",
                use_tts=True,
                wait_for_playback=True,
            )
            self.assertEqual(out.stage, ReplyStage.GENERATION_FAILED)
            self.assertTrue(out.safe_to_resume_listen)
            self.assertIn("流未正常结束", out.text)
        finally:
            assistant.shutdown()


if __name__ == "__main__":
    unittest.main()
