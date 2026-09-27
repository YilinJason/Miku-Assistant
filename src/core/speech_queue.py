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
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


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
    accepted: int = 0
    finished: int = 0
    speaking: bool = False
    production_finished: bool = False
    terminal: Optional[PlaybackStatus] = None


@dataclass(frozen=True)
class _SpeechItem:
    turn_id: int
    text: str


_STOP = object()


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
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._tts = tts_engine
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._poll_interval = poll_interval
        self._logger = logger or logging.getLogger(__name__)
        self._stop = threading.Event()
        self._consumer_failed = threading.Event()
        self._turn_id = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._space_event = threading.Event()
        self._space_event.set()
        self._turns: Dict[int, _TurnPlaybackState] = {}
        self._worker = threading.Thread(
            target=self._run, name="TurnSpeechPlayer", daemon=True
        )
        self._worker.start()

    @property
    def turn_id(self) -> int:
        return self._turn_id

    @property
    def consumer_failed(self) -> bool:
        return self._consumer_failed.is_set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def begin_turn(self) -> int:
        """开始新轮次：丢弃待播、清除失败标志，返回新 turn_id。"""
        with self._cond:
            self._invalidate_current_unlocked(PlaybackStatus.INVALIDATED)
            self._turn_id += 1
            tid = self._turn_id
            self._drain_unlocked()
            self._consumer_failed.clear()
            self._turns[tid] = _TurnPlaybackState(turn_id=tid)
            self._space_event.set()
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
            self._turn_id += 1
            self._drain_unlocked()
            self._consumer_failed.clear()
            self._turns[self._turn_id] = _TurnPlaybackState(turn_id=self._turn_id)
            self._space_event.set()
            self._cond.notify_all()
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
        item = _SpeechItem(turn_id=turn_id, text=text.strip())
        while True:
            if self._stop.is_set() or self._consumer_failed.is_set():
                return False
            if turn_id != self._turn_id:
                return False
            if cancel_event is not None and cancel_event.is_set():
                return False
            if extra_cancel is not None and extra_cancel.is_set():
                return False
            try:
                self._queue.put(item, timeout=self._poll_interval)
            except queue.Full:
                self._space_event.wait(timeout=self._poll_interval)
                continue
            with self._cond:
                if turn_id != self._turn_id or self._stop.is_set():
                    # 入队竞态：轮次已变，不计入本轮 accepted
                    self._space_event.set()
                    return False
                state = self._turns.get(turn_id)
                if state is None or state.terminal is not None:
                    self._space_event.set()
                    return False
                state.accepted += 1
                if self._queue.full():
                    self._space_event.clear()
                self._cond.notify_all()
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
                # 未知轮：可能已被清理；若当前已停则 STOPPED
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
                    # 等待方取消：不强制改写终端态；调用方通常会 invalidate
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
            self._invalidate_current_unlocked(PlaybackStatus.STOPPED)
            self._drain_unlocked()
            self._space_event.set()
            self._cond.notify_all()

    def shutdown(self, timeout: float = 2.0) -> bool:
        """停止并丢弃全部待播项，有界等待消费者退出。"""
        self._stop.set()
        with self._cond:
            self._invalidate_current_unlocked(PlaybackStatus.STOPPED)
            for state in self._turns.values():
                if state.terminal is None:
                    state.terminal = PlaybackStatus.STOPPED
            self._drain_unlocked()
            try:
                self._queue.put_nowait(_STOP)
            except queue.Full:
                self._drain_unlocked()
                try:
                    self._queue.put_nowait(_STOP)
                except queue.Full:
                    pass
            self._space_event.set()
            self._cond.notify_all()
        self._worker.join(timeout=timeout)
        alive = self._worker.is_alive()
        if alive:
            self._logger.error(
                "TurnSpeechPlayer 在 %.1fs 内未退出；不声称底层音频已停止",
                timeout,
            )
        return not alive

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
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self._poll_interval)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            finally:
                self._space_event.set()

            if item is _STOP:
                break
            if self._stop.is_set():
                continue
            assert isinstance(item, _SpeechItem)

            with self._cond:
                if item.turn_id != self._turn_id:
                    continue
                if self._consumer_failed.is_set():
                    continue
                state = self._turns.get(item.turn_id)
                if state is None or state.terminal is not None:
                    continue
                if self._tts is None:
                    # 无引擎：计为完成以免永远等待
                    state.finished += 1
                    self._maybe_complete_unlocked(state)
                    self._cond.notify_all()
                    continue
                state.speaking = True
                self._cond.notify_all()

            speak_ok = True
            try:
                self._tts.speak(item.text)
            except Exception as exc:
                speak_ok = False
                self._logger.error("播报消费者失败: %s", exc, exc_info=True)

            with self._cond:
                state = self._turns.get(item.turn_id)
                if state is None:
                    continue
                # 过期轮：不污染新轮
                if item.turn_id != self._turn_id:
                    if state.terminal is None:
                        state.speaking = False
                    continue
                state.speaking = False
                if not speak_ok:
                    if state.terminal is None:
                        state.terminal = PlaybackStatus.FAILED
                    self._consumer_failed.set()
                    self._drain_unlocked()
                    self._space_event.set()
                    self._cond.notify_all()
                    continue
                state.finished += 1
                self._maybe_complete_unlocked(state)
                self._cond.notify_all()
