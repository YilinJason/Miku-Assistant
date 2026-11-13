# -*- coding: utf-8 -*-
"""edge-tts 句级韵律：纯本地配置解析与确定性参数推导（无 SSML / 无正文改写）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

_RATE_VOLUME_RE = re.compile(r"^([+-])(\d+)%$")
_PITCH_RE = re.compile(r"^([+-])(\d+)Hz$")

RATE_MIN, RATE_MAX = -30, 30
PITCH_MIN, PITCH_MAX = -50, 50
VOLUME_MIN, VOLUME_MAX = -50, 50

_NEUTRAL_RATE = "+0%"
_NEUTRAL_PITCH = "+0Hz"
_NEUTRAL_VOLUME = "+0%"


@dataclass(frozen=True)
class EdgeProsody:
    """传给 ``edge_tts.Communicate`` 的 rate / pitch / volume。"""

    rate: str
    pitch: str
    volume: str


NEUTRAL_EDGE_PROSODY = EdgeProsody(
    rate=_NEUTRAL_RATE,
    pitch=_NEUTRAL_PITCH,
    volume=_NEUTRAL_VOLUME,
)


def _require_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"voice.tts.edge_prosody.{name} must be int (not bool/float)")
    return value


def _parse_signed_percent(name: str, raw: object) -> int:
    if not isinstance(raw, str):
        raise ValueError(
            f"voice.tts.edge_prosody.{name} must be str like '+0%' or '-4%'"
        )
    match = _RATE_VOLUME_RE.fullmatch(raw.strip())
    if not match:
        raise ValueError(
            f"voice.tts.edge_prosody.{name} must match '+N%' or '-N%', got {raw!r}"
        )
    sign, digits = match.group(1), match.group(2)
    value = int(digits)
    return value if sign == "+" else -value


def _parse_signed_hz(name: str, raw: object) -> int:
    if not isinstance(raw, str):
        raise ValueError(
            f"voice.tts.edge_prosody.{name} must be str like '+0Hz' or '-2Hz'"
        )
    match = _PITCH_RE.fullmatch(raw.strip())
    if not match:
        raise ValueError(
            f"voice.tts.edge_prosody.{name} must match '+NHz' or '-NHz', got {raw!r}"
        )
    sign, digits = match.group(1), match.group(2)
    value = int(digits)
    return value if sign == "+" else -value


def format_signed_percent(value: int) -> str:
    clamped = max(RATE_MIN, min(RATE_MAX, int(value)))
    return f"{clamped:+d}%"


def format_signed_hz(value: int) -> str:
    clamped = max(PITCH_MIN, min(PITCH_MAX, int(value)))
    return f"{clamped:+d}Hz"


def format_signed_volume_percent(value: int) -> str:
    clamped = max(VOLUME_MIN, min(VOLUME_MAX, int(value)))
    return f"{clamped:+d}%"


def classify_sentence_ending(text: str) -> str:
    """
    仅看投影后句段末尾标点。

    优先级：问号 ``？?`` > 感叹号 ``！!`` > 省略号 ``…`` / ``...`` > neutral。
    末尾连续标点簇内按优先级取最高者（如 ``?!`` → question）。
    """
    if not isinstance(text, str):
        return "neutral"
    stripped = text.rstrip()
    if not stripped:
        return "neutral"

    i = len(stripped)
    chunk_parts: list[str] = []
    while i > 0:
        if i >= 3 and stripped[i - 3 : i] == "...":
            chunk_parts.append("...")
            i -= 3
            continue
        ch = stripped[i - 1]
        if ch in "？?！!…":
            chunk_parts.append(ch)
            i -= 1
            continue
        break

    if not chunk_parts:
        return "neutral"

    chunk = "".join(reversed(chunk_parts))
    if "？" in chunk or "?" in chunk:
        return "question"
    if "！" in chunk or "!" in chunk:
        return "exclamation"
    if "…" in chunk or "..." in chunk:
        return "ellipsis"
    return "neutral"


@dataclass(frozen=True)
class EdgeProsodySettings:
    """``voice.tts.edge_prosody`` 解析结果；非法输入在构造期失败。"""

    enabled: bool
    base_rate_pct: int
    base_pitch_hz: int
    base_volume_pct: int
    question_rate_delta_pct: int
    question_pitch_delta_hz: int
    exclamation_rate_delta_pct: int
    exclamation_pitch_delta_hz: int
    ellipsis_rate_delta_pct: int
    ellipsis_pitch_delta_hz: int

    @classmethod
    def disabled(cls) -> "EdgeProsodySettings":
        return cls(
            enabled=False,
            base_rate_pct=0,
            base_pitch_hz=0,
            base_volume_pct=0,
            question_rate_delta_pct=0,
            question_pitch_delta_hz=0,
            exclamation_rate_delta_pct=0,
            exclamation_pitch_delta_hz=0,
            ellipsis_rate_delta_pct=0,
            ellipsis_pitch_delta_hz=0,
        )

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "EdgeProsodySettings":
        if raw is None:
            return cls.disabled()
        if not isinstance(raw, Mapping):
            raise ValueError("voice.tts.edge_prosody must be a mapping")
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("voice.tts.edge_prosody.enabled must be bool")
        return cls(
            enabled=enabled,
            base_rate_pct=_parse_signed_percent("rate", raw.get("rate", "+0%")),
            base_pitch_hz=_parse_signed_hz("pitch", raw.get("pitch", "+0Hz")),
            base_volume_pct=_parse_signed_percent("volume", raw.get("volume", "+0%")),
            question_rate_delta_pct=_require_int(
                "question_rate_delta_pct", raw.get("question_rate_delta_pct", 0)
            ),
            question_pitch_delta_hz=_require_int(
                "question_pitch_delta_hz", raw.get("question_pitch_delta_hz", 0)
            ),
            exclamation_rate_delta_pct=_require_int(
                "exclamation_rate_delta_pct",
                raw.get("exclamation_rate_delta_pct", 0),
            ),
            exclamation_pitch_delta_hz=_require_int(
                "exclamation_pitch_delta_hz",
                raw.get("exclamation_pitch_delta_hz", 0),
            ),
            ellipsis_rate_delta_pct=_require_int(
                "ellipsis_rate_delta_pct", raw.get("ellipsis_rate_delta_pct", 0)
            ),
            ellipsis_pitch_delta_hz=_require_int(
                "ellipsis_pitch_delta_hz", raw.get("ellipsis_pitch_delta_hz", 0)
            ),
        )

    def resolve_for_sentence(self, text: str) -> EdgeProsody:
        """确定性句级参数；``enabled=false`` 时始终中性值。"""
        if not self.enabled:
            return NEUTRAL_EDGE_PROSODY

        rate = self.base_rate_pct
        pitch = self.base_pitch_hz
        volume = self.base_volume_pct
        kind = classify_sentence_ending(text)
        if kind == "question":
            rate += self.question_rate_delta_pct
            pitch += self.question_pitch_delta_hz
        elif kind == "exclamation":
            rate += self.exclamation_rate_delta_pct
            pitch += self.exclamation_pitch_delta_hz
        elif kind == "ellipsis":
            rate += self.ellipsis_rate_delta_pct
            pitch += self.ellipsis_pitch_delta_hz

        return EdgeProsody(
            rate=format_signed_percent(rate),
            pitch=format_signed_hz(pitch),
            volume=format_signed_volume_percent(volume),
        )
