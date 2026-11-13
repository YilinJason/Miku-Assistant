# -*- coding: utf-8 -*-
"""固定 style_id → 已验证 instruct 表（禁止自由 instruct）。"""
from __future__ import annotations

from typing import Dict, Tuple

from src.voice.cosyvoice.assets import SAMPLE_CASES

_STYLE_TABLE: Dict[str, str] = {
    case_id: instruct for case_id, _text, instruct in SAMPLE_CASES
}

KNOWN_STYLE_IDS: Tuple[str, ...] = tuple(_STYLE_TABLE.keys())


def resolve_style_instruct(style_id: str) -> str:
    """返回固定 instruct；未知 style_id 抛 KeyError。"""
    return _STYLE_TABLE[style_id]
