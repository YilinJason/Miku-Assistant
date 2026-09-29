"""Round 3：活动 sweep 硬预算、pending 挡 extract、并发 close 串行。"""
from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator, List
from unittest.mock import MagicMock, patch

from src.core.file_cleaner import CleanupRule, FileCleaner
from src.core.safety import SafetyPolicy
from src.memory.admission import ForegroundAwareAdmissionGate
from src.memory.runtime import MemoryRuntime, MemorySettings
from src.memory.service_models import IngestionStopStatus


class _CountingEntry:
    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.name = path.name
        self._path = path

    def is_file(self, follow_symlinks: bool = True) -> bool:
        return self._path.is_file()

    def stat(self, follow_symlinks: bool = True) -> os.stat_result:
        return self._path.stat()


class _CountingScandir:
    def __init__(self, entries: List[Path]) -> None:
        self._entries = list(entries)
        self._index = 0
        self.next_calls = 0
        self.closed = False

    def __iter__(self) -> Iterator[_CountingEntry]:
        return self

    def __next__(self) -> _CountingEntry:
        if self._index >= len(self._entries):
            raise StopIteration
        self.next_calls += 1
        item = self._entries[self._index]
        self._index += 1
        return _CountingEntry(item)

    def close(self) -> None:
        self.closed = True


def _retention_off(db: Path) -> MemorySettings:
    return MemorySettings(
        enabled=True,
        db_path=db,
        max_entries=8,
        max_chars=1200,
        candidate_limit=200,
        queue_capacity=2,
        max_extractions_per_turn=2,
        close_drain=True,
        close_timeout=1.0,
        extractor_base_url="http://localhost:11434",
        extractor_model="m",
        extractor_timeout=5.0,
        extractor_keep_alive="1m",
        retention_enabled=False,
    )


