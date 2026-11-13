# -*- coding: utf-8 -*-
"""Worker 侧 stdin 请求校验。"""
from __future__ import annotations

from dataclasses import dataclass

from src.voice.cosyvoice.protocol import (
    REQ_SHUTDOWN,
    REQ_SYNTH,
    SCHEMA_VERSION,
    parse_json_line,
)
from src.voice.cosyvoice.styles import KNOWN_STYLE_IDS

_MAX_REQUEST_ID_LEN = 128


@dataclass(frozen=True)
class SynthInbound:
    request_id: str
    epoch: int
    style_id: str
    text: str
    output_path: str
    output_dev: int
    output_ino: int


def _strict_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("not_int")
    return value


def parse_inbound_line(line: str, *, max_text_chars: int) -> tuple[str, SynthInbound | None]:
    """
    解析一行 stdin。

    Returns:
        (kind, payload) 其中 kind 为 ``shutdown``/``synth``/``invalid``。
    """
    if line and not line.endswith("\n"):
        return "invalid", None
    try:
        payload = parse_json_line(line)
    except ValueError:
        return "invalid", None
    schema = payload.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA_VERSION:
        return "invalid", None
    msg_type = payload.get("type")
    if msg_type == REQ_SHUTDOWN:
        return "shutdown", None
    if msg_type != REQ_SYNTH:
        return "invalid", None
    try:
        request_id = payload.get("request_id")
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > _MAX_REQUEST_ID_LEN
        ):
            return "invalid", None
        epoch = _strict_int(payload.get("epoch"))
        if epoch < 0:
            return "invalid", None
        style_id = payload.get("style_id")
        if not isinstance(style_id, str) or style_id not in KNOWN_STYLE_IDS:
            return "invalid", None
        text = payload.get("text")
        if not isinstance(text, str) or len(text) > max_text_chars:
            return "invalid", None
        output_path = payload.get("output_path")
        if not isinstance(output_path, str) or not output_path:
            return "invalid", None
        output_dev = _strict_int(payload.get("output_dev"))
        output_ino = _strict_int(payload.get("output_ino"))
        if output_dev < 0 or output_ino < 0:
            return "invalid", None
    except (TypeError, ValueError):
        return "invalid", None
    return "synth", SynthInbound(
        request_id=request_id,
        epoch=epoch,
        style_id=style_id,
        text=text,
        output_path=output_path,
        output_dev=output_dev,
        output_ino=output_ino,
    )
