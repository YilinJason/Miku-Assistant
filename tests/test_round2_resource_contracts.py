"""Round 2 评审四项：scandir 续扫、admission 互斥、Timer 幂等、close 可重试。"""
from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator, List, Optional
from unittest.mock import MagicMock, patch

from src.core.file_cleaner import CleanupRule, FileCleaner
from src.core.safety import SafetyPolicy
from src.memory.admission import ForegroundAwareAdmissionGate
from src.memory.errors import MemoryValidationError
from src.memory.ingestion import MemoryIngestionService
from src.memory.models import MemorySource, NewMemory
from src.memory.query import MemoryQueryService
from src.memory.runtime import MemoryRuntime, MemorySettings
from src.memory.service_models import IngestionStopStatus
from src.memory.sqlite_store import SqliteMemoryStore


class _FakeDirEntry:
    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.name = path.name
        self._path = path

    def is_file(self, follow_symlinks: bool = True) -> bool:
        return self._path.is_file()

    def stat(self, follow_symlinks: bool = True) -> os.stat_result:
        return self._path.stat()


class _StreamingScandir:
    """流式 scandir；超过预算再 next 则抛错；支持 close。"""

    def __init__(self, entries: List[Path], *, budget: Optional[int] = None) -> None:
        self._entries = list(entries)
        self._index = 0
        self._budget = budget
        self.taken = 0
        self.closed = False

    def __iter__(self) -> Iterator[_FakeDirEntry]:
        return self

    def __next__(self) -> _FakeDirEntry:
        if self._index >= len(self._entries):
            raise StopIteration
        self.taken += 1
        if self._budget is not None and self.taken > self._budget:
            raise RuntimeError("enumerated past scan budget")
        item = self._entries[self._index]
        self._index += 1
        return _FakeDirEntry(item)

    def __enter__(self) -> "_StreamingScandir":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True


