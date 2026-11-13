"""CosyVoice 阻塞 stdin 写入场景的离线测试夹具（与生产超时/Job 无关）。"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.voice.cosyvoice.client import CosyVoiceWorkerClient

WRITE_ENTERED_RENDEZVOUS_SEC = 5.0
SYNTH_THREAD_JOIN_SEC = 12.0
PROC_STOP_JOIN_SEC = 2.0
CLIENT_SHUTDOWN_RETRY_SEC = 0.5


class _FakeStdinQueue:
    def __init__(self) -> None:
        self._lines: queue.Queue[str] = queue.Queue()
        self._closed = False

    def write(self, data: str) -> int:
        if data:
            self._lines.put(data)
        return len(data)

    def flush(self) -> None:
        return None

    def readline(self) -> str:
        while not self._closed:
            try:
                return self._lines.get(timeout=0.1)
            except queue.Empty:
                continue
        return ""

    def close(self) -> None:
        self._closed = True


class BlockingFakeStdin(_FakeStdinQueue):
    """客户端 stdin 写入线程在此阻塞，直到 release 或 close。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, data: str) -> int:
        if data:
            self.entered.set()
            self.release.wait(WRITE_ENTERED_RENDEZVOUS_SEC)
        return super().write(data)

    def close(self) -> None:
        self.release.set()
        super().close()


def wait_write_entered(stdin: BlockingFakeStdin, *, timeout: float | None = None) -> bool:
    budget = WRITE_ENTERED_RENDEZVOUS_SEC if timeout is None else timeout
    return stdin.entered.wait(budget)


def release_blocked_write(stdin: BlockingFakeStdin) -> None:
    stdin.release.set()
    stdin.close()


def thread_body_capture(
    errors: list[BaseException],
    body: Callable[[], None],
) -> Callable[[], None]:
    """包装线程目标：将线程体异常写入共享列表，避免后台 traceback 代替测试失败。"""

    def _wrapped() -> None:
        try:
            body()
        except BaseException as exc:
            errors.append(exc)

    return _wrapped


def _join_thread_or_error(
    thread: threading.Thread,
    *,
    label: str,
    timeout: float,
    errors: list[BaseException],
) -> None:
    if not thread.is_alive():
        return
    thread.join(timeout=timeout)
    if thread.is_alive():
        errors.append(
            RuntimeError(f"{label} 未在 {timeout}s 内退出（仍 alive）")
        )


def _merge_thread_errors(
    errors: list[BaseException],
    thread_errors: list[BaseException],
) -> None:
    seen = {id(exc) for exc in errors}
    for exc in thread_errors:
        token = id(exc)
        if token in seen:
            continue
        errors.append(exc)
        seen.add(token)


def _raise_teardown_errors(errors: list[BaseException]) -> None:
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup("blocked-write teardown 失败", errors)


@dataclass
class BlockedWriteTeardown:
    client: CosyVoiceWorkerClient
    proc: Any
    stdin: BlockingFakeStdin
    synth_thread: threading.Thread | None = None
    stop_thread: threading.Thread | None = None
    ready_gate: threading.Event | None = None
    worker_exit_gate: threading.Event | None = None
    thread_errors: list[BaseException] = field(default_factory=list)

    def run(self, *, shutdown_timeout: float = CLIENT_SHUTDOWN_RETRY_SEC) -> None:
        errors: list[BaseException] = []
        exit_gate = self.worker_exit_gate
        if exit_gate is not None:
            exit_gate.set()
        release_blocked_write(self.stdin)
        gate = self.ready_gate
        if gate is not None:
            gate.set()
        try:
            status = self.client.shutdown(timeout=shutdown_timeout)
            if not status.complete:
                self.client.continue_shutdown(timeout=shutdown_timeout)
        except BaseException as exc:
            errors.append(exc)
        synth = self.synth_thread
        if synth is not None:
            _join_thread_or_error(
                synth,
                label="synth 线程",
                timeout=SYNTH_THREAD_JOIN_SEC,
                errors=errors,
            )
        stop = self.stop_thread
        if stop is not None:
            _join_thread_or_error(
                stop,
                label="stop 线程",
                timeout=SYNTH_THREAD_JOIN_SEC,
                errors=errors,
            )
        _merge_thread_errors(errors, self.thread_errors)
        try:
            self.proc.stop(join_timeout=PROC_STOP_JOIN_SEC)
        except BaseException as exc:
            errors.append(exc)
        try:
            final = self.client.continue_shutdown(timeout=shutdown_timeout)
            if not final.complete:
                errors.append(
                    RuntimeError(
                        "blocked-write teardown：最终 continue_shutdown 仍未 complete"
                    )
                )
        except BaseException as exc:
            errors.append(exc)
        _raise_teardown_errors(errors)
