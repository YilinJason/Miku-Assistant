# -*- coding: utf-8 -*-
"""自然陪伴 Round 2：朗读投影隐私与自然度契约——确定性离线测试。"""
from __future__ import annotations

import time
import threading
import unittest
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

from src.core.assistant import MikuAssistant, _GenerationTurn
from src.core.dispatch_outcome import DispatchOutcome, ReplyStage
from src.core.handlers.llm_handler import LLMIntentHandler
from src.core.router import IntentRouter
from src.core.speech_queue import PlaybackStatus, TurnSpeechPlayer
from src.llm.stream_events import StreamEvent
from src.voice.language import sanitize_for_speech
from src.voice.speech_projection import (
    CODE_TIP,
    IMAGE_TIP,
    LINK_TIP,
    MAX_SPEECH_HOLD_BUFFER_CHARS,
    MAX_SPEECH_PROJECTION_INPUT_CHARS,
    SpeechProjector,
    project_for_speech,
)
from tests.test_assistant_orchestration import (
    _require_player_quiet,
    _shutdown_assistant_with_player,
)


def _fence(*parts: str) -> str:
    mark = "`" * 3
    return mark + "".join(parts) + mark


class KaomojiAndParenTests(unittest.TestCase):
    def test_normal_parens_kept(self) -> None:
        text = "支持 (API)、(RVC)、(Python 3.12) 与（可选）参数。"
        out = project_for_speech(text)
        self.assertIn("(API)", out)
        self.assertIn("(RVC)", out)
        self.assertIn("(Python 3.12)", out)
        self.assertIn("（可选）", out)
        self.assertIn("支持", out)

    def test_kaomoji_removed_with_face_evidence(self) -> None:
        out = project_for_speech("开心 (´･ω･`) 呀 （＾▽＾）")
        self.assertNotIn("ω", out)
        self.assertNotIn("＾", out)
        self.assertIn("开心", out)
        self.assertIn("呀", out)

    def test_cjk_and_ascii_preserved(self) -> None:
        text = "版本 3.12 与 Hello 世界 123"
        self.assertEqual(project_for_speech(text), text)


class MarkdownAndUrlPrivacyTests(unittest.TestCase):
    def test_links_images_bare_urls(self) -> None:
        sentinel = "SECRET_TOKEN_9f3a"
        text = (
            f"见 [文档](https://evil.example/path?tok={sentinel}#frag) ，"
            f"图 ![说明](https://evil.example/i.png?x={sentinel}) ，"
            f"裸 https://user:pass@evil.example/a?q={sentinel}#z 结束。"
        )
        out = project_for_speech(text)
        self.assertIn("文档", out)
        self.assertIn(IMAGE_TIP, out)
        self.assertIn(LINK_TIP, out)
        self.assertNotIn("evil.example", out)
        self.assertNotIn(sentinel, out)
        self.assertNotIn("https://", out)
        self.assertNotIn("user:pass", out)

    def test_emphasis_headers_lists_inline_code_tables(self) -> None:
        text = "\n".join(
            [
                "## 标题",
                "> 引用行",
                "- 列表一项",
                "1. 第二项",
                "用 `Ctrl+C` 复制",
                "| A | B |",
                "| --- | --- |",
                "| 甲 | 乙 |",
                "**粗** 与 *斜*",
            ]
        )
        out = project_for_speech(text)
        self.assertIn("标题", out)
        self.assertIn("引用行", out)
        self.assertIn("列表一项", out)
        self.assertIn("第二项", out)
        self.assertIn("Ctrl+C", out)
        self.assertNotIn("`", out)
        self.assertIn("甲", out)
        self.assertIn("乙", out)
        self.assertNotIn("---", out)
        self.assertIn("粗", out)
        self.assertIn("斜", out)
        self.assertNotIn("**", out)