class FileCleanerRound2Tests(unittest.TestCase):
    def test_scandir_budget_and_listdir_forbidden(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ordered: List[Path] = []
            for i in range(5):
                p = root / f"noise_{i}.txt"
                p.write_text("x", encoding="utf-8")
                ordered.append(p)
            target = root / "miku_old.wav"
            target.write_text("x", encoding="utf-8")
            os.utime(target, (1, 1))
            ordered.append(target)

            last_ctx: List[_StreamingScandir] = []

            def fake_scandir(path: Any) -> _StreamingScandir:
                ctx = _StreamingScandir(ordered, budget=2)
                last_ctx.append(ctx)
                return ctx

            def boom_listdir(*_a: Any, **_k: Any) -> list:
                raise RuntimeError("listdir must not be called")

            policy = SafetyPolicy()
            policy.register_file_delete_dir(root)
            cleaner = FileCleaner(
                rules=[
                    CleanupRule(
                        name="t",
                        directory=root,
                        patterns=["miku_*.wav"],
                        max_age_seconds=0.0,
                    )
                ],
                safety_policy=policy,
                max_scan_files=2,
                max_delete_files=10,
                scandir_fn=fake_scandir,
            )
            with patch("os.listdir", side_effect=boom_listdir), patch(
                "pathlib.Path.iterdir",
                side_effect=RuntimeError("iterdir must not be called"),
            ):
                deleted = cleaner.cleanup_now()
            self.assertEqual(deleted, 0)
            self.assertEqual(last_ctx[0].taken, 2)
            # 预算耗尽但未穷尽：保留活动 sweep，不关闭句柄
            self.assertFalse(last_ctx[0].closed)
            self.assertTrue(target.exists())
            cleaner.stop_auto_cleanup()
            self.assertTrue(last_ctx[0].closed)

    def test_multi_round_resume_reaches_starved_target(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 稳定顺序：先噪音后目标（用注入 scandir 固定顺序）
            n1 = root / "noise-1.txt"
            n2 = root / "noise-2.txt"
            target = root / "miku_old.wav"
            for p in (n1, n2, target):
                p.write_text("x", encoding="utf-8")
                os.utime(p, (1, 1))
            ordered = [n1, n2, target]
            calls = {"n": 0}

            def fake_scandir(path: Any) -> _StreamingScandir:
                calls["n"] += 1
                return _StreamingScandir(ordered)

            policy = SafetyPolicy()
            policy.register_file_delete_dir(root)
            cleaner = FileCleaner(
                rules=[
                    CleanupRule(
                        name="t",
                        directory=root,
                        patterns=["miku_*.wav"],
                        max_age_seconds=0.0,
                    )
                ],
                safety_policy=policy,
                max_scan_files=2,
                max_delete_files=10,
                scandir_fn=fake_scandir,
            )
            cleaner.cleanup_now()
            self.assertTrue(target.exists())
            cleaner.cleanup_now()
            self.assertFalse(target.exists())
            # 活动 sweep 复用同一 scandir，只打开一次
            self.assertEqual(calls["n"], 1)


class AdmissionMutualExclusionTests(unittest.TestCase):
    def test_extract_blocked_during_maintenance(self) -> None:
        gate = ForegroundAwareAdmissionGate()
        self.assertTrue(gate.try_acquire_maintenance())
        self.assertFalse(gate.try_acquire_extract())
        gate.release_maintenance()
        self.assertTrue(gate.try_acquire_extract())
        gate.release_extract()

    def test_maintenance_blocked_during_extract(self) -> None:
        gate = ForegroundAwareAdmissionGate()
        self.assertTrue(gate.try_acquire_extract())
        self.assertFalse(gate.try_acquire_maintenance())
        gate.release_extract()
        self.assertTrue(gate.try_acquire_maintenance())
        gate.release_maintenance()

    def test_foreground_waits_until_maintenance_releases(self) -> None:
        gate = ForegroundAwareAdmissionGate()
        self.assertTrue(gate.try_acquire_maintenance())
        entered = threading.Event()
        done = threading.Event()
        order: List[str] = []

        def fg() -> None:
            entered.set()
            gate.begin_foreground()
            order.append("fg")
            done.set()

        t = threading.Thread(target=fg)
        t.start()
        self.assertTrue(entered.wait(timeout=2.0))
        self.assertTrue(gate.foreground_pending_count >= 1)
        self.assertFalse(gate.try_acquire_maintenance())  # pending 挡新维护
        self.assertEqual(gate.foreground_count, 0)
        gate.release_maintenance()
        self.assertTrue(done.wait(timeout=2.0))
        t.join(timeout=2.0)
        self.assertEqual(order, ["fg"])
        self.assertEqual(gate.foreground_count, 1)
        self.assertEqual(gate.maintenance_count, 0)
        gate.end_foreground()

    def test_spurious_notify_does_not_enter_during_maintenance(self) -> None:
        gate = ForegroundAwareAdmissionGate()
        self.assertTrue(gate.try_acquire_maintenance())
        woke = threading.Event()
        finished = threading.Event()

        def fg() -> None:
            woke.set()
            gate.begin_foreground()
            finished.set()

        t = threading.Thread(target=fg)
        t.start()
        self.assertTrue(woke.wait(timeout=2.0))
        # 虚假唤醒：notify 但维护仍持有
        with gate._cond:
            gate._cond.notify_all()
        self.assertFalse(finished.wait(timeout=0.2))
        self.assertEqual(gate.foreground_count, 0)
        gate.release_maintenance()
        self.assertTrue(finished.wait(timeout=2.0))
        t.join(timeout=2.0)
        gate.end_foreground()

    def test_multi_foreground_waiters(self) -> None:
        gate = ForegroundAwareAdmissionGate()
        self.assertTrue(gate.try_acquire_maintenance())
        barrier = threading.Barrier(3)
        results: List[int] = []
        lock = threading.Lock()

        def fg() -> None:
            barrier.wait(timeout=2.0)
            gate.begin_foreground()
            with lock:
                results.append(gate.foreground_count)
            gate.end_foreground()

        threads = [threading.Thread(target=fg) for _ in range(2)]
        for t in threads:
            t.start()
        barrier.wait(timeout=2.0)
        gate.release_maintenance()
        for t in threads:
            t.join(timeout=2.0)
        self.assertEqual(len(results), 2)
        self.assertEqual(gate.foreground_count, 0)


class _ControllableTimer:
    """可注入的 Timer：不真实 sleep，可手动 fire。"""

    created: List["_ControllableTimer"] = []

    def __init__(self, interval: float, function: Any, args: Any = None, kwargs: Any = None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False
        self._cancelled = False
        self.started = False
        self.fired = False
        _ControllableTimer.created.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self._cancelled = True

    def join(self, timeout: Optional[float] = None) -> None:
        return None

    def fire(self) -> None:
        if self._cancelled:
            return
        self.fired = True
        self.function(*self.args, **self.kwargs)


def _retention_settings(db_path: Path, **overrides: Any) -> MemorySettings:
    base = dict(
        enabled=True,
        db_path=db_path,
        max_entries=8,
        max_chars=1200,
        candidate_limit=200,
        queue_capacity=4,
        max_extractions_per_turn=4,
        close_drain=True,
        close_timeout=1.0,
        extractor_base_url="http://localhost:11434",
        extractor_model="m",
        extractor_timeout=5.0,
        extractor_keep_alive="1m",
        retention_enabled=True,
        retention_max_records=100,
        retention_max_age_days=365.0,
        retention_maintenance_interval_hours=1.0,
        retention_preserve_user_confirmed=True,
        retention_batch_limit=8,
    )
    base.update(overrides)
    return MemorySettings(**base)


class MaintenanceTimerIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        _ControllableTimer.created.clear()

    def test_duplicate_start_single_timer(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")
            ingestion = MemoryIngestionService(
                store, MagicMock(), queue_capacity=2, owns_store=False
            )
            runtime = MemoryRuntime(
                store,
                MemoryQueryService(store),
                ingestion,
                settings=_retention_settings(Path(tmp) / "m.sqlite3"),
                owns_store=False,
                admission_gate=ForegroundAwareAdmissionGate(),
            )
            runtime._timer_factory = _ControllableTimer  # type: ignore[method-assign]
            runtime.start()
            runtime.start()
            self.assertEqual(len(_ControllableTimer.created), 1)
            self.assertIs(runtime._maintenance_timer, _ControllableTimer.created[0])
            runtime.close(drain=False, timeout=1.0)
            self.assertTrue(_ControllableTimer.created[0]._cancelled)
            self.assertIsNone(runtime._maintenance_timer)
            store.close()

    def test_stale_callback_does_not_reschedule_after_close(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")
            ingestion = MemoryIngestionService(
                store, MagicMock(), queue_capacity=2, owns_store=False
            )
            runtime = MemoryRuntime(
                store,
                MemoryQueryService(store),
                ingestion,
                settings=_retention_settings(Path(tmp) / "m.sqlite3"),
                owns_store=False,
                admission_gate=ForegroundAwareAdmissionGate(),
            )
            runtime._timer_factory = _ControllableTimer  # type: ignore[method-assign]
            runtime.start()
            first = _ControllableTimer.created[0]
            gen = first.args[0]
            runtime.close(drain=False, timeout=1.0)
            before = len(_ControllableTimer.created)
            first.fire()  # 旧 generation 回调
            self.assertEqual(len(_ControllableTimer.created), before)
            store.close()


class CloseRetryTests(unittest.TestCase):
    def test_ingestion_failure_allows_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            store_closes = {"n": 0}

            class TrackingStore:
                def close(self) -> None:
                    store_closes["n"] += 1

            class FlakyIngestion:
                def __init__(self) -> None:
                    self.calls = 0
                    self.started = False

                def start(self) -> None:
                    self.started = True

                def is_idle(self) -> bool:
                    return True

                def wait_until_idle(self, timeout: float) -> bool:
                    return True

                def close(self, *, drain: bool = False, timeout: float = 5.0) -> IngestionStopStatus:
                    self.calls += 1
                    if self.calls == 1:
                        raise RuntimeError("ingestion-boom")
                    return IngestionStopStatus(
                        discarded=0,
                        timed_out=False,
                        worker_alive=False,
                        drained=True,
                        pending=0,
                    )

            ingestion = FlakyIngestion()
            runtime = MemoryRuntime(
                TrackingStore(),  # type: ignore[arg-type]
                MagicMock(),
                ingestion,  # type: ignore[arg-type]
                settings=_retention_settings(Path(tmp) / "x.sqlite3", retention_enabled=False),
                owns_store=True,
            )
            with self.assertRaises(RuntimeError):
                runtime.close(drain=False, timeout=1.0)
            self.assertFalse(runtime.is_closed)
            self.assertEqual(ingestion.calls, 1)
            self.assertEqual(store_closes["n"], 0)
            status = runtime.close(drain=False, timeout=1.0)
            self.assertTrue(runtime.is_closed)
            self.assertEqual(ingestion.calls, 2)
            self.assertEqual(store_closes["n"], 1)
            self.assertTrue(status.drained)
            # 重复成功 close 返回真实状态
            again = runtime.close(drain=False, timeout=1.0)
            self.assertTrue(again.drained)
            self.assertEqual(ingestion.calls, 2)
            self.assertEqual(store_closes["n"], 1)

    def test_store_failure_retries_without_reingesting(self) -> None:
        class FlakyStore:
            def __init__(self) -> None:
                self.calls = 0

            def close(self) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("store-boom")

        class OkIngestion:
            def __init__(self) -> None:
                self.calls = 0

            def start(self) -> None:
                return None

            def is_idle(self) -> bool:
                return True

            def close(self, *, drain: bool = False, timeout: float = 5.0) -> IngestionStopStatus:
                self.calls += 1
                return IngestionStopStatus(
                    discarded=0,
                    timed_out=False,
                    worker_alive=False,
                    drained=True,
                    pending=0,
                )

        store = FlakyStore()
        ingestion = OkIngestion()
        runtime = MemoryRuntime(
            store,  # type: ignore[arg-type]
            MagicMock(),
            ingestion,  # type: ignore[arg-type]
            settings=_retention_settings(Path("x"), retention_enabled=False),
            owns_store=True,
        )
        with self.assertRaises(RuntimeError):
            runtime.close(drain=False, timeout=1.0)
        self.assertFalse(runtime.is_closed)
        self.assertEqual(ingestion.calls, 1)
        self.assertEqual(store.calls, 1)
        runtime.close(drain=False, timeout=1.0)
        self.assertTrue(runtime.is_closed)
        self.assertEqual(ingestion.calls, 1)
        self.assertEqual(store.calls, 2)

    def test_keyboard_interrupt_retryable(self) -> None:
        class KIIngestion:
            def __init__(self) -> None:
                self.calls = 0

            def start(self) -> None:
                return None

            def close(self, *, drain: bool = False, timeout: float = 5.0) -> IngestionStopStatus:
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt()
                return IngestionStopStatus(
                    discarded=0,
                    timed_out=False,
                    worker_alive=False,
                    drained=True,
                    pending=0,
                )

        class Store:
            def __init__(self) -> None:
                self.calls = 0

            def close(self) -> None:
                self.calls += 1

        ingestion = KIIngestion()
        store = Store()
        runtime = MemoryRuntime(
            store,  # type: ignore[arg-type]
            MagicMock(),
            ingestion,  # type: ignore[arg-type]
            settings=_retention_settings(Path("x"), retention_enabled=False),
            owns_store=True,
        )
        with self.assertRaises(KeyboardInterrupt):
            runtime.close(drain=False, timeout=1.0)
        self.assertFalse(runtime.is_closed)
        runtime.close(drain=False, timeout=1.0)
        self.assertTrue(runtime.is_closed)
        self.assertEqual(ingestion.calls, 2)
        self.assertEqual(store.calls, 1)

    def test_worker_alive_incomplete_not_fabricated_success(self) -> None:
        class AliveIngestion:
            def __init__(self) -> None:
                self.calls = 0

            def start(self) -> None:
                return None

            def close(self, *, drain: bool = False, timeout: float = 5.0) -> IngestionStopStatus:
                self.calls += 1
                if self.calls == 1:
                    return IngestionStopStatus(
                        discarded=0,
                        timed_out=True,
                        worker_alive=True,
                        drained=False,
                        pending=1,
                    )
                return IngestionStopStatus(
                    discarded=0,
                    timed_out=False,
                    worker_alive=False,
                    drained=True,
                    pending=0,
                )

        class Store:
            def __init__(self) -> None:
                self.calls = 0

            def close(self) -> None:
                self.calls += 1

        ingestion = AliveIngestion()
        store = Store()
        runtime = MemoryRuntime(
            store,  # type: ignore[arg-type]
            MagicMock(),
            ingestion,  # type: ignore[arg-type]
            settings=_retention_settings(Path("x"), retention_enabled=False),
            owns_store=True,
        )
        first = runtime.close(drain=True, timeout=0.1)
        self.assertTrue(first.worker_alive)
        self.assertFalse(first.drained)
        self.assertFalse(runtime.is_closed)
        self.assertEqual(store.calls, 0)
        second = runtime.close(drain=True, timeout=1.0)
        self.assertTrue(second.drained)
        self.assertTrue(runtime.is_closed)
        self.assertEqual(store.calls, 1)


if __name__ == "__main__":
    unittest.main()
