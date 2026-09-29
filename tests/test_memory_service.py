"""长期记忆应用服务、词法检索、上下文预算与异步提取的纯离线测试。"""
from __future__ import annotations

import logging
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Optional, Sequence

from src.memory import (
    CONTEXT_HEADER,
    ExtractedMemory,
    LexicalMemoryRecall,
    MemoryCategory,
    MemoryContext,
    MemoryDataCorruptError,
    MemoryIngestionService,
    MemoryQueryService,
    MemorySource,
    MemoryStoreUnavailableError,
    MemoryTurnEvent,
    MemoryTurnOutcome,
    MemoryValidationError,
    NewMemory,
    ScoredMemory,
    SqliteMemoryStore,
    format_memory_context,
    lexical_tokens,
)
from src.memory.lexical_recall import MAX_CANDIDATE_LIMIT
from src.memory.models import MemoryRecord, normalize_dedupe_key
from src.memory.service_models import IngestionStopStatus
from src.memory.sqlite_store import _LIST_HAS_MORE_PROBE_LIMIT


def _utc(hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 3, 15, hour, minute, second, tzinfo=timezone.utc)


def _success(turn_id: str, user: str = "用户说", assistant: str = "助手答") -> MemoryTurnEvent:
    return MemoryTurnEvent(
        turn_id=turn_id,
        user_text=user,
        assistant_text=assistant,
        outcome=MemoryTurnOutcome.SUCCESS,
    )


class RecordingExtractor:
    def __init__(
        self,
        items: Optional[Sequence[ExtractedMemory]] = None,
        *,
        error: Optional[BaseException] = None,
        gate: Optional[threading.Event] = None,
        started: Optional[threading.Event] = None,
    ) -> None:
        self.items = list(items or [])
        self.error = error
        self.gate = gate
        self.started = started
        self.calls: List[MemoryTurnEvent] = []
        self.lock = threading.Lock()

    def extract(self, turn: MemoryTurnEvent) -> Sequence[ExtractedMemory]:
        with self.lock:
            self.calls.append(turn)
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            self.gate.wait(timeout=5.0)
        if self.error is not None:
            raise self.error
        return list(self.items)


class FakeStore:
    """可控假存储：支持阻塞写与注入故障。"""

    def __init__(self, real: SqliteMemoryStore) -> None:
        self.real = real
        self.write_gate: Optional[threading.Event] = None
        self.write_entered: Optional[threading.Event] = None
        self.fail_list_with: Optional[BaseException] = None
        self.closed = False
        self.close_calls = 0
        self.add_after_close = False

    def add(self, draft: NewMemory):
        if self.closed:
            self.add_after_close = True
        if self.write_entered is not None:
            self.write_entered.set()
        if self.write_gate is not None:
            self.write_gate.wait(timeout=5.0)
        return self.real.add(draft)

    def get(self, memory_id: str):
        return self.real.get(memory_id)

    def list_memories(self, *, limit: int = 50, offset: int = 0, newest_first: bool = False):
        if self.fail_list_with is not None:
            raise self.fail_list_with
        return self.real.list_memories(
            limit=limit, offset=offset, newest_first=newest_first
        )

    def update(self, memory_id: str, **kwargs):
        return self.real.update(memory_id, **kwargs)

    def delete(self, memory_id: str) -> bool:
        return self.real.delete(memory_id)

    def clear(self) -> int:
        return self.real.clear()

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self.real.close()


class TurnEventTests(unittest.TestCase):
    def test_only_success_nonempty_is_extractable(self) -> None:
        ok = _success("t1")
        self.assertTrue(ok.is_extractable)
        failed = MemoryTurnEvent(
            turn_id="t2",
            user_text="u",
            assistant_text="a",
            outcome=MemoryTurnOutcome.FAILED,
        )
        self.assertFalse(failed.is_extractable)
        cancelled = MemoryTurnEvent(
            turn_id="t3",
            user_text="u",
            assistant_text="a",
            outcome=MemoryTurnOutcome.CANCELLED,
        )
        self.assertFalse(cancelled.is_extractable)
        empty_user = MemoryTurnEvent(
            turn_id="t4",
            user_text="  ",
            assistant_text="a",
            outcome=MemoryTurnOutcome.SUCCESS,
        )
        self.assertFalse(empty_user.is_extractable)
        empty_assistant = MemoryTurnEvent(
            turn_id="t5",
            user_text="u",
            assistant_text="",
            outcome=MemoryTurnOutcome.SUCCESS,
        )
        self.assertFalse(empty_assistant.is_extractable)
        with self.assertRaises(MemoryValidationError):
            MemoryTurnEvent(
                turn_id="",
                user_text="u",
                assistant_text="a",
                outcome=MemoryTurnOutcome.SUCCESS,
            )


