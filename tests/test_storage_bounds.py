"""有界存储与低干扰清理 Round 1 修复的纯离线行为测试。"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

from src.core.assembly import assemble_default
from src.core.file_cleaner import CleanupRule, FileCleaner
from src.core.safety import SafetyPolicy
from src.llm.conversation_history import ConversationHistory, require_max_stored_turns
from src.llm.ollama_client import OllamaClient
from src.memory.admission import ForegroundAwareAdmissionGate
from src.memory.ingestion import MemoryIngestionService
from src.memory.models import MemoryCategory, MemorySource, NewMemory
from src.memory.ollama_extractor import (
    OllamaMemoryExtractor,
    is_unowned_first_person_fact,
)
from src.memory.query import MemoryQueryService
from src.memory.runtime import MemoryRuntime, MemorySettings, parse_memory_settings
from src.memory.service_models import MemoryTurnEvent, MemoryTurnOutcome
from src.memory.sqlite_store import SqliteMemoryStore
from src.voice.audio.temp_files import TempFileManager
from src.voice.tts.engine import TTSEngine


class _Cfg:
    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def resolve_path(self, path: str) -> str:
        return path

    def get_rvc_config(self) -> Optional[dict]:
        return None


class _BudgetIterDir:
    """每取一项计数；超过预算再取则抛错。"""

    def __init__(self, entries: List[Path], *, budget: int) -> None:
        self._entries = list(entries)
        self._budget = budget
        self._taken = 0
        self._index = 0

    def __iter__(self) -> Iterator[Path]:
        return self

    def __next__(self) -> Path:
        if self._index >= len(self._entries):
            raise StopIteration
        self._taken += 1
        if self._taken > self._budget:
            raise RuntimeError("enumerated past scan budget")
        item = self._entries[self._index]
        self._index += 1
        return item


class FileCleanerBoundsTests(unittest.TestCase):
    def test_start_does_not_sync_scan(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(20):
                p = root / f"miku_{i}.wav"
                p.write_text("x", encoding="utf-8")
                os.utime(p, (1, 1))

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
                cleanup_interval_minutes=60,
                safety_policy=policy,
                cleanup_on_start=False,
            )
            cleaner.start_auto_cleanup()
            try:
                self.assertEqual(len(list(root.glob("miku_*.wav"))), 20)
            finally:
                cleaner.stop_auto_cleanup()

    def test_lazy_scandir_stops_at_scan_budget_including_noise(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries: List[Path] = []
            for i in range(5):
                d = root / f"subdir_{i}"
                d.mkdir()
                entries.append(d)
            for i in range(5):
                p = root / f"noise_{i}.txt"
                p.write_text("x", encoding="utf-8")
                entries.append(p)
            for i in range(3):
                p = root / f"miku_{i}.wav"
                p.write_text("x", encoding="utf-8")
                os.utime(p, (1, 1))
                entries.append(p)

            class BudgetScandir:
                def __init__(self) -> None:
                    self.taken = 0
                    self.closed = False
                    self._i = 0

                def __iter__(self):
                    return self

                def __next__(self):
                    if self._i >= len(entries):
                        raise StopIteration
                    self.taken += 1
                    if self.taken > 3:
                        raise RuntimeError("enumerated past scan budget")
                    p = entries[self._i]
                    self._i += 1

                    class E:
                        path = str(p)
                        name = p.name

                        def is_file(self, follow_symlinks: bool = True):
                            return p.is_file()

                        def stat(self, follow_symlinks: bool = True):
                            return p.stat()

                    return E()

                def close(self) -> None:
                    self.closed = True

            budgeted = BudgetScandir()

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
                max_scan_files=3,
                max_delete_files=10,
                scandir_fn=lambda _p: budgeted,
            )
            with patch("os.listdir", side_effect=RuntimeError("no listdir")):
                deleted = cleaner.cleanup_now()
            self.assertEqual(deleted, 0)
            self.assertEqual(budgeted.taken, 3)
            # 预算耗尽未穷尽：活动 sweep 保留句柄；stop 时 exactly-once close
            self.assertFalse(budgeted.closed)
            cleaner.stop_auto_cleanup()
            self.assertTrue(budgeted.closed)

    def test_from_config_rejects_string_bool_and_negative_interval(self) -> None:
        with self.assertRaises(ValueError):
            FileCleaner.from_config(
                _Cfg(
                    {
                        "assistant": {
                            "file_cleanup": {
                                "interval_minutes": 10,
                                "cleanup_on_start": "false",
                                "tmp_max_age_hours": 1.0,
                                "logs_max_age_days": 1,
                                "test_max_age_hours": 72,
                            }
                        }
                    }
                )
            )
        with self.assertRaises(ValueError):
            FileCleaner.from_config(
                _Cfg(
                    {
                        "assistant": {
                            "file_cleanup": {
                                "interval_minutes": -1,
                                "tmp_max_age_hours": 1.0,
                                "logs_max_age_days": 1,
                                "test_max_age_hours": 72,
                            }
                        }
                    }
                )
            )

    def test_does_not_target_repo_root_tmp(self) -> None:
        cfg = _Cfg(
            {
                "assistant": {
                    "file_cleanup": {
                        "interval_minutes": 10,
                        "tmp_max_age_hours": 1.0,
                        "logs_max_age_days": 1,
                        "test_max_age_hours": 72,
                    }
                }
            }
        )
        cleaner = FileCleaner.from_config(cfg)
        dirs = {str(r.directory.resolve()) for r in cleaner.rules}
        from src.core.paths import PROJECT_ROOT, TMP_DIR

        repo_tmp = (PROJECT_ROOT / "tmp").resolve()
        self.assertNotIn(str(repo_tmp), dirs)
        self.assertIn(str(TMP_DIR.resolve()), dirs)


class TempFileManagerBoundsTests(unittest.TestCase):
    def test_shutdown_interrupts_long_delay(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = SafetyPolicy()
            policy.register_file_delete_dir(root)
            mgr = TempFileManager(safety_policy=policy, delayed_queue_capacity=4)
            path = root / "miku_long.wav"
            path.write_text("x", encoding="utf-8")
            mgr.remove_delayed(str(path), delay=30.0, max_retries=5)
            time.sleep(0.05)
            self.assertTrue(
                any(
                    t.name == "TempFileDelayedDelete" and t.is_alive()
                    for t in threading.enumerate()
                )
            )
            mgr.shutdown(timeout=0.5)
            self.assertFalse(
                any(
                    t.name == "TempFileDelayedDelete" and t.is_alive()
                    for t in threading.enumerate()
                )
            )
            mgr.shutdown(timeout=0.1)  # idempotent

    def test_queue_full_does_not_spawn_extra_threads(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = SafetyPolicy()
            policy.register_file_delete_dir(root)
            mgr = TempFileManager(safety_policy=policy, delayed_queue_capacity=2)
            # 阻塞 worker：先塞一个超长 delay
            p0 = root / "miku_block.wav"
            p0.write_text("x", encoding="utf-8")
            mgr.remove_delayed(str(p0), delay=30.0, max_retries=1)
            time.sleep(0.05)
            before_threads = sum(
                1 for t in threading.enumerate() if t.name == "TempFileDelayedDelete"
            )
            for i in range(5):
                p = root / f"miku_q_{i}.wav"
                p.write_text("x", encoding="utf-8")
                mgr.remove_delayed(str(p), delay=30.0, max_retries=1)
            after_threads = sum(
                1 for t in threading.enumerate() if t.name == "TempFileDelayedDelete"
            )
            self.assertEqual(before_threads, 1)
            self.assertEqual(after_threads, 1)
            mgr.shutdown(timeout=0.5)

    def test_tts_shutdown_reclaims_owned_temp_manager(self) -> None:
        with patch("src.voice.tts.engine.AudioPlayer"), patch(
            "src.voice.rvc.infer.stop_rvc_worker"
        ):
            engine = TTSEngine(
                engine="pyttsx3",
                fallback_enabled=False,
                pipeline_enabled=False,
                rvc_config={},
            )
            # 强制启动 delayed worker
            engine._temp_files.remove_delayed(
                str(Path("nonexistent_for_queue.wav")), delay=30.0
            )
            time.sleep(0.05)
            engine.shutdown()
            engine.shutdown()
            self.assertFalse(
                any(
                    t.name == "TempFileDelayedDelete" and t.is_alive()
                    for t in threading.enumerate()
                )
            )


class MemoryRetentionTests(unittest.TestCase):
    def test_protected_prefix_no_longer_blocks_deletable(self) -> None:
        with TemporaryDirectory() as tmp:
            clock = {"t": datetime(2020, 1, 1, tzinfo=timezone.utc)}

            def now() -> datetime:
                return clock["t"]

            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3", clock=now)
            try:
                for i in range(5):
                    clock["t"] = datetime(2020, 1, 1 + i, tzinfo=timezone.utc)
                    store.add(
                        NewMemory(
                            text=f"confirmed-{i}",
                            source=MemorySource.USER_CONFIRMED,
                            importance=0.9,
                        )
                    )
                clock["t"] = datetime(2020, 1, 10, tzinfo=timezone.utc)
                store.add(
                    NewMemory(
                        text="inferred-only",
                        source=MemorySource.MODEL_INFERRED,
                        importance=0.1,
                    )
                )
                r1 = store.prune_for_retention(
                    max_records=2,
                    max_age_days=None,
                    preserve_user_confirmed=True,
                    batch_limit=1,
                    now=datetime(2020, 1, 20, tzinfo=timezone.utc),
                )
                self.assertEqual(r1.deleted, 1)
                self.assertEqual(store.count_memories(), 5)
                r2 = store.prune_for_retention(
                    max_records=2,
                    max_age_days=None,
                    preserve_user_confirmed=True,
                    batch_limit=1,
                    now=datetime(2020, 1, 20, tzinfo=timezone.utc),
                )
                # 已无可删 MODEL_INFERRED；受保护记录超过上限
                self.assertEqual(r2.deleted, 0)
                self.assertTrue(r2.over_capacity_unresolvable)
                self.assertEqual(store.count_memories(), 5)
            finally:
                store.close()

    def test_all_protected_reports_unresolvable(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")
            try:
                for i in range(4):
                    store.add(
                        NewMemory(
                            text=f"uc-{i}",
                            source=MemorySource.USER_CONFIRMED,
                        )
                    )
                result = store.prune_for_retention(
                    max_records=1,
                    max_age_days=None,
                    preserve_user_confirmed=True,
                    batch_limit=8,
                )
                self.assertEqual(result.deleted, 0)
                self.assertTrue(result.over_capacity_unresolvable)
                self.assertEqual(store.count_memories(), 4)
            finally:
                store.close()

    def test_importance_then_id_stable_order(self) -> None:
        with TemporaryDirectory() as tmp:
            fixed = datetime(2020, 6, 1, tzinfo=timezone.utc)
            store = SqliteMemoryStore(
                Path(tmp) / "m.sqlite3", clock=lambda: fixed
            )
            try:
                store.add(
                    NewMemory(
                        text="high",
                        source=MemorySource.MODEL_INFERRED,
                        importance=0.9,
                        memory_id="id-b",
                    )
                )
                store.add(
                    NewMemory(
                        text="low-a",
                        source=MemorySource.MODEL_INFERRED,
                        importance=0.1,
                        memory_id="id-a",
                    )
                )
                store.add(
                    NewMemory(
                        text="low-c",
                        source=MemorySource.MODEL_INFERRED,
                        importance=0.1,
                        memory_id="id-c",
                    )
                )
                result = store.prune_for_retention(
                    max_records=2,
                    max_age_days=None,
                    preserve_user_confirmed=True,
                    batch_limit=1,
                )
                self.assertEqual(result.deleted, 1)
                texts = {r.text for r in store.list_memories(limit=10)}
                self.assertNotIn("low-a", texts)  # 同重要度按 id ASC
                self.assertIn("low-c", texts)
                self.assertIn("high", texts)
            finally:
                store.close()

    def test_age_and_count_share_batch_budget(self) -> None:
        with TemporaryDirectory() as tmp:
            clock = {"t": datetime(2019, 1, 1, tzinfo=timezone.utc)}

            def now() -> datetime:
                return clock["t"]

            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3", clock=now)
            try:
                for i in range(3):
                    clock["t"] = datetime(2019, 1, 1 + i, tzinfo=timezone.utc)
                    store.add(
                        NewMemory(
                            text=f"old-{i}",
                            source=MemorySource.MODEL_INFERRED,
                            importance=0.5,
                        )
                    )
                for i in range(3):
                    clock["t"] = datetime(2020, 6, 1 + i, tzinfo=timezone.utc)
                    store.add(
                        NewMemory(
                            text=f"new-{i}",
                            source=MemorySource.MODEL_INFERRED,
                            importance=0.5,
                        )
                    )
                result = store.prune_for_retention(
                    max_records=2,
                    max_age_days=100.0,
                    preserve_user_confirmed=True,
                    batch_limit=2,
                    now=datetime(2020, 7, 1, tzinfo=timezone.utc),
                )
                # 全局预算 2，不得删到 4
                self.assertEqual(result.deleted, 2)
                self.assertEqual(store.count_memories(), 4)
            finally:
                store.close()

    def test_maintenance_race_foreground_after_idle_check(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")
            extractor = MagicMock()
            ingestion = MemoryIngestionService(
                store, extractor, queue_capacity=4, owns_store=False
            )
            gate = ForegroundAwareAdmissionGate()
            settings = MemorySettings(
                enabled=True,
                db_path=Path(tmp) / "m.sqlite3",
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
                retention_max_records=1,
                retention_max_age_days=365.0,
                retention_maintenance_interval_hours=0.0,
                retention_preserve_user_confirmed=True,
                retention_batch_limit=8,
            )
            runtime = MemoryRuntime(
                store,
                MemoryQueryService(store),
                ingestion,
                settings=settings,
                owns_store=False,
                admission_gate=gate,
            )
            store.add(
                NewMemory(text="a", source=MemorySource.MODEL_INFERRED)
            )
            store.add(
                NewMemory(text="b", source=MemorySource.MODEL_INFERRED)
            )
            barrier_entered = threading.Event()
            allow_acquire = threading.Event()
            results: List[Any] = []

            class RacingGate(ForegroundAwareAdmissionGate):
                def try_acquire_maintenance(self) -> bool:
                    barrier_entered.set()
                    if not allow_acquire.wait(timeout=2.0):
                        return False
                    return super().try_acquire_maintenance()

            racing = RacingGate()
            runtime._admission_gate = racing

            def maintenance() -> None:
                results.append(runtime.maybe_run_maintenance(force=True))

            t = threading.Thread(target=maintenance)
            t.start()
            self.assertTrue(barrier_entered.wait(timeout=2.0))
            racing.begin_foreground()
            allow_acquire.set()
            t.join(timeout=2.0)
            self.assertIsNone(results[0])
            self.assertEqual(store.count_memories(), 2)
            racing.end_foreground()
            runtime.close(drain=False, timeout=1.0)
            store.close()

    def test_maintenance_skips_custom_gate_without_lease_api(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")

            class LegacyGate:
                def begin_foreground(self) -> None:
                    return None

                def end_foreground(self) -> None:
                    return None

                def try_acquire_extract(self) -> bool:
                    return True

                def release_extract(self) -> None:
                    return None

            ingestion = MemoryIngestionService(
                store, MagicMock(), queue_capacity=2, owns_store=False
            )
            settings = MemorySettings(
                enabled=True,
                db_path=Path(tmp) / "m.sqlite3",
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
                retention_enabled=True,
                retention_max_records=1,
                retention_maintenance_interval_hours=0.0,
            )
            runtime = MemoryRuntime(
                store,
                MemoryQueryService(store),
                ingestion,
                settings=settings,
                owns_store=False,
                admission_gate=LegacyGate(),  # type: ignore[arg-type]
            )
            store.add(NewMemory(text="x", source=MemorySource.MODEL_INFERRED))
            store.add(NewMemory(text="y", source=MemorySource.MODEL_INFERRED))
            self.assertIsNone(runtime.maybe_run_maintenance(force=True))
            self.assertEqual(store.count_memories(), 2)
            runtime.close(drain=False, timeout=1.0)
            store.close()

    def test_maintenance_releases_lease_on_prune_error(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")
            gate = ForegroundAwareAdmissionGate()
            ingestion = MemoryIngestionService(
                store, MagicMock(), queue_capacity=2, owns_store=False
            )
            settings = MemorySettings(
                enabled=True,
                db_path=Path(tmp) / "m.sqlite3",
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
                retention_enabled=True,
                retention_max_records=1,
                retention_maintenance_interval_hours=0.0,
            )
            runtime = MemoryRuntime(
                store,
                MemoryQueryService(store),
                ingestion,
                settings=settings,
                owns_store=False,
                admission_gate=gate,
            )

            def boom(**kwargs: Any) -> Any:
                raise RuntimeError("prune-boom")

            store.prune_for_retention = boom  # type: ignore[method-assign]
            self.assertIsNone(runtime.maybe_run_maintenance(force=True))
            self.assertEqual(gate.maintenance_count, 0)
            # 可再次获取
            self.assertTrue(gate.try_acquire_maintenance())
            gate.release_maintenance()
            runtime.close(drain=False, timeout=1.0)
            store.close()


class HistoryConfigWiringTests(unittest.TestCase):
    def test_require_max_stored_turns_rejects_bad_types(self) -> None:
        for bad in (True, "10", 1.5, 0, -1, 100_000):
            with self.assertRaises(ValueError):
                require_max_stored_turns("ollama.history.max_stored_turns", bad)

    def test_assemble_passes_config_to_default_history(self) -> None:
        captured: Dict[str, Any] = {}

        class CapturingClient:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)
                self._history = ConversationHistory(
                    max_stored_turns=kwargs.get("max_stored_turns", 500)
                )

            @property
            def conversation_history(self) -> list:
                return self._history.snapshot()

        cfg = _Cfg(
            {
                "ollama": {
                    "base_url": "http://x",
                    "model": "m",
                    "timeout": 1,
                    "keep_alive": "1m",
                    "history": {"max_stored_turns": 17},
                },
                "memory": {"enabled": False},
                "voice": {
                    "tts": {
                        "engine": "pyttsx3",
                        "language": "zh-CN",
                        "rate": 150,
                        "volume": 0.8,
                        "voice": "v",
                        "fallback_enabled": False,
                        "fallback_engine": "pyttsx3",
                        "pipeline_enabled": False,
                    },
                    "stt": {
                        "engine": "speech_recognition",
                        "language": "zh-CN",
                        "energy_threshold": 1,
                    },
                },
                "system": {
                    "windows_control_enabled": False,
                    "require_confirmation": True,
                },
                "search": {
                    "chrome_path": None,
                    "search_engine": "google",
                    "auto_open": False,
                },
                "extensions": {"character_model": {"enabled": False}},
                "assistant": {"file_cleanup": {"interval_minutes": 60}},
                "security": {"permissions": {}},
            }
        )
        parts = assemble_default(
            config_path="unused",
            startup_mode="text",
            config_loader=lambda _p: cfg,
            logging_setup=lambda _c: None,
            dir_preparer=lambda: None,
            ollama_client_cls=CapturingClient,
            tts_engine_cls=lambda **k: MagicMock(shutdown=MagicMock()),
            stt_engine_cls=lambda **k: MagicMock(),
            file_cleaner_from_config=lambda *a, **k: None,
            router_builder=lambda **k: MagicMock(),
        )
        self.assertEqual(captured.get("max_stored_turns"), 17)
        self.assertEqual(parts.llm_client._history.max_stored_turns, 17)

    def test_injected_history_not_overwritten(self) -> None:
        hist = ConversationHistory(max_stored_turns=3)
        client = OllamaClient(
            base_url="http://fake",
            model="t",
            history=hist,
            max_stored_turns=99,
        )
        self.assertIs(client._history, hist)
        self.assertEqual(client._history.max_stored_turns, 3)


class ExtractorFirstPersonGuardTests(unittest.TestCase):
    def test_is_unowned_first_person_helpers(self) -> None:
        self.assertTrue(is_unowned_first_person_fact("我最喜欢喝茶"))
        self.assertTrue(is_unowned_first_person_fact("我的代号是银杏"))
        self.assertFalse(is_unowned_first_person_fact("用户最喜欢喝茶"))
        self.assertFalse(is_unowned_first_person_fact("用户的测试代号是银杏4729"))

    def test_parse_skips_first_person_keeps_third(self) -> None:
        extractor = OllamaMemoryExtractor(
            base_url="http://fake",
            model="m",
            timeout=1.0,
            keep_alive="1m",
        )
        turn = MemoryTurnEvent(
            turn_id="fp",
            user_text="我最喜欢喝茶，其实我喜欢桂花乌龙",
            assistant_text="好的",
            outcome=MemoryTurnOutcome.SUCCESS,
        )
        from src.memory.fact_units import enumerate_safe_fact_units

        units = enumerate_safe_fact_units(turn.user_text)
        payload = {
            "done": True,
            "message": {
                "content": json.dumps(
                    [
                        {"unit_id": 0, "importance": 0.5, "confidence": 0.5},
                        {"unit_id": 1, "importance": 0.5, "confidence": 0.5},
                    ],
                    ensure_ascii=False,
                )
            },
        }
        items = extractor._parse_response(payload, units=units, turn=turn)
        self.assertEqual(len(items), 2)
        texts = {i.text for i in items}
        self.assertEqual(texts, {"用户最喜欢喝茶", "用户喜欢桂花乌龙"})


if __name__ == "__main__":
    unittest.main()
