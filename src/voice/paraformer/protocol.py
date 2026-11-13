# -*- coding: utf-8 -*-
"""Paraformer ASR worker JSON-line 协议 v1（有界、无敏感正文日志）。"""
from __future__ import annotations

import base64
import json
import math
import re
from typing import Any, Dict, Mapping, Set

from src.voice.paraformer.settings import MAX_OUTPUT_TEXT_CHARS, MAX_PCM_BYTES, SAMPLE_RATE_HZ

SCHEMA_VERSION = 1
# 出站（worker -> 主进程）：短 JSON
MAX_OUTBOUND_JSON_LINE_CHARS = 16_384
# 入站 transcribe：15s PCM base64 + 固定 envelope（约 640KiB 量级）
MAX_INBOUND_JSON_LINE_CHARS = 655_360
STDOUT_QUEUE_CAPACITY = 32

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

MSG_READY = "ready"
MSG_TRANSCRIBE_RESULT = "transcribe_result"
MSG_ERROR = "error"
MSG_SHUTDOWN_ACK = "shutdown_ack"

REQ_TRANSCRIBE = "transcribe"
REQ_SHUTDOWN = "shutdown"
REQ_CANCEL = "cancel"

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"

ERR_SCHEMA = "schema_invalid"
ERR_REQUEST_BUSY = "request_busy"
ERR_PCM_TOO_LARGE = "pcm_too_large"
ERR_PCM_INVALID = "pcm_invalid"
ERR_CANCELLED = "cancelled"
ERR_TIMEOUT = "timeout"
ERR_PROTOCOL = "protocol_error"
ERR_CLOSING = "shutdown_closing"
ERR_WORKER_START = "worker_start_failed"
ERR_INSUFFICIENT_RAM = "insufficient_ram"
ERR_ASSET = "asset_invalid"
ERR_RESOURCE_UNOBSERVABLE = "resource_unobservable"
ERR_INFER_FAILED = "infer_failed"

_ALLOWED_ERROR_LABELS: Set[str] = {
    ERR_SCHEMA,
    ERR_REQUEST_BUSY,
    ERR_PCM_TOO_LARGE,
    ERR_PCM_INVALID,
    ERR_CANCELLED,
    ERR_TIMEOUT,
    ERR_PROTOCOL,
    ERR_CLOSING,
    ERR_WORKER_START,
    ERR_INSUFFICIENT_RAM,
    ERR_ASSET,
    ERR_RESOURCE_UNOBSERVABLE,
    ERR_INFER_FAILED,
}

_INBOUND_ALLOWED_KEYS = frozenset(
    {"schema", "req", "request_id", "sample_rate", "pcm_b64"}
)
_OUTBOUND_RESULT_KEYS = frozenset({"schema", "msg", "request_id", "status", "text"})


def _strict_schema_version(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("schema")
    if value != SCHEMA_VERSION:
        raise ValueError("schema")


def _strict_request_id(value: Any) -> str:
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
        raise ValueError("request_id")
    return value


def parse_json_line(line: str, *, max_chars: int = MAX_OUTBOUND_JSON_LINE_CHARS) -> Dict[str, Any]:
    if len(line) > max_chars:
        raise ValueError("line_too_long")
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise ValueError("not_object")
    return payload


def validate_outbound_message(msg: Mapping[str, Any]) -> None:
    _strict_schema_version(msg.get("schema"))
    kind = msg.get("msg")
    if kind == MSG_READY:
        if msg.get("status") != "ready":
            raise ValueError("ready_status")
        return
    if kind == MSG_SHUTDOWN_ACK:
        extra = set(msg.keys()) - {"schema", "msg"}
        if extra:
            raise ValueError("keys")
        return
    if kind == MSG_ERROR:
        label = msg.get("error")
        if label not in _ALLOWED_ERROR_LABELS:
            raise ValueError("error_label")
        if "request_id" in msg:
            _strict_request_id(msg["request_id"])
        return
    if kind == MSG_TRANSCRIBE_RESULT:
        if set(msg.keys()) != _OUTBOUND_RESULT_KEYS:
            raise ValueError("keys")
        _strict_request_id(msg["request_id"])
        status = msg.get("status")
        if status not in (STATUS_OK, STATUS_EMPTY, STATUS_FAILED):
            raise ValueError("result_status")
        text = msg.get("text")
        if not isinstance(text, str) or len(text) > MAX_OUTPUT_TEXT_CHARS:
            raise ValueError("text")
        if status == STATUS_EMPTY and text:
            raise ValueError("text")
        if status == STATUS_OK and (not text or not text.strip()):
            raise ValueError("text")
        if status == STATUS_FAILED and text:
            raise ValueError("text")
        return
    raise ValueError("unknown_msg")


def validate_inbound_request(msg: Mapping[str, Any]) -> None:
    _strict_schema_version(msg.get("schema"))
    req = msg.get("req")
    if req == REQ_SHUTDOWN:
        if set(msg.keys()) != {"schema", "req"}:
            raise ValueError("keys")
        return
    if req == REQ_CANCEL:
        if set(msg.keys()) != {"schema", "req", "request_id"}:
            raise ValueError("keys")
        _strict_request_id(msg["request_id"])
        return
    if req != REQ_TRANSCRIBE:
        raise ValueError("req")
    if set(msg.keys()) != _INBOUND_ALLOWED_KEYS:
        raise ValueError("keys")
    _strict_request_id(msg["request_id"])
    if msg.get("sample_rate") != SAMPLE_RATE_HZ:
        raise ValueError("sample_rate")
    decode_transcribe_pcm(msg)


def build_transcribe_request(request_id: str, pcm_bytes: bytes) -> str:
    rid = _strict_request_id(request_id)
    if len(pcm_bytes) > MAX_PCM_BYTES or len(pcm_bytes) == 0 or len(pcm_bytes) % 2 != 0:
        raise ValueError("pcm_invalid")
    b64 = base64.b64encode(pcm_bytes).decode("ascii")
    line = {
        "schema": SCHEMA_VERSION,
        "req": REQ_TRANSCRIBE,
        "request_id": rid,
        "sample_rate": SAMPLE_RATE_HZ,
        "pcm_b64": b64,
    }
    raw = json.dumps(line, separators=(",", ":"), ensure_ascii=False)
    if len(raw) + 1 > MAX_INBOUND_JSON_LINE_CHARS:
        raise ValueError("line_too_large")
    return raw + "\n"


def build_shutdown_request() -> str:
    return json.dumps({"schema": SCHEMA_VERSION, "req": REQ_SHUTDOWN}) + "\n"


def build_cancel_request(request_id: str) -> str:
    return (
        json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "req": REQ_CANCEL,
                "request_id": _strict_request_id(request_id),
            }
        )
        + "\n"
    )


def decode_transcribe_pcm(msg: Mapping[str, Any]) -> bytes:
    if msg.get("req") != REQ_TRANSCRIBE:
        raise ValueError("req")
    b64 = msg.get("pcm_b64")
    if not isinstance(b64, str) or not b64:
        raise ValueError("pcm_b64")
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        raise ValueError("pcm_b64") from None
    if len(raw) > MAX_PCM_BYTES or len(raw) == 0 or len(raw) % 2 != 0:
        raise ValueError("pcm_size")
    sr = msg.get("sample_rate")
    if sr != SAMPLE_RATE_HZ:
        raise ValueError("sample_rate")
    return raw