class FiniteContractTests(unittest.TestCase):
    def test_scored_memory_rejects_non_finite_bool_and_non_positive(self) -> None:
        record = MemoryRecord(
            id="s1",
            text="hit",
            category=MemoryCategory.OTHER,
            source=MemorySource.MODEL_INFERRED,
            importance=0.5,
            confidence=0.5,
            created_at=_utc(),
            updated_at=_utc(),
            dedupe_key=normalize_dedupe_key("hit"),
        )
        for bad in (float("nan"), float("inf"), float("-inf"), True, 0.0, -1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(MemoryValidationError):
                    ScoredMemory(record=record, score=bad)  # type: ignore[arg-type]
        ok = ScoredMemory(record=record, score=2.5)
        self.assertEqual(ok.score, 2.5)


class IngestionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "memory.sqlite3"
        self.real = SqliteMemoryStore(self.db_path, clock=_utc, id_factory=lambda: "id-1")
        self.store = FakeStore(self.real)
        self.services: List[MemoryIngestionService] = []

    def tearDown(self) -> None:
        for service in self.services:
            try:
                service.close(drain=False, timeout=2.0)
            except Exception:
                pass
        try:
            self.real.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _make(
        self,
        extractor: RecordingExtractor,
        *,
        capacity: int = 4,
        max_extractions: int = 8,
        owns_store: bool = False,
    ) -> MemoryIngestionService:
        service = MemoryIngestionService(
            self.store,
            extractor,
            queue_capacity=capacity,
            max_extractions_per_turn=max_extractions,
            owns_store=owns_store,
        )
        self.services.append(service)
        return service

    def _wait_processed(self, extractor: RecordingExtractor, count: int, timeout: float = 2.0) -> None:
        deadline = threading.Event()
        end = timeout
        # Event-based polling without sleep-as-assertion: wait on a short event loop
        finished = threading.Event()

        def _poll() -> None:
            while not deadline.is_set():
                with extractor.lock:
                    if len(extractor.calls) >= count:
                        finished.set()
                        return
                deadline.wait(0.01)
            # final check
            with extractor.lock:
                if len(extractor.calls) >= count:
                    finished.set()

        thread = threading.Thread(target=_poll, daemon=True)
        thread.start()
        ok = finished.wait(timeout=end)
        deadline.set()
        thread.join(timeout=1.0)
        self.assertTrue(ok, f"extractor calls={len(extractor.calls)} expected>={count}")

    def test_success_enqueued_others_rejected_without_extract(self) -> None:
        extractor = RecordingExtractor(
            [ExtractedMemory(text="喜欢拉面", category=MemoryCategory.PREFERENCE)]
        )
        service = self._make(extractor)
        service.start()
        self.assertTrue(service.submit(_success("ok-1")))
        self._wait_processed(extractor, 1)
        service.stop(drain=True, timeout=2.0)

        self.assertEqual(len(extractor.calls), 1)
        listed = self.real.list_memories()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].source, MemorySource.MODEL_INFERRED)

        extractor2 = RecordingExtractor([ExtractedMemory(text="不应写入")])
        service2 = self._make(extractor2)
        service2.start()
        for event in (
            MemoryTurnEvent("f", "u", "a", MemoryTurnOutcome.FAILED),
            MemoryTurnEvent("c", "u", "a", MemoryTurnOutcome.CANCELLED),
            MemoryTurnEvent("eu", "  ", "a", MemoryTurnOutcome.SUCCESS),
            MemoryTurnEvent("ea", "u", "", MemoryTurnOutcome.SUCCESS),
        ):
            self.assertFalse(service2.submit(event))
        self.assertFalse(service2.submit("not-an-event"))  # type: ignore[arg-type]
        service2.stop(drain=True, timeout=2.0)
        self.assertEqual(extractor2.calls, [])
        self.assertEqual(len(self.real.list_memories()), 1)

    def test_extractor_source_forced_model_inferred_and_rejects_illegal(self) -> None:
        ids = iter(["a", "b", "c"])
        self.real.close()
        self.real = SqliteMemoryStore(
            self.db_path, clock=_utc, id_factory=lambda: next(ids)
        )
        self.store = FakeStore(self.real)
        extractor = RecordingExtractor(
            [
                ExtractedMemory(text="合法事实", importance=0.7, confidence=0.6),
                "not-extracted",  # type: ignore[list-item]
            ]
        )
        # Patch second call items via mutable list after first processed — use one turn
        service = self._make(extractor, max_extractions=8)
        service.start()
        self.assertTrue(service.submit(_success("t-legal")))
        self._wait_processed(extractor, 1)
        service.stop(drain=True, timeout=2.0)
        rows = self.real.list_memories()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source, MemorySource.MODEL_INFERRED)
        self.assertEqual(rows[0].text, "合法事实")

        with self.assertRaises(MemoryValidationError):
            ExtractedMemory(text="   ")
        with self.assertRaises(MemoryValidationError):
            ExtractedMemory(text="x", importance=float("nan"))
        with self.assertRaises(MemoryValidationError):
            ExtractedMemory(text="x", category=123)  # type: ignore[arg-type]

    def test_extractor_and_store_errors_fail_open_to_next_turn(self) -> None:
        ids = iter([f"id-{i}" for i in range(10)])
        self.real.close()
        self.real = SqliteMemoryStore(
            self.db_path, clock=_utc, id_factory=lambda: next(ids)
        )
        self.store = FakeStore(self.real)

        class FlakyExtractor:
            def __init__(self) -> None:
                self.calls = 0
                self.lock = threading.Lock()

            def extract(self, turn: MemoryTurnEvent):
                with self.lock:
                    self.calls += 1
                    n = self.calls
                if n == 1:
                    raise RuntimeError("boom")
                if turn.turn_id == "bad-item":
                    return [
                        ExtractedMemory(text="ok-one"),
                        object(),  # illegal
                        ExtractedMemory(text="ok-two"),
                    ]
                return [ExtractedMemory(text=f"from-{turn.turn_id}")]

        flaky = FlakyExtractor()
        service = MemoryIngestionService(self.store, flaky, queue_capacity=8)
        self.services.append(service)
        service.start()
        self.assertTrue(service.submit(_success("boom-turn")))
        self.assertTrue(service.submit(_success("bad-item")))
        self.assertTrue(service.submit(_success("good-turn")))

        def _done() -> bool:
            return len(self.real.list_memories()) >= 3

        finished = threading.Event()

        def poll() -> None:
            while not finished.wait(0.01):
                if _done():
                    finished.set()
                    return

        t = threading.Thread(target=poll, daemon=True)
        t.start()
        self.assertTrue(finished.wait(timeout=3.0))
        t.join(timeout=1.0)
        service.stop(drain=True, timeout=2.0)
        texts = {m.text for m in self.real.list_memories()}
        self.assertIn("ok-one", texts)
        self.assertIn("ok-two", texts)
        self.assertIn("from-good-turn", texts)
        self.assertEqual(flaky.calls, 3)

    def test_exact_dedupe_keeps_first_source(self) -> None:
        ids = iter(["d1", "d2"])
        self.real.close()
        self.real = SqliteMemoryStore(
            self.db_path, clock=_utc, id_factory=lambda: next(ids)
        )
        self.store = FakeStore(self.real)
        self.real.add(
            NewMemory(
                text="喜欢吃  拉面",
                source=MemorySource.USER_CONFIRMED,
            )
        )
        extractor = RecordingExtractor(
            [ExtractedMemory(text="  喜欢吃 拉面  ", confidence=0.3)]
        )
        service = self._make(extractor)
        service.start()
        self.assertTrue(service.submit(_success("dup")))
        self._wait_processed(extractor, 1)
        service.stop(drain=True, timeout=2.0)
        rows = self.real.list_memories()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source, MemorySource.USER_CONFIRMED)
        self.assertEqual(rows[0].text, "喜欢吃  拉面")

    def test_queue_full_rejects_without_blocking_submitter(self) -> None:
        gate = threading.Event()
        started = threading.Event()
        extractor = RecordingExtractor(
            [ExtractedMemory(text="blocked")],
            gate=gate,
            started=started,
        )
        service = self._make(extractor, capacity=1)
        service.start()
        self.assertTrue(service.submit(_success("hold")))
        self.assertTrue(started.wait(timeout=2.0))
        # worker busy; fill the single queue slot
        self.assertTrue(service.submit(_success("queued")))
        # next must reject immediately
        blocked = threading.Event()
        result_box: List[bool] = []

        def submitter() -> None:
            result_box.append(service.submit(_success("overflow")))
            blocked.set()

        thread = threading.Thread(target=submitter)
        thread.start()
        self.assertTrue(blocked.wait(timeout=1.0), "submit blocked on full queue")
        thread.join(timeout=1.0)
        self.assertEqual(result_box, [False])
        self.assertGreaterEqual(service.rejected_full_count, 1)
        gate.set()
        service.stop(drain=True, timeout=2.0)

    def test_drain_true_full_queue_does_not_drop_accepted_turns(self) -> None:
        """容量 1 + 阻塞首个 extract 时，drain=True 不得偷删第二个已接受任务。"""
        gate = threading.Event()
        started = threading.Event()

        class PerTurnExtractor:
            def __init__(self) -> None:
                self.calls: List[MemoryTurnEvent] = []
                self.lock = threading.Lock()

            def extract(self, turn: MemoryTurnEvent):
                with self.lock:
                    self.calls.append(turn)
                if turn.turn_id == "first":
                    started.set()
                    gate.wait(timeout=5.0)
                return [ExtractedMemory(text=f"body-{turn.turn_id}")]

        extractor = PerTurnExtractor()
        ids = iter([f"d{i}" for i in range(10)])
        self.real.close()
        self.real = SqliteMemoryStore(
            self.db_path, clock=_utc, id_factory=lambda: next(ids)
        )
        self.store = FakeStore(self.real)
        service = MemoryIngestionService(
            self.store, extractor, queue_capacity=1, max_extractions_per_turn=8
        )
        self.services.append(service)
        service.start()
        self.assertTrue(service.submit(_success("first")))
        self.assertTrue(started.wait(timeout=2.0))
        self.assertTrue(service.submit(_success("second")))

        stop_done = threading.Event()
        status_box: List[IngestionStopStatus] = []

        def stopper() -> None:
            try:
                status_box.append(service.stop(drain=True, timeout=3.0))
            finally:
                stop_done.set()

        stop_thread = threading.Thread(target=stopper)
        stop_thread.start()
        # 等待 stop 进入 closing 后再释放首个 extract（无 sleep 主断言）
        closing_seen = threading.Event()

        def watch_closing() -> None:
            while not stop_done.is_set():
                if service._closing:  # noqa: SLF001
                    closing_seen.set()
                    return
                closing_seen.wait(0.01)

        watcher = threading.Thread(target=watch_closing)
        watcher.start()
        try:
            self.assertTrue(closing_seen.wait(timeout=2.0))
            gate.set()
            self.assertTrue(stop_done.wait(timeout=3.0))
        finally:
            gate.set()
            watcher.join(timeout=1.0)
            stop_thread.join(timeout=1.0)

        self.assertTrue(status_box)
        self.assertTrue(status_box[0].drained)
        self.assertEqual(status_box[0].pending, 0)
        self.assertEqual(status_box[0].discarded, 0)
        turn_ids = [c.turn_id for c in extractor.calls]
        self.assertEqual(turn_ids, ["first", "second"])
        texts = {m.text for m in self.real.list_memories()}
        self.assertEqual(texts, {"body-first", "body-second"})

    def test_submit_cannot_return_true_after_stop_boundary(self) -> None:
        """状态检查之后、入队之前交错 stop 时，不得出现 True 却落在停止边界之后。"""
        extractor = RecordingExtractor([ExtractedMemory(text="race")])
        service = self._make(extractor, capacity=4)
        service.start()
        entered = threading.Event()
        release = threading.Event()
        service._test_delay_after_accept = lambda: (entered.set(), release.wait(5.0))  # noqa: SLF001
        result_box: List[Optional[bool]] = [None]
        submit_done = threading.Event()

        def submitter() -> None:
            try:
                result_box[0] = service.submit(_success("late"))
            finally:
                submit_done.set()

        submit_thread = threading.Thread(target=submitter)
        submit_thread.start()
        try:
            self.assertTrue(entered.wait(timeout=2.0))
            status = service.stop(drain=True, timeout=2.0)
            release.set()
            self.assertTrue(submit_done.wait(timeout=2.0))
        finally:
            release.set()
            submit_thread.join(timeout=2.0)
            service._test_delay_after_accept = None  # noqa: SLF001

        self.assertIsNotNone(result_box[0])
        if result_box[0]:
            # 若返回 True，任务必须已在边界前入队并由 drain 处理
            self.assertTrue(status.drained)
            self.assertEqual([c.turn_id for c in extractor.calls], ["late"])
        else:
            self.assertEqual(extractor.calls, [])
            self.assertEqual(status.pending, 0)

    def test_baseexception_abort_does_not_lie_about_drain(self) -> None:
        for exc_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exc=exc_type.__name__):
                gate = threading.Event()
                started = threading.Event()
                calls: List[str] = []
                exc_cls = exc_type

                class AbortExtractor:
                    def extract(self, turn: MemoryTurnEvent):
                        calls.append(turn.turn_id)
                        if turn.turn_id == "abort":
                            started.set()
                            gate.wait(timeout=5.0)
                            raise exc_cls()
                        return [ExtractedMemory(text=f"saved-{turn.turn_id}")]

                ids = iter([f"{exc_type.__name__}-{i}" for i in range(10)])
                try:
                    self.real.close()
                except Exception:
                    pass
                path = Path(self._tmp.name) / f"mem-{exc_type.__name__}.sqlite3"
                self.real = SqliteMemoryStore(
                    path, clock=_utc, id_factory=lambda: next(ids)
                )
                self.store = FakeStore(self.real)
                extractor = AbortExtractor()
                service = MemoryIngestionService(
                    self.store, extractor, queue_capacity=4
                )
                self.services.append(service)
                service.start()
                self.assertTrue(service.submit(_success("abort")))
                self.assertTrue(started.wait(timeout=2.0))
                self.assertTrue(service.submit(_success("queued")))
                gate.set()
                status = service.stop(drain=True, timeout=3.0)
                self.assertFalse(status.worker_alive)
                # 不得谎报 drained；替代 worker 应排空后续已接受任务
                self.assertTrue(
                    status.drained,
                    f"pending={status.pending} calls={calls}",
                )
                self.assertEqual(status.pending, 0)
                self.assertIn("queued", calls)
                texts = {m.text for m in self.real.list_memories()}
                self.assertIn("saved-queued", texts)

    def test_stop_timeout_rejects_non_finite(self) -> None:
        service = self._make(RecordingExtractor())
        for bad in (float("nan"), float("inf"), float("-inf"), True):
            with self.subTest(bad=bad):
                with self.assertRaises(MemoryValidationError):
                    service.stop(timeout=bad)  # type: ignore[arg-type]

    def test_start_stop_close_idempotent_drain_and_timeout(self) -> None:
        gate = threading.Event()
        started = threading.Event()
        extractor = RecordingExtractor(
            [ExtractedMemory(text="slow")],
            gate=gate,
            started=started,
        )
        service = self._make(extractor, capacity=2)
        service.start()
        service.start()  # idempotent
        self.assertTrue(service.submit(_success("s1")))
        self.assertTrue(started.wait(timeout=2.0))
        # timeout while worker blocked
        status = service.stop(drain=True, timeout=0.05)
        self.assertIsInstance(status, IngestionStopStatus)
        self.assertTrue(status.timed_out)
        self.assertTrue(status.worker_alive)
        gate.set()
        # 再 stop 回收
        status2 = service.stop(drain=True, timeout=2.0)
        self.assertFalse(status2.worker_alive)
        status3 = service.stop(drain=False, timeout=1.0)
        self.assertFalse(status3.worker_alive)

        extractor2 = RecordingExtractor([ExtractedMemory(text="x")])
        service2 = self._make(extractor2, capacity=2)
        service2.start()
        self.assertTrue(service2.submit(_success("keep")))
        self.assertTrue(service2.submit(_success("drop-me")))
        # Immediately discard without processing both necessarily
        status_drop = service2.stop(drain=False, timeout=2.0)
        self.assertFalse(status_drop.worker_alive)
        self.assertGreaterEqual(status_drop.discarded, 0)

        owned = self._make(RecordingExtractor(), owns_store=True)
        owned.start()
        owned.close(drain=False, timeout=2.0)
        owned.close(drain=False, timeout=1.0)
        self.assertEqual(self.store.close_calls, 1)

    def test_owned_store_not_closed_while_worker_alive_on_timeout(self) -> None:
        gate = threading.Event()
        started = threading.Event()
        extractor = RecordingExtractor(
            [ExtractedMemory(text="owned-saved")],
            gate=gate,
            started=started,
        )
        ids = iter(["own-1", "own-2"])
        self.real.close()
        self.real = SqliteMemoryStore(
            self.db_path, clock=_utc, id_factory=lambda: next(ids)
        )
        self.store = FakeStore(self.real)
        service = MemoryIngestionService(
            self.store,
            extractor,
            queue_capacity=2,
            owns_store=True,
        )
        self.services.append(service)
        service.start()
        self.assertTrue(service.submit(_success("owned-turn")))
        self.assertTrue(started.wait(timeout=2.0))

        first = None
        second = None
        try:
            first = service.close(drain=True, timeout=0.0)
            self.assertTrue(first.worker_alive)
            self.assertFalse(first.drained)
            self.assertTrue(first.timed_out)
            self.assertGreaterEqual(first.pending, 1)
            self.assertEqual(self.store.close_calls, 0)
            self.assertFalse(self.store.closed)

            gate.set()
            # 等待 worker 完成写入并退出（store 仍由 owns_store 保留打开）
            finished = threading.Event()

            def wait_dead() -> None:
                while service._worker is not None and service._worker.is_alive():  # noqa: SLF001
                    finished.wait(0.01)
                finished.set()

            waiter = threading.Thread(target=wait_dead)
            waiter.start()
            try:
                self.assertTrue(finished.wait(timeout=2.0))
            finally:
                waiter.join(timeout=1.0)

            texts = {m.text for m in self.real.list_memories()}
            self.assertIn("owned-saved", texts)
            self.assertEqual(self.store.close_calls, 0)
            self.assertFalse(self.store.add_after_close)

            second = service.close(drain=True, timeout=2.0)
            self.assertFalse(second.worker_alive)
            self.assertTrue(second.drained)
            self.assertEqual(second.pending, 0)
            self.assertEqual(self.store.close_calls, 1)
            self.assertFalse(self.store.add_after_close)
        finally:
            gate.set()
            try:
                service.close(drain=False, timeout=1.0)
            except Exception:
                pass

    def test_keyboard_interrupt_from_extractor_recovers_worker(self) -> None:
        class KIExtractor:
            def __init__(self) -> None:
                self.calls = 0

            def extract(self, turn: MemoryTurnEvent):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt()
                return [ExtractedMemory(text="after-ki")]

        ki = KIExtractor()
        service = MemoryIngestionService(self.store, ki, queue_capacity=4)
        self.services.append(service)
        service.start()
        self.assertTrue(service.submit(_success("ki-turn")))
        # worker should exit due to KI; stop must still be bounded
        status = service.stop(drain=True, timeout=2.0)
        self.assertFalse(status.worker_alive)
        # restart and process next
        extractor2 = RecordingExtractor([ExtractedMemory(text="recovered")])
        service2 = self._make(extractor2)
        service2.start()
        self.assertTrue(service2.submit(_success("next")))
        self._wait_processed(extractor2, 1)
        service2.stop(drain=True, timeout=2.0)
        texts = {m.text for m in self.real.list_memories()}
        self.assertIn("recovered", texts)


class LexicalRecallAndFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "memory.sqlite3"
        self.ids = iter([f"m{i}" for i in range(100)])
        self.clock_values = [_utc(12, 0, i) for i in range(60)]

        def clock() -> datetime:
            if len(self.clock_values) == 1:
                return self.clock_values[0]
            return self.clock_values.pop(0)

        self.store = SqliteMemoryStore(
            self.db_path,
            clock=clock,
            id_factory=lambda: next(self.ids),
        )

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_chinese_english_recall_and_empty_unrelated(self) -> None:
        self.store.add(
            NewMemory(text="用户喜欢吃拉面", category=MemoryCategory.PREFERENCE, importance=0.9)
        )
        self.store.add(
            NewMemory(text="User likes ramen noodles", category=MemoryCategory.PREFERENCE, importance=0.5)
        )
        self.store.add(
            NewMemory(text="完全无关的天气记录", category=MemoryCategory.EVENT, importance=1.0)
        )
        recall = LexicalMemoryRecall(self.store)
        zh = recall.recall("今天想吃拉面吗", limit=5)
        self.assertTrue(any("拉面" in h.record.text for h in zh.hits))
        self.assertFalse(any("天气" in h.record.text for h in zh.hits))

        en = recall.recall("I want ramen please", limit=5)
        self.assertTrue(any("ramen" in h.record.text.lower() for h in en.hits))

        empty = recall.recall("量子纠缠校准参数XYZ", limit=5)
        self.assertTrue(empty.empty)

    def test_deterministic_sort_candidate_cap_and_newest_scan(self) -> None:
        # same lexical score on "猫"; higher importance / newer should win ties by rules
        self.store.add(NewMemory(text="猫", importance=0.2))
        self.store.add(NewMemory(text="猫", importance=0.9))  # exact dedupe → only first
        # use distinct texts with same token overlap
        self.store.clear()
        self.ids = iter([f"s{i}" for i in range(20)])
        self.store.close()
        self.clock_values = [_utc(12, 0, i) for i in range(20)]

        def clock() -> datetime:
            if len(self.clock_values) == 1:
                return self.clock_values[0]
            return self.clock_values.pop(0)

        self.store = SqliteMemoryStore(
            self.db_path,
            clock=clock,
            id_factory=lambda: next(self.ids),
        )
        self.store.add(NewMemory(text="养了一只猫", importance=0.2))
        self.store.add(NewMemory(text="家里有猫", importance=0.9))
        self.store.add(NewMemory(text="猫很可爱", importance=0.9))
        recall = LexicalMemoryRecall(self.store)
        result = recall.recall("猫", limit=2, candidate_limit=10)
        self.assertEqual(len(result.hits), 2)
        # both top have importance 0.9; newer updated_at first among them
        self.assertEqual(result.hits[0].record.importance, 0.9)
        self.assertGreaterEqual(
            result.hits[0].record.updated_at,
            result.hits[1].record.updated_at,
        )
        with self.assertRaises(MemoryValidationError):
            recall.recall("猫", limit=0)
        with self.assertRaises(MemoryValidationError):
            recall.recall("猫", candidate_limit=0)

        # candidate newest_first: plant many then ensure cap works
        for i in range(5):
            self.store.add(NewMemory(text=f"填充{i}无关"))
        capped = recall.recall("猫", limit=5, candidate_limit=2)
        self.assertLessEqual(capped.candidate_count, 2)
        self.assertTrue(capped.truncated_candidates)

    def test_truncated_candidates_only_when_more_remain(self) -> None:
        recall = LexicalMemoryRecall(self.store)
        self.store.add(NewMemory(text="唯一一条猫"))
        one = recall.recall("猫", limit=5, candidate_limit=2)
        self.assertEqual(one.candidate_count, 1)
        self.assertFalse(one.truncated_candidates)

        self.store.add(NewMemory(text="第二条也有猫"))
        exact = recall.recall("猫", limit=5, candidate_limit=2)
        self.assertEqual(exact.candidate_count, 2)
        self.assertFalse(exact.truncated_candidates)

        self.store.add(NewMemory(text="第三条仍有猫"))
        over = recall.recall("猫", limit=5, candidate_limit=2)
        self.assertEqual(over.candidate_count, 2)
        self.assertTrue(over.truncated_candidates)

        # 最大 candidate_limit：有界探测 limit+1，candidate_count 不含探测项
        self.assertEqual(MAX_CANDIDATE_LIMIT + 1, _LIST_HAS_MORE_PROBE_LIMIT)

        class ProbeStore:
            def __init__(self) -> None:
                self.last_limit: Optional[int] = None
                now = _utc()
                self._rows = tuple(
                    MemoryRecord(
                        id=f"p{i}",
                        text=f"猫候选{i}",
                        category=MemoryCategory.OTHER,
                        source=MemorySource.MODEL_INFERRED,
                        importance=0.5,
                        confidence=0.5,
                        created_at=now,
                        updated_at=now,
                        dedupe_key=normalize_dedupe_key(f"猫候选{i}"),
                    )
                    for i in range(MAX_CANDIDATE_LIMIT + 1)
                )

            def list_memories(
                self, *, limit: int = 50, offset: int = 0, newest_first: bool = False
            ):
                self.last_limit = limit
                return self._rows[offset : offset + limit]

        probe = ProbeStore()
        at_max = LexicalMemoryRecall(probe).recall(
            "猫", limit=5, candidate_limit=MAX_CANDIDATE_LIMIT
        )
        self.assertEqual(probe.last_limit, MAX_CANDIDATE_LIMIT + 1)
        self.assertEqual(at_max.candidate_count, MAX_CANDIDATE_LIMIT)
        self.assertTrue(at_max.truncated_candidates)

    def test_formatter_budget_escape_and_truncation(self) -> None:
        records = [
            self.store.add(
                NewMemory(
                    text='含"引号"与\n换行及伪分隔||还有🙂',
                    category=MemoryCategory.OTHER,
                )
            ).record
        ]
        ctx = format_memory_context(records, max_entries=3, max_chars=500)
        self.assertIn(CONTEXT_HEADER, ctx.rendered)
        self.assertIn("subject", ctx.rendered)
        self.assertIn('"subject":"user"', ctx.rendered)
        self.assertIn('"fact":', ctx.rendered)
        self.assertIn("数据边界", ctx.rendered)
        self.assertNotIn("\n换行", ctx.rendered.split("\n", 1)[-1])  # escaped in JSON
        self.assertIn("\\n", ctx.rendered)
        self.assertIn("🙂", ctx.rendered)
        self.assertLessEqual(len(ctx.rendered), 500)
        self.assertEqual(ctx.chars_used, len(ctx.rendered))
        # 不得把 provenance 伪装进注入块
        self.assertNotIn("model_inferred", ctx.rendered)
        self.assertNotIn("user_confirmed", ctx.rendered)

        long_rec = self.store.add(
            NewMemory(text="长" * 200, category=MemoryCategory.EVENT)
        ).record
        tiny = format_memory_context([long_rec], max_entries=1, max_chars=80)
        self.assertLessEqual(len(tiny.rendered), 80)
        self.assertTrue(tiny.truncated_entry or tiny.empty)
        if not tiny.empty:
            self.assertIn("…", tiny.rendered)

        multi = [
            self.store.add(NewMemory(text=f"条目{i}内容够长一些")).record
            for i in range(5)
        ]
        limited = format_memory_context(multi, max_entries=2, max_chars=400)
        self.assertLessEqual(limited.entries_used, 2)
        self.assertLessEqual(len(limited.rendered), 400)
        self.assertEqual(format_memory_context([], max_entries=3, max_chars=100).rendered, "")

    def test_recall_store_failure_fail_open_without_logging_body(self) -> None:
        self.store.add(NewMemory(text="秘密正文不应出现在日志"))
        fake = FakeStore(self.store)
        fake.fail_list_with = MemoryStoreUnavailableError("down")
        recall = LexicalMemoryRecall(fake)
        with self.assertLogs(recall._logger, level="ERROR") as captured:  # noqa: SLF001
            result = recall.recall("秘密正文不应出现在日志")
        self.assertTrue(result.empty)
        self.assertEqual(result.error_type, "MemoryStoreUnavailableError")
        joined = "\n".join(captured.output)
        self.assertNotIn("秘密正文不应出现在日志", joined)

        fake.fail_list_with = MemoryDataCorruptError("bad row")
        result2 = recall.recall("拉面")
        self.assertEqual(result2.error_type, "MemoryDataCorruptError")

        query = MemoryQueryService(fake)
        ctx = query.build_context("拉面")
        self.assertIsInstance(ctx, MemoryContext)
        self.assertEqual(ctx.rendered, "")
        self.assertEqual(ctx.error_type, "MemoryDataCorruptError")

    def test_lexical_tokens_stable(self) -> None:
        self.assertIn("w:hello", lexical_tokens("  Hello "))
        self.assertIn("c:拉", lexical_tokens("拉面"))
        self.assertIn("cb:拉面", lexical_tokens("拉面"))


if __name__ == "__main__":
    unittest.main()