class FencedCodeStreamTests(unittest.TestCase):
    def test_complete_fence_suppresses_body_tip_once(self) -> None:
        body = _fence("python\n", "print('SECRET_CODE')\n") + "\n之后"
        out = project_for_speech(body)
        self.assertEqual(out.count(CODE_TIP), 1)
        self.assertNotIn("SECRET_CODE", out)
        self.assertNotIn("print", out)
        self.assertIn("之后", out)

    def test_char_chunks_and_arbitrary_splits(self) -> None:
        full = "前言\n" + _fence("py\n", "x=1\n", "y=2\n") + "\n后语"
        for size in (1, 2, 3, 5, 7, 11):
            p = SpeechProjector()
            parts: List[str] = []
            for i in range(0, len(full), size):
                parts.append(p.feed(full[i : i + size]))
            parts.append(p.finish())
            joined = "".join(parts)
            # 允许分句空白差异，语义拼接后核对
            collapsed = project_for_speech(full)
            self.assertEqual(joined.replace(" ", ""), collapsed.replace(" ", ""))
            self.assertEqual(joined.count(CODE_TIP), 1)
            self.assertNotIn("x=1", joined)

    def test_unclosed_fence_suppresses_and_new_turn_resets(self) -> None:
        p1 = SpeechProjector()
        out1 = p1.feed("开\n" + "`" * 3 + "py\nsecret_line\n")
        out1 += p1.finish()
        self.assertIn(CODE_TIP, out1)
        self.assertNotIn("secret_line", out1)
        p2 = SpeechProjector()
        out2 = p2.feed("新轮正常 (API)")
        out2 += p2.finish()
        self.assertIn("(API)", out2)
        self.assertNotIn("secret_line", out2)

    def test_adjacent_fences_tip_each_once(self) -> None:
        text = (
            _fence("a\n", "code1\n")
            + "\n中\n"
            + _fence("b\n", "code2\n")
            + "\n尾"
        )
        out = project_for_speech(text)
        self.assertEqual(out.count(CODE_TIP), 2)
        self.assertNotIn("code1", out)
        self.assertNotIn("code2", out)


class IdempotenceAndBoundsTests(unittest.TestCase):
    def test_projection_idempotent(self) -> None:
        text = (
            "见 [x](https://evil.example/z) "
            + _fence("py\n", "hide()\n")
            + "\n完 :smile: (´･ω･`)"
        )
        once = project_for_speech(text)
        twice = project_for_speech(once)
        self.assertEqual(once, twice)
        self.assertEqual(sanitize_for_speech(once), once)

    def test_large_input_bounded_time_and_output(self) -> None:
        huge = ("字" * 1000 + "\n") * 20  # 约 20k+
        self.assertGreater(len(huge), MAX_SPEECH_PROJECTION_INPUT_CHARS)
        t0 = time.perf_counter()
        out = project_for_speech(huge)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 2.0)
        self.assertLessEqual(len(out), MAX_SPEECH_PROJECTION_INPUT_CHARS + 64)


class _Cfg:
    def get(self, key: str, default: Any = None) -> Any:
        mapping = {
            "assistant.stream_sentence_max_buffer": 500,
            "assistant.stream_speech_queue_size": 8,
            "assistant.stream_speech_poll_interval": 0.01,
            "assistant.stream_playback_wait_timeout": 5,
            "assistant.auto_tts": True,
            "resource.profile": "balanced",
        }
        return mapping.get(key, default)


