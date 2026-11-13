# -*- coding: utf-8 -*-
"""Paraformer runtime 有界设置（stdlib only）。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

STARTUP_DEADLINE_SEC = 45.0
INFERENCE_DEADLINE_SEC = 15.0
CLOSE_BUDGET_SEC = 2.0
MAX_OUTPUT_TEXT_CHARS = 4096
MAX_UTTERANCE_SEC = 15.0
SAMPLE_RATE_HZ = 16000
BYTES_PER_SAMPLE = 2
MAX_PCM_BYTES = int(SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * MAX_UTTERANCE_SEC)


def _strict_cpu_threads(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("cpu_threads")
    if value < 1 or value > 4:
        raise ValueError("cpu_threads")
    return value


def _reject_non_finite_number(path: str, value: Any, *, min_val: float, max_val: float) -> float:
    if isinstance(value, bool):
        raise ValueError(path)
    if isinstance(value, int):
        out = float(value)
    elif isinstance(value, float):
        out = float(value)
    else:
        raise ValueError(path)
    if not math.isfinite(out) or out < min_val or out > max_val:
        raise ValueError(path)
    return out


@dataclass(frozen=True)
class ParaformerSettings:
    cpu_threads: int = 2
    startup_deadline_sec: float = STARTUP_DEADLINE_SEC
    inference_deadline_sec: float = INFERENCE_DEADLINE_SEC
    close_budget_sec: float = CLOSE_BUDGET_SEC

    def __post_init__(self) -> None:
        _strict_cpu_threads(self.cpu_threads)
        _reject_non_finite_number(
            "startup_deadline_sec",
            self.startup_deadline_sec,
            min_val=1.0,
            max_val=STARTUP_DEADLINE_SEC,
        )
        _reject_non_finite_number(
            "inference_deadline_sec",
            self.inference_deadline_sec,
            min_val=1.0,
            max_val=INFERENCE_DEADLINE_SEC,
        )
        _reject_non_finite_number(
            "close_budget_sec",
            self.close_budget_sec,
            min_val=0.25,
            max_val=CLOSE_BUDGET_SEC,
        )

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "ParaformerSettings":
        data = dict(raw or {})
        return cls(
            cpu_threads=data.get("cpu_threads", 2),
            startup_deadline_sec=data.get("startup_deadline_sec", STARTUP_DEADLINE_SEC),
            inference_deadline_sec=data.get(
                "inference_deadline_sec", INFERENCE_DEADLINE_SEC
            ),
            close_budget_sec=data.get("close_budget_sec", CLOSE_BUDGET_SEC),
        )


def resolve_paraformer_settings_from_stt_config(stt_cfg: Any) -> ParaformerSettings:
    if stt_cfg is not None and not isinstance(stt_cfg, dict):
        raise ValueError("voice.stt must be a mapping")
    inner = (stt_cfg or {}).get("paraformer")
    if inner is not None and not isinstance(inner, dict):
        raise ValueError("voice.stt.paraformer must be a mapping when present")
    return ParaformerSettings.from_mapping(inner)
