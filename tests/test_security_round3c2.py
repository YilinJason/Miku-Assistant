# -*- coding: utf-8 -*-
"""Round 3C2：SQLite 路径/连接硬化与删除计划 v2 行为测试（仅临时目录）。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, List
from unittest.mock import patch

from src.memory.errors import MemoryStoreUnavailableError, MemoryValidationError
from src.memory.id_audit import (
    PLAN_VERSION,
    build_confirmation_plan,
    load_audit_entries,
    open_readonly_connection,
    parse_confirmation_plan,
    row_fingerprint,
)
from src.memory.models import MemoryCategory, MemorySource, NewMemory
from src.memory import sqlite_security as sec
from src.memory.sqlite_store import SqliteMemoryStore
import diagnose_memory_ids as diag_cli


def _seed(db: Path, texts: List[str] | None = None) -> list[str]:
    texts = texts or ["alpha-sentinel-body", "beta-sentinel-body"]
    store = SqliteMemoryStore(db)
    try:
        ids = []
        for text in texts:
            result = store.add(
                NewMemory(
                    text=text,
                    category=MemoryCategory.PREFERENCE,
                    source=MemorySource.MODEL_INFERRED,
                )
            )
            ids.append(result.record.id)
        return ids
    finally:
        store.close()


class C2PathBoundaryTests(unittest.TestCase):
    def test_safe_new_and_existing_db_ok(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "ok.sqlite3"
            store = SqliteMemoryStore(db)
            try:
                store.add(
                    NewMemory(
                        text="hello",
                        category=MemoryCategory.PREFERENCE,
                        source=MemorySource.USER_CONFIRMED,
                    )
                )
            finally:
                store.close()
            reopened = SqliteMemoryStore(db)
            try:
                self.assertEqual(reopened.count_memories(), 1)
            finally:
                reopened.close()

    def test_dangerous_names_rejected_before_connect(self) -> None:
        with TemporaryDirectory() as tmp:
            calls: list[Any] = []

            def boom(*_a, **_k):
                calls.append(1)
                raise AssertionError("sqlite3.connect must not be called")

            cases = [
                Path(tmp) / "evil.txt",
                Path(tmp) / "CON.sqlite3",
                Path(tmp) / "bad name .sqlite3",
                Path(tmp) / "nest" / "x" / "y.sqlite3",  # name ok but we'll use name only
            ]
            with patch.object(sec.sqlite3, "connect", side_effect=boom):
                with self.assertRaises(MemoryValidationError):
                    sec.validate_sqlite_db_file_name("evil.txt")
                with self.assertRaises(MemoryValidationError):
                    sec.validate_sqlite_db_file_name("CON.sqlite3")
                with self.assertRaises((MemoryValidationError, MemoryStoreUnavailableError)):
                    SqliteMemoryStore(Path(tmp) / "evil.txt")
                self.assertEqual(calls, [])

            # 目录目标
            directory = Path(tmp) / "as_dir.sqlite3"
            directory.mkdir()
            with patch.object(sec.sqlite3, "connect", side_effect=boom):
                with self.assertRaises((MemoryValidationError, MemoryStoreUnavailableError)):
                    SqliteMemoryStore(directory)
                self.assertEqual(calls, [])

    def test_symlink_db_rejected_before_connect(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real.sqlite3"
            real.write_bytes(b"")
            link = root / "link.sqlite3"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlink not available")
            calls: list[int] = []

            def boom(*_a, **_k):
                calls.append(1)
                raise AssertionError("connect")

            with patch.object(sec.sqlite3, "connect", side_effect=boom):
                with self.assertRaises((MemoryValidationError, MemoryStoreUnavailableError)):
                    SqliteMemoryStore(link)
            self.assertEqual(calls, [])

    def test_hardlink_db_rejected_before_connect(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real.sqlite3"
            real.write_bytes(b"not-empty")
            link = root / "hard.sqlite3"
            try:
                os.link(real, link)
            except (OSError, NotImplementedError):
                self.skipTest("hardlink not available")
            calls: list[int] = []

            def boom(*_a, **_k):
                calls.append(1)
                raise AssertionError("connect")

            with patch.object(sec.sqlite3, "connect", side_effect=boom):
                with self.assertRaises((MemoryValidationError, MemoryStoreUnavailableError)):
                    SqliteMemoryStore(link)
            self.assertEqual(calls, [])
            self.assertEqual(real.read_bytes(), b"not-empty")

    def test_junction_parent_rejected(self) -> None:
        if sys.platform != "win32":
            self.skipTest("junction Windows-only")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_parent = root / "real_parent"
            outside = root / "outside"
            real_parent.mkdir()
            outside.mkdir()
            junc = root / "junc_parent"
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junc), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest("junction unavailable")
            calls: list[int] = []

            def boom(*_a, **_k):
                calls.append(1)
                raise AssertionError("connect")

            target = junc / "mem.sqlite3"
            with patch.object(sec.sqlite3, "connect", side_effect=boom):
                with self.assertRaises((MemoryValidationError, MemoryStoreUnavailableError)):
                    SqliteMemoryStore(target)
            self.assertEqual(calls, [])
            self.assertEqual(list(outside.iterdir()), [])


class C2OpenRaceTests(unittest.TestCase):
    def test_parent_swap_before_connect_fails_sentinel_intact(self) -> None:
        if sys.platform != "win32":
            self.skipTest("junction race Windows-only")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "parent"
            outside = root / "outside"
            saved = root / "parent-saved"
            logs.mkdir()
            outside.mkdir()
            sentinel = outside / "sentinel.bin"
            sentinel.write_bytes(b"OUTSIDE_SENTINEL")
            db = logs / "race.sqlite3"
            swapped = {"done": False}

            def _swap(_path: Path) -> None:
                if swapped["done"]:
                    return
                swapped["done"] = True
                logs.rename(saved)
                completed = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(logs), str(outside)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    raise unittest.SkipTest("junction unavailable")

            prev = sec._test_pre_connect_hook
            sec._test_pre_connect_hook = _swap
            try:
                with self.assertRaises(
                    (MemoryValidationError, MemoryStoreUnavailableError)
                ):
                    SqliteMemoryStore(db)
                self.assertEqual(sentinel.read_bytes(), b"OUTSIDE_SENTINEL")
                # F6：外部不得出现空库或 sidecar
                self.assertFalse((outside / "race.sqlite3").exists())
                for suffix in ("-journal", "-wal", "-shm"):
                    self.assertFalse((outside / f"race.sqlite3{suffix}").exists())
                # 可信旧父目录内允许保留预建空目标
                self.assertTrue((saved / "race.sqlite3").is_file())
            finally:
                sec._test_pre_connect_hook = prev
                if logs.exists() and getattr(logs, "is_junction", lambda: False)():
                    try:
                        logs.rmdir()
                    except OSError:
                        pass
                if saved.exists() and not logs.exists():
                    saved.rename(logs)

    def test_parent_swap_with_outside_same_name_sentinel_unchanged(self) -> None:
        if sys.platform != "win32":
            self.skipTest("junction race Windows-only")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "parent"
            outside = root / "outside"
            saved = root / "parent-saved"
            logs.mkdir()
            outside.mkdir()
            outside_db = outside / "race.sqlite3"
            outside_sentinel = b"OUTSIDE_DB_SENTINEL_BYTES"
            outside_db.write_bytes(outside_sentinel)
            db = logs / "race.sqlite3"

            def _swap(_path: Path) -> None:
                logs.rename(saved)
                completed = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(logs), str(outside)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    raise unittest.SkipTest("junction unavailable")

            prev = sec._test_pre_connect_hook
            sec._test_pre_connect_hook = _swap
            try:
                with self.assertRaises(
                    (MemoryValidationError, MemoryStoreUnavailableError)
                ):
                    SqliteMemoryStore(db)
                self.assertEqual(outside_db.read_bytes(), outside_sentinel)
            finally:
                sec._test_pre_connect_hook = prev
                if logs.exists() and getattr(logs, "is_junction", lambda: False)():
                    try:
                        logs.rmdir()
                    except OSError:
                        pass
                if saved.exists() and not logs.exists():
                    saved.rename(logs)


class C2RuntimeIdentityTests(unittest.TestCase):
    def test_parent_identity_change_rejects_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "live.sqlite3"
            store = SqliteMemoryStore(db)
            try:
                store.add(
                    NewMemory(
                        text="keep",
                        category=MemoryCategory.PREFERENCE,
                        source=MemorySource.USER_CONFIRMED,
                    )
                )
                fake = sec.TrustedDbParent(
                    path=store._trusted_parent.path,
                    identity=(-1, -2),
                    resolved=store._trusted_parent.resolved,
                )
                store._trusted_parent = fake
                with self.assertRaises(MemoryStoreUnavailableError):
                    store.add(
                        NewMemory(
                            text="more",
                            category=MemoryCategory.PREFERENCE,
                            source=MemorySource.USER_CONFIRMED,
                        )
                    )
                with self.assertRaises(MemoryStoreUnavailableError):
                    store.clear()
                with self.assertRaises(MemoryStoreUnavailableError):
                    store.vacuum_offline()
            finally:
                store.close()

    def test_db_file_identity_change_rejects_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "ident.sqlite3"
            store = SqliteMemoryStore(db)
            try:
                store.add(
                    NewMemory(
                        text="keep",
                        category=MemoryCategory.PREFERENCE,
                        source=MemorySource.USER_CONFIRMED,
                    )
                )
                # 同父目录：伪造已绑定的数据库文件身份（不伪造父目录）
                store._trusted_db_identity = (-9, -9)
                with self.assertRaises(MemoryStoreUnavailableError):
                    store.add(
                        NewMemory(
                            text="after-replace",
                            category=MemoryCategory.PREFERENCE,
                            source=MemorySource.USER_CONFIRMED,
                        )
                    )
            finally:
                store.close()


class C2PragmaCapabilityTests(unittest.TestCase):
    def test_readwrite_pragmas_and_attach_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "pragma.sqlite3"
            opened = sec.connect_memory_db(db, readonly=False)
            try:
                conn = opened.conn
                self.assertEqual(sec.read_pragma(conn, "foreign_keys"), 1)
                self.assertEqual(sec.read_pragma(conn, "trusted_schema"), 0)
                self.assertEqual(sec.read_pragma(conn, "temp_store"), 2)
                self.assertEqual(str(sec.read_pragma(conn, "journal_mode")).lower(), "delete")
                self.assertEqual(sec.read_pragma(conn, "secure_delete"), 1)
                attach_target = Path(tmp) / "other.sqlite3"
                attach_target.write_bytes(b"")
                self.assertTrue(sec.attach_should_be_rejected(conn, attach_target))
                busy = sec.read_pragma(conn, "busy_timeout")
                self.assertIsNotNone(busy)
                self.assertLessEqual(int(busy), int(sec.DEFAULT_BUSY_TIMEOUT_SEC * 1000))
            finally:
                opened.conn.close()

    def test_busy_timeout_bounds(self) -> None:
        with self.assertRaises(MemoryValidationError):
            sec.validate_busy_timeout_sec(6)
        with self.assertRaises(MemoryValidationError):
            sec.validate_busy_timeout_sec(float("nan"))


class C2ReadonlyTests(unittest.TestCase):
    def test_missing_db_zero_create_and_query_only(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                open_readonly_connection(missing)
            self.assertFalse(missing.exists())

            db = Path(tmp) / "ro.sqlite3"
            _seed(db)
            before = {p.name for p in Path(tmp).iterdir()}
            conn = open_readonly_connection(db)
            try:
                self.assertEqual(sec.read_pragma(conn, "query_only"), 1)
                with self.assertRaises(sqlite3.Error):
                    conn.execute("CREATE TABLE hack(x INT)")
                with self.assertRaises(sqlite3.Error):
                    conn.execute("UPDATE memories SET text='x' WHERE 1=1")
                self.assertTrue(
                    sec.attach_should_be_rejected(conn, Path(tmp) / "x.sqlite3")
                )
            finally:
                conn.close()
            after = {p.name for p in Path(tmp).iterdir()}
            self.assertEqual(before, after)
            for suffix in ("-journal", "-wal", "-shm"):
                self.assertFalse((Path(str(db) + suffix)).exists())


class C2PlanV2Tests(unittest.TestCase):
    def test_plan_blob_omits_text_and_db_path(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "plan.sqlite3"
            texts = ["SENTINEL_ONE_UNIQUE", "SENTINEL_TWO_UNIQUE"]
            ids = _seed(db, texts)
            conn = open_readonly_connection(db)
            try:
                entries, _ = load_audit_entries(conn, ids)
                plan_a = build_confirmation_plan(
                    db_path=db, entries=entries, plan_nonce="n" * 16
                )
                plan_b = build_confirmation_plan(
                    db_path=db, entries=entries, plan_nonce="m" * 16
                )
            finally:
                conn.close()
            blob = json.dumps(plan_a, ensure_ascii=False)
            self.assertNotIn(texts[0], blob)
            self.assertNotIn(texts[1], blob)
            self.assertNotIn(str(db.resolve()), blob)
            self.assertNotIn("dedupe_key", blob)
            self.assertEqual(plan_a["version"], PLAN_VERSION)
            self.assertNotEqual(
                plan_a["entries"][0]["row_fingerprint"],
                plan_b["entries"][0]["row_fingerprint"],
            )
            # 不同 nonce → 指纹不同（同一正文）
            fp1 = row_fingerprint(plan_nonce="n" * 16, entry=entries[0])
            fp2 = row_fingerprint(plan_nonce="m" * 16, entry=entries[0])
            self.assertNotEqual(fp1, fp2)

    def test_wrong_db_tamper_dup_stale_v1_zero_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "a.sqlite3"
            other = Path(tmp) / "b.sqlite3"
            ids = _seed(db)
            _seed(other, ["other1", "other2"])
            plan_path = Path(tmp) / "good.json"
            self.assertEqual(
                diag_cli.main(
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
                ),
                0,
            )
            # 错库
            self.assertEqual(
                diag_cli.main(
                    ["--db", str(other), "--execute", "--plan", str(plan_path)]
                ),
                2,
            )
            # 篡改指纹与 digest
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["entries"][0]["row_fingerprint"] = "0" * 64
            plan["digest"] = "0" * 64
            bad = Path(tmp) / "tamper.json"
            bad.write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(
                diag_cli.main(["--db", str(db), "--execute", "--plan", str(bad)]),
                2,
            )
            # v1
            v1 = {
                "version": 1,
                "db": str(db.resolve()),
                "entries": [
                    {
                        "id": ids[0],
                        "text": "x",
                        "source": "model_inferred",
                        "updated_at": "t",
                    }
                ],
                "digest": "x",
            }
            v1_path = Path(tmp) / "v1.json"
            v1_path.write_text(json.dumps(v1), encoding="utf-8")
            self.assertEqual(
                diag_cli.main(["--db", str(db), "--execute", "--plan", str(v1_path)]),
                2,
            )
            conn = open_readonly_connection(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 2)
            finally:
                conn.close()

            # 正确计划精确删除
            self.assertEqual(
                diag_cli.main(
                    ["--db", str(db), "--execute", "--plan", str(plan_path)]
                ),
                0,
            )
            conn2 = open_readonly_connection(db)
            try:
                self.assertEqual(conn2.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)
            finally:
                conn2.close()
            # 计划不能改变 CLI 库：other 仍有 2 行
            conn3 = open_readonly_connection(other)
            try:
                self.assertEqual(conn3.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 2)
            finally:
                conn3.close()

    def test_plan_file_symlink_not_overwritten(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "s.sqlite3"
            ids = _seed(db, ["only"])
            real = Path(tmp) / "real-plan.json"
            real.write_text("KEEP", encoding="utf-8")
            link = Path(tmp) / "plan-link.json"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlink not available")
            code = diag_cli.main(
                ["--db", str(db), "--id", ids[0], "--write-plan", str(link)]
            )
            self.assertEqual(code, 2)
            self.assertEqual(real.read_text(encoding="utf-8"), "KEEP")


class C2FaultMappingTests(unittest.TestCase):
    def test_connect_failure_maps_without_path_or_body_in_logs(self) -> None:
        logger = logging.getLogger("test.c2.fault")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        records: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = Capture()
        logger.addHandler(handler)
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "fault.sqlite3"
            with patch.object(
                sec.sqlite3,
                "connect",
                side_effect=sqlite3.OperationalError("disk I/O secret-path-should-hide"),
            ):
                with self.assertRaises(MemoryStoreUnavailableError):
                    SqliteMemoryStore(db, logger=logger)
        logger.removeHandler(handler)
        self.assertTrue(records)
        blob = " ".join(h.getMessage() for h in records)
        self.assertNotIn("secret-path", blob)
        self.assertNotIn(str(db), blob)
        self.assertIn("OperationalError", blob)

    def test_keyboard_interrupt_not_swallowed_on_open(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "ki.sqlite3"

            def _raise(_path: Path) -> None:
                raise KeyboardInterrupt()

            prev = sec._test_pre_connect_hook
            sec._test_pre_connect_hook = _raise
            try:
                with self.assertRaises(KeyboardInterrupt):
                    SqliteMemoryStore(db)
            finally:
                sec._test_pre_connect_hook = prev


class C2ReviewF1F5Tests(unittest.TestCase):
    """Codex Round 1 F1–F5 返修确定性行为测试。"""

    def test_f1_pragma_not_before_revalidate_sentinel_intact(self) -> None:
        class FakeCursor:
            def __init__(self, value=None):
                self.value = value

            def fetchone(self):
                return None if self.value is None else (self.value,)

        class MutatingFakeConnection:
            def __init__(self, outside: Path):
                self.outside = outside
                self.row_factory = None
                self.commands: list[str] = []

            def execute(self, sql, *_args):
                command = str(sql)
                self.commands.append(command)
                if command.upper() == "PRAGMA JOURNAL_MODE=DELETE":
                    self.outside.write_bytes(b"MUTATED_BEFORE_REVALIDATE")
                    return FakeCursor("delete")
                upper = command.upper()
                if upper == "PRAGMA SECURE_DELETE":
                    return FakeCursor(1)
                if upper == "PRAGMA FOREIGN_KEYS":
                    return FakeCursor(1)
                if upper == "PRAGMA TRUSTED_SCHEMA":
                    return FakeCursor(0)
                if upper == "PRAGMA TEMP_STORE":
                    return FakeCursor(2)
                if upper == "PRAGMA QUERY_ONLY":
                    return FakeCursor(1)
                return FakeCursor()

            def enable_load_extension(self, _enabled):
                return None

            def setlimit(self, _category, _value):
                return 0

            def close(self):
                return None

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "memory.sqlite3"
            outside = root / "outside.sqlite3"
            sentinel = b"OUTSIDE_SENTINEL"
            outside.write_bytes(sentinel)
            fake = MutatingFakeConnection(outside)

            def swap(_path: Path) -> None:
                try:
                    if target.exists() or target.is_symlink():
                        target.unlink()
                    os.link(outside, target)
                except (OSError, NotImplementedError):
                    raise unittest.SkipTest("hardlink unavailable")

            prev = sec._test_pre_connect_hook
            sec._test_pre_connect_hook = swap
            try:
                with patch.object(sec.sqlite3, "connect", return_value=fake):
                    with self.assertRaises(
                        (MemoryValidationError, MemoryStoreUnavailableError)
                    ):
                        sec.connect_memory_db(target, readonly=False)
                self.assertNotIn("PRAGMA journal_mode=DELETE", fake.commands)
                self.assertEqual(outside.read_bytes(), sentinel)
            finally:
                sec._test_pre_connect_hook = prev

    def test_f2_ancestor_junction_rejects_nested_create(self) -> None:
        if sys.platform != "win32":
            self.skipTest("junction Windows-only")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            junction = root / "redirect"
            outside.mkdir()
            made = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if made.returncode != 0:
                self.skipTest("junction unavailable")
            escaped = junction / "new" / "nested" / "memory.sqlite3"
            store = None
            try:
                with self.assertRaises(
                    (MemoryValidationError, MemoryStoreUnavailableError)
                ):
                    store = SqliteMemoryStore(escaped)
            finally:
                if store is not None:
                    store.close()
                try:
                    if junction.exists():
                        os.rmdir(junction)
                except OSError:
                    pass
            self.assertFalse((outside / "new").exists())
            self.assertFalse((outside / "new" / "nested" / "memory.sqlite3").exists())

    def test_f3_same_parent_file_identity_mismatch_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "a.sqlite3"
            parent = sec.bind_trusted_db_parent(Path(tmp))
            store = SqliteMemoryStore(db)
            try:
                identity = store._trusted_db_identity
                self.assertIsNotNone(identity)
                with self.assertRaises(MemoryStoreUnavailableError):
                    sec.revalidate_db_location(
                        db, parent, expected_db_identity=(-1, -1)
                    )
            finally:
                store.close()

    def test_f4_post_delete_revalidate_failure_rolls_back(self) -> None:
        from src.memory import id_audit
        from src.memory.id_audit import (
            build_confirmation_plan,
            load_audit_entries,
            open_readonly_connection,
            parse_confirmation_plan,
            verify_and_delete_plan_entries_atomic,
        )

        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "plan.sqlite3"
            store = SqliteMemoryStore(db)
            try:
                rec = store.add(
                    NewMemory(
                        text="delete-me",
                        category=MemoryCategory.PREFERENCE,
                        source=MemorySource.USER_CONFIRMED,
                    )
                ).record
            finally:
                store.close()
            ro = open_readonly_connection(db)
            try:
                entries, missing = load_audit_entries(ro, [rec.id])
                self.assertEqual(missing, [])
                plan = parse_confirmation_plan(
                    build_confirmation_plan(db_path=db, entries=entries)
                )
            finally:
                ro.close()
            opened = sec.connect_memory_db(db, readonly=False, check_same_thread=True)
            calls = {"count": 0}

            def fail_second(*_a, **_k):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise MemoryValidationError("post-delete identity failure")
                return opened.db_identity

            try:
                with patch.object(
                    id_audit, "revalidate_db_location", side_effect=fail_second
                ):
                    with self.assertRaises(MemoryValidationError):
                        verify_and_delete_plan_entries_atomic(
                            opened.conn,
                            plan,
                            db_path=opened.path,
                            trusted_parent=opened.parent,
                            trusted_db_identity=opened.db_identity,
                        )
                self.assertFalse(opened.conn.in_transaction)
                self.assertEqual(
                    opened.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                    1,
                )
            finally:
                opened.conn.close()

    def test_f4_keyboard_interrupt_rolls_back_and_propagates(self) -> None:
        from src.memory import id_audit
        from src.memory.id_audit import (
            build_confirmation_plan,
            load_audit_entries,
            open_readonly_connection,
            parse_confirmation_plan,
            verify_and_delete_plan_entries_atomic,
        )

        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "ki-plan.sqlite3"
            store = SqliteMemoryStore(db)
            try:
                rec = store.add(
                    NewMemory(
                        text="ki-row",
                        category=MemoryCategory.PREFERENCE,
                        source=MemorySource.USER_CONFIRMED,
                    )
                ).record
            finally:
                store.close()
            ro = open_readonly_connection(db)
            try:
                entries, _ = load_audit_entries(ro, [rec.id])
                plan = parse_confirmation_plan(
                    build_confirmation_plan(db_path=db, entries=entries)
                )
            finally:
                ro.close()
            opened = sec.connect_memory_db(db, readonly=False, check_same_thread=True)
            calls = {"count": 0}

            def fail_second(*_a, **_k):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise KeyboardInterrupt()
                return opened.db_identity

            try:
                with patch.object(
                    id_audit, "revalidate_db_location", side_effect=fail_second
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        verify_and_delete_plan_entries_atomic(
                            opened.conn,
                            plan,
                            db_path=opened.path,
                            trusted_parent=opened.parent,
                            trusted_db_identity=opened.db_identity,
                        )
                self.assertFalse(opened.conn.in_transaction)
                self.assertEqual(
                    opened.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                    1,
                )
            finally:
                opened.conn.close()

    def test_f5_all_writes_reject_before_sql_on_identity_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "crud.sqlite3"
            store = SqliteMemoryStore(db)
            try:
                created = store.add(
                    NewMemory(
                        text="seed",
                        category=MemoryCategory.PREFERENCE,
                        source=MemorySource.USER_CONFIRMED,
                    )
                ).record
                store.add(
                    NewMemory(
                        text="seed-2",
                        category=MemoryCategory.PREFERENCE,
                        source=MemorySource.MODEL_INFERRED,
                    )
                )
                store._trusted_db_identity = (-3, -4)

                class ConnProxy:
                    def __init__(self, inner):
                        self._inner = inner
                        self.seen: list[str] = []

                    def execute(self, sql, *args, **kwargs):
                        text = str(sql).strip().upper()
                        if text.startswith(("INSERT", "UPDATE", "DELETE", "VACUUM")):
                            self.seen.append(text.split()[0])
                        return self._inner.execute(sql, *args, **kwargs)

                    def __getattr__(self, name):
                        return getattr(self._inner, name)

                proxy = ConnProxy(store._conn)
                store._conn = proxy  # type: ignore[assignment]
                ops = [
                    lambda: store.add(
                        NewMemory(
                            text="x",
                            category=MemoryCategory.PREFERENCE,
                            source=MemorySource.MODEL_INFERRED,
                        )
                    ),
                    lambda: store.update(created.id, text="y"),
                    lambda: store.delete(created.id),
                    lambda: store.clear(),
                    lambda: store.prune_for_retention(
                        max_records=1,
                        max_age_days=None,
                        preserve_user_confirmed=False,
                        batch_limit=10,
                    ),
                    lambda: store.vacuum_offline(),
                ]
                for op in ops:
                    with self.assertRaises(MemoryStoreUnavailableError):
                        op()
                self.assertEqual(proxy.seen, [])
            finally:
                store.close()


class C2ReviewF6F7Tests(unittest.TestCase):
    """Codex Round 2/3 F6–F7 返修。"""

    def test_f6_connect_uses_rw_uri_not_rwc(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "uri.sqlite3"
            seen: list[str] = []
            real_connect = sec.sqlite3.connect

            def spy(database, *args, **kwargs):
                seen.append(str(database))
                return real_connect(database, *args, **kwargs)

            with patch.object(sec.sqlite3, "connect", side_effect=spy):
                opened = sec.connect_memory_db(db, readonly=False)
                try:
                    self.assertTrue(any("mode=rw" in s for s in seen))
                    self.assertFalse(any("mode=rwc" in s for s in seen))
                    self.assertTrue(any("cache=private" in s for s in seen))
                finally:
                    opened.conn.close()

    def test_f7_commit_failure_rolls_back_no_hanging_txn(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "txn.sqlite3"
            store = SqliteMemoryStore(db)
            try:
                before = store.count_memories()
                inner = store._conn
                rollbacks = {"n": 0}

                class CommitFails:
                    def __init__(self, real):
                        self._real = real

                    def execute(self, *a, **k):
                        return self._real.execute(*a, **k)

                    def commit(self):
                        raise sqlite3.OperationalError("simulated commit failure")

                    def rollback(self):
                        rollbacks["n"] += 1
                        return self._real.rollback()

                    def __getattr__(self, name):
                        return getattr(self._real, name)

                store._conn = CommitFails(inner)  # type: ignore[assignment]
                with self.assertRaises(MemoryStoreUnavailableError):
                    store.add(
                        NewMemory(
                            text="should-not-stick",
                            category=MemoryCategory.PREFERENCE,
                            source=MemorySource.USER_CONFIRMED,
                        )
                    )
                self.assertFalse(inner.in_transaction)
                self.assertEqual(rollbacks["n"], 1)
                store._conn = inner
                self.assertEqual(store.count_memories(), before)
            finally:
                store.close()
            # 新连接也看不到新增
            reopened = SqliteMemoryStore(db)
            try:
                self.assertEqual(reopened.count_memories(), 0)
            finally:
                reopened.close()

    def test_f7_commit_keyboard_interrupt_rolls_back(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "ki-txn.sqlite3"
            store = SqliteMemoryStore(db)
            try:
                inner = store._conn
                rollbacks = {"n": 0}

                class CommitKI:
                    def __init__(self, real):
                        self._real = real

                    def execute(self, *a, **k):
                        return self._real.execute(*a, **k)

                    def commit(self):
                        raise KeyboardInterrupt()

                    def rollback(self):
                        rollbacks["n"] += 1
                        return self._real.rollback()

                    def __getattr__(self, name):
                        return getattr(self._real, name)

                store._conn = CommitKI(inner)  # type: ignore[assignment]
                with self.assertRaises(KeyboardInterrupt):
                    store.add(
                        NewMemory(
                            text="ki-row",
                            category=MemoryCategory.PREFERENCE,
                            source=MemorySource.USER_CONFIRMED,
                        )
                    )
                self.assertFalse(inner.in_transaction)
                self.assertEqual(rollbacks["n"], 1)
                store._conn = inner
                self.assertEqual(store.count_memories(), 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
