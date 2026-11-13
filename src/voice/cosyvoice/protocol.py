# -*- coding: utf-8 -*-
"""CosyVoice runtime worker JSON-line 协议（版本化、有界、无正文/路径回显）。"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Set, Tuple

SCHEMA_VERSION = 1
MAX_JSON_LINE_CHARS = 16_384
STDOUT_QUEUE_CAPACITY = 32
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_AUDIO_SECONDS = 30.0

MSG_READY = "ready"
MSG_STARTUP_PROGRESS = "startup_progress"
MSG_SYNTH_RESULT = "synth_result"
MSG_ERROR = "error"
MSG_SHUTDOWN_ACK = "shutdown_ack"

STARTUP_PHASE_BEGIN = "begin"
STARTUP_PHASE_END = "end"

STARTUP_STAGES: Tuple[str, ...] = (
    "layout",
    "resource_admission",
    "asset_pin",
    "cache_dirs",
    "wetext_import",
    "cosyvoice_import",
    "automodel_construct",
)
_STARTUP_STAGE_SET: Set[str] = set(STARTUP_STAGES)
_MAX_STARTUP_PROGRESS_EVENTS = len(STARTUP_STAGES) * 2
_STARTUP_PHASES: Set[str] = {STARTUP_PHASE_BEGIN, STARTUP_PHASE_END}

REQ_SYNTH = "synth"
REQ_SHUTDOWN = "shutdown"

# 固定错误标签（日志/协议不得携带异常原文或用户正文）
ERR_SCHEMA = "schema_invalid"
ERR_REQUEST_BUSY = "request_busy"
ERR_TEXT_TOO_LONG = "text_too_long"
ERR_UNKNOWN_STYLE = "unknown_style"
ERR_OUTPUT_REJECTED = "output_rejected"
ERR_SYNTH_FAILED = "synth_failed"
ERR_CANCELLED = "cancelled"
ERR_TIMEOUT = "timeout"
ERR_PROTOCOL = "protocol_error"
ERR_CLOSING = "shutdown_closing"
ERR_INSTANCE_BUSY = "instance_busy"
ERR_WORKER_START = "worker_start_failed"
ERR_INSUFFICIENT_RAM = "insufficient_ram"
ERR_INSUFFICIENT_VRAM = "insufficient_vram"
ERR_CUDA_UNAVAILABLE = "cuda_unavailable"
ERR_MODEL_LOCK_BUSY = "model_lock_busy"
ERR_ASSET_PIN = "asset_pin_failed"
ERR_RESOURCE_UNOBSERVABLE = "resource_unobservable"

_ALLOWED_ERROR_LABELS: Set[str] = {
    ERR_SCHEMA,
    ERR_REQUEST_BUSY,
    ERR_TEXT_TOO_LONG,
    ERR_UNKNOWN_STYLE,
    ERR_OUTPUT_REJECTED,
    ERR_SYNTH_FAILED,
    ERR_CANCELLED,
    ERR_TIMEOUT,
    ERR_PROTOCOL,
    ERR_CLOSING,
    ERR_INSTANCE_BUSY,
    ERR_WORKER_START,
    ERR_INSUFFICIENT_RAM,
    ERR_INSUFFICIENT_VRAM,
    ERR_CUDA_UNAVAILABLE,
    ERR_MODEL_LOCK_BUSY,
    ERR_ASSET_PIN,
    ERR_RESOURCE_UNOBSERVABLE,
}

_REQUIRED_INT_FIELDS = frozenset(
    {
        "schema",
        "epoch",
        "output_dev",
        "output_ino",
    }
)


def _reject_non_finite_int(path: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(path)
    return value


def _strict_finite_nonneg_number(path: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(path)
    if isinstance(value, int):
        if value < 0:
            raise ValueError(path)
        try:
            out = float(value)
        except OverflowError:
            raise ValueError(path) from None
        if not math.isfinite(out):
            raise ValueError(path)
        return out
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(path)
        return float(value)
    raise ValueError(path)


def _strict_request_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("request_id")
    return value


def _reject_line_too_long(line: str) -> None:
    if len(line) > MAX_JSON_LINE_CHARS:
        raise ValueError("line_too_long")


def dumps_message(payload: Mapping[str, Any]) -> str:
    """编码单行 JSON（UTF-8 文本模式由调用方写入管道）。"""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_JSON_LINE_CHARS:
        raise ValueError("payload_too_long")
    return text + "\n"


def parse_json_line(line: str) -> Dict[str, Any]:
    _reject_line_too_long(line)
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        raise ValueError("not_json_object")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        raise ValueError("json_decode") from None
    if not isinstance(payload, dict):
        raise ValueError("not_object")
    return payload


def validate_outbound_message(payload: Mapping[str, Any]) -> Dict[str, Any]:
    schema = payload.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ValueError(ERR_SCHEMA)
    if schema != SCHEMA_VERSION:
        raise ValueError(ERR_SCHEMA)
    msg_type = payload.get("type")
    if msg_type not in (
        MSG_READY,
        MSG_STARTUP_PROGRESS,
        MSG_SYNTH_RESULT,
        MSG_ERROR,
        MSG_SHUTDOWN_ACK,
    ):
        raise ValueError(ERR_SCHEMA)
    if msg_type == MSG_ERROR:
        label = payload.get("label")
        if label not in _ALLOWED_ERROR_LABELS:
            raise ValueError(ERR_SCHEMA)
    if msg_type == MSG_READY:
        _strict_finite_nonneg_number("load_ms", payload.get("load_ms"))
    if msg_type == MSG_STARTUP_PROGRESS:
        stage = payload.get("stage")
        if stage not in _STARTUP_STAGE_SET:
            raise ValueError(ERR_SCHEMA)
        phase = payload.get("phase")
        if phase not in _STARTUP_PHASES:
            raise ValueError(ERR_SCHEMA)
        _strict_finite_nonneg_number("elapsed_ms", payload.get("elapsed_ms"))
    if msg_type == MSG_SYNTH_RESULT:
        if payload.get("ok") is not True:
            raise ValueError(ERR_SCHEMA)
        epoch = payload.get("epoch")
        _reject_non_finite_int("epoch", epoch)
        if epoch < 0:
            raise ValueError(ERR_SCHEMA)
        _strict_request_id(payload.get("request_id"))
        _strict_finite_nonneg_number("duration_ms", payload.get("duration_ms"))
        nbytes = payload.get("bytes")
        if isinstance(nbytes, bool) or not isinstance(nbytes, int):
            raise ValueError(ERR_SCHEMA)
        if nbytes <= 0 or nbytes > MAX_AUDIO_BYTES:
            raise ValueError(ERR_SCHEMA)
    if msg_type == MSG_ERROR:
        if "request_id" in payload:
            _strict_request_id(payload.get("request_id"))
        if "epoch" in payload:
            epoch = payload.get("epoch")
            _reject_non_finite_int("epoch", epoch)
            if epoch < 0:
                raise ValueError(ERR_SCHEMA)
    return dict(payload)


def build_synth_request(
    *,
    request_id: str,
    epoch: int,
    style_id: str,
    text: str,
    output_path: str,
    output_dev: int,
    output_ino: int,
) -> str:
    """``output_dev``/``output_ino`` 为 CosyVoice portable 身份（见 cross_file_identity），非裸 os.stat。"""
    payload = {
        "schema": SCHEMA_VERSION,
        "type": REQ_SYNTH,
        "request_id": request_id,
        "epoch": epoch,
        "style_id": style_id,
        "text": text,
        "output_path": output_path,
        "output_dev": output_dev,
        "output_ino": output_ino,
    }
    return dumps_message(payload)


def build_shutdown_request() -> str:
    return dumps_message({"schema": SCHEMA_VERSION, "type": REQ_SHUTDOWN})


@dataclass(frozen=True)
class StartupProgressEvent:
    stage: str
    phase: str
    elapsed_ms: float


@dataclass(frozen=True)
class StartupProgressSnapshot:
    """有界、不可变的启动阶段快照（仅固定 stage/phase/elapsed_ms）。"""

    events: Tuple[StartupProgressEvent, ...]


_EMPTY_STARTUP_SNAPSHOT = StartupProgressSnapshot(events=())


def empty_startup_progress_snapshot() -> StartupProgressSnapshot:
    return _EMPTY_STARTUP_SNAPSHOT


def build_startup_progress_message(
    *,
    stage: str,
    phase: str,
    elapsed_ms: float,
) -> str:
    if stage not in _STARTUP_STAGE_SET:
        raise ValueError(ERR_SCHEMA)
    if phase not in _STARTUP_PHASES:
        raise ValueError(ERR_SCHEMA)
    elapsed_ms = _strict_finite_nonneg_number("elapsed_ms", elapsed_ms)
    return dumps_message(
        {
            "schema": SCHEMA_VERSION,
            "type": MSG_STARTUP_PROGRESS,
            "stage": stage,
            "phase": phase,
            "elapsed_ms": round(float(elapsed_ms), 3),
        }
    )


class StartupProgressAccumulator:
    """父进程侧：严格校验顺序与次数，生成不可变快照。"""

    def __init__(self) -> None:
        self._events: list[StartupProgressEvent] = []
        self._seen: Set[Tuple[str, str]] = set()
        self._ended: Set[str] = set()
        self._open_stage: Optional[str] = None

    def reset(self) -> None:
        self._events.clear()
        self._seen.clear()
        self._ended.clear()
        self._open_stage = None

    def snapshot(self) -> StartupProgressSnapshot:
        return StartupProgressSnapshot(events=tuple(self._events))

    def accept(self, payload: Mapping[str, Any]) -> StartupProgressEvent:
        if len(self._events) >= _MAX_STARTUP_PROGRESS_EVENTS:
            raise ValueError(ERR_SCHEMA)
        stage = payload.get("stage")
        phase = payload.get("phase")
        if stage not in _STARTUP_STAGE_SET or phase not in _STARTUP_PHASES:
            raise ValueError(ERR_SCHEMA)
        elapsed_ms = _strict_finite_nonneg_number(
            "elapsed_ms", payload.get("elapsed_ms")
        )
        key = (str(stage), str(phase))
        if key in self._seen:
            raise ValueError(ERR_SCHEMA)
        if phase == STARTUP_PHASE_END:
            if self._open_stage != stage:
                raise ValueError(ERR_SCHEMA)
            self._open_stage = None
            self._ended.add(str(stage))
        else:
            if self._open_stage is not None:
                raise ValueError(ERR_SCHEMA)
            self._open_stage = str(stage)
        self._seen.add(key)
        event = StartupProgressEvent(
            stage=str(stage),
            phase=str(phase),
            elapsed_ms=float(elapsed_ms),
        )
        self._events.append(event)
        return event


class StartupProgressReporter:
    """Worker 侧：各 stage 最多 begin/end 各一次，总事件数有硬上限。"""

    def __init__(
        self,
        emit: Callable[[str], None],
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._emit = emit
        self._clock = clock or time.perf_counter
        self._t0 = self._clock()
        self._seen: Set[Tuple[str, str]] = set()
        self._open_stage: Optional[str] = None
        self._count = 0

    def _elapsed_ms(self) -> float:
        return max(0.0, (self._clock() - self._t0) * 1000.0)

    def _send(self, stage: str, phase: str) -> bool:
        if self._count >= _MAX_STARTUP_PROGRESS_EVENTS:
            return False
        key = (stage, phase)
        if key in self._seen:
            return False
        if phase == STARTUP_PHASE_BEGIN:
            if self._open_stage is not None:
                return False
            self._open_stage = stage
        else:
            if self._open_stage != stage:
                return False
            self._open_stage = None
        self._seen.add(key)
        self._count += 1
        self._emit(
            build_startup_progress_message(
                stage=stage,
                phase=phase,
                elapsed_ms=self._elapsed_ms(),
            )
        )
        return True

    def begin(self, stage: str) -> bool:
        if stage not in _STARTUP_STAGE_SET:
            return False
        return self._send(stage, STARTUP_PHASE_BEGIN)

    def end(self, stage: str) -> bool:
        if stage not in _STARTUP_STAGE_SET:
            return False
        return self._send(stage, STARTUP_PHASE_END)


def build_ready_message(load_ms: float) -> str:
    if not math.isfinite(load_ms) or load_ms < 0:
        load_ms = 0.0
    return dumps_message(
        {
            "schema": SCHEMA_VERSION,
            "type": MSG_READY,
            "load_ms": round(float(load_ms), 3),
        }
    )


def build_synth_ok(
    *,
    request_id: str,
    epoch: int,
    duration_ms: float,
    bytes_written: int,
) -> str:
    _strict_request_id(request_id)
    _reject_non_finite_int("epoch", epoch)
    if epoch < 0:
        raise ValueError(ERR_SCHEMA)
    duration_ms = _strict_finite_nonneg_number("duration_ms", duration_ms)
    if isinstance(bytes_written, bool) or not isinstance(bytes_written, int):
        raise ValueError(ERR_SCHEMA)
    if bytes_written <= 0 or bytes_written > MAX_AUDIO_BYTES:
        raise ValueError(ERR_SCHEMA)
    return dumps_message(
        {
            "schema": SCHEMA_VERSION,
            "type": MSG_SYNTH_RESULT,
            "request_id": request_id,
            "epoch": epoch,
            "ok": True,
            "duration_ms": round(float(duration_ms), 3),
            "bytes": bytes_written,
        }
    )


def build_error_message(
    label: str,
    *,
    request_id: Optional[str] = None,
    epoch: Optional[int] = None,
) -> str:
    if label not in _ALLOWED_ERROR_LABELS:
        label = ERR_PROTOCOL
    payload: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "type": MSG_ERROR,
        "label": label,
    }
    if request_id is not None:
        payload["request_id"] = _strict_request_id(request_id)
    if epoch is not None:
        epoch_val = _reject_non_finite_int("epoch", epoch)
        if epoch_val < 0:
            raise ValueError(ERR_SCHEMA)
        payload["epoch"] = epoch_val
    return dumps_message(payload)
