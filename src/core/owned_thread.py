"""窄 stdlib 线程任务 owner：供流式预取等场景复用，与 TFM 语义对齐但不共享实现。"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Callable, Optional

_ThreadFactory = Callable[..., threading.Thread]


@dataclass(frozen=True)
class OwnedThreadStatus:
    creator_pending: bool
    start_unknown: bool
    entered: bool
    terminal: bool
    alive: bool
    quiet: bool


def _quiet_from_fields(
    *,
    start_claimed: bool,
    creator_pending: bool,
    start_unknown: bool,
    entered: bool,
    terminal: bool,
    os_start_committed: bool,
    alive: bool,
) -> bool:
    if not start_claimed:
        return True
    if creator_pending:
        return False
    if start_unknown:
        if alive:
            return False
        if entered and terminal:
            return True
        return False
    if not os_start_committed:
        if alive:
            return False
        return True
    if not entered or not terminal:
        return False
    if alive:
        return False
    return True


class OwnedThreadTask:
    """单实例单代线程 owner；``join`` 表 quiet 而非成功。"""

    __slots__ = (
        "_cancel",
        "_creator_pending",
        "_entered",
        "_error",
        "_lock",
        "_name",
        "_os_start_committed",
        "_start_claimed",
        "_start_unknown",
        "_target",
        "_terminal",
        "_thread",
        "_thread_factory",
    )

    def __init__(
        self,
        target: Callable[[threading.Event], None],
        *,
        name: str,
        thread_factory: Optional[_ThreadFactory] = None,
    ) -> None:
        self._target = target
        self._name = name
        self._thread_factory = thread_factory
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._entered = threading.Event()
        self._terminal = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_claimed = False
        self._os_start_committed = False
        self._creator_pending = False
        self._start_unknown = False
        self._error: Optional[BaseException] = None

    @property
    def error(self) -> Optional[BaseException]:
        with self._lock:
            return self._error

    def cancel(self) -> None:
        self._cancel.set()

    def start(self) -> bool:
        if self._cancel.is_set():
            return False

        with self._lock:
            if self._cancel.is_set():
                return False
            if self._start_claimed:
                return False
            self._start_claimed = True
            self._creator_pending = True

        thread: Optional[threading.Thread] = None
        ack = False
        try:
            factory = (
                self._thread_factory
                if self._thread_factory is not None
                else threading.Thread
            )
            thread = factory(target=self._wrapper, name=self._name, daemon=True)

            with self._lock:
                if self._cancel.is_set():
                    self._thread = thread
                    return False
                self._thread = thread
                self._entered.clear()
                self._terminal.clear()
                self._os_start_committed = True

            thread.start()
            ack = True
        except BaseException as exc:
            with self._lock:
                if self._error is None:
                    self._error = exc
                if self._os_start_committed:
                    self._start_unknown = True
                if thread is not None and self._thread is None:
                    self._thread = thread
            raise
        finally:
            with self._lock:
                self._creator_pending = False

        return ack

    def join(self, timeout: float = 2.0) -> bool:
        budget = _validate_join_timeout(timeout)
        with self._lock:
            start_claimed = self._start_claimed
            creator_pending = self._creator_pending
            thread = self._thread
            start_unknown = self._start_unknown
            entered = self._entered.is_set()
            terminal = self._terminal.is_set()
            os_committed = self._os_start_committed

        if not start_claimed:
            return True
        if creator_pending:
            return False

        alive = thread.is_alive() if thread is not None else False

        if thread is not None and os_committed and alive:
            thread.join(timeout=budget)
            alive = thread.is_alive()

        with self._lock:
            entered = self._entered.is_set()
            terminal = self._terminal.is_set()
            if (
                self._start_unknown
                and thread is not None
                and self._thread is thread
                and entered
                and terminal
                and not thread.is_alive()
            ):
                self._start_unknown = False
                start_unknown = False
            else:
                start_unknown = self._start_unknown
            creator_pending = self._creator_pending
            os_committed = self._os_start_committed

        alive = thread.is_alive() if thread is not None else False
        return _quiet_from_fields(
            start_claimed=start_claimed,
            creator_pending=creator_pending,
            start_unknown=start_unknown,
            entered=entered,
            terminal=terminal,
            os_start_committed=os_committed,
            alive=alive,
        )

    def snapshot(self) -> OwnedThreadStatus:
        with self._lock:
            creator_pending = self._creator_pending
            start_unknown = self._start_unknown
            entered = self._entered.is_set()
            terminal = self._terminal.is_set()
            start_claimed = self._start_claimed
            os_committed = self._os_start_committed
            thread = self._thread

        alive = thread.is_alive() if thread is not None else False
        quiet = _quiet_from_fields(
            start_claimed=start_claimed,
            creator_pending=creator_pending,
            start_unknown=start_unknown,
            entered=entered,
            terminal=terminal,
            os_start_committed=os_committed,
            alive=alive,
        )
        return OwnedThreadStatus(
            creator_pending=creator_pending,
            start_unknown=start_unknown,
            entered=entered,
            terminal=terminal,
            alive=alive,
            quiet=quiet,
        )

    def _wrapper(self) -> None:
        if self._cancel.is_set():
            with self._lock:
                self._entered.set()
            with self._lock:
                self._terminal.set()
            return

        with self._lock:
            self._entered.set()

        try:
            self._target(self._cancel)
        except BaseException as exc:
            with self._lock:
                self._error = exc
        finally:
            with self._lock:
                self._terminal.set()


def _validate_join_timeout(timeout: object) -> float:
    if timeout is None or isinstance(timeout, bool):
        raise ValueError("join timeout must be a finite non-negative number")
    if not isinstance(timeout, (int, float)):
        raise ValueError("join timeout must be a finite non-negative number")
    value = float(timeout)
    if not math.isfinite(value) or value < 0:
        raise ValueError("join timeout must be a finite non-negative number")
    if value > 10.0:
        raise ValueError("join timeout exceeds hard cap")
    return value
