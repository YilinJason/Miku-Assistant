# -*- coding: utf-8 -*-
"""按 ID 只读审计与确认计划删除（全有或全无；计划 v2 无正文、不可重定向库）。"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from src.memory.errors import MemoryStoreUnavailableError, MemoryValidationError
from src.memory.sqlite_security import (
    DEFAULT_BUSY_TIMEOUT_SEC,
    OpenedMemoryDb,
    bind_trusted_db_parent,
    connect_memory_db,
    database_path_fingerprint,
    revalidate_db_location,
)

PLAN_VERSION = 2
PLAN_VERSION_V1 = 1
_V1_MIGRATION_HINT = (
    "plan version 1 is rejected: regenerate with diagnose_memory_ids.py "
    "(v2 plans omit memory text and cannot redirect --db)"
)


@dataclass(frozen=True)
class AuditEntry:
    id: str
    text: str
    source: str
    updated_at: str

    def to_console_dict(self) -> dict[str, str]:
        """仅供用户显式 dry-run 控制台展示；不得写入计划文件。"""
        return {
            "id": self.id,
            "text": self.text,
            "source": self.source,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class PlanEntryV2:
    id: str
    updated_at: str
    row_fingerprint: str


@dataclass(frozen=True)
class ConfirmationPlanV2:
    version: int
    plan_nonce: str
    db_fingerprint: str
    entries: Tuple[PlanEntryV2, ...]
    digest: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "plan_nonce": self.plan_nonce,
            "db_fingerprint": self.db_fingerprint,
            "entries": [
                {
                    "id": e.id,
                    "updated_at": e.updated_at,
                    "row_fingerprint": e.row_fingerprint,
                }
                for e in self.entries
            ],
            "digest": self.digest,
        }


def _dt_to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def open_readonly_connection(
    db_path: Path | str,
    *,
    busy_timeout_sec: float = DEFAULT_BUSY_TIMEOUT_SEC,
) -> sqlite3.Connection:
    """
    以 URI ``mode=ro&cache=private`` 打开已有库；不创建父目录、不改 journal、不跑 schema。
    """
    opened = connect_memory_db(
        db_path,
        readonly=True,
        busy_timeout_sec=busy_timeout_sec,
        check_same_thread=True,
    )
    return opened.conn


def open_readonly_memory_db(
    db_path: Path | str,
    *,
    busy_timeout_sec: float = DEFAULT_BUSY_TIMEOUT_SEC,
) -> OpenedMemoryDb:
    return connect_memory_db(
        db_path,
        readonly=True,
        busy_timeout_sec=busy_timeout_sec,
        check_same_thread=True,
    )


def count_memories_readonly(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    except sqlite3.Error as exc:
        raise MemoryStoreUnavailableError("failed to count memories") from exc
    return int(row["c"] if row is not None else 0)


def fetch_audit_entry(
    conn: sqlite3.Connection, memory_id: str
) -> Optional[AuditEntry]:
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise MemoryValidationError("memory id must be non-empty str")
    mid = memory_id.strip()
    try:
        row = conn.execute(
            "SELECT id, text, source, updated_at FROM memories WHERE id = ?",
            (mid,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise MemoryStoreUnavailableError("failed to fetch memory") from exc
    if row is None:
        return None
    updated = row["updated_at"]
    if isinstance(updated, datetime):
        updated_s = _dt_to_iso(updated)
    else:
        updated_s = str(updated)
    return AuditEntry(
        id=str(row["id"]),
        text=str(row["text"]),
        source=str(row["source"]),
        updated_at=updated_s,
    )


def load_audit_entries(
    conn: sqlite3.Connection, ids: Sequence[str]
) -> Tuple[List[AuditEntry], List[str]]:
    found: List[AuditEntry] = []
    missing: List[str] = []
    seen: set[str] = set()
    for raw in ids:
        mid = raw.strip() if isinstance(raw, str) else ""
        if not mid:
            raise MemoryValidationError("memory id must be non-empty str")
        if mid in seen:
            raise MemoryValidationError(f"duplicate memory id in request: {mid}")
        seen.add(mid)
        entry = fetch_audit_entry(conn, mid)
        if entry is None:
            missing.append(mid)
        else:
            found.append(entry)
    return found, missing


def row_fingerprint(*, plan_nonce: str, entry: AuditEntry) -> str:
    """带计划 nonce 的行指纹；低熵正文不得以无盐裸哈希进入计划。"""
    if not isinstance(plan_nonce, str) or len(plan_nonce) < 16:
        raise MemoryValidationError("plan_nonce must be a non-trivial str")
    payload = "\0".join(
        (plan_nonce, entry.id, entry.text, entry.source, entry.updated_at)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_digest_v2(
    *,
    plan_nonce: str,
    db_fingerprint: str,
    entries: Sequence[PlanEntryV2],
) -> str:
    canonical = {
        "version": PLAN_VERSION,
        "plan_nonce": plan_nonce,
        "db_fingerprint": db_fingerprint,
        "entries": [
            {
                "id": e.id,
                "updated_at": e.updated_at,
                "row_fingerprint": e.row_fingerprint,
            }
            for e in entries
        ],
    }
    blob = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_confirmation_plan(
    *,
    db_path: Path | str,
    entries: Sequence[AuditEntry],
    plan_nonce: Optional[str] = None,
) -> dict[str, Any]:
    """构建计划 v2 JSON（无 text / dedupe_key / 数据库路径）。"""
    if not entries:
        raise MemoryValidationError("confirmation plan requires at least one entry")
    ids = [e.id for e in entries]
    if len(ids) != len(set(ids)):
        raise MemoryValidationError("confirmation plan contains duplicate ids")
    nonce = plan_nonce if plan_nonce is not None else secrets.token_hex(16)
    if not isinstance(nonce, str) or len(nonce) < 16:
        raise MemoryValidationError("plan_nonce must be a non-trivial str")
    db_fp = database_path_fingerprint(Path(db_path))
    plan_entries = tuple(
        PlanEntryV2(
            id=e.id,
            updated_at=e.updated_at,
            row_fingerprint=row_fingerprint(plan_nonce=nonce, entry=e),
        )
        for e in entries
    )
    digest = plan_digest_v2(
        plan_nonce=nonce, db_fingerprint=db_fp, entries=plan_entries
    )
    plan = ConfirmationPlanV2(
        version=PLAN_VERSION,
        plan_nonce=nonce,
        db_fingerprint=db_fp,
        entries=plan_entries,
        digest=digest,
    )
    return plan.to_json_dict()


def parse_confirmation_plan(raw: Any) -> ConfirmationPlanV2:
    if not isinstance(raw, Mapping):
        raise MemoryValidationError("plan must be a mapping")
    version = raw.get("version")
    if version == PLAN_VERSION_V1:
        raise MemoryValidationError(_V1_MIGRATION_HINT)
    if version != PLAN_VERSION:
        raise MemoryValidationError("unsupported plan version")
    if "db" in raw:
        raise MemoryValidationError("plan must not contain database path")
    nonce = raw.get("plan_nonce")
    if not isinstance(nonce, str) or len(nonce) < 16:
        raise MemoryValidationError("plan.plan_nonce must be a non-trivial str")
    db_fp = raw.get("db_fingerprint")
    if not isinstance(db_fp, str) or not db_fp.strip():
        raise MemoryValidationError("plan.db_fingerprint must be non-empty str")
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise MemoryValidationError("plan.entries must be a non-empty list")
    entries: List[PlanEntryV2] = []
    seen: set[str] = set()
    for item in entries_raw:
        if not isinstance(item, Mapping):
            raise MemoryValidationError("plan entry must be a mapping")
        if "text" in item or "dedupe_key" in item:
            raise MemoryValidationError("plan entry must not contain memory text fields")
        mid = item.get("id")
        updated = item.get("updated_at")
        fp = item.get("row_fingerprint")
        if not all(isinstance(v, str) and v for v in (mid, updated, fp)):
            raise MemoryValidationError("plan entry fields must be non-empty str")
        assert isinstance(mid, str)
        if mid in seen:
            raise MemoryValidationError(f"plan contains duplicate id: {mid}")
        seen.add(mid)
        entries.append(
            PlanEntryV2(id=mid, updated_at=updated, row_fingerprint=fp)
        )
    digest = raw.get("digest")
    if not isinstance(digest, str) or not digest.strip():
        raise MemoryValidationError("plan.digest must be non-empty str")
    expected = plan_digest_v2(
        plan_nonce=nonce, db_fingerprint=db_fp, entries=entries
    )
    if digest != expected:
        raise MemoryValidationError("plan digest mismatch")
    return ConfirmationPlanV2(
        version=PLAN_VERSION,
        plan_nonce=nonce,
        db_fingerprint=db_fp,
        entries=tuple(entries),
        digest=digest,
    )


def write_confirmation_plan_file(path: Path | str, plan: Mapping[str, Any]) -> None:
    """
    写入计划 JSON：父目录须已存在；不跟随 symlink/reparse；不以静默方式覆盖已有文件。
    """
    target = Path(path)
    validate_plan_output_path(target)
    payload = json.dumps(dict(plan), ensure_ascii=False, indent=2) + "\n"
    data = payload.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(str(target), flags, 0o600)
    except FileExistsError as exc:
        raise MemoryValidationError("plan output file already exists") from exc
    except OSError as exc:
        raise MemoryStoreUnavailableError("failed to create plan file") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        try:
            target.unlink(missing_ok=True)
        except TypeError:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        raise MemoryStoreUnavailableError("failed to write plan file") from exc


def validate_plan_output_path(path: Path) -> None:
    from src.memory import sqlite_security as _sec

    if path.exists() or path.is_symlink() or _sec._is_reparse_or_link(path):
        raise MemoryValidationError("plan output must not already exist or be a link")
    bind_trusted_db_parent(path.parent)


def verify_plan_against_cli_db(
    plan: ConfirmationPlanV2, db_path: Path | str
) -> None:
    """计划只能核对 CLI/默认选定库指纹，绝不能替换执行目标。"""
    actual = database_path_fingerprint(Path(db_path))
    if actual != plan.db_fingerprint:
        raise MemoryValidationError("plan db_fingerprint does not match --db")


def verify_and_delete_plan_entries_atomic(
    conn: sqlite3.Connection,
    plan: ConfirmationPlanV2,
    *,
    db_path: Path,
    trusted_parent,
    trusted_db_identity: Tuple[int, int],
) -> int:
    """
    单一事务：复验库身份 → 重读行并核对指纹 → 条件删除；任一步失败则零删除。

    ``BEGIN IMMEDIATE`` 成功后任意 ``BaseException`` 都先尽力 rollback，再按契约重抛；
    不吞 ``KeyboardInterrupt`` / ``SystemExit``。
    """
    if not plan.entries:
        raise MemoryValidationError("no entries to delete")
    ids = [e.id for e in plan.entries]
    if len(ids) != len(set(ids)):
        raise MemoryValidationError("duplicate ids in delete set")

    begun = False
    try:
        revalidate_db_location(
            db_path,
            trusted_parent,
            expected_db_identity=trusted_db_identity,
        )
        conn.execute("BEGIN IMMEDIATE")
        begun = True
        deleted = 0
        for entry in plan.entries:
            current = fetch_audit_entry(conn, entry.id)
            if current is None:
                raise MemoryValidationError(
                    f"memory missing at execute: {entry.id}"
                )
            if current.updated_at != entry.updated_at:
                raise MemoryValidationError(f"memory stale at execute: {entry.id}")
            expected_fp = row_fingerprint(plan_nonce=plan.plan_nonce, entry=current)
            if expected_fp != entry.row_fingerprint:
                raise MemoryValidationError(f"memory stale at execute: {entry.id}")
            cur = conn.execute(
                """
                DELETE FROM memories
                WHERE id = ? AND text = ? AND source = ? AND updated_at = ?
                """,
                (current.id, current.text, current.source, current.updated_at),
            )
            if cur.rowcount != 1:
                raise MemoryValidationError(
                    f"conditional delete failed (missing or stale): {entry.id}"
                )
            deleted += 1
        revalidate_db_location(
            db_path,
            trusted_parent,
            expected_db_identity=trusted_db_identity,
        )
        conn.execute("COMMIT")
        begun = False
        return deleted
    except BaseException as exc:
        if begun:
            try:
                conn.rollback()
            except Exception:
                pass
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, (MemoryValidationError, MemoryStoreUnavailableError)):
            raise
        if isinstance(exc, sqlite3.Error):
            raise MemoryStoreUnavailableError("atomic delete failed") from exc
        raise


# --- 兼容旧测试名：保留符号但改为基于指纹的删除封装 ---


def verify_entries_against_db(
    conn: sqlite3.Connection, entries: Sequence[AuditEntry]
) -> None:
    """旧 API：用 AuditEntry 全文比对（仅测试/内部）；生产执行走计划指纹。"""
    for entry in entries:
        current = fetch_audit_entry(conn, entry.id)
        if current is None:
            raise MemoryValidationError(f"memory missing at execute: {entry.id}")
        if (
            current.text != entry.text
            or current.source != entry.source
            or current.updated_at != entry.updated_at
        ):
            raise MemoryValidationError(f"memory stale at execute: {entry.id}")


def delete_entries_atomic(
    conn: sqlite3.Connection, entries: Sequence[AuditEntry]
) -> int:
    """旧 API：条件删除含正文匹配；生产 CLI 使用 verify_and_delete_plan_entries_atomic。"""
    if not entries:
        raise MemoryValidationError("no entries to delete")
    ids = [e.id for e in entries]
    if len(ids) != len(set(ids)):
        raise MemoryValidationError("duplicate ids in delete set")
    try:
        conn.execute("BEGIN IMMEDIATE")
        deleted = 0
        for entry in entries:
            cur = conn.execute(
                """
                DELETE FROM memories
                WHERE id = ? AND text = ? AND source = ? AND updated_at = ?
                """,
                (entry.id, entry.text, entry.source, entry.updated_at),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise MemoryValidationError(
                    f"conditional delete failed (missing or stale): {entry.id}"
                )
            deleted += 1
        conn.execute("COMMIT")
        return deleted
    except MemoryValidationError:
        raise
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise MemoryStoreUnavailableError("atomic delete failed") from exc
