# -*- coding: utf-8 -*-
"""单 worker 进程对应的有界 stdout JSON-line reader（非每请求一线程）。"""
from __future__ import annotations

import logging
import queue
import threading
from typing import IO, Any, Dict, Optional

from src.voice.cosyvoice.bounded_line import BoundedLineTooLongError, read_bounded_line
from src.voice.cosyvoice.protocol import (
    MAX_JSON_LINE_CHARS,
    STDOUT_QUEUE_CAPACITY,
    parse_json_line,
    validate_outbound_message,
)

logger = logging.getLogger(__name__)


class CosyVoiceStdoutReader:
    """阻塞读取 stdout，解析 JSON 行并放入有界队列。"""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(
            maxsize=STDOUT_QUEUE_CAPACITY
        )
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._fatal = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._fatal = False
        self._thread = threading.Thread(
            target=self._run,
            name="CosyVoiceStdoutReader",
            daemon=True,
        )
        self._thread.start()

    def _fatal_stop(self) -> None:
        self._fatal = True
        self._stop.set()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    line = read_bounded_line(self._stream, MAX_JSON_LINE_CHARS)
                except BoundedLineTooLongError:
                    logger.warning("CosyVoice stdout 行过长")
                    self._fatal_stop()
                    break
                if line is None:
                    if not self._stop.is_set():
                        self._fatal_stop()
                    break
                if not line.endswith("\n"):
                    logger.warning("CosyVoice stdout 截断协议行")
                    self._fatal_stop()
                    break
                stripped = line.strip()
                if not stripped or not stripped.startswith("{"):
                    logger.warning("CosyVoice stdout 非 JSON 行")
                    self._fatal_stop()
                    break
                try:
                    payload = parse_json_line(line)
                    validate_outbound_message(payload)
                except ValueError:
                    logger.warning("CosyVoice stdout 协议行拒绝")
                    self._fatal_stop()
                    break
                try:
                    self._queue.put(payload, timeout=0.5)
                except queue.Full:
                    logger.warning("CosyVoice stdout 队列已满")
                    self._fatal_stop()
                    break
        except Exception:
            self._fatal_stop()
            logger.debug("CosyVoice stdout reader 结束", exc_info=True)
        finally:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

    def poll_message(self, timeout: float) -> Optional[Dict[str, Any]]:
        try:
            return self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop.set()

    def join(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def had_fatal_protocol_error(self) -> bool:
        return self._fatal
