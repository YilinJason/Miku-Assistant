"""Turn 级有界句间音频预取：producer 惰性拉段；consumer 只 ready/play/cleanup。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Callable, Iterable, Optional, Set, Tuple

from src.core.owned_thread import OwnedThreadTask

PrepareFn = Callable[[str], Tuple[Optional[str], Set[str]]]
PlayFn = Callable[[str], None]
PathCleanupFn = Callable[[str], None]
PlanFn = Callable[[str], list[str]]
OnChunkDone = Callable[[], None]
InterruptedFn = Callable[[], bool]
IsOwnedFn = Callable[[str], bool]

_JOIN_BUDGET = 2.0
_EDGE_MAX_HELD_PATHS = 6
_COSY_MAX_HELD_WAV = 4

_EOF = object()


@dataclass(frozen=True)
class _PreparedItem:
    play_path: Optional[str]
    temp_paths: frozenset[str]
    chunk_done_after: bool = False
    empty_chunk: bool = False


@dataclass(frozen=True)
class _SegmentWork:
    text: Optional[str]
    chunk_done_after: bool
    empty_chunk: bool = False


class _LazySegmentSource:
    __slots__ = (
        "_chunks",
        "_plan",
        "_lock",
        "_pending",
        "_pending_idx",
        "_exhausted",
    )

    def __init__(self, chunks: Iterable[str], plan: PlanFn) -> None:
        self._chunks = iter(chunks)
        self._plan = plan
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._pending_idx = 0
        self._exhausted = False

    def next_work(self) -> Optional[_SegmentWork]:
        with self._lock:
            if self._exhausted:
                return None
            while True:
                if self._pending_idx < len(self._pending):
                    text = self._pending[self._pending_idx]
                    last = self._pending_idx == len(self._pending) - 1
                    self._pending_idx += 1
                    if self._pending_idx >= len(self._pending):
                        self._pending.clear()
                        self._pending_idx = 0
                    return _SegmentWork(text=text, chunk_done_after=last)
                try:
                    chunk = next(self._chunks)
                except StopIteration:
                    self._exhausted = True
                    return None
                segments = self._plan(chunk) if chunk else []
                if not segments:
                    return _SegmentWork(
                        text=None, chunk_done_after=True, empty_chunk=True
                    )
                self._pending = list(segments)
                self._pending_idx = 0


class _MergedSessionCancel:
    __slots__ = ("_external", "_session", "_owner")

    def __init__(
        self,
        external: threading.Event,
        session: "TurnStreamPrefetchSession",
        owner: Optional[threading.Event] = None,
    ) -> None:
        self._external = external
        self._session = session
        self._owner = owner

    def is_set(self) -> bool:
        if self._external.is_set():
            return True
        if self._session._session_cancel.is_set():
            return True
        owner = self._owner
        return owner is not None and owner.is_set()


class TurnStreamPrefetchSession:
    __slots__ = (
        "_owner",
        "_ready",
        "_credit",
        "_credit_lock",
        "_session_cancel",
        "_held_paths",
        "_held_lock",
        "_max_held_paths",
        "_prepare",
        "_prepare_error",
        "_prepare_error_lock",
        "_producer_eof",
        "_source",
        "_tfm_busy",
        "_last_cleanup_path",
        "_last_is_owned",
        "_tfm_pending_fn",
        "_external_cancel",
    )

    def __init__(
        self,
        *,
        max_held_paths: int,
        thread_factory: Optional[Callable[..., threading.Thread]] = None,
    ) -> None:
        self._max_held_paths = max_held_paths
        self._ready: Queue[object] = Queue(maxsize=1)
        self._credit = threading.Event()
        self._credit.set()
        self._credit_lock = threading.Lock()
        self._session_cancel = threading.Event()
        self._held_paths: Set[str] = set()
        self._held_lock = threading.Lock()
        self._prepare = self._missing_prepare
        self._prepare_error: Optional[BaseException] = None
        self._prepare_error_lock = threading.Lock()
        self._producer_eof = threading.Event()
        self._source: Optional[_LazySegmentSource] = None
        self._tfm_busy = False
        self._last_cleanup_path: Optional[PathCleanupFn] = None
        self._last_is_owned: Optional[IsOwnedFn] = None
        self._tfm_pending_fn: Optional[Callable[[], bool]] = None
        self._external_cancel: Optional[threading.Event] = None
        self._owner = OwnedThreadTask(
            self._producer_loop,
            name="TurnStreamPrefetch",
            thread_factory=thread_factory,
        )

    def snapshot(self):
        return self._owner.snapshot()

    def bind_prepare(self, prepare: PrepareFn) -> None:
        self._prepare = prepare

    def prepare_cancel_view(self, external: threading.Event) -> _MergedSessionCancel:
        """外部 + session 永久取消；Edge/Cosy prepare 只读观察（不含 owner）。"""
        return _MergedSessionCancel(external, self)

    def paths_released(
        self, tfm_pending: Optional[Callable[[], bool]] = None
    ) -> bool:
        if tfm_pending is not None and tfm_pending():
            return False
        if self._tfm_busy and tfm_pending is None:
            return False
        with self._held_lock:
            return len(self._held_paths) == 0

    def cancel(self) -> None:
        self._session_cancel.set()
        self._owner.cancel()
        self._release_credit()
        self._producer_eof.set()

    @staticmethod
    def _missing_prepare(_text: str) -> Tuple[Optional[str], Set[str]]:
        raise RuntimeError("TurnStreamPrefetchSession.prepare 未绑定")

    def run(
        self,
        chunks: Iterable[str],
        *,
        cancel: threading.Event,
        prefetch: bool,
        plan_segments: PlanFn,
        play: PlayFn,
        cleanup_path: PathCleanupFn,
        is_owned: IsOwnedFn,
        interrupted: InterruptedFn,
        on_chunk_done: OnChunkDone,
        tfm_pending: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._external_cancel = cancel
        merged = _MergedSessionCancel(cancel, self)
        self._source = _LazySegmentSource(chunks, plan_segments)
        self._last_cleanup_path = cleanup_path
        self._last_is_owned = is_owned
        self._tfm_pending_fn = tfm_pending
        self._credit.set()
        self._producer_eof.clear()

        if prefetch and not merged.is_set():
            if not self._owner.start():
                raise RuntimeError("TurnStreamPrefetch producer 未能启动")
        elif prefetch:
            self._producer_eof.set()

        play_error: Optional[BaseException] = None
        try:
            if prefetch:
                play_error = self._run_prefetch_consumer(
                    merged,
                    play,
                    cleanup_path,
                    is_owned,
                    interrupted,
                    on_chunk_done,
                    tfm_pending,
                )
            else:
                play_error = self._run_serial_consumer(
                    merged,
                    play,
                    cleanup_path,
                    is_owned,
                    interrupted,
                    on_chunk_done,
                    tfm_pending,
                )
        finally:
            self._request_producer_stop()
        if play_error is not None:
            raise play_error

    def finish(
        self,
        timeout: float = _JOIN_BUDGET,
        *,
        tfm_pending: Optional[Callable[[], bool]] = None,
    ) -> bool:
        self._request_producer_stop()
        quiet = self._owner.join(timeout=timeout)
        cleanup_path = self._last_cleanup_path
        is_owned = self._last_is_owned
        self._drain_ready_unplayed(
            cleanup_path=cleanup_path,
            is_owned=is_owned,
        )
        if cleanup_path is not None and is_owned is not None:
            self._reconcile_held_paths(cleanup_path, is_owned, tfm_pending)
        return quiet and self.paths_released(tfm_pending)

    def _request_producer_stop(self) -> None:
        self._session_cancel.set()
        self._owner.cancel()
        self._release_credit()
        self._producer_eof.set()

    def _drain_ready_unplayed(
        self,
        *,
        cleanup_path: Optional[PathCleanupFn],
        is_owned: Optional[IsOwnedFn],
    ) -> None:
        while True:
            try:
                item = self._ready.get_nowait()
            except Empty:
                break
            if item is _EOF:
                break
            if not isinstance(item, _PreparedItem):
                continue
            if cleanup_path is not None and item.temp_paths:
                self._cleanup_item_paths(
                    item, cleanup_path, is_owned or (lambda _p: False), None
                )

    def _take_credit(self, cancel: _MergedSessionCancel) -> bool:
        while not cancel.is_set():
            if self._credit.wait(timeout=0.05):
                with self._credit_lock:
                    if self._credit.is_set():
                        self._credit.clear()
                        return True
            self._raise_if_prepare_failed()
        return False

    def _release_credit(self) -> None:
        with self._credit_lock:
            self._credit.set()

    def _track_paths(self, paths: Set[str]) -> None:
        with self._held_lock:
            self._held_paths.update(paths)
            if len(self._held_paths) > self._max_held_paths:
                raise RuntimeError(
                    f"turn stream 持有路径数 {len(self._held_paths)} "
                    f"超过上限 {self._max_held_paths}"
                )

    def _release_path_if_freed(self, path: str, is_owned: IsOwnedFn) -> None:
        with self._held_lock:
            if is_owned(path):
                self._held_paths.add(path)
            else:
                self._held_paths.discard(path)

    def _reconcile_held_paths(
        self,
        cleanup_path: PathCleanupFn,
        is_owned: IsOwnedFn,
        tfm_pending: Optional[Callable[[], bool]],
    ) -> None:
        with self._held_lock:
            pending = set(self._held_paths)
        for path in pending:
            if not is_owned(path):
                with self._held_lock:
                    self._held_paths.discard(path)
                continue
            try:
                cleanup_path(path)
            except BaseException:
                pass
            self._release_path_if_freed(path, is_owned)
        with self._held_lock:
            if not self._held_paths and not (
                tfm_pending is not None and tfm_pending()
            ):
                self._tfm_busy = False

    def _store_prepare_error(self, exc: BaseException) -> None:
        with self._prepare_error_lock:
            if self._prepare_error is None:
                self._prepare_error = exc

    def _raise_if_prepare_failed(self) -> None:
        with self._prepare_error_lock:
            err = self._prepare_error
        if err is not None:
            raise err

    def _raise_if_owner_failed(self) -> None:
        err = self._owner.error
        if err is not None:
            with self._prepare_error_lock:
                if self._prepare_error is None:
                    self._prepare_error = err
            raise err

    def _pending_prepare_error(
        self, tfm_pending: Optional[Callable[[], bool]]
    ) -> Optional[RuntimeError]:
        if tfm_pending is None or not tfm_pending():
            return None
        self._tfm_busy = True
        return RuntimeError("TFM 仍有 pending 清理，拒绝 prepare")

    def _fail_producer(
        self,
        exc: BaseException,
        *,
        release_credit: bool = False,
    ) -> None:
        self._store_prepare_error(exc)
        if release_credit:
            self._release_credit()
        self._producer_eof.set()

    def _cleanup_item_paths(
        self,
        item: _PreparedItem,
        cleanup_path: PathCleanupFn,
        is_owned: IsOwnedFn,
        primary: Optional[BaseException],
    ) -> Optional[BaseException]:
        cleanup_error: Optional[BaseException] = None
        for path in item.temp_paths:
            try:
                cleanup_path(path)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                self._release_path_if_freed(path, is_owned)
            else:
                self._release_path_if_freed(path, is_owned)
        if cleanup_error is not None and primary is None:
            return cleanup_error
        return None

    def _run_serial_consumer(
        self,
        cancel: _MergedSessionCancel,
        play: PlayFn,
        cleanup_path: PathCleanupFn,
        is_owned: IsOwnedFn,
        interrupted: InterruptedFn,
        on_chunk_done: OnChunkDone,
        tfm_pending: Optional[Callable[[], bool]],
    ) -> Optional[BaseException]:
        assert self._source is not None
        play_error: Optional[BaseException] = None
        while not cancel.is_set() and not interrupted():
            pending_err = self._pending_prepare_error(tfm_pending)
            if pending_err is not None:
                return pending_err
            try:
                work = self._source.next_work()
            except BaseException as exc:
                return exc
            if work is None:
                break
            if work.empty_chunk:
                on_chunk_done()
                continue
            assert work.text is not None
            if cancel.is_set():
                return None
            try:
                play_path, temp_paths = self._prepare(work.text)
            except BaseException as exc:
                return exc
            if cancel.is_set():
                paths = frozenset(temp_paths)
                if paths:
                    item = _PreparedItem(
                        play_path, paths, chunk_done_after=work.chunk_done_after
                    )
                    self._discard_prepared_item(item, cleanup_path, is_owned)
                return None
            paths = frozenset(temp_paths)
            if paths:
                try:
                    self._track_paths(set(paths))
                except BaseException as exc:
                    return exc
            item = _PreparedItem(play_path, paths, chunk_done_after=work.chunk_done_after)
            play_error = self._play_item(
                item, cancel, play, cleanup_path, is_owned, interrupted
            )
            if play_error is not None:
                self._session_cancel.set()
                return play_error
            if work.chunk_done_after:
                on_chunk_done()
        return play_error

    def _run_prefetch_consumer(
        self,
        cancel: _MergedSessionCancel,
        play: PlayFn,
        cleanup_path: PathCleanupFn,
        is_owned: IsOwnedFn,
        interrupted: InterruptedFn,
        on_chunk_done: OnChunkDone,
        tfm_pending: Optional[Callable[[], bool]],
    ) -> Optional[BaseException]:
        play_error: Optional[BaseException] = None
        while True:
            if cancel.is_set() or interrupted():
                break
            pending_err = self._pending_prepare_error(tfm_pending)
            if pending_err is not None:
                return pending_err
            self._raise_if_prepare_failed()
            self._raise_if_owner_failed()
            try:
                item = self._ready.get(timeout=0.05)
            except Empty:
                if self._producer_eof.is_set() and self._ready.empty():
                    self._raise_if_prepare_failed()
                    self._raise_if_owner_failed()
                    break
                if self._prepare_error is not None:
                    self._raise_if_prepare_failed()
                continue
            if item is _EOF:
                self._raise_if_prepare_failed()
                self._raise_if_owner_failed()
                break
            if not isinstance(item, _PreparedItem):
                continue
            self._release_credit()
            if item.empty_chunk:
                on_chunk_done()
                continue
            play_error = self._play_item(
                item, cancel, play, cleanup_path, is_owned, interrupted
            )
            if play_error is not None:
                self._session_cancel.set()
                return play_error
            if item.chunk_done_after:
                on_chunk_done()
        return play_error

    def _play_item(
        self,
        item: _PreparedItem,
        cancel: _MergedSessionCancel,
        play: PlayFn,
        cleanup_path: PathCleanupFn,
        is_owned: IsOwnedFn,
        interrupted: InterruptedFn,
    ) -> Optional[BaseException]:
        primary: Optional[BaseException] = None
        try:
            if item.play_path is None:
                if not cancel.is_set() and not interrupted():
                    primary = RuntimeError("音频准备未返回可播放路径")
            else:
                play(item.play_path)
        except BaseException as exc:
            primary = exc
            self._session_cancel.set()
        finally:
            if item.temp_paths:
                cleanup_err = self._cleanup_item_paths(
                    item, cleanup_path, is_owned, primary
                )
                if primary is None and cleanup_err is not None:
                    primary = cleanup_err
        return primary

    def _discard_prepared_item(
        self,
        item: _PreparedItem,
        cleanup_path: PathCleanupFn,
        is_owned: IsOwnedFn,
    ) -> None:
        if not item.temp_paths:
            return
        self._cleanup_item_paths(item, cleanup_path, is_owned, None)

    def _producer_loop(self, owner_cancel: threading.Event) -> None:
        external = self._external_cancel
        assert external is not None
        cancel = _MergedSessionCancel(external, self, owner_cancel)
        assert self._source is not None
        cleanup_path = self._last_cleanup_path
        is_owned = self._last_is_owned
        tfm_pending = getattr(self, "_tfm_pending_fn", None)
        try:
            while not cancel.is_set():
                pending_err = self._pending_prepare_error(tfm_pending)
                if pending_err is not None:
                    self._fail_producer(pending_err)
                    return
                if not self._take_credit(cancel):
                    if cancel.is_set():
                        self._producer_eof.set()
                        return
                    continue
                if cancel.is_set():
                    self._release_credit()
                    self._producer_eof.set()
                    return
                work = self._source.next_work()
                if work is None:
                    self._release_credit()
                    self._put_ready(
                        _EOF,
                        cancel,
                        cleanup_path=cleanup_path,
                        is_owned=is_owned,
                    )
                    self._producer_eof.set()
                    return
                if work.empty_chunk:
                    tick = _PreparedItem(
                        None, frozenset(), chunk_done_after=True, empty_chunk=True
                    )
                    self._put_ready(
                        tick,
                        cancel,
                        cleanup_path=cleanup_path,
                        is_owned=is_owned,
                    )
                    continue
                assert work.text is not None
                if cancel.is_set():
                    self._release_credit()
                    self._producer_eof.set()
                    return
                play_path, temp_paths = self._prepare(work.text)
                if cancel.is_set():
                    paths = frozenset(temp_paths)
                    if paths and cleanup_path is not None and is_owned is not None:
                        late = _PreparedItem(
                            play_path,
                            paths,
                            chunk_done_after=work.chunk_done_after,
                        )
                        self._discard_prepared_item(late, cleanup_path, is_owned)
                    self._release_credit()
                    self._producer_eof.set()
                    return
                paths = frozenset(temp_paths)
                if paths:
                    self._track_paths(set(paths))
                prepared = _PreparedItem(
                    play_path,
                    paths,
                    chunk_done_after=work.chunk_done_after,
                )
                if cancel.is_set():
                    if cleanup_path is not None and is_owned is not None:
                        self._discard_prepared_item(
                            prepared, cleanup_path, is_owned
                        )
                    self._release_credit()
                    self._producer_eof.set()
                    return
                self._put_ready(
                    prepared,
                    cancel,
                    cleanup_path=cleanup_path,
                    is_owned=is_owned,
                )
        except BaseException as exc:
            self._fail_producer(exc, release_credit=True)
        finally:
            owner_err = self._owner.error
            if owner_err is not None:
                self._store_prepare_error(owner_err)
            self._producer_eof.set()

    def _put_ready(
        self,
        item: object,
        cancel: _MergedSessionCancel,
        *,
        cleanup_path: Optional[PathCleanupFn] = None,
        is_owned: Optional[IsOwnedFn] = None,
    ) -> None:
        while not cancel.is_set():
            try:
                self._ready.put(item, timeout=0.05)
                return
            except Full:
                continue
        if (
            isinstance(item, _PreparedItem)
            and item.temp_paths
            and cleanup_path is not None
            and is_owned is not None
        ):
            self._discard_prepared_item(item, cleanup_path, is_owned)


def edge_max_held_paths() -> int:
    return _EDGE_MAX_HELD_PATHS


def cosy_max_held_wav() -> int:
    return _COSY_MAX_HELD_WAV
