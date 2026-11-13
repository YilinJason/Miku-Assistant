# -*- coding: utf-8 -*-
"""单 worker 进程对应的有界 stdin 写入线程（避免 synthesize/shutdown 阻塞在管道写）。"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, IO, Optional

_POLL_SEC = 0.05


@dataclass
class _WriteJob:
    data: str
    done: threading.Event
    ok: bool = False
    cancelled: bool = False


class CosyVoiceStdinWriter:
    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._queue: queue.Queue[Optional[_WriteJob]] = queue.Queue(maxsize=4)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._active_job: Optional[_WriteJob] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="CosyVoiceStdinWriter",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=_POLL_SEC)
            except queue.Empty:
                continue
            if job is None:
                break
            self._active_job = job
            if self._stop.is_set():
                job.cancelled = True
                job.done.set()
                self._active_job = None
                break
            try:
                self._stream.write(job.data)
                self._stream.flush()
                job.ok = True
            except OSError:
                job.ok = False
            finally:
                job.done.set()
                self._active_job = None

    def write_bounded(
        self,
        data: str,
        deadline: float,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> bool:
        if self._stop.is_set():
            return False
        job = _WriteJob(data=data, done=threading.Event())
        while True:
            if self._stop.is_set() or (should_cancel is not None and should_cancel()):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            try:
                self._queue.put(job, timeout=min(_POLL_SEC, remaining))
                break
            except queue.Full:
                continue
        while True:
            if self._stop.is_set() or (should_cancel is not None and should_cancel()):
                job.cancelled = True
                job.done.set()
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            if job.done.wait(timeout=min(_POLL_SEC, remaining)):
                return job.ok and not job.cancelled
        return False

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def close(self) -> None:
        self._stop.set()
        active = self._active_job
        if active is not None:
            active.cancelled = True
            active.done.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def join(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        end = time.monotonic() + max(0.0, timeout)
        while thread.is_alive() and time.monotonic() < end:
            thread.join(timeout=min(_POLL_SEC, end - time.monotonic()))
        return not thread.is_alive()