class ActiveSweepBudgetTests(unittest.TestCase):
    def test_budget_1_never_calls_next_more_than_once_per_round(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ordered: List[Path] = []
            for i in range(1000):
                p = root / f"noise_{i:04d}.txt"
                p.write_text("x", encoding="utf-8")
                ordered.append(p)
            target = root / "miku_target.wav"
            target.write_text("x", encoding="utf-8")
            os.utime(target, (1, 1))
            ordered.append(target)

            ctx_holder: List[_CountingScandir] = []

            def fake_scandir(_path: Any) -> _CountingScandir:
                ctx = _CountingScandir(ordered)
                ctx_holder.append(ctx)
                return ctx

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
                max_scan_files=1,
                max_delete_files=10,
                scandir_fn=fake_scandir,
                continue_delay_seconds=0.0,
            )
            with patch("os.listdir", side_effect=RuntimeError("no listdir")):
                for _ in range(5):
                    before = ctx_holder[0].next_calls if ctx_holder else 0
                    cleaner.cleanup_now()
                    after = ctx_holder[0].next_calls
                    self.assertEqual(after - before, 1)
            self.assertTrue(target.exists())
            # 连续批次最终到达第 N=1001 个目标
            for _ in range(1000):
                cleaner.cleanup_now()
                if not target.exists():
                    break
            self.assertFalse(target.exists())
            self.assertEqual(len(ctx_holder), 1)
            # 删除目标时本轮预算已用尽，再扫一轮触发 StopIteration 后关闭
            cleaner.cleanup_now()
            self.assertTrue(ctx_holder[0].closed)

    def test_stop_closes_active_handle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = []
            for i in range(10):
                p = root / f"miku_{i}.wav"
                p.write_text("x", encoding="utf-8")
                os.utime(p, (1, 1))
                files.append(p)
            ctx_holder: List[_CountingScandir] = []

            def fake_scandir(_path: Any) -> _CountingScandir:
                ctx = _CountingScandir(files)
                ctx_holder.append(ctx)
                return ctx

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
                scandir_fn=fake_scandir,
            )
            cleaner.cleanup_now()
            self.assertFalse(ctx_holder[0].closed)
            cleaner.stop_auto_cleanup()
            self.assertTrue(ctx_holder[0].closed)

    def test_exception_closes_active_handle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "miku_1.wav"
            target.write_text("x", encoding="utf-8")
            os.utime(target, (1, 1))
            ctx_holder: List[_CountingScandir] = []

            class BoomScandir(_CountingScandir):
                def __next__(self) -> _CountingEntry:  # type: ignore[override]
                    self.next_calls += 1
                    raise OSError("dir vanished")

            def fake_scandir(_path: Any) -> BoomScandir:
                ctx = BoomScandir([target])
                ctx_holder.append(ctx)
                return ctx

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
                max_scan_files=5,
                scandir_fn=fake_scandir,
            )
            deleted = cleaner.cleanup_now()
            self.assertEqual(deleted, 0)
            self.assertTrue(ctx_holder[0].closed)

    def test_same_display_name_rules_isolated(self) -> None:
        with TemporaryDirectory() as tmp:
            d1 = Path(tmp) / "a"
            d2 = Path(tmp) / "b"
            d1.mkdir()
            d2.mkdir()
            t1 = d1 / "miku_1.wav"
            t2 = d2 / "miku_2.wav"
            for p in (t1, t2):
                p.write_text("x", encoding="utf-8")
                os.utime(p, (1, 1))

            opens = {"a": 0, "b": 0}

            def fake_scandir(path: Any) -> _CountingScandir:
                resolved = str(Path(path).resolve())
                if resolved == str(d1.resolve()):
                    opens["a"] += 1
                    return _CountingScandir([t1])
                opens["b"] += 1
                return _CountingScandir([t2])

            policy = SafetyPolicy()
            policy.register_file_delete_dir(d1)
            policy.register_file_delete_dir(d2)
            r1 = CleanupRule(
                name="same", directory=d1, patterns=["miku_*.wav"], max_age_seconds=0.0
            )
            r2 = CleanupRule(
                name="same", directory=d2, patterns=["miku_*.wav"], max_age_seconds=0.0
            )
            cleaner = FileCleaner(
                rules=[r1, r2],
                safety_policy=policy,
                max_scan_files=10,
                scandir_fn=fake_scandir,
            )
            cleaner.cleanup_now()
            self.assertFalse(t1.exists())
            self.assertFalse(t2.exists())
            self.assertEqual(opens["a"], 1)
            self.assertEqual(opens["b"], 1)


class PendingBlocksExtractTests(unittest.TestCase):
    def test_extract_fails_while_foreground_pending_then_fg_wins(self) -> None:
        gate = ForegroundAwareAdmissionGate()
        self.assertTrue(gate.try_acquire_maintenance())
        pending_seen = threading.Event()
        fg_entered = threading.Event()
        extract_done = threading.Event()
        order: List[str] = []
        extract_during_pending: List[bool] = []
        extract_at_release: List[bool] = []
        release_barrier = threading.Barrier(2)

        def fg() -> None:
            # begin 登记 pending 后阻塞在 maintenance 上
            gate.begin_foreground()
            order.append("fg")
            fg_entered.set()
            # 保持前台直到 extract 竞态结束，避免 end 过早让 extract 误成功
            extract_done.wait(timeout=2.0)
            gate.end_foreground()

        t_fg = threading.Thread(target=fg)
        t_fg.start()
        for _ in range(200):
            if gate.foreground_pending_count > 0:
                pending_seen.set()
                break
            threading.Event().wait(0.01)
        self.assertTrue(pending_seen.wait(timeout=2.0))
        # 维护仍持有且 pending>0：extract 必须失败
        extract_during_pending.append(gate.try_acquire_extract())

        def extract_racer() -> None:
            release_barrier.wait(timeout=2.0)
            got = gate.try_acquire_extract()
            extract_at_release.append(got)
            if got:
                order.append("extract")
                gate.release_extract()
            extract_done.set()

        t_ex = threading.Thread(target=extract_racer)
        t_ex.start()
        release_barrier.wait(timeout=2.0)
        gate.release_maintenance()
        self.assertTrue(fg_entered.wait(timeout=2.0))
        t_fg.join(timeout=2.0)
        t_ex.join(timeout=2.0)
        self.assertEqual(extract_during_pending, [False])
        self.assertEqual(extract_at_release, [False])
        self.assertEqual(order, ["fg"])
        # 前台结束后 extract 可再获
        self.assertTrue(gate.try_acquire_extract())
        gate.release_extract()

    def test_pending_zeroed_after_keyboard_interrupt(self) -> None:
        gate = ForegroundAwareAdmissionGate()
        self.assertTrue(gate.try_acquire_maintenance())

        def boom_wait(*_a: Any, **_k: Any) -> bool:
            raise KeyboardInterrupt()

        with patch.object(gate._cond, "wait", side_effect=boom_wait):
            with self.assertRaises(KeyboardInterrupt):
                gate.begin_foreground()
        self.assertEqual(gate.foreground_pending_count, 0)
        self.assertEqual(gate.foreground_count, 0)
        gate.release_maintenance()
        self.assertTrue(gate.try_acquire_extract())
        gate.release_extract()


