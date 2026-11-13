"""全双工语音输入的轻量门控。

本模块只判断已经得到的转写文本，不打开麦克风、不加载模型。真正的声学回声消除
仍应由未来的连续音频采集适配器承担；这里用于阻止助手自身播报、语气词和极短噪声
转写进入对话核心。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from typing import Iterable


class InputGateDecision(str, Enum):
    """一次已识别文本的门控结论。"""

    ACCEPT = "accept"
    ECHO = "echo"
    FILLER = "filler"
    TOO_SHORT = "too_short"


@dataclass(frozen=True)
class InputGateResult:
    decision: InputGateDecision
    normalized_text: str

    @property
    def accepted(self) -> bool:
        return self.decision is InputGateDecision.ACCEPT


class VoiceInputGate:
    """在助手输出期间过滤回声与低信息转写，保留明确插话指令。"""

    DEFAULT_FILLERS = (
        "嗯",
        "啊",
        "呃",
        "哦",
        "唔",
        "额",
        "哎",
        "诶",
        "哼",
        "哈",
        "um",
        "uh",
        "erm",
        "hmm",
    )
    DEFAULT_CONTROLS = (
        "停",
        "停止",
        "等等",
        "等一下",
        "打住",
        "别说了",
        "stop",
        "wait",
    )

    def __init__(
        self,
        *,
        min_interrupt_chars: int = 2,
        echo_match_ratio: float = 0.72,
        fillers: Iterable[str] = DEFAULT_FILLERS,
        controls: Iterable[str] = DEFAULT_CONTROLS,
    ) -> None:
        if min_interrupt_chars < 1:
            raise ValueError("min_interrupt_chars must be >= 1")
        if not 0.0 <= echo_match_ratio <= 1.0:
            raise ValueError("echo_match_ratio must be between 0 and 1")
        self.min_interrupt_chars = min_interrupt_chars
        self.echo_match_ratio = echo_match_ratio
        self._fillers = {self.normalize(item) for item in fillers}
        self._single_char_fillers = {
            item for item in self._fillers if len(item) == 1
        }
        self._controls = {self.normalize(item) for item in controls}

    @staticmethod
    def normalize(text: str) -> str:
        value = unicodedata.normalize("NFKC", text or "").lower()
        return re.sub(r"[^\w\u3400-\u9fff]+", "", value, flags=re.UNICODE)

    def evaluate(
        self,
        text: str,
        *,
        assistant_text: str = "",
        output_active: bool = False,
    ) -> InputGateResult:
        normalized = self.normalize(text)
        if normalized in self._controls:
            return InputGateResult(InputGateDecision.ACCEPT, normalized)
        if not output_active:
            # 空闲时仍丢弃单字噪声，避免「呀」这类误识别直接开一轮回复。
            # 语气词保留，短指令（停/等等）已在上面放行。
            if (
                len(normalized) < self.min_interrupt_chars
                and normalized not in self._fillers
            ):
                return InputGateResult(InputGateDecision.TOO_SHORT, normalized)
            return InputGateResult(InputGateDecision.ACCEPT, normalized)
        if normalized in self._fillers or (
            len(normalized) <= 6
            and bool(normalized)
            and all(char in self._single_char_fillers for char in normalized)
        ):
            return InputGateResult(InputGateDecision.FILLER, normalized)
        if len(normalized) < self.min_interrupt_chars:
            return InputGateResult(InputGateDecision.TOO_SHORT, normalized)

        reference = self.normalize(assistant_text)
        if reference and self._looks_like_echo(normalized, reference):
            return InputGateResult(InputGateDecision.ECHO, normalized)
        return InputGateResult(InputGateDecision.ACCEPT, normalized)

    def _looks_like_echo(self, candidate: str, reference: str) -> bool:
        if candidate in reference:
            return True
        match = SequenceMatcher(None, candidate, reference).find_longest_match(
            0, len(candidate), 0, len(reference)
        )
        coverage = match.size / max(1, len(candidate))
        if coverage >= self.echo_match_ratio:
            return True

        # 对较短英文/数字转写，整体相似度比最长中文片段更稳定。
        if len(candidate) <= 24:
            ratio = SequenceMatcher(None, candidate, reference).ratio()
            return ratio >= self.echo_match_ratio
        return False


__all__ = ["InputGateDecision", "InputGateResult", "VoiceInputGate"]
