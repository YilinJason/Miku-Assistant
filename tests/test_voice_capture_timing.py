# -*- coding: utf-8 -*-
"""全双工回声时间窗（采集区间 vs 回复活动）— 纯 fake，无设备/模型。"""
from __future__ import annotations

import logging
import threading
import unittest
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import patch

from src.cli.voice_session import (
    VoiceSession,
    _ReplyTask,
    duplex_output_active_during_capture,
)
from src.voice.input_gate import InputGateDecision, VoiceInputGate
from src.voice.recognition import RecognitionResult, RecognitionStatus


class ReplyActivityWindowTests(unittest.TestCase):
    def test_delayed_asr_after_grace_capture_still_overlaps(self) -> None:
        task = _ReplyTask()
        task.started_at = 100.0
        task.finished_at = 101.0
        task.done.set()
        now = 105.0
        grace = 1.0
        recognition = RecognitionResult.success(
            "重复回声",
            capture_start_mono=100.4,
            capture_end_mono=101.3,
        )
        self.assertTrue(
            duplex_output_active_during_capture(
                recognition, task, now=now, grace=grace
            )
        )
        gate = VoiceInputGate().evaluate(
            recognition.text,
            assistant_text="重复回声内容",
            output_active=True,
        )
        self.assertEqual(gate.decision, InputGateDecision.ECHO)

    def test_capture_after_activity_window_not_treated_active(self) -> None:
        task = _ReplyTask()
        task.started_at = 100.0
        task.finished_at = 101.0
        task.done.set()
        recognition = RecognitionResult.success(
            "新问题",
            capture_start_mono=103.0,
            capture_end_mono=103.5,
        )
        self.assertFalse(
            duplex_output_active_during_capture(
                recognition, task, now=106.0, grace=1.0
            )
        )
        gate = VoiceInputGate().evaluate(
            recognition.text,
            assistant_text="旧回复",
            output_active=False,
        )
        self.assertTrue(gate.accepted)

    def test_capture_before_reply_started_no_overlap(self) -> None:
        task = _ReplyTask()
        task.started_at = 100.0
        task.done.set()
        task.finished_at = 101.0
        recognition = RecognitionResult.success(
            "提前一句",
            capture_start_mono=95.0,
            capture_end_mono=96.0,
        )
        self.assertFalse(
            duplex_output_active_during_capture(
                recognition, task, now=102.0, grace=1.0
            )
        )

    def test_ongoing_reply_overlap(self) -> None:
        task = _ReplyTask()
        task.started_at = 50.0
        recognition = RecognitionResult.success(
            "插话",
            capture_start_mono=50.5,
            capture_end_mono=51.0,
        )
        self.assertTrue(
            duplex_output_active_during_capture(
                recognition, task, now=51.2, grace=1.0
            )
        )

    def test_legacy_missing_capture_timestamps_wall_clock(self) -> None:
        task = _ReplyTask()
        task.started_at = 10.0
        task.finished_at = 12.0
        task.done.set()
        recognition = RecognitionResult.success("无时间戳")
        self.assertTrue(
            duplex_output_active_during_capture(
                recognition, task, now=12.5, grace=1.0
            )
        )
        self.assertFalse(
            duplex_output_active_during_capture(
                recognition, task, now=14.0, grace=1.0
            )
        )

    def test_no_task_never_active(self) -> None:
        recognition = RecognitionResult.success(
            "x", capture_start_mono=1.0, capture_end_mono=2.0
        )
        self.assertFalse(
            duplex_output_active_during_capture(
                recognition, None, now=5.0, grace=1.0
            )
        )

    def test_boundary_touching_intervals_do_not_overlap(self) -> None:
        task = _ReplyTask()
        task.started_at = 10.0
        task.finished_at = 11.0
        task.done.set()
        recognition = RecognitionResult.success(
            "边界",
            capture_start_mono=12.0,
            capture_end_mono=12.5,
        )
        self.assertFalse(
            duplex_output_active_during_capture(
                recognition, task, now=20.0, grace=1.0
            )
        )


class DuplexLoopEchoIntegrationTests(unittest.TestCase):
    def test_done_reply_overlap_rejects_without_interrupt(self) -> None:
        assistant = SimpleNamespace(
            config={},
            logger=logging.getLogger("test.voice_capture_timing"),
            begin_voice_input_session=lambda: (True, ""),
            end_voice_input_session=lambda: True,
            check_mode_switch=lambda _t: None,
            recognize_voice_input=None,
        )
        session = VoiceSession(
            assistant,
            duplex_listening=True,
            stream_reply=False,
            stop_keywords=("退出",),
        )
        replies_started: List[str] = []

        def track_start(text: str) -> _ReplyTask:
            replies_started.append(text)
            task = _ReplyTask()
            task.started_at = 100.0
            task.finished_at = 101.0
            task.done.set()
            task.append_text("我是助手正在说的长句内容")
            return task

        recognitions = [
            RecognitionResult.success(
                "你好",
                capture_start_mono=99.0,
                capture_end_mono=99.3,
            ),
            RecognitionResult.success(
                "我是助手正在说的长句内容",
                capture_start_mono=100.2,
                capture_end_mono=100.9,
            ),
            RecognitionResult.success("退出"),
        ]

        def recognize_once(**_kwargs: Any) -> RecognitionResult:
            if not recognitions:
                raise AssertionError("recognize_once called without pending result")
            return recognitions.pop(0)

        with patch.object(session, "_start_reply_task", side_effect=track_start):
            with patch.object(session, "_recognize_once", side_effect=recognize_once):
                with patch.object(session, "_interrupt_reply") as interrupt:
                    exit_code = session._run_duplex_loop([None])
        self.assertEqual(exit_code, "exit")
        self.assertEqual(replies_started, ["你好"])
        interrupt.assert_not_called()

    def test_same_text_after_window_accepted_new_turn(self) -> None:
        assistant = SimpleNamespace(
            config={},
            logger=logging.getLogger("test.voice_capture_timing"),
            begin_voice_input_session=lambda: (True, ""),
            end_voice_input_session=lambda: True,
            check_mode_switch=lambda _t: None,
            recognize_voice_input=None,
        )
        session = VoiceSession(
            assistant,
            duplex_listening=True,
            stream_reply=False,
            stop_keywords=("退出",),
        )
        assistant_line = "固定答复内容"
        replies_started: List[str] = []

        def track_start(text: str) -> _ReplyTask:
            replies_started.append(text)
            task = _ReplyTask()
            task.started_at = 100.0
            task.finished_at = 101.0
            task.done.set()
            task.append_text(assistant_line)
            return task

        recognitions = [
            RecognitionResult.success(
                "你好",
                capture_start_mono=99.0,
                capture_end_mono=99.3,
            ),
            RecognitionResult.success(
                assistant_line,
                capture_start_mono=103.0,
                capture_end_mono=103.4,
            ),
            RecognitionResult.success("退出"),
        ]

        def recognize_once(**_kwargs: Any) -> RecognitionResult:
            if not recognitions:
                raise AssertionError("recognize_once called without pending result")
            return recognitions.pop(0)

        with patch.object(session, "_start_reply_task", side_effect=track_start):
            with patch.object(session, "_recognize_once", side_effect=recognize_once):
                with patch.object(session, "_interrupt_reply") as interrupt:
                    exit_code = session._run_duplex_loop([None])
        self.assertEqual(exit_code, "exit")
        self.assertEqual(replies_started, ["你好", assistant_line])
        interrupt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
