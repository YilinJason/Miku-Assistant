"""有界异步记忆提取服务：单消费者队列，失败开放，不接入产品装配。"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from typing import Callable, Optional

from src.memory.admission import ExtractionAdmissionGate
from src.memory.errors import MemoryError, MemoryValidationError
from src.memory.extractor import MemoryExtractor
from src.memory.models import MemorySource, NewMemory
from src.memory.service_models import (
    ExtractedMemory,
    IngestionStopStatus,
    MemoryTurnEvent,
)
from src.memory.store import MemoryStore

DEFAULT_QUEUE_CAPACITY = 32
MIN_QUEUE_CAPACITY = 1
MAX_QUEUE_CAPACITY = 256
DEFAULT_MAX_EXTRACTIONS_PER_TURN = 16
MAX_MAX_EXTRACTIONS_PER_TURN = 64
_WORKER_POLL_SECONDS = 0.05

# 日志可观察的固定 skip_reason；未知/非字符串一律映射为 unknown（禁止 str(任意对象)）
ALLOWED_INGESTION_SKIP_REASONS = frozenset(
    {
        "no_safe_fact_unit",
        "eligibility_skip",
    }
)
_SAFE_SKIP_REASON_UNKNOWN = "unknown"


def normalize_ingestion_skip_reason(raw: object) -> str:
    """
    将 duck-typed skip_reason 规范为固定原因码。

    不得对任意对象调用 ``str()``，以免秘密正文或 ``__str__`` 副作用进入日志。
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return _SAFE_SKIP_REASON_UNKNOWN
    if raw == "":
        return ""
    if raw in ALLOWED_INGESTION_SKIP_REASONS:
        return raw
    return _SAFE_SKIP_REASON_UNKNOWN