class EmptyProjectionPlaybackTests(unittest.TestCase):
    def test_pure_emoji_skips_enqueue_and_keeps_display(self) -> None:
        spoken: List[str] = []

        class CapturingTTS:
            def speak(self, text: str, async_mode: bool = False) -> None:
                spoken.append(text)

            def interrupt(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        class StreamLLM:
            def generate_stream(self, prompt: str, system=None):
                yield StreamEvent.text_delta("😀🎉")
                yield StreamEvent.completed("😀🎉")

        llm = StreamLLM()
        assistant = MikuAssistant.from_dependencies(
            config=_Cfg(),
            llm_client=llm,
            tts_engine=CapturingTTS(),
            router=IntentRouter([LLMIntentHandler(llm)]),
        )
        deltas: List[str] = []
        outcome = assistant.process_text_input_streaming(
            "hi", use_tts=True, on_delta=deltas.append, wait_for_playback=True
        )
        self.assertEqual(outcome.stage, ReplyStage.OK)
        self.assertEqual(outcome.text, "😀🎉")
        self.assertEqual(deltas, ["😀🎉"])
        self.assertEqual(spoken, [])
        assistant.shutdown()


class StreamNonstreamEquivalenceTests(unittest.TestCase):
    def test_same_reply_spoken_semantics(self) -> None:
        reply = (
            "你好，见 [文档](https://evil.example/a?s=1)。\n"
            + _fence("py\n", "print(1)\n")
            + "\n用 `Ctrl+C`。"
        )
        full = project_for_speech(reply)
        p = SpeechProjector()
        # 模拟分句切块
        chunks = ["你好，见 [文档](https://evil.example/a?s=1)。\n", _fence("py\n", "print(1)\n") + "\n", "用 `Ctrl+C`。"]
        inc = "".join(p.feed(c) for c in chunks) + p.finish()
        self.assertEqual(inc.replace(" ", ""), full.replace(" ", ""))
        self.assertNotIn("evil.example", inc)
        self.assertNotIn("print", inc)


class AssistantWiringProjectionTests(unittest.TestCase):
    def test_streaming_does_not_speak_url_or_code(self) -> None:
        spoken: List[str] = []
        reply = (
            "链接 https://evil.example/q?tok=SECRET_URL\n"
            + _fence("\n", "leak_payload()\n")
            + "\n好的。"
        )

        class CapturingTTS:
            def speak(self, text: str, async_mode: bool = False) -> None:
                spoken.append(text)

            def interrupt(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        class StreamLLM:
            def __init__(self) -> None:
                self.calls = 0

            def generate_stream(self, prompt: str, system=None):
                self.calls += 1
                yield StreamEvent.text_delta(reply)
                yield StreamEvent.completed(reply)

        llm = StreamLLM()
        assistant = MikuAssistant.from_dependencies(
            config=_Cfg(),
            llm_client=llm,
            tts_engine=CapturingTTS(),
            router=IntentRouter([LLMIntentHandler(llm)]),
        )
        outcome = assistant.process_text_input_streaming(
            "问", use_tts=True, wait_for_playback=True
        )
        self.assertEqual(outcome.text, reply)
        self.assertEqual(llm.calls, 1)
        joined = "\n".join(spoken)
        self.assertNotIn("SECRET_URL", joined)
        self.assertNotIn("evil.example", joined)
        self.assertNotIn("leak_payload", joined)
        self.assertNotIn("https://", joined)
        self.assertTrue(
            any("好的" in s or LINK_TIP in s or CODE_TIP in s for s in spoken)
        )
        assistant.shutdown()

    def test_empty_projection_skips_queue_accepted(self) -> None:
        calls: List[str] = []
        player_tts = MagicMock()
        player_tts.speak = lambda text, async_mode=False: calls.append(text)
        player = TurnSpeechPlayer(player_tts, maxsize=4, poll_interval=0.01)
        tid = player.begin_turn()
        self.assertTrue(player.enqueue(tid, project_for_speech("😀🎉")))
        player.mark_production_finished(tid)
        status = player.wait_for_turn(tid, timeout=1.0)
        self.assertIn(status, (PlaybackStatus.COMPLETED, PlaybackStatus.SKIPPED))
        self.assertEqual(calls, [])
        player.shutdown(timeout=1.0)


class Finding1NestedAndLongUrlTests(unittest.TestCase):
    def test_nested_parens_and_title_and_unicode(self) -> None:
        cases = [
            ("[文档](https://evil.example/a_(x)?tok=SECRET_NESTED)", "文档", "SECRET_NESTED"),
            (
                '[文档](https://evil.example/a_(x)?tok=SECRET_NEST2 "标题SECRET")',
                "文档",
                "SECRET_NEST2",
            ),
            ("[说明](https://例.example/路径?q=SECRET_UNI)", "说明", "SECRET_UNI"),
            (
                "![图](https://evil.example/a_(b)/i.png?x=SECRET_IMG)",
                IMAGE_TIP,
                "SECRET_IMG",
            ),
        ]
        for src, expect_part, sentinel in cases:
            with self.subTest(src=src[:40]):
                out = project_for_speech(src)
                self.assertIn(expect_part if expect_part != IMAGE_TIP else IMAGE_TIP, out)
                self.assertNotIn(sentinel, out)
                self.assertNotIn("evil.example", out)
                self.assertNotIn("https://", out)

    def test_long_url_does_not_leak_tail(self) -> None:
        long_url = "https://evil.example/" + ("p" * 2500) + "SECRET_LONG"
        out = project_for_speech("前 " + long_url + " 后")
        self.assertIn(LINK_TIP, out)
        self.assertIn("前", out)
        self.assertIn("后", out)
        self.assertNotIn("SECRET_LONG", out)
        self.assertNotIn("evil.example", out)

    def test_unclosed_markdown_and_bare_discard_on_finish(self) -> None:
        p = SpeechProjector()
        out = p.feed("见 [文档](https://evil.example/open?tok=SECRET_OPEN")
        out += p.finish()
        self.assertNotIn("SECRET_OPEN", out)
        self.assertNotIn("evil.example", out)
        self.assertIn("文档", out)

        p2 = SpeechProjector()
        out2 = p2.feed("裸 https://evil.example/x?tok=SECRET_BARE")
        out2 += p2.finish()
        self.assertNotIn("SECRET_BARE", out2)
        self.assertIn(LINK_TIP, out2)


class Finding2ChunkBoundaryTests(unittest.TestCase):
    def _assert_split_equiv(self, full: str, *chunks: str) -> None:
        expect = project_for_speech(full).replace(" ", "")
        p = SpeechProjector()
        got = "".join(p.feed(c) for c in chunks) + p.finish()
        self.assertEqual(got.replace(" ", ""), expect)
        for bad in (
            "SECRET",
            "evil.example",
            "https://",
            "leak_payload",
            "user:pass",
        ):
            if bad in full or bad.upper() in full.upper():
                self.assertNotIn(bad if bad != "SECRET" else "SECRET", got)

    def test_url_split_mid_query(self) -> None:
        full = "看 https://evil.example/a?tok=SECRET_SPLIT 好"
        p = SpeechProjector()
        a = p.feed("看 https://evil.example/a?tok=SEC")
        b = p.feed("RET_SPLIT 好")
        c = p.finish()
        joined = a + b + c
        self.assertNotIn("SECRET_SPLIT", joined)
        self.assertNotIn("evil.example", joined)
        self.assertIn("好", joined)
        self.assertIn(LINK_TIP, joined)
        self.assertEqual(joined.replace(" ", ""), project_for_speech(full).replace(" ", ""))

    def test_every_split_point_and_char_feed(self) -> None:
        samples = [
            "裸 https://user:pass@evil.example/a?q=SECRET_Q#f 结束",
            "见 [文档](https://evil.example/a_(x)?tok=SECRET_MD) 完",
            "图 ![alt](https://evil.example/i?x=SECRET_IM) 完",
            "用 `Ctrl+C` 与码",
            "前\n" + _fence("py\n", "leak_payload()\n") + "\n后",
        ]
        for full in samples:
            with self.subTest(full=full[:32]):
                expect = project_for_speech(full).replace(" ", "")
                # 逐字符
                p = SpeechProjector()
                got = "".join(p.feed(ch) for ch in full) + p.finish()
                self.assertEqual(got.replace(" ", ""), expect)
                for sentinel in ("SECRET_Q", "SECRET_MD", "SECRET_IM", "leak_payload", "user:pass"):
                    self.assertNotIn(sentinel, got)
                # 若干切分点
                for cut in range(1, len(full), max(1, len(full) // 7)):
                    p2 = SpeechProjector()
                    g2 = p2.feed(full[:cut]) + p2.feed(full[cut:]) + p2.finish()
                    self.assertEqual(g2.replace(" ", ""), expect)


class Finding3AssistantEntryProjectionTests(unittest.TestCase):
    def test_side_effects_legacy_player_projection(self) -> None:
        spoken: List[str] = []

        class FakeTTS:
            def speak(self, text: str, async_mode: bool = False) -> None:
                spoken.append(text)

            def shutdown(self) -> None:
                return None

        assistant = MikuAssistant.from_dependencies(
            config=_Cfg(),
            tts_engine=FakeTTS(),
            llm_client=MagicMock(),
            router=IntentRouter([]),
        )
        try:
            outcome = DispatchOutcome(
                text="看 https://evil.example/?tok=SECRET_PUBLIC 完",
                allow_tts=True,
                allow_character=False,
            )
            turn = assistant._begin_generation_turn(external_cancel=None)  # noqa: SLF001
            assert turn is not None
            assistant._apply_outcome_side_effects(  # noqa: SLF001
                outcome, use_tts=True, use_legacy_thread=True, turn=turn
            )
            player = assistant._speech_player  # noqa: SLF001
            assert player is not None and turn.speech_turn_id
            status = player.wait_for_turn(turn.speech_turn_id, timeout=2.0)
            self.assertEqual(status, PlaybackStatus.COMPLETED)
            self.assertTrue(spoken)
            self.assertNotIn("SECRET_PUBLIC", spoken[0])
            self.assertNotIn("evil.example", spoken[0])
            self.assertEqual(
                outcome.text, "看 https://evil.example/?tok=SECRET_PUBLIC 完"
            )

            spoken.clear()
            assistant._begin_generation_turn(external_cancel=None)  # noqa: SLF001
            assistant._apply_outcome_side_effects(  # noqa: SLF001
                outcome, use_tts=True, use_legacy_thread=True, turn=turn
            )
            self.assertEqual(spoken, [])

            spoken.clear()
            emoji_out = DispatchOutcome(
                text="😀🎉", allow_tts=True, allow_character=False
            )
            emoji_turn = assistant._begin_generation_turn(external_cancel=None)  # noqa: SLF001
            assert emoji_turn is not None
            assistant._apply_outcome_side_effects(  # noqa: SLF001
                emoji_out, use_tts=True, use_legacy_thread=True, turn=emoji_turn
            )
            self.assertEqual(spoken, [])
        finally:
            _shutdown_assistant_with_player(assistant)

    def test_public_speak_and_tracked_empty(self) -> None:
        spoken: List[str] = []

        class FakeTTS:
            def speak(self, text: str, async_mode: bool = False) -> None:
                spoken.append(text)

            def interrupt(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        assistant = MikuAssistant.from_dependencies(
            config=_Cfg(),
            tts_engine=FakeTTS(),
            llm_client=MagicMock(),
            router=IntentRouter([]),
        )
        try:
            assistant.speak("https://evil.example/?tok=SECRET_SPEAK")
            self.assertEqual(len(spoken), 1)
            self.assertNotIn("SECRET_SPEAK", spoken[0])
            self.assertIn(LINK_TIP, spoken[0])

            spoken.clear()
            assistant.speak("😀")
            self.assertEqual(spoken, [])

            turn = _GenerationTurn(turn_id=1)
            player = TurnSpeechPlayer(FakeTTS(), maxsize=4, poll_interval=0.01)
            assistant._speech_player = player
            assistant._active_turn = turn
            out = DispatchOutcome(text="🎉", allow_tts=True, allow_character=False)
            result = assistant._speak_outcome_tracked(  # noqa: SLF001
                out,
                turn=turn,
                use_tts=True,
                cancel_event=None,
                playback_timeout=1.0,
            )
            self.assertEqual(result.text, "🎉")
            state = player._turns.get(turn.speech_turn_id)  # noqa: SLF001
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.accepted, 0)
        finally:
            player_ref = assistant._speech_player  # noqa: SLF001
            if player_ref is not None:
                _require_player_quiet(player_ref, timeout=2.0)
            _shutdown_assistant_with_player(assistant)


class Finding4HoldOverflowQuarantineTests(unittest.TestCase):
    def test_hold_limit_aligned_with_input(self) -> None:
        self.assertEqual(
            MAX_SPEECH_HOLD_BUFFER_CHARS, MAX_SPEECH_PROJECTION_INPUT_CHARS
        )

    def test_long_url_single_feed_no_secret(self) -> None:
        source = "前 https://evil.example/" + ("a" * 5000) + "SECRET_OVER 后"
        out = project_for_speech(source)
        self.assertNotIn("SECRET_OVER", out)
        self.assertNotIn("evil.example", out)
        self.assertIn(LINK_TIP, out)
        self.assertIn("前", out)
        self.assertIn("后", out)

    def test_long_url_incremental_feed_no_secret(self) -> None:
        source = "前 https://evil.example/" + ("a" * 5000) + "SECRET_CHUNK 后"
        p = SpeechProjector()
        out = p.feed(source) + p.finish()
        self.assertNotIn("SECRET_CHUNK", out)
        self.assertNotIn("evil.example", out)
        self.assertIn(LINK_TIP, out)
        self.assertIn("后", out)

    def test_small_hold_overflow_quarantines_not_reparse_tail(self) -> None:
        """人为缩小 hold：溢出必须整段丢弃，不得截前缀后吐出 SECRET。"""
        p = SpeechProjector(max_input_chars=20_000, max_line_buffer=64)
        prefix = "前 https://evil.example/"
        mid = "x" * 80
        out1 = p.feed(prefix + mid)
        self.assertNotIn("evil.example", out1)
        self.assertLessEqual(len(p._hold), 64)  # noqa: SLF001
        out2 = p.feed("SECRET_QSMALL 后")
        out3 = p.finish()
        joined = out1 + out2 + out3
        self.assertNotIn("SECRET_QSMALL", joined)
        self.assertNotIn("evil.example", joined)
        self.assertIn(LINK_TIP, joined)
        self.assertIn("后", joined)

    def test_hold_exact_boundary_and_plus_one(self) -> None:
        limit = 128
        scheme = "https://evil.example/"
        # 未完成 URL 本体恰好占满 hold 上限（前文已被 soft-flush）
        exact_body = "b" * (limit - len(scheme))
        p = SpeechProjector(max_input_chars=10_000, max_line_buffer=limit)
        held = "前 " + scheme + exact_body
        out = p.feed(held)
        self.assertEqual(p._hold, scheme + exact_body)  # noqa: SLF001
        self.assertEqual(len(p._hold), limit)  # noqa: SLF001
        self.assertFalse(p._quarantine)  # noqa: SLF001
        self.assertNotIn("evil.example", out)
        self.assertIn("前", out)
        # 边界+1：再多一字符 → quarantine；后续 URL 尾在终止符前必须丢弃
        out2 = p.feed("Z")
        self.assertTrue(p._quarantine)  # noqa: SLF001
        self.assertEqual(p._hold, "")  # noqa: SLF001
        self.assertNotIn("Z", out2)
        out3 = p.feed("SECRET_BOUND 尾")
        out4 = p.finish()
        joined = out + out2 + out3 + out4
        self.assertNotIn("SECRET_BOUND", joined)
        self.assertNotIn("evil.example", joined)
        self.assertIn(LINK_TIP, joined)
        self.assertIn("尾", joined)

    def test_input_exhausted_mid_url_no_secret(self) -> None:
        p = SpeechProjector(max_input_chars=40, max_line_buffer=40)
        chunk = "见 https://evil.example/" + ("q" * 30) + "SECRET_MID"
        out = p.feed(chunk) + p.finish()
        self.assertNotIn("SECRET_MID", out)
        self.assertNotIn("evil.example", out)
        self.assertIn(LINK_TIP, out)
        self.assertLessEqual(len(p._hold), 40)  # noqa: SLF001


class Finding5SchemeCaseAndAngleTitleTests(unittest.TestCase):
    def test_uppercase_scheme_projected(self) -> None:
        out = project_for_speech("看 HTTPS://evil.example/a?q=SECRET_UPPER 结束")
        self.assertNotIn("SECRET_UPPER", out)
        self.assertNotIn("HTTPS://", out)
        self.assertNotIn("evil.example", out)
        self.assertIn(LINK_TIP, out)
        self.assertIn("结束", out)

    def test_mixed_case_scheme_chunk_prefix(self) -> None:
        full = "见 HtTpS://evil.example/x?q=SECRET_MIX 完"
        expect = project_for_speech(full).replace(" ", "")
        p = SpeechProjector()
        got = "".join(p.feed(ch) for ch in full) + p.finish()
        self.assertEqual(got.replace(" ", ""), expect)
        self.assertNotIn("SECRET_MIX", got)
        for cut in range(1, len(full), max(1, len(full) // 5)):
            p2 = SpeechProjector()
            g2 = p2.feed(full[:cut]) + p2.feed(full[cut:]) + p2.finish()
            self.assertEqual(g2.replace(" ", ""), expect)
            self.assertNotIn("SECRET_MIX", g2)

    def test_angle_destination_with_double_quote_title(self) -> None:
        src = '见 [文档](<https://evil.example/a?q=x> "SECRET_TITLE") 完'
        out = project_for_speech(src)
        self.assertIn("文档", out)
        self.assertIn("完", out)
        self.assertNotIn("SECRET_TITLE", out)
        self.assertNotIn("evil.example", out)
        self.assertNotIn('"', out)

    def test_angle_title_with_paren_inside_quotes(self) -> None:
        src = '看 [说明](<https://evil.example/a> "含)SECRET_PAREN 题") 尾'
        out = project_for_speech(src)
        self.assertIn("说明", out)
        self.assertIn("尾", out)
        self.assertNotIn("SECRET_PAREN", out)
        self.assertNotIn("evil.example", out)

    def test_angle_single_quote_and_paren_title(self) -> None:
        cases = [
            "[a](<https://evil.example/u> 'SECRET_SQ')",
            "[a](<https://evil.example/u> (SECRET_PQ))",
        ]
        for src in cases:
            with self.subTest(src=src):
                out = project_for_speech(src)
                self.assertNotIn("SECRET_", out)
                self.assertNotIn("evil.example", out)
                self.assertIn("a", out)

    def test_angle_title_split_and_char_feed(self) -> None:
        full = '前 [文档](<https://evil.example/a?q=x> "SECRET_TITLE") 后'
        expect = project_for_speech(full).replace(" ", "")
        p = SpeechProjector()
        got = "".join(p.feed(ch) for ch in full) + p.finish()
        self.assertEqual(got.replace(" ", ""), expect)
        self.assertNotIn("SECRET_TITLE", got)
        for cut in range(1, len(full), max(1, len(full) // 6)):
            p2 = SpeechProjector()
            g2 = p2.feed(full[:cut]) + p2.feed(full[cut:]) + p2.finish()
            self.assertEqual(g2.replace(" ", ""), expect)


class Finding6BareUrlCjkTerminatorTests(unittest.TestCase):
    _TERMINATORS = "，。！？；：、）】》」』"

    def test_period_preserves_following_cjk(self) -> None:
        out = project_for_speech("见 https://example.com。谢谢")
        self.assertIn("谢谢", out)
        self.assertIn(LINK_TIP, out)
        self.assertNotIn("example.com", out)
        self.assertNotIn("https://", out)

    def test_each_cjk_terminator_keeps_body(self) -> None:
        for term in self._TERMINATORS:
            with self.subTest(term=term):
                src = f"见 https://evil.example/path{term}后文OK"
                out = project_for_speech(src)
                self.assertIn(term, out)
                self.assertIn("后文OK", out)
                # F8 可能把提示尾部 ``。`` 与源句读合并为单一句读
                self.assertIn("链接已显示", out)
                self.assertNotIn("evil.example", out)
                self.assertNotIn("https://", out)

    def test_markdown_dest_keeps_unicode_path_private(self) -> None:
        """Markdown 目标内 Unicode 路径仍整段吞掉，不套用裸 URL 中文终止。"""
        src = "[说明](https://例.example/路径。含句号?q=SECRET_MDCJK)"
        out = project_for_speech(src)
        self.assertIn("说明", out)
        self.assertNotIn("SECRET_MDCJK", out)
        self.assertNotIn("例.example", out)
        self.assertNotIn("路径", out)

    def test_percent_encoded_and_unicode_bare_still_private(self) -> None:
        cases = [
            "看 https://evil.example/%E8%B7%AF%E5%BE%84?q=SECRET_PCT 完",
            "看 https://evil.example/路径段?q=SECRET_UNI 完",
        ]
        for src in cases:
            with self.subTest(src=src[:24]):
                out = project_for_speech(src)
                self.assertIn(LINK_TIP, out)
                self.assertIn("完", out)
                self.assertNotIn("SECRET_", out)
                self.assertNotIn("evil.example", out)


class Finding7LinearScanComplexityTests(unittest.TestCase):
    def _char_feed_url_steps(self, body_len: int) -> int:
        p = SpeechProjector()
        url = "https://evil.example/" + ("a" * body_len)
        for ch in url:
            p.feed(ch)
        p.finish()
        return p.scan_steps

    def test_scan_steps_scale_linearly_1k_to_8k(self) -> None:
        s1 = self._char_feed_url_steps(1000)
        s8 = self._char_feed_url_steps(8000)
        # 线性：8x 输入不得接近 64x 平方；允许小常数开销
        self.assertGreater(s1, 0)
        self.assertLessEqual(s8 / s1, 10.0)
        self.assertLessEqual(s8 / 8000, (s1 / 1000) * 2.5)

    def test_chunk_sizes_equiv_and_private(self) -> None:
        full = "前 https://evil.example/" + ("b" * 1200) + "SECRET_LIN 后"
        expect = project_for_speech(full).replace(" ", "")
        for size in (1, 7, 500):
            with self.subTest(size=size):
                p = SpeechProjector()
                got = ""
                for i in range(0, len(full), size):
                    got += p.feed(full[i : i + size])
                got += p.finish()
                self.assertEqual(got.replace(" ", ""), expect)
                self.assertNotIn("SECRET_LIN", got)
                self.assertNotIn("evil.example", got)

    def test_15k_char_feed_wall_clock_loose_guard(self) -> None:
        import time

        p = SpeechProjector()
        url = "https://evil.example/" + ("c" * 15000)
        t0 = time.perf_counter()
        for ch in url:
            p.feed(ch)
        p.finish()
        elapsed = time.perf_counter() - t0
        # 修复前约 16s 平方增长；线性后应远低于 2s（宽松防死循环）
        self.assertLess(elapsed, 2.0)
        self.assertLessEqual(p.scan_steps, 15000 * 3)


class Finding8TipPunctuationMergeTests(unittest.TestCase):
    def test_cjk_period_not_doubled(self) -> None:
        out = project_for_speech("见 https://example.com。谢谢")
        self.assertIn("谢谢", out)
        self.assertNotIn("。。", out)
        self.assertIn("链接已显示。", out)

    def test_each_follow_punct_single(self) -> None:
        puncts = "。.!！?？,，;；:："
        for ch in puncts:
            with self.subTest(ch=ch):
                out = project_for_speech(f"见 https://example.com{ch}后文")
                self.assertIn("后文", out)
                self.assertIn("链接已显示", out)
                self.assertNotIn("。。", out)
                self.assertNotIn("。.", out)
                self.assertEqual(out.count("链接已显示"), 1)
                # 问号/叹号替换提示句号；句号类不重复
                if ch in "!?！？":
                    self.assertIn(f"链接已显示{ch}", out)
                elif ch in ".。":
                    self.assertIn("链接已显示。", out)
                    self.assertNotIn(f"链接已显示。{ch}", out)
                else:
                    self.assertIn(f"链接已显示{ch}", out)

    def test_no_punct_keeps_tip_period(self) -> None:
        out = project_for_speech("见 https://example.com 谢谢")
        self.assertIn("链接已显示。", out)
        self.assertIn("谢谢", out)

    def test_image_tip_merge(self) -> None:
        out = project_for_speech("图 ![x](https://evil.example/i.png)。完")
        self.assertIn("图片已显示。", out)
        self.assertNotIn("。。", out)
        self.assertIn("完", out)
        self.assertNotIn("evil.example", out)

    def test_cross_chunk_punct_merge(self) -> None:
        full = "见 https://example.com。谢谢"
        expect = project_for_speech(full)
        p = SpeechProjector()
        got = "".join(p.feed(ch) for ch in full) + p.finish()
        self.assertEqual(got.replace(" ", ""), expect.replace(" ", ""))
        self.assertNotIn("。。", got)
        for cut in (1, 5, 12, 20, len(full) - 2):
            p2 = SpeechProjector()
            g2 = p2.feed(full[:cut]) + p2.feed(full[cut:]) + p2.finish()
            self.assertNotIn("。。", g2)
            self.assertIn("谢谢", g2)


if __name__ == "__main__":
    unittest.main()
