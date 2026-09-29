# -*- coding: utf-8 -*-
"""长期记忆按 ID 只读审计 / 确认计划定点删除。

默认 dry-run：SQLite URI ``mode=ro``，不创建目录、不改 schema/journal。
删除必须 ``--execute --plan <file>``：先完整验证计划，再单事务条件删除；
任一缺失/变化/重复 → 零删除失败。

禁止对 live 测试库在未评审前执行删除。

用法::

    python diagnose_memory_ids.py --count-only
    python diagnose_memory_ids.py --id <uuid> [--write-plan plan.json]
    python diagnose_memory_ids.py --execute --plan plan.json
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent))

from src.memory.errors import MemoryStoreUnavailableError, MemoryValidationError
from src.memory.id_audit import (
    build_confirmation_plan,
    count_memories_readonly,
    delete_entries_atomic,
    load_audit_entries,
    open_readonly_connection,
    parse_confirmation_plan,
    verify_entries_against_db,
)

DEFAULT_DB = Path("tmp") / "memory-live-test.sqlite3"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按完整 memory ID 只读审计或确认计划删除（默认 dry-run）"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite 路径（默认 {DEFAULT_DB.as_posix()}）",
    )
    parser.add_argument(
        "--id",
        action="append",
        default=[],
        dest="ids",
        help="完整 memory ID；可重复。dry-run 审计时使用。",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="只打印库内记录总数（只读 URI）",
    )
    parser.add_argument(
        "--write-plan",
        type=Path,
        default=None,
        help="dry-run 时把确认计划写入该 JSON 文件",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=None,
        help="执行删除时必须提供的确认计划 JSON",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="按 --plan 单事务条件删除；不得仅用 --id 执行",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    db_path: Path = args.db

    if args.execute:
        if args.plan is None:
            print("执行删除必须提供 --plan <file>", file=sys.stderr)
            return 2
        if args.ids:
            print("执行模式请勿再传 --id；以计划为准", file=sys.stderr)
            return 2
        try:
            plan_raw = json.loads(args.plan.read_text(encoding="utf-8"))
            plan_db, entries, digest = parse_confirmation_plan(plan_raw)
        except (OSError, json.JSONDecodeError, MemoryValidationError) as exc:
            print(f"计划无效: {exc}", file=sys.stderr)
            return 2
        if plan_db.resolve() != db_path.resolve():
            # 允许 --db 与计划一致；若用户未改 --db 则用计划路径
            if args.db == DEFAULT_DB:
                db_path = plan_db
            else:
                print(
                    f"计划 db 与 --db 不一致: plan={plan_db} db={db_path}",
                    file=sys.stderr,
                )
                return 2
        if not db_path.is_file():
            print(f"数据库不存在: {db_path}", file=sys.stderr)
            return 2
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            verify_entries_against_db(conn, entries)
            deleted = delete_entries_atomic(conn, entries)
            print(
                f"mode=EXECUTE db={db_path.as_posix()} "
                f"deleted={deleted} digest={digest}"
            )
            for entry in entries:
                print(f"deleted id={entry.id}")
            return 0
        except (MemoryValidationError, MemoryStoreUnavailableError) as exc:
            print(f"执行失败（零删除）: {exc}", file=sys.stderr)
            return 3
        finally:
            if conn is not None:
                conn.close()

    # dry-run / count-only：只读
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 2
    conn_ro: Optional[sqlite3.Connection] = None
    try:
        conn_ro = open_readonly_connection(db_path)
        total = count_memories_readonly(conn_ro)
        if args.count_only:
            print(f"db={db_path.as_posix()} count={total}")
            return 0
        ids: List[str] = list(args.ids or [])
        if not ids:
            print(
                "请用 --id 指定完整 memory ID，或使用 --count-only",
                file=sys.stderr,
            )
            return 2
        entries, missing = load_audit_entries(conn_ro, ids)
        if missing:
            print("以下 ID 不存在:", file=sys.stderr)
            for mid in missing:
                print(f"  missing id={mid}", file=sys.stderr)
            return 2
        plan = build_confirmation_plan(db_path=db_path, entries=entries)
        print(f"mode=DRY-RUN db={db_path.as_posix()} total={total}")
        print(f"plan_digest={plan['digest']}")
        for entry in entries:
            print(f"id={entry.id}")
            print(f"  source={entry.source}")
            print(f"  updated_at={entry.updated_at}")
            print(f"  text={entry.text}")
        if args.write_plan is not None:
            args.write_plan.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"wrote_plan={args.write_plan.as_posix()}")
        print(
            "dry-run: 未删除。执行请：python diagnose_memory_ids.py "
            "--execute --plan <plan.json>"
        )
        return 0
    except (MemoryValidationError, MemoryStoreUnavailableError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    finally:
        if conn_ro is not None:
            conn_ro.close()


if __name__ == "__main__":
    raise SystemExit(main())
