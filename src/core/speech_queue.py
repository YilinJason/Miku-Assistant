"""
单消费者有界播报队列。

按轮次标识丢弃过期句子；顺序调用既有 ``tts.speak``（同步）。
背压时阻塞等待空位（可取消），不静默丢句。
停止后丢弃全部待播项；消费者失败后不再播放同轮已排队项。
提供按 ``turn_id`` 等待本轮播报完成的窄接口（不得用 queue.empty 推断）。
不声称能立刻打断底层已在播放的音频。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Deque, Dict, Iterator, Optional

from src.core.log_privacy import exception_log_label
from src.core.owned_thread import OwnedThreadTask

_MAX_TURN_STATES = 128


class PlaybackStatus(str, Enum):
    """本轮播报等待结果（与生成完成独立）。"""

    COMPLETED = "completed"
    FAILED = "failed"
    INVALIDATED = "invalidated"
    STOPPED = "stopped"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"  # 无播报项、无需等待


@dataclass
class _TurnPlaybackState:
    turn_id: int
    cancel_event: threading.Event
    accepted: int = 0
    finished: int = 0
    speaking: bool = False
    production_finished: bool = False
    terminal: Optional[PlaybackStatus] = None


@dataclass(frozen=True)
class _SpeechItem:
    turn_id: int
    text: str


def _validate_maxsize(maxsize: object) -> int:
    if isinstance(maxsize, bool) or not isinstance(maxsize, int):
        raise ValueError("maxsize must be int in 1..128")
    if not 1 <= maxsize <= 128:
        raise ValueError("maxsize must be int in 1..128")
    return maxsize


def _validate_poll_interval(poll_interval: object) -> float:
    if poll_interval is None or isinstance(poll_interval, bool):
        raise ValueError("poll_interval must be a finite number in 0.001..1")
    if not isinstance(poll_interval, (int, float)):
        raise ValueError("poll_interval must be a finite number in 0.001..1")
    value = float(poll_interval)
    if not math.isfinite(value) or not 0.001 <= value <= 1.0:
        raise ValueError("poll_interval must be a finite number in 0.001..1")
    return value


def _validate_shutdown_timeout(timeout: object) -> float:
    if timeout is None or isinstance(timeout, bool):
        raise ValueError("shutdown timeout must be a finite non-negative number")
    if not isinstance(timeout, (int, float)):
        raise ValueError("shutdown timeout must be a finite non-negative number")
    value = float(timeout)
    if not math.isfinite(value) or value < 0:
        raise ValueError("shutdown timeout must be a finite non-negative number")
    if value > 10.0:
        raise ValueError("shutdown timeout exceeds hard cap")
    return value


class TurnSpeechPlayer:
    """
    单个受管理播报消费者。

    - 容量 ``maxsize``；满时 ``enqueue`` 可阻塞等待空位。
    - ``compare_and_invalidate`` / ``begin_turn`` / 失败 / shutdown 会终结旧轮等待。
    - ``wait_for_turn``：生产结束后等待本轮全部 ``speak`` 返回；锁不覆盖 ``speak``。
    """

    def __init__(
        self,
        tts_engine: Any,
        *,
        maxsize: int = 8,
        poll_interval: float = 0.05,
        logger: Optional[logging.Logger] = None,
        thread_factory: Optional[Callable[..., threading.Thread]] = None,
    ) -> None:
        self._maxsize = _validate_maxsize(maxsize)
        self._poll_interval = _validate_poll_interval(poll_interval)
        self._tts = tts_engine
        self._logger = logger or logging.getLogger(__name__)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._deque: Deque[_SpeechItem] = deque()
        self._turn_id = 0
        self._turns: Dict[int, _TurnPlaybackState] = {}
        self._consumer_failed = False
        self._consumer_fatal = False
        self._bootstrap_failed = False
        self._consumer_started = False
        self._consumer_start_claimed = False
        self._owner = OwnedThreadTask(
            self._run, name="TurnSpeechPlayer", thread_factory=thread_factory
        )
        self._turn_cancel: Optional[threading.Event] = None

    @staticmethod
    def _tts_supports_turn_stream(tts: Any) -> bool:
        if tts is None:
            return False
        return getattr(tts, "supports_turn_stream", None) is True

    def _rotate_turn_cancel_unlocked(self) -> threading.Event:
        if self._turn_cancel is not None:
            self._turn_cancel.set()
        self._turn_cancel = threading.Event()
        return self._turn_cancel

    def _cancel_turn_unlocked(self) -> None:
        if self._turn_cancel is not None:
            self._turn_cancel.set()

    @property
    def turn_id(self) -> int:
        return self._turn_id

    @property
    def consumer_failed(self) -> bool:
        return self._consumer_failed

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def begin_turn(self) -> int:
        """开始新轮次：丢弃待播、清除可恢复失败标志，返回新 turn_id。"""
        with self._cond:
            self._invalidate_current_unlocked(PlaybackStatus.INVALIDATED)
            self._rotate_turn_cancel_unlocked()
            self._turn_id += 1
            tid = self._turn_id
            self._drain_unlocked()
            if not self._consumer_fatal and not self._bootstrap_failed:
                self._consumer_failed = False
            bound_cancel = self._turn_cancel
            assert bound_cancel is not None
            self._turns[tid] = _TurnPlaybackState(
                turn_id=tid, cancel_event=bound_cancel
            )
            self._trim_turns_unlocked()
            self._cond.notify_all()
            return tid

    def invalidate(self) -> int:
        """取消/退出：等同提升轮次并丢弃未播项。"""
        return self.begin_turn()

    def compare_and_invalidate(self, expected_turn_id: int) -> bool:
        """锁内 CAS：仅当 turn_id 匹配时提升并清空；终结旧轮等待为 INVALIDATED。"""
        with self._cond:
            if self._turn_id != expected_turn_id:
                return False
            self._invalidate_current_unlocked(PlaybackStatus.INVALIDATED)
            self._rotate_turn_cancel_unlocked()
            self._turn_id += 1
            self._drain_unlocked()
            if not self._consumer_fatal and not self._bootstrap_failed:
                self._consumer_failed = False
            bound_cancel = self._turn_cancel
            assert bound_cancel is not None
            self._turns[self._turn_id] = _TurnPlaybackState(
                turn_id=self._turn_id, cancel_event=bound_cancel
            )
            self._trim_turns_unlocked()
            self._cond.notify_all()
            return True

    def _enqueue_may_commit_unlocked(
        self,
        turn_id: int,
        cancel_event: Optional[threading.Event],
        extra_cancel: Optional[threading.Event],
    ) -> bool:
        if self._stop.is_set():
            return False
        if self._consumer_fatal or self._bootstrap_failed or self._consumer_failed:
            return False
        if turn_id != self._turn_id:
            return False
        if cancel_event is not None and cancel_event.is_set():
            return False
        if extra_cancel is not None and extra_cancel.is_set():
            return False
        state = self._turns.get(turn_id)
        if state is None or state.terminal is not None:
            return False
        if state.production_finished:
            return False
        return True

    def enqueue(
        self,
        turn_id: int,
        text: str,
        *,
        cancel_event: Optional[threading.Event] = None,
        extra_cancel: Optional[threading.Event] = None,
    ) -> bool:
        """入队一句。空文本视为成功且不增加计数。"""
        if not text or not text.strip():
            return True
        if (
            self._stop.is_set()
            or self._consumer_fatal
            or self._bootstrap_failed
            or self._consumer_failed
        ):
            return False
        item = _SpeechItem(turn_id=turn_id, text=text.strip())
        need_start = False
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False
            if extra_cancel is not None and extra_cancel.is_set():
                return False
            with self._cond:
                if not self._enqueue_may_commit_unlocked(
                    turn_id, cancel_event, extra_cancel
                ):
                    return False
                if len(self._deque) < self._maxsize:
                    self._deque.append(item)
                    state = self._turns[turn_id]
                    state.accepted += 1
                    if not self._consumer_started and not self._consumer_start_claimed:
                        self._consumer_start_claimed = True
                        need_start = True
                    self._cond.notify_all()
                    break
                self._cond.wait(timeout=self._poll_interval)

        if need_start:
            try:
                if self._owner.start():
                    self._consumer_started = True
                else:
                    with self._cond:
                        if item in self._deque:
                            self._deque.remove(item)
                            st = self._turns.get(turn_id)
                            if st is not None and st.accepted > 0:
                                st.accepted -= 1
                        self._cond.notify_all()
                    return False
            except BaseException:
                self._mark_bootstrap_failed()
                raise
        return True

    def mark_production_finished(self, turn_id: int) -> None:
        """声明本轮不再入队；若已无在途项则立即 COMPLETED。"""
        with self._cond:
            state = self._turns.get(turn_id)
            if state is None:
                return
            if state.terminal is not None:
                return
            state.production_finished = True
            self._maybe_complete_unlocked(state)
            self._cond.notify_all()

    def wait_for_turn(
        self,
        turn_id: int,
        *,
        timeout: Optional[float] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> PlaybackStatus:
        """
        等待本轮播报终结。

        不得用 ``queue.empty()`` 推断完成。``timeout`` 为有界秒数；取消/关闭会唤醒。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            state = self._turns.get(turn_id)
            if state is None:
                if self._stop.is_set():
                    return PlaybackStatus.STOPPED
                return PlaybackStatus.INVALIDATED

            while True:
                if state.terminal is not None:
                    return state.terminal
                if self._stop.is_set():
                    state.terminal = PlaybackStatus.STOPPED
                    self._cond.notify_all()
                    return PlaybackStatus.STOPPED
                if cancel_event is not None and cancel_event.is_set():
                    return PlaybackStatus.INVALIDATED

                if (
                    state.production_finished
                    and state.accepted == state.finished
                    and not state.speaking
                ):
                    state.terminal = PlaybackStatus.COMPLETED
                    self._cond.notify_all()
                    return PlaybackStatus.COMPLETED

                wait_timeout = self._poll_interval
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return PlaybackStatus.TIMEOUT
                    wait_timeout = min(wait_timeout, remaining)
                self._cond.wait(timeout=wait_timeout)

    def stop_accepting(self) -> None:
        """停止接收新播报并丢弃待播。"""
        self._stop.set()
        with self._cond:
            self._cancel_turn_unlocked()
            self._invalidate_current_unlocked(PlaybackStatus.STOPPED)
            self._drain_unlocked()
            self._cond.notify_all()

    def shutdown(self, timeout: float = 2.0) -> bool:
        """停止并丢弃全部待播项，有界等待消费者退出。"""
        budget = _validate_shutdown_timeout(timeout)
        self._stop.set()
        with self._cond:
            self._cancel_turn_unlocked()
            self._invalidate_current_unlocked(PlaybackStatus.STOPPED)
            for state in self._turns.values():
                if state.terminal is None:
                    state.terminal = PlaybackStatus.STOPPED
            self._drain_unlocked()
            self._cond.notify_all()
        self._owner.cancel()
        quiet = self._owner.join(timeout=budget)
        if not quiet:
            self._logger.error(
                "TurnSpeechPlayer 在 %.1fs 内未 quiet 退出；不声称底层音频已停止",
                budget,
            )
        return quiet

    def _mark_bootstrap_failed(self) -> None:
        with self._cond:
            self._cancel_turn_unlocked()
            self._bootstrap_failed = True
            self._consumer_fatal = True
            self._consumer_failed = True
            for state in self._turns.values():
                if state.terminal is None:
                    state.terminal = PlaybackStatus.FAILED
            self._drain_unlocked()
            self._cond.notify_all()

    def _apply_fatal_consumer_failure_unlocked(self) -> None:
        self._cancel_turn_unlocked()
        self._consumer_fatal = True
        self._consumer_failed = True
        for state in self._turns.values():
            if state.terminal is None:
                state.terminal = PlaybackStatus.FAILED
        self._drain_unlocked()

    def _trim_turns_unlocked(self) -> None:
        while len(self._turns) > _MAX_TURN_STATES:
            oldest = next(iter(self._turns))
            old_state = self._turns[oldest]
            if old_state.terminal is None:
                break
            del self._turns[oldest]

    def _invalidate_current_unlocked(self, status: PlaybackStatus) -> None:
        state = self._turns.get(self._turn_id)
        if state is not None and state.terminal is None:
            state.terminal = status
            state.speaking = False

    def _maybe_complete_unlocked(self, state: _TurnPlaybackState) -> None:
        if state.terminal is not None:
            return
        if (
            state.production_finished
            and state.accepted == state.finished
            and not state.speaking
        ):
            state.terminal = PlaybackStatus.COMPLETED

    def _drain_unlocked(self) -> None:
        self._deque.clear()

    def _dequeue_for_speak_unlocked(self) -> Optional[_SpeechItem]:
        while self._deque:
            item = self._deque.popleft()
            if self._stop.is_set():
                return None
            if self._consumer_failed and not self._consumer_fatal:
                continue
            if item.turn_id != self._turn_id:
                continue
            state = self._turns.get(item.turn_id)
            if state is None or state.terminal is not None:
                continue
            if self._tts is None:
                state.finished += 1
                self._maybe_complete_unlocked(state)
                continue
            state.speaking = True
            return item
        return None

    def _turn_sentence_iter(
        self,
        first: _SpeechItem,
        turn_id: int,
        consumer_cancel: threading.Event,
        turn_cancel: threading.Event,
    ) -> Iterator[str]:
        with self._cond:
            if consumer_cancel.is_set() or self._stop.is_set():
                return
            if turn_id != self._turn_id:
                return
            if turn_cancel.is_set():
                return
            state = self._turns.get(turn_id)
            if state is None or state.terminal is not None:
                return
            first_text = first.text
        yield first_text
        while True:
            text: Optional[str] = None
            with self._cond:
                if consumer_cancel.is_set() or self._stop.is_set():
                    return
                if turn_id != self._turn_id:
                    return
                if turn_cancel.is_set():
                    return
                state = self._turns.get(turn_id)
                if state is None or state.terminal is not None:
                    return
                if self._deque and self._deque[0].turn_id == turn_id:
                    nxt = self._deque.popleft()
                    self._cond.notify_all()
                    text = nxt.text
                elif state.production_finished:
                    return
                else:
                    self._cond.wait(timeout=self._poll_interval)
                    continue
            if text is not None:
                yield text

    def _run(self, cancel: threading.Event) -> None:
        try:
            while not cancel.is_set() and not self._stop.is_set():
                with self._cond:
                    while True:
                        if cancel.is_set() or self._stop.is_set():
                            return
                        item = self._dequeue_for_speak_unlocked()
                        if item is not None:
                            self._cond.notify_all()
                            break
                        self._cond.wait(timeout=self._poll_interval)

                turn_id = item.turn_id
                bound_turn_cancel: threading.Event
                with self._cond:
                    if item.turn_id != self._turn_id:
                        state = self._turns.get(item.turn_id)
                        if state is not None:
                            state.speaking = False
                        continue
                    state = self._turns.get(turn_id)
                    if state is None:
                        continue
                    bound_turn_cancel = state.cancel_event

                speak_ok = True
                try:
                    if self._tts_supports_turn_stream(self._tts):
                        def _on_chunk_done() -> None:
                            with self._cond:
                                state = self._turns.get(turn_id)
                                if state is None or turn_id != self._turn_id:
                                    return
                                state.finished += 1
                                self._maybe_complete_unlocked(state)
                                self._cond.notify_all()

                        self._tts.speak_stream(
                            self._turn_sentence_iter(
                                item, turn_id, cancel, bound_turn_cancel
                            ),
                            cancel_event=bound_turn_cancel,
                            on_chunk_done=_on_chunk_done,
                        )
                    else:
                        self._tts.speak(item.text)
                except Exception as exc:
                    speak_ok = False
                    self._logger.error(
                        "播报消费者失败 err=%s",
                        exception_log_label(exc),
                    )

                with self._cond:
                    state = self._turns.get(turn_id)
                    if state is None:
                        continue
                    if turn_id != self._turn_id:
                        state.speaking = False
                        continue
                    state.speaking = False
                    if not speak_ok:
                        self._cancel_turn_unlocked()
                        if state.terminal is None:
                            state.terminal = PlaybackStatus.FAILED
                        self._consumer_failed = True
                        self._drain_unlocked()
                        self._cond.notify_all()
                        continue
                    if not self._tts_supports_turn_stream(self._tts):
                        state.finished += 1
                        self._maybe_complete_unlocked(state)
                    self._cond.notify_all()
        except BaseException:
            with self._cond:
                self._apply_fatal_consumer_failure_unlocked()
                self._cond.notify_all()
            raise
