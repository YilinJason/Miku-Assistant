"""id_audit：只读 URI、确认计划与全有或全无删除。"""
from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.memory.errors import MemoryValidationError
from src.memory.id_audit import (
    build_confirmation_plan,
    count_memories_readonly,
    delete_entries_atomic,
    load_audit_entries,
    open_readonly_connection,
    parse_confirmation_plan,
    verify_entries_against_db,
)
from src.memory.models import MemoryCategory, MemorySource, NewMemory
from src.memory.sqlite_store import SqliteMemoryStore
import diagnose_memory_ids as diag_cli


class IdAuditReadonlyAndPlanTests(unittest.TestCase):
    def _seed(self, db: Path) -> list[str]:
        store = SqliteMemoryStore(db)
        try:
            a = store.add(
                NewMemory(
                    text="用户喜欢乌龙茶",
                    category=MemoryCategory.PREFERENCE,
                    source=MemorySource.MODEL_INFERRED,
                )
            )
            b = store.add(
                NewMemory(
                    text="用户住在青禾市",
                    category=MemoryCategory.USER_PROFILE,
                    source=MemorySource.MODEL_INFERRED,
                )
            )
            return [a.record.id, b.record.id]
        finally:
            store.close()

    def test_readonly_open_does_not_create_or_mutate(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope" / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                open_readonly_connection(missing)
            self.assertFalse(missing.exists())
            self.assertFalse(missing.parent.exists())

            db = Path(tmp) / "ro.sqlite3"
            ids = self._seed(db)
            before = db.stat().st_mtime_ns
            conn = open_readonly_connection(db)
            try:
                self.assertEqual(count_memories_readonly(conn), 2)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("CREATE TABLE IF NOT EXISTS hack (id TEXT)")
            finally:
                conn.close()
            self.assertEqual(db.stat().st_mtime_ns, before)
            code = diag_cli.main(["--db", str(db), "--id", ids[0]])
            self.assertEqual(code, 0)
            self.assertEqual(db.stat().st_mtime_ns, before)

    def test_stale_plan_and_duplicate_zero_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "stale.sqlite3"
            ids = self._seed(db)
            conn = open_readonly_connection(db)
            try:
                entries, missing = load_audit_entries(conn, ids)
                self.assertEqual(missing, [])
                plan = build_confirmation_plan(db_path=db, entries=entries)
            finally:
                conn.close()

            w = sqlite3.connect(str(db))
            w.row_factory = sqlite3.Row
            try:
                w.execute(
                    "UPDATE memories SET text = ? WHERE id = ?",
                    ("用户喜欢改写后的茶", ids[0]),
                )
                w.commit()
                _db, parsed_entries, _ = parse_confirmation_plan(plan)
                with self.assertRaises(MemoryValidationError):
                    verify_entries_against_db(w, parsed_entries)
                with self.assertRaises(MemoryValidationError):
                    delete_entries_atomic(w, parsed_entries)
                after = w.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                self.assertEqual(after, 2)
            finally:
                w.close()

            ro = open_readonly_connection(db)
            try:
                with self.assertRaises(MemoryValidationError):
                    load_audit_entries(ro, [ids[0], ids[0]])
            finally:
                ro.close()

    def test_execute_requires_plan_and_is_atomic(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "atom.sqlite3"
            ids = self._seed(db)
            plan_path = Path(tmp) / "plan.json"
            code = diag_cli.main(
                [
                    "--db",
                    str(db),
                    "--id",
                    ids[0],
                    "--id",
                    ids[1],
                    "--write-plan",
                    str(plan_path),
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(plan_path.is_file())

            self.assertEqual(diag_cli.main(["--db", str(db), "--execute"]), 2)

            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["entries"][1]["text"] = "不存在的正文"
            plan["digest"] = "deadbeef"
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(
                diag_cli.main(["--db", str(db), "--execute", "--plan", str(bad)]),
                2,
            )
            ro = open_readonly_connection(db)
            try:
                self.assertEqual(count_memories_readonly(ro), 2)
            finally:
                ro.close()

            self.assertEqual(
                diag_cli.main(
                    ["--db", str(db), "--execute", "--plan", str(plan_path)]
                ),
                0,
            )
            ro2 = open_readonly_connection(db)
            try:
                self.assertEqual(count_memories_readonly(ro2), 0)
            finally:
                ro2.close()


if __name__ == "__main__":
    unittest.main()