class MemoryIngestionService:
    """
    对成功对话轮做非阻塞、有界、异步提取并写入 ``MemoryStore``。

    - 构造不启动线程；``start()`` 显式启动且幂等。
    - 业务队列与停止信号分离：``drain=True`` 从不偷删已接受任务。
    - ``submit`` 与停止边界在同一锁协议下线性化。
    - 默认不拥有 store（``owns_store=False``）；停止时不关闭共享存储。
    """

    def __init__(
        self,
        store: MemoryStore,
        extractor: MemoryExtractor,
        *,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        max_extractions_per_turn: int = DEFAULT_MAX_EXTRACTIONS_PER_TURN,
        owns_store: bool = False,
        admission_gate: Optional[ExtractionAdmissionGate] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not isinstance(queue_capacity, int) or isinstance(queue_capacity, bool):
            raise MemoryValidationError("queue_capacity must be int")
        if queue_capacity < MIN_QUEUE_CAPACITY or queue_capacity > MAX_QUEUE_CAPACITY:
            raise MemoryValidationError(
                f"queue_capacity must be in "
                f"[{MIN_QUEUE_CAPACITY}, {MAX_QUEUE_CAPACITY}]"
            )
        if (
            not isinstance(max_extractions_per_turn, int)
            or isinstance(max_extractions_per_turn, bool)
            or max_extractions_per_turn < 1
            or max_extractions_per_turn > MAX_MAX_EXTRACTIONS_PER_TURN
        ):
            raise MemoryValidationError(
                f"max_extractions_per_turn must be in "
                f"[1, {MAX_MAX_EXTRACTIONS_PER_TURN}]"
            )
        if not isinstance(owns_store, bool):
            raise MemoryValidationError("owns_store must be bool")

        self._store = store
        self._extractor = extractor
        self._queue_capacity = queue_capacity
        self._max_extractions_per_turn = max_extractions_per_turn
        self._owns_store = owns_store
        self._admission_gate = admission_gate
        self._logger = logger or logging.getLogger(__name__)

        self._lock = threading.Lock()
        self._idle_cond = threading.Condition(self._lock)
        self._queue: queue.Queue = queue.Queue(maxsize=queue_capacity)
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._started = False
        self._closing = False
        self._closed = False
        self._drain_mode = True
        self._in_flight = False
        self._accepted_pending = 0
        self._aborted = False
        self._rejected_full = 0
        self._rejected_invalid = 0
        # 仅供确定性竞态测试：在首次接受检查之后、入队之前调用（锁外）
        self._test_delay_after_accept: Optional[Callable[[], None]] = None
        # 仅供确定性竞态测试：dequeue 成功后、开始处理前调用（锁外）
        self._test_delay_after_dequeue: Optional[Callable[[], None]] = None

    @property
    def queue_capacity(self) -> int:
        return self._queue_capacity

    @property
    def rejected_full_count(self) -> int:
        with self._lock:
            return self._rejected_full

    @property
    def rejected_invalid_count(self) -> int:
        with self._lock:
            return self._rejected_invalid

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise MemoryValidationError("cannot start a closed ingestion service")
            if self._worker is not None and self._worker.is_alive():
                return
            self._closing = False
            self._drain_mode = True
            self._aborted = False
            self._stop_event.clear()
            self._started = True
            worker = threading.Thread(
                target=self._run_worker,
                name="MemoryIngestionWorker",
                daemon=False,
            )
            self._worker = worker
            worker.start()

    def submit(self, event: object) -> bool:
        """
        非阻塞入队。成功返回 True 时任务一定位于停止边界之前。

        非成功轮、非法事件、关闭后、停止中或队列满返回 False。
        """
        if not isinstance(event, MemoryTurnEvent):
            with self._lock:
                self._rejected_invalid += 1
            self._logger.warning(
                "memory ingestion rejected invalid_event err=%s",
                type(event).__name__,
            )
            return False
        if not event.is_extractable:
            with self._lock:
                self._rejected_invalid += 1
            self._logger.info(
                "memory ingestion skipped turn_id=%s outcome=%s",
                event.turn_id,
                event.outcome.value,
            )
            return False

        delay: Optional[Callable[[], None]]
        with self._lock:
            if not self._is_accepting_unlocked():
                return False
            delay = self._test_delay_after_accept
            if delay is None:
                return self._enqueue_unlocked(event)

        # 测试钩子在锁外暂停；入队前必须在锁内再次确认仍可接受
        delay()
        with self._lock:
            if not self._is_accepting_unlocked():
                return False
            return self._enqueue_unlocked(event)

    def stop(
        self, *, drain: bool = True, timeout: float = 5.0
    ) -> IngestionStopStatus:
        timeout_f = self._require_finite_timeout(timeout)
        if not isinstance(drain, bool):
            raise MemoryValidationError("drain must be bool")

        with self._lock:
            if self._closed and (self._worker is None or not self._worker.is_alive()):
                pending = self._pending_count_unlocked()
                return IngestionStopStatus(
                    discarded=0,
                    timed_out=False,
                    worker_alive=False,
                    drained=drain and pending == 0,
                    pending=pending,
                )
            self._closing = True
            self._drain_mode = drain
            self._stop_event.set()
            worker = self._worker

        discarded = 0
        if not drain:
            discarded = self._discard_pending()
            with self._idle_cond:
                self._idle_cond.notify_all()

        timed_out = False
        worker_alive = False
        if worker is not None:
            worker.join(timeout=timeout_f)
            worker_alive = worker.is_alive()
            timed_out = worker_alive
            if not worker_alive:
                with self._lock:
                    if self._worker is worker:
                        self._worker = None
                        self._started = False

        # worker 已因 BaseException 退出且仍有已接受任务：尝试替代 worker 排空
        if drain and not worker_alive:
            pending_before = self._pending_count()
            if pending_before > 0:
                replacement = self._spawn_drain_replacement()
                if replacement is not None:
                    replacement.join(timeout=timeout_f)
                    worker_alive = replacement.is_alive()
                    timed_out = timed_out or worker_alive
                    with self._lock:
                        if self._worker is replacement and not worker_alive:
                            self._worker = None
                            self._started = False

        pending = self._pending_count()
        drained = bool(drain and not timed_out and not worker_alive and pending == 0)
        return IngestionStopStatus(
            discarded=discarded,
            timed_out=timed_out,
            worker_alive=worker_alive,
            drained=drained,
            pending=pending,
        )

    def wait_until_idle(self, timeout: float) -> bool:
        """
        有界等待至已接受任务全部完成或丢弃（``_accepted_pending == 0``）。

        成功返回 True；超时返回 False。使用 Condition，禁止调用方以 sleep 猜测完成时机。
        """
        timeout_f = self._require_finite_timeout(timeout)
        deadline = time.monotonic() + timeout_f
        with self._idle_cond:
            while True:
                if self._accepted_pending == 0:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle_cond.wait(timeout=remaining)

    def is_idle(self) -> bool:
        """当前无已接受待处理任务且无在飞提取。"""
        with self._idle_cond:
            return self._accepted_pending == 0 and not self._in_flight

    def close(
        self, *, drain: bool = False, timeout: float = 5.0
    ) -> IngestionStopStatus:
        status = self.stop(drain=drain, timeout=timeout)
        close_store = False
        with self._idle_cond:
            self._closed = True
            self._closing = True
            self._started = False
            self._stop_event.set()
            worker = self._worker
            worker_still_alive = bool(status.worker_alive) or (
                worker is not None and worker.is_alive()
            )
            # worker 仍可能访问 store 时保留 owns_store，供后续 close 只关一次
            if self._owns_store and not worker_still_alive:
                self._owns_store = False
                close_store = True
            if worker_still_alive:
                # 超时关闭：状态必须继续显示未完成，不得在 worker 存活时关 store
                status = IngestionStopStatus(
                    discarded=status.discarded,
                    timed_out=True,
                    worker_alive=True,
                    drained=False,
                    pending=max(status.pending, self._pending_count_unlocked()),
                )
            self._idle_cond.notify_all()
        if close_store:
            self._close_owned_store()
        return status

    def _close_owned_store(self) -> None:
        closer = getattr(self._store, "close", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception as exc:  # noqa: BLE001 — 关闭路径失败开放
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._logger.error(
                "memory ingestion owns_store close failed err=%s",
                type(exc).__name__,
            )

    def _is_accepting_unlocked(self) -> bool:
        return (
            not self._closed
            and not self._closing
            and self._started
            and self._worker is not None
            and self._worker.is_alive()
        )

    def _enqueue_unlocked(self, event: MemoryTurnEvent) -> bool:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._rejected_full += 1
            self._logger.warning(
                "memory ingestion queue full turn_id=%s capacity=%s",
                event.turn_id,
                self._queue_capacity,
            )
            return False
        self._accepted_pending += 1
        return True

    def _release_accepted_unlocked(self, count: int = 1) -> None:
        if count <= 0:
            return
        self._accepted_pending = max(0, self._accepted_pending - count)
        self._idle_cond.notify_all()

    def _discard_pending(self) -> int:
        discarded = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            discarded += 1
        if discarded:
            with self._idle_cond:
                self._release_accepted_unlocked(discarded)
        return discarded

    def _pending_count(self) -> int:
        with self._lock:
            return self._pending_count_unlocked()

    def _pending_count_unlocked(self) -> int:
        # 单一线性化计数：已接受且尚未完成/丢弃的任务
        return self._accepted_pending

    def _spawn_drain_replacement(self) -> Optional[threading.Thread]:
        """在原 worker 异常退出后，为 drain 启动替代消费者（submit 仍拒绝）。"""
        with self._lock:
            if self._closed:
                return None
            if self._worker is not None and self._worker.is_alive():
                return None
            if self._pending_count_unlocked() <= 0:
                return None
            self._drain_mode = True
            self._aborted = False
            # 保持 _closing=True，禁止新 submit；清除 stop 以便处理剩余项
            self._stop_event.clear()
            self._started = True
            worker = threading.Thread(
                target=self._run_worker,
                name="MemoryIngestionDrainWorker",
                daemon=False,
            )
            self._worker = worker
            worker.start()
            # 立即再次请求停止：worker 将排空队列后退出
            self._stop_event.set()
            return worker

    def _run_worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=_WORKER_POLL_SECONDS)
            except queue.Empty:
                if self._should_exit_when_idle():
                    return
                continue
            except (KeyboardInterrupt, SystemExit) as exc:
                self._mark_aborted(exc)
                return
            except Exception as exc:  # noqa: BLE001
                self._logger.error(
                    "memory ingestion queue get failed err=%s", type(exc).__name__
                )
                continue

            delay = self._test_delay_after_dequeue
            try:
                if delay is not None:
                    delay()

                if not isinstance(item, MemoryTurnEvent):
                    self._logger.warning(
                        "memory ingestion worker skip bad_item err=%s",
                        type(item).__name__,
                    )
                else:
                    with self._idle_cond:
                        self._in_flight = True
                    try:
                        self._process_turn(item)
                    except (KeyboardInterrupt, SystemExit) as exc:
                        self._mark_aborted(exc)
                        return
                    except Exception as exc:  # noqa: BLE001 — 失败开放，继续下一轮
                        self._logger.error(
                            "memory ingestion turn failed turn_id=%s err=%s",
                            item.turn_id,
                            type(exc).__name__,
                        )
            except (KeyboardInterrupt, SystemExit) as exc:
                self._mark_aborted(exc)
                return
            finally:
                with self._idle_cond:
                    self._in_flight = False
                    self._release_accepted_unlocked(1)

            if self._should_exit_when_idle():
                return

    def _should_exit_when_idle(self) -> bool:
        with self._lock:
            if not self._stop_event.is_set():
                return False
            if self._accepted_pending > 0 or self._in_flight:
                return False
            if not self._drain_mode:
                return True
            try:
                return self._queue.qsize() == 0
            except NotImplementedError:
                return self._queue.empty()

    def _mark_aborted(self, exc: BaseException) -> None:
        with self._idle_cond:
            self._aborted = True
            self._in_flight = False
            self._idle_cond.notify_all()
        self._logger.error(
            "memory ingestion worker stopping err=%s",
            type(exc).__name__,
        )

    def _process_turn(self, event: MemoryTurnEvent) -> None:
        gate = self._admission_gate
        acquired = False
        if gate is not None:
            acquired = gate.try_acquire_extract()
            if not acquired:
                self._logger.info(
                    "memory extraction deferred/skipped turn_id=%s reason=foreground_busy",
                    event.turn_id,
                )
                return
        try:
            detailed = getattr(self._extractor, "extract_detailed", None)
            local_candidates = -1
            selected_count = -1
            skip_reason = ""
            if callable(detailed):
                result = detailed(event)
                raw_items = getattr(result, "items", ())
                lc = getattr(result, "local_candidates", -1)
                sel = getattr(result, "selected", -1)
                local_candidates = (
                    int(lc)
                    if isinstance(lc, int) and not isinstance(lc, bool)
                    else -1
                )
                selected_count = (
                    int(sel)
                    if isinstance(sel, int) and not isinstance(sel, bool)
                    else -1
                )
                skip_reason = normalize_ingestion_skip_reason(
                    getattr(result, "skip_reason", None)
                )
                if raw_items is None:
                    self._logger.warning(
                        "memory extractor returned None turn_id=%s", event.turn_id
                    )
                    return
                raw = raw_items
            else:
                raw = self._extractor.extract(event)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001
            self._logger.error(
                "memory extractor failed turn_id=%s err=%s",
                event.turn_id,
                type(exc).__name__,
            )
            return
        finally:
            if acquired and gate is not None:
                gate.release_extract()

        if raw is None:
            self._logger.warning(
                "memory extractor returned None turn_id=%s", event.turn_id
            )
            return

        try:
            items = list(raw)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001
            self._logger.error(
                "memory extractor result invalid turn_id=%s err=%s",
                event.turn_id,
                type(exc).__name__,
            )
            return

        if len(items) > self._max_extractions_per_turn:
            self._logger.warning(
                "memory extractor truncated turn_id=%s count=%s max=%s",
                event.turn_id,
                len(items),
                self._max_extractions_per_turn,
            )
            items = items[: self._max_extractions_per_turn]

        accepted = 0
        created = 0
        deduped = 0
        for index, item in enumerate(items):
            try:
                extracted = self._coerce_extracted(item)
                draft = NewMemory(
                    text=extracted.text,
                    category=extracted.category,
                    source=MemorySource.MODEL_INFERRED,
                    importance=extracted.importance,
                    confidence=extracted.confidence,
                )
                write_result = self._store.add(draft)
                accepted += 1
                if getattr(write_result, "created", True):
                    created += 1
                else:
                    deduped += 1
            except (KeyboardInterrupt, SystemExit):
                raise
            except MemoryError as exc:
                self._logger.error(
                    "memory write skipped turn_id=%s index=%s err=%s",
                    event.turn_id,
                    index,
                    type(exc).__name__,
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.error(
                    "memory write skipped turn_id=%s index=%s err=%s",
                    event.turn_id,
                    index,
                    type(exc).__name__,
                )

        if local_candidates == 0 and not skip_reason:
            skip_reason = "no_safe_fact_unit"
        if local_candidates >= 0:
            self._logger.info(
                "memory ingestion processed turn_id=%s accepted=%s "
                "local_candidates=%s selected=%s created=%s deduped=%s%s",
                event.turn_id,
                accepted,
                local_candidates,
                selected_count if selected_count >= 0 else len(items),
                created,
                deduped,
                f" reason={skip_reason}" if skip_reason and accepted == 0 else "",
            )
        else:
            self._logger.info(
                "memory ingestion processed turn_id=%s accepted=%s",
                event.turn_id,
                accepted,
            )

    @staticmethod
    def _coerce_extracted(item: object) -> ExtractedMemory:
        if isinstance(item, ExtractedMemory):
            return item
        raise MemoryValidationError(
            f"extractor item must be ExtractedMemory, got {type(item).__name__}"
        )

    @staticmethod
    def _require_finite_timeout(timeout: object) -> float:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise MemoryValidationError("timeout must be a number")
        timeout_f = float(timeout)
        if not math.isfinite(timeout_f):
            raise MemoryValidationError("timeout must be finite")
        if timeout_f < 0.0:
            raise MemoryValidationError("timeout must be >= 0")
        return timeout_f
