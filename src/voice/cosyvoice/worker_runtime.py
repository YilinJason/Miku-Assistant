# -*- coding: utf-8 -*-
"""CosyVoice runtime worker 主循环（可注入 fake 模型/stdin 单测）。"""
from __future__ import annotations

import contextlib
import io
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from src.core.parent_process import parent_process_is_alive
from src.voice.cosyvoice.bounded_line import BoundedLineTooLongError, read_bounded_line
from src.voice.cosyvoice.protocol import (
    ERR_OUTPUT_REJECTED,
    ERR_PROTOCOL,
    ERR_SYNTH_FAILED,
    ERR_UNKNOWN_STYLE,
    MSG_SHUTDOWN_ACK,
    build_error_message,
    build_synth_ok,
    dumps_message,
)
from src.voice.cosyvoice.styles import resolve_style_instruct
from src.voice.cosyvoice.worker_inbound import parse_inbound_line
from src.voice.cosyvoice.worker_output import (
    run_inference_instruct_to_wav,
    validate_worker_output_path,
)

_STDIN_QUEUE_CAPACITY = 8
_STDIN_LINE_MAX = 16_384


@dataclass
class StdinReaderFatal:
    triggered: bool = False


def _stdin_fatal_active(fatal: StdinReaderFatal | None) -> bool:
    return fatal is not None and fatal.triggered


def stdin_reader_thread(
    stdin: io.TextIOBase,
    out_q: queue.Queue[Optional[str]],
    *,
    fatal: StdinReaderFatal | None = None,
    line_max: int = _STDIN_LINE_MAX,
) -> None:
    state = fatal if fatal is not None else StdinReaderFatal()
    try:
        while True:
            try:
                line = read_bounded_line(stdin, line_max)
            except BoundedLineTooLongError:
                state.triggered = True
                break
            if line is None:
                break
            if not line.endswith("\n"):
                state.triggered = True
                break
            try:
                out_q.put(line, timeout=0.5)
            except queue.Full:
                state.triggered = True
                break
    finally:
        try:
            out_q.put_nowait(None)
        except queue.Full:
            state.triggered = True


@contextlib.contextmanager
def redirect_stdout_to_stderr():
    import sys

    old = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = old


def run_worker_loop(
    *,
    model: Any,
    settings: Any,
    parent_pid: int,
    repo_root: Path,
    tmp_root: Path,
    stdin_q: queue.Queue[Optional[str]],
    emit: Callable[[str], None],
    parent_alive: Callable[[int], bool] | None = None,
    redirect_stdout: Any | None = None,
    stdin_fatal: StdinReaderFatal | None = None,
) -> None:
    """处理 synth/shutdown 直至 idle、父退出或协议致命错误。"""
    alive = parent_alive or parent_process_is_alive
    redir = redirect_stdout or redirect_stdout_to_stderr
    last_active = time.monotonic()
    speaker = "中文女"

    while True:
        if _stdin_fatal_active(stdin_fatal):
            emit(build_error_message(ERR_PROTOCOL))
            break
        if not alive(parent_pid):
            break
        if time.monotonic() - last_active > float(settings.idle_timeout_sec):
            break
        try:
            line = stdin_q.get(timeout=0.25)
        except queue.Empty:
            continue
        if _stdin_fatal_active(stdin_fatal):
            emit(build_error_message(ERR_PROTOCOL))
            break
        if line is None:
            if _stdin_fatal_active(stdin_fatal):
                emit(build_error_message(ERR_PROTOCOL))
            break
        kind, inbound = parse_inbound_line(line, max_text_chars=settings.max_text_chars)
        if kind == "invalid":
            emit(build_error_message(ERR_PROTOCOL))
            break
        if kind == "shutdown":
            emit(dumps_message({"schema": 1, "type": MSG_SHUTDOWN_ACK}))
            break
        if inbound is None:
            continue
        if _stdin_fatal_active(stdin_fatal):
            emit(build_error_message(ERR_PROTOCOL))
            break
        out_path = validate_worker_output_path(
            inbound.output_path,
            inbound.output_dev,
            inbound.output_ino,
            repo_root=repo_root,
            tmp_root=tmp_root,
        )
        if out_path is None:
            if _stdin_fatal_active(stdin_fatal):
                emit(build_error_message(ERR_PROTOCOL))
                break
            emit(
                build_error_message(
                    ERR_OUTPUT_REJECTED,
                    request_id=inbound.request_id,
                    epoch=inbound.epoch,
                )
            )
            last_active = time.monotonic()
            continue
        try:
            instruct = resolve_style_instruct(inbound.style_id)
        except KeyError:
            emit(
                build_error_message(
                    ERR_UNKNOWN_STYLE,
                    request_id=inbound.request_id,
                    epoch=inbound.epoch,
                )
            )
            last_active = time.monotonic()
            continue
        t1 = time.perf_counter()
        try:
            nbytes = run_inference_instruct_to_wav(
                model,
                text=inbound.text,
                speaker=speaker,
                instruct=instruct,
                out_path=out_path,
                redirect_stdout=redir,
                expected_dev=inbound.output_dev,
                expected_ino=inbound.output_ino,
            )
        except Exception:
            if _stdin_fatal_active(stdin_fatal):
                emit(build_error_message(ERR_PROTOCOL))
                break
            emit(
                build_error_message(
                    ERR_SYNTH_FAILED,
                    request_id=inbound.request_id,
                    epoch=inbound.epoch,
                )
            )
            last_active = time.monotonic()
            continue
        if _stdin_fatal_active(stdin_fatal):
            emit(build_error_message(ERR_PROTOCOL))
            break
        duration_ms = (time.perf_counter() - t1) * 1000.0
        emit(
            build_synth_ok(
                request_id=inbound.request_id,
                epoch=inbound.epoch,
                duration_ms=duration_ms,
                bytes_written=nbytes,
            )
        )
        last_active = time.monotonic()
