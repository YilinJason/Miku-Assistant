"""长期记忆领域契约与 SQLite 持久层的纯离线测试。"""
from __future__ import annotations

import math
import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List
from unittest.mock import patch

from src.llm.conversation_history import ConversationHistory
from src.memory import (
    SCHEMA_VERSION,
    MemoryCategory,
    MemoryDataCorruptError,
    MemoryNotFoundError,
    MemoryRecord,
    MemorySchemaIncompatibleError,
    MemorySource,
    MemoryStoreUnavailableError,
    MemoryValidationError,
    MemoryWriteResult,
    NewMemory,
    SqliteMemoryStore,
    default_memory_db_path,
    normalize_dedupe_key,
)


def _utc(hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 3, 15, hour, minute, second, tzinfo=timezone.utc)


class DomainInvariantTests(unittest.TestCase):
    def test_rejects_empty_range_nan_inf_and_time_order(self) -> None:
        with self.assertRaises(MemoryValidationError):
            NewMemory(text="   ")
        with self.assertRaises(MemoryValidationError):
            NewMemory(text="ok", importance=1.5)
        with self.assertRaises(MemoryValidationError):
            NewMemory(text="ok", confidence=-0.1)
        with self.assertRaises(MemoryValidationError):
            NewMemory(text="ok", source="invented")  # type: ignore[arg-type]
        with self.assertRaises(MemoryValidationError):
            NewMemory(text="x", importance=float("nan"))
        with self.assertRaises(MemoryValidationError):
            NewMemory(text="x", confidence=float("inf"))
        with self.assertRaises(MemoryValidationError):
            NewMemory(text="x", importance=True)  # type: ignore[arg-type]

        created = _utc()
        with self.assertRaises(MemoryValidationError):
            MemoryRecord(
                id="x",
                text="ok",
                category=MemoryCategory.OTHER,
                source=MemorySource.USER_CONFIRMED,
                importance=0.5,
                confidence=0.5,
                created_at=created,
                updated_at=created - timedelta(seconds=1),
                dedupe_key=normalize_dedupe_key("ok"),
            )
        with self.assertRaises(MemoryValidationError):
            MemoryRecord(
                id="x",
                text="Hello World",
                category=MemoryCategory.OTHER,
                source=MemorySource.USER_CONFIRMED,
                importance=0.5,
                confidence=0.5,
                created_at=created,
                updated_at=created,
                dedupe_key="wrong-key",
            )

    def test_normalize_dedupe_key_preserves_cjk_and_folds_case(self) -> None:
        self.assertEqual(
            normalize_dedupe_key("  Hello   World  "),
            normalize_dedupe_key("hello world"),
        )
        self.assertEqual(normalize_dedupe_key("喜欢吃拉面"), "喜欢吃拉面")
        self.assertIn("🙂", normalize_dedupe_key("你好 🙂"))
        draft = NewMemory(text="  AbC  ")
        self.assertEqual(draft.text, "  AbC  ")
        self.assertFalse(math.isnan(draft.importance))
        self.assertEqual(draft.dedupe_key, "abc")

    def test_display_text_keeps_leading_trailing_and_inner_whitespace(self) -> None:
        draft = NewMemory(text="  原样正文  ")
        self.assertEqual(draft.text, "  原样正文  ")
        self.assertEqual(draft.dedupe_key, normalize_dedupe_key("  原样正文  "))
        record = MemoryRecord(
            id="r1",
            text="  a  b  ",
            category=MemoryCategory.OTHER,
            source=MemorySource.USER_CONFIRMED,
            importance=0.5,
            confidence=1.0,
            created_at=_utc(),
            updated_at=_utc(),
            dedupe_key=normalize_dedupe_key("  a  b  "),
        )
        self.assertEqual(record.text, "  a  b  ")
        self.assertEqual(record.dedupe_key, "a b")


class SqliteMemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "memory.sqlite3"
        self.clock_values: List[datetime] = [_utc(12, 0, 0)]
        self.ids = iter(["id-a", "id-b", "id-c", "id-d", "id-e"])

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
        self.store = None  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_roundtrip_unicode_and_sql_like_text(self) -> None:
        text = "中文🙂 引号'\"与\n换行; DROP TABLE memories;--"
        result = self.store.add(
            NewMemory(
                text=text,
                category=MemoryCategory.PREFERENCE,
                source=MemorySource.USER_CONFIRMED,
                importance=0.8,
                confidence=0.9,
            )
        )
        self.assertTrue(result.created)
        loaded = self.store.get(result.record.id)
        assert loaded is not None
        self.assertEqual(loaded.text, text)
        self.assertEqual(loaded.category, MemoryCategory.PREFERENCE)
        self.assertEqual(loaded.source, MemorySource.USER_CONFIRMED)

    def test_exact_dedupe_keeps_display_text_and_source(self) -> None:
        first = self.store.add(
            NewMemory(text="喜欢吃  拉面", source=MemorySource.MODEL_INFERRED)
        )
        self.assertTrue(first.created)
        self.assertEqual(first.record.text, "喜欢吃  拉面")
        second = self.store.add(
            NewMemory(text="  喜欢吃 拉面  ", source=MemorySource.USER_CONFIRMED)
        )
        self.assertFalse(second.created)
        self.assertEqual(second.record.id, first.record.id)
        self.assertEqual(second.record.text, "喜欢吃  拉面")
        self.assertEqual(second.record.source, MemorySource.MODEL_INFERRED)
        self.assertEqual(len(self.store.list_memories()), 1)

    def test_sqlite_roundtrip_preserves_raw_display_text(self) -> None:
        raw = "  首尾空白\n与  内部  "
        created = self.store.add(NewMemory(text=raw, source=MemorySource.USER_CONFIRMED))
        self.assertTrue(created.created)
        self.assertEqual(created.record.text, raw)
        loaded = self.store.get(created.record.id)
        assert loaded is not None
        self.assertEqual(loaded.text, raw)
        again = self.store.add(
            NewMemory(text="首尾空白 与 内部", source=MemorySource.MODEL_INFERRED)
        )
        self.assertFalse(again.created)
        self.assertEqual(again.record.id, created.record.id)
        self.assertEqual(again.record.text, raw)
        self.assertEqual(again.record.source, MemorySource.USER_CONFIRMED)

    def test_crud_list_clear_and_idempotent_close(self) -> None:
        a = self.store.add(NewMemory(text="第一条", category=MemoryCategory.EVENT))
        self.clock_values = [_utc(12, 0, 1)]
        b = self.store.add(NewMemory(text="第二条", category=MemoryCategory.EVENT))
        listed = self.store.list_memories(limit=10)
        self.assertEqual([item.id for item in listed], [a.record.id, b.record.id])
        page = self.store.list_memories(limit=1, offset=1)
        self.assertEqual([item.id for item in page], [b.record.id])
        with self.assertRaises(MemoryValidationError):
            self.store.list_memories(limit=0)

        updated = self.store.update(a.record.id, text="第一条已改", importance=0.2)
        self.assertEqual(updated.text, "第一条已改")
        self.assertEqual(updated.source, MemorySource.USER_CONFIRMED)
        self.assertTrue(self.store.delete(b.record.id))
        self.assertFalse(self.store.delete(b.record.id))
        self.assertEqual(self.store.clear(), 1)
        self.assertEqual(list(self.store.list_memories()), [])
        self.store.close()
        self.store.close()
        with self.assertRaises(MemoryStoreUnavailableError):
            self.store.add(NewMemory(text="closed"))

    def test_list_memories_newest_first_stable_order(self) -> None:
        a = self.store.add(NewMemory(text="旧条目"))
        self.clock_values = [_utc(12, 0, 1)]
        b = self.store.add(NewMemory(text="新条目"))
        asc = self.store.list_memories(limit=10)
        self.assertEqual([item.id for item in asc], [a.record.id, b.record.id])
        desc = self.store.list_memories(limit=10, newest_first=True)
        self.assertEqual([item.id for item in desc], [b.record.id, a.record.id])
        with self.assertRaises(MemoryValidationError):
            self.store.list_memories(newest_first="yes")  # type: ignore[arg-type]

    def test_reopen_persistence_and_file_isolation(self) -> None:
        result = self.store.add(
            NewMemory(
                text="持久化条目",
                source=MemorySource.MODEL_INFERRED,
                confidence=0.4,
            )
        )
        self.store.close()
        reopened = SqliteMemoryStore(self.db_path)
        loaded = reopened.get(result.record.id)
        assert loaded is not None
        self.assertEqual(loaded.source, MemorySource.MODEL_INFERRED)
        self.assertEqual(loaded.confidence, 0.4)
        self.assertEqual(reopened.schema_version, SCHEMA_VERSION)

        other_path = Path(self._tmp.name) / "other.sqlite3"
        other = SqliteMemoryStore(other_path)
        other.add(NewMemory(text="另一库"))
        self.assertIsNone(other.get(result.record.id))
        self.assertEqual(len(reopened.list_memories()), 1)
        self.assertEqual(len(other.list_memories()), 1)
        other.close()
        reopened.close()
        self.store = SqliteMemoryStore.__new__(SqliteMemoryStore)
        self.store._closed = True  # noqa: SLF001
        self.store._conn = None  # noqa: SLF001

    def test_update_preserves_source_and_rejects_missing(self) -> None:
        created = self.store.add(
            NewMemory(text="推断项", source=MemorySource.MODEL_INFERRED)
        )
        updated = self.store.update(created.record.id, text="推断项-更新")
        self.assertEqual(updated.source, MemorySource.MODEL_INFERRED)
        with self.assertRaises(MemoryNotFoundError):
            self.store.update("missing", text="x")

    def test_unknown_category_readable_as_other(self) -> None:
        created = self.store.add(NewMemory(text="旧类别"))
        memory_id = created.record.id
        self.store.close()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE memories SET category = ? WHERE id = ?",
                ("legacy_unknown_cat", memory_id),
            )
            conn.commit()
        self.store = SqliteMemoryStore(self.db_path)
        loaded = self.store.get(memory_id)
        assert loaded is not None
        self.assertEqual(loaded.category, MemoryCategory.OTHER)

    def test_schema_newer_rejected_without_destructive_change(self) -> None:
        marker = self.store.add(NewMemory(text="勿删"))
        marker_id = marker.record.id
        self.store.close()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
            conn.commit()
        with self.assertRaises(MemorySchemaIncompatibleError) as ctx:
            SqliteMemoryStore(self.db_path)
        self.assertEqual(ctx.exception.found_version, SCHEMA_VERSION + 5)
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT text FROM memories WHERE id = ?", (marker_id,)
            ).fetchone()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(row[0], "勿删")
        self.assertEqual(version, SCHEMA_VERSION + 5)
        self.store = SqliteMemoryStore.__new__(SqliteMemoryStore)
        self.store._closed = True  # noqa: SLF001
        self.store._conn = None  # noqa: SLF001

    def test_schema_init_idempotent_and_not_autocommit(self) -> None:
        self.assertIsNotNone(self.store._conn.isolation_level)  # noqa: SLF001
        self.store.close()
        first = SqliteMemoryStore(self.db_path)
        first.add(NewMemory(text="幂等"))
        first.close()
        self.store = SqliteMemoryStore(self.db_path)
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)
        self.assertEqual(len(self.store.list_memories()), 1)

    def test_real_transaction_rollback_keeps_original(self) -> None:
        created = self.store.add(NewMemory(text="原始正文"))
        conn = self.store._conn  # noqa: SLF001
        assert conn is not None
        self.assertNotEqual(conn.isolation_level, None)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE memories SET text = ? WHERE id = ?",
            ("不应提交", created.record.id),
        )
        conn.rollback()
        loaded = self.store.get(created.record.id)
        assert loaded is not None
        self.assertEqual(loaded.text, "原始正文")

    def test_schema_version_not_bumped_when_init_fails(self) -> None:
        self.store.close()
        fresh = Path(self._tmp.name) / "fresh.sqlite3"

        def boom(self, conn, *, found_version):  # noqa: ANN001
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY NOT NULL)"
            )
            raise sqlite3.Error("fail before user_version")

        with patch.object(SqliteMemoryStore, "_initialize_schema", boom):
            with self.assertRaises(MemoryStoreUnavailableError):
                SqliteMemoryStore(fresh)
        with sqlite3.connect(str(fresh)) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(version, 0)
        self.store = SqliteMemoryStore.__new__(SqliteMemoryStore)
        self.store._closed = True  # noqa: SLF001
        self.store._conn = None  # noqa: SLF001

    def test_closed_underlying_connection_maps_to_unavailable(self) -> None:
        self.store._conn.close()  # noqa: SLF001
        with self.assertRaises(MemoryStoreUnavailableError):
            self.store.get("id-a")

    def test_mkdir_failure_maps_to_unavailable(self) -> None:
        self.store.close()
        target = Path(self._tmp.name) / "blocked" / "mem.sqlite3"
        with patch.object(Path, "mkdir", side_effect=OSError("no mkdir")):
            with self.assertRaises(MemoryStoreUnavailableError):
                SqliteMemoryStore(target)
        self.store = SqliteMemoryStore.__new__(SqliteMemoryStore)
        self.store._closed = True  # noqa: SLF001
        self.store._conn = None  # noqa: SLF001

    def test_corrupt_row_raises_data_corrupt(self) -> None:
        created = self.store.add(NewMemory(text="完好"))
        memory_id = created.record.id
        self.store.close()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE memories SET source = ? WHERE id = ?",
                ("not-a-source", memory_id),
            )
            conn.commit()
        self.store = SqliteMemoryStore(self.db_path)
        with self.assertRaises(MemoryDataCorruptError):
            self.store.get(memory_id)

    def test_concurrent_adds_serialize_without_duplicate_rows(self) -> None:
        barrier = threading.Barrier(2)
        results: List[MemoryWriteResult] = []
        errors: List[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=2.0)
                results.append(self.store.add(NewMemory(text="并发同一句")))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, name="mem-a"),
            threading.Thread(target=worker, name="mem-b"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].record.id, results[1].record.id)
        self.assertEqual(sum(1 for item in results if item.created), 1)
        self.assertEqual(len(self.store.list_memories()), 1)

    def test_default_path_resolver_does_not_create(self) -> None:
        path = default_memory_db_path(self._tmp.name)
        self.assertEqual(path.name, "long_term.sqlite3")
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())


class ConversationHistoryUntouchedTests(unittest.TestCase):
    def test_history_still_isolated_and_windowed(self) -> None:
        history = ConversationHistory(request_window=2)
        history.commit_turn("u1", "a1")
        history.commit_turn("u2", "a2")
        history.commit_turn("u3", "a3")
        messages = history.build_messages("now")
        self.assertEqual(
            [item["content"] for item in messages],
            ["u3", "a3", "now"],
        )


if __name__ == "__main__":
    unittest.main()
