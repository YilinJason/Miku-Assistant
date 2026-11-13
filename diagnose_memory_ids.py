# -*- coding: utf-8 -*-
"""长期记忆按 ID 只读审计 / 确认计划定点删除。

默认 dry-run：SQLite URI ``mode=ro&cache=private``，不创建目录、不改 schema/journal。
删除必须 ``--execute --plan <file>``：始终以 CLI ``--db``（或固定默认）为目标，
校验计划 v2 指纹后单事务条件删除；计划不得含正文或重定向数据库。

禁止对 live 测试库在未评审前执行删除。

用法::

    python diagnose_memory_ids.py --count-only
    python diagnose_memory_ids.py --id <uuid> [--write-plan plan.json]
    python diagnose_memory_ids.py --execute --plan plan.json --db <path>
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
    load_audit_entries,
    open_readonly_memory_db,
    parse_confirmation_plan,
    verify_and_delete_plan_entries_atomic,
    verify_plan_against_cli_db,
    write_confirmation_plan_file,
)
from src.memory.sqlite_security import connect_memory_db

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
        help="dry-run 时把确认计划写入该 JSON 文件（v2，无正文）",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=None,
        help="执行删除时必须提供的确认计划 JSON（v2）",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="按 --plan 单事务条件删除；始终使用 --db，不得由计划改库",
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
            plan = parse_confirmation_plan(plan_raw)
            # 始终使用 CLI --db（含代码内固定默认）；计划不能替换目标
            verify_plan_against_cli_db(plan, db_path)
        except (OSError, json.JSONDecodeError, MemoryValidationError) as exc:
            print(f"计划无效: {exc}", file=sys.stderr)
            return 2
        if not db_path.is_file():
            print(f"数据库不存在: {db_path}", file=sys.stderr)
            return 2
        opened = None
        try:
            opened = connect_memory_db(db_path, readonly=False, check_same_thread=True)
            deleted = verify_and_delete_plan_entries_atomic(
                opened.conn,
                plan,
                db_path=opened.path,
                trusted_parent=opened.parent,
                trusted_db_identity=opened.db_identity,
            )
            print(
                f"mode=EXECUTE db={db_path.as_posix()} "
                f"deleted={deleted} digest={plan.digest}"
            )
            for entry in plan.entries:
                print(f"deleted id={entry.id}")
            return 0
        except (MemoryValidationError, MemoryStoreUnavailableError) as exc:
            print(f"执行失败（零删除）: {type(exc).__name__}", file=sys.stderr)
            return 3
        finally:
            if opened is not None:
                try:
                    opened.conn.close()
                except sqlite3.Error:
                    pass

    # dry-run / count-only：只读
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 2
    opened_ro = None
    try:
        opened_ro = open_readonly_memory_db(db_path)
        conn_ro = opened_ro.conn
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
            write_confirmation_plan_file(args.write_plan, plan)
            print(f"wrote_plan={args.write_plan.as_posix()}")
        print(
            "dry-run: 未删除。执行请：python diagnose_memory_ids.py "
            "--execute --plan <plan.json> --db <path>"
        )
        return 0
    except (MemoryValidationError, MemoryStoreUnavailableError, OSError) as exc:
        print(f"错误: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        if opened_ro is not None:
            try:
                opened_ro.conn.close()
            except sqlite3.Error:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
