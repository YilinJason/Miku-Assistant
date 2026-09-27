"""全双工语音门控与插话的纯离线测试。"""
from __future__ import annotations

import logging
import threading
import unittest
from typing import Any

from src.cli.voice_session import VoiceSession
from src.core.dispatch_outcome import DispatchOutcome
from src.voice.input_gate import InputGateDecision, VoiceInputGate
from src.voice.recognition import RecognitionResult


class VoiceInputGateTests(unittest.TestCase):
    def test_output_echo_filler_and_short_noise_are_rejected(self) -> None:
        gate = VoiceInputGate(min_interrupt_chars=2, echo_match_ratio=0.7)
        reference = "你好，我是你的桌面助手，很高兴见到你。"

        self.assertEqual(
            gate.evaluate(
                "我是你的桌面助手很高兴见到你",
                assistant_text=reference,
                output_active=True,
            ).decision,
            InputGateDecision.ECHO,
        )
        self.assertEqual(
            gate.evaluate("嗯", assistant_text=reference, output_active=True).decision,
            InputGateDecision.FILLER,
        )
        self.assertEqual(
            gate.evaluate("嗯嗯啊", assistant_text=reference, output_active=True).decision,
            InputGateDecision.FILLER,
        )
        self.assertEqual(
            gate.evaluate("哈", assistant_text=reference, output_active=True).decision,
            InputGateDecision.FILLER,
        )
        self.assertEqual(
            gate.evaluate("呀", assistant_text=reference, output_active=True).decision,
            InputGateDecision.TOO_SHORT,
        )

    def test_control_and_meaningful_interruptions_are_accepted(self) -> None:
        gate = VoiceInputGate(min_interrupt_chars=3)
        self.assertTrue(
            gate.evaluate("停", assistant_text="不停地说", output_active=True).accepted
        )
        self.assertTrue(
            gate.evaluate(
                "等等我有一个问题",
                assistant_text="这是助手自己的回答",
                output_active=True,
            ).accepted
        )
        # 门控只在输出期间变严格；空闲时保留既有识别行为。
        self.assertTrue(gate.evaluate("嗯", output_active=False).accepted)


class _Config:
    def __init__(self) -> None:
        self.values = {
            "assistant.stream_reply": True,
            "assistant.duplex_listening": True,
            "assistant.duplex_listen_timeout": 0.05,
            "assistant.duplex_phrase_time_limit": 1.0,
            "assistant.duplex_echo_grace_seconds": 0.2,
            "assistant.duplex_interrupt_wait_timeout": 1.0,
            "assistant.duplex_min_interrupt_chars": 2,
            "assistant.duplex_echo_match_ratio": 0.7,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class _DuplexAssistant:
    def __init__(self) -> None:
        self.config = _Config()
        self.logger = logging.getLogger("test.duplex")
        self.prompts: list[str] = []
        self.recognition_calls: list[dict] = []
        self.interrupt_calls = 0
        self._recognition_index = 0
        self.first_reply_started = threading.Event()
        self.second_reply_started = threading.Event()

    def recognize_voice_input(self, **kwargs) -> RecognitionResult:
        self.recognition_calls.append(dict(kwargs))
        self._recognition_index += 1
        if self._recognition_index == 1:
            return RecognitionResult.success("你好")
        if self._recognition_index == 2:
            self.first_reply_started.wait(timeout=1.0)
            return RecognitionResult.success("这是助手正在播放的内容")
        if self._recognition_index == 3:
            return RecognitionResult.success("等等我有一个问题")
        self.second_reply_started.wait(timeout=1.0)
        return RecognitionResult.success("退出")

    def process_text_input_streaming(
        self,
        text: str,
        *,
        on_delta,
        cancel_event: threading.Event,
        **kwargs,
    ) -> DispatchOutcome:
        self.prompts.append(text)
        if len(self.prompts) == 1:
            assistant_text = "这是助手正在播放的内容。"
            started = self.first_reply_started
        else:
            assistant_text = "第二轮回复。"
            started = self.second_reply_started
        on_delta(assistant_text)
        started.set()
        cancel_event.wait(timeout=2.0)
        return DispatchOutcome.cancelled("本轮已取消", safe_to_resume_listen=False)

    def interrupt_active_output(self) -> bool:
        self.interrupt_calls += 1
        return True

    @staticmethod
    def check_mode_switch(text: str):
        return None


class DuplexVoiceSessionTests(unittest.TestCase):
    def test_listens_during_reply_filters_echo_and_accepts_interruption(self) -> None:
        assistant = _DuplexAssistant()
        session = VoiceSession(
            assistant,  # type: ignore[arg-type]
            use_tts=True,
            stream_reply=True,
            duplex_listening=True,
            stop_keywords=("退出",),
        )

        self.assertEqual(session.run(), "exit")
        self.assertEqual(assistant.prompts, ["你好", "等等我有一个问题"])
        self.assertGreaterEqual(assistant.interrupt_calls, 2)
        self.assertTrue(assistant.first_reply_started.is_set())
        self.assertTrue(assistant.second_reply_started.is_set())
        self.assertTrue(
            all(call.get("timeout") == 0.05 for call in assistant.recognition_calls)
        )


if __name__ == "__main__":
    unittest.main()