class ConcurrentCloseSerializeTests(unittest.TestCase):
    def test_concurrent_close_single_ingestion(self) -> None:
        class CountingIngestion:
            def __init__(self) -> None:
                self.calls = 0
                self._lock = threading.Lock()
                self._in_flight = 0
                self.max_in_flight = 0
                self.entered = threading.Event()
                self.release = threading.Event()

            def start(self) -> None:
                return None

            def close(self, *, drain: bool = False, timeout: float = 5.0) -> IngestionStopStatus:
                with self._lock:
                    self.calls += 1
                    self._in_flight += 1
                    self.max_in_flight = max(self.max_in_flight, self._in_flight)
                self.entered.set()
                # 串行化：第二个 close 不得进入此处与第一个并行
                self.release.wait(timeout=2.0)
                with self._lock:
                    self._in_flight -= 1
                return IngestionStopStatus(
                    discarded=0,
                    timed_out=False,
                    worker_alive=False,
                    drained=True,
                    pending=0,
                )

        class CountingStore:
            def __init__(self) -> None:
                self.calls = 0

            def close(self) -> None:
                self.calls += 1

        ingestion = CountingIngestion()
        store = CountingStore()
        runtime = MemoryRuntime(
            store,  # type: ignore[arg-type]
            MagicMock(),
            ingestion,  # type: ignore[arg-type]
            settings=_retention_off(Path("x")),
            owns_store=True,
        )
        results: List[Any] = []
        errors: List[BaseException] = []
        start_barrier = threading.Barrier(3)

        def worker() -> None:
            try:
                start_barrier.wait(timeout=2.0)
                results.append(runtime.close(drain=False, timeout=1.0))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        start_barrier.wait(timeout=2.0)
        self.assertTrue(ingestion.entered.wait(timeout=2.0))
        # 首个 close 仍在途中：仅一次进入 ingestion，第二者阻塞在 close_gate
        threading.Event().wait(0.05)
        self.assertEqual(ingestion.calls, 1)
        self.assertEqual(ingestion.max_in_flight, 1)
        ingestion.release.set()
        for t in threads:
            t.join(timeout=3.0)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(ingestion.calls, 1)
        self.assertEqual(ingestion.max_in_flight, 1)
        self.assertEqual(store.calls, 1)
        self.assertTrue(runtime.is_closed)
        self.assertIs(results[0], results[1])

    def test_three_concurrent_closes_serialize(self) -> None:
        class CountingIngestion:
            def __init__(self) -> None:
                self.calls = 0
                self.max_in_flight = 0
                self._in_flight = 0
                self._lock = threading.Lock()

            def start(self) -> None:
                return None

            def close(self, *, drain: bool = False, timeout: float = 5.0) -> IngestionStopStatus:
                with self._lock:
                    self.calls += 1
                    self._in_flight += 1
                    self.max_in_flight = max(self.max_in_flight, self._in_flight)
                try:
                    threading.Event().wait(0.02)
                finally:
                    with self._lock:
                        self._in_flight -= 1
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

        ingestion = CountingIngestion()
        store = Store()
        runtime = MemoryRuntime(
            store,  # type: ignore[arg-type]
            MagicMock(),
            ingestion,  # type: ignore[arg-type]
            settings=_retention_off(Path("x")),
            owns_store=True,
        )
        barrier = threading.Barrier(3)
        results: List[Any] = []

        def worker() -> None:
            barrier.wait(timeout=2.0)
            results.append(runtime.close(drain=False, timeout=1.0))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)
        self.assertEqual(len(results), 3)
        self.assertEqual(ingestion.calls, 1)
        self.assertEqual(ingestion.max_in_flight, 1)
        self.assertEqual(store.calls, 1)

    def test_concurrent_close_first_fails_second_retries(self) -> None:
        class FlakyIngestion:
            def __init__(self) -> None:
                self.calls = 0
                self._lock = threading.Lock()

            def start(self) -> None:
                return None

            def close(self, *, drain: bool = False, timeout: float = 5.0) -> IngestionStopStatus:
                with self._lock:
                    self.calls += 1
                    n = self.calls
                if n == 1:
                    raise RuntimeError("first-close-boom")
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

        ingestion = FlakyIngestion()
        store = Store()
        runtime = MemoryRuntime(
            store,  # type: ignore[arg-type]
            MagicMock(),
            ingestion,  # type: ignore[arg-type]
            settings=_retention_off(Path("x")),
            owns_store=True,
        )
        first_done = threading.Event()
        outcomes: List[str] = []

        def first() -> None:
            try:
                runtime.close(drain=False, timeout=1.0)
                outcomes.append("ok1")
            except RuntimeError:
                outcomes.append("err1")
            finally:
                first_done.set()

        def second() -> None:
            self.assertTrue(first_done.wait(timeout=2.0))
            status = runtime.close(drain=False, timeout=1.0)
            outcomes.append("ok2" if status.drained else "bad2")

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)
        self.assertEqual(outcomes, ["err1", "ok2"])
        self.assertEqual(ingestion.calls, 2)
        self.assertEqual(store.calls, 1)
        self.assertTrue(runtime.is_closed)

    def test_store_fail_then_retry_closes_once(self) -> None:
        class OkIngestion:
            def __init__(self) -> None:
                self.calls = 0

            def start(self) -> None:
                return None

            def close(self, *, drain: bool = False, timeout: float = 5.0) -> IngestionStopStatus:
                self.calls += 1
                return IngestionStopStatus(
                    discarded=0,
                    timed_out=False,
                    worker_alive=False,
                    drained=True,
                    pending=0,
                )

        class FlakyStore:
            def __init__(self) -> None:
                self.calls = 0

            def close(self) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("store-boom")

        ingestion = OkIngestion()
        store = FlakyStore()
        runtime = MemoryRuntime(
            store,  # type: ignore[arg-type]
            MagicMock(),
            ingestion,  # type: ignore[arg-type]
            settings=_retention_off(Path("x")),
            owns_store=True,
        )
        with self.assertRaises(RuntimeError):
            runtime.close(drain=False, timeout=1.0)
        self.assertFalse(runtime.is_closed)
        status = runtime.close(drain=False, timeout=1.0)
        self.assertTrue(status.drained)
        self.assertEqual(ingestion.calls, 1)
        self.assertEqual(store.calls, 2)
        self.assertTrue(runtime.is_closed)


if __name__ == "__main__":
    unittest.main()
