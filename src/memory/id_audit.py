"""按 ID 只读审计与确认计划删除（全有或全无）。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.memory.errors import MemoryStoreUnavailableError, MemoryValidationError

PLAN_VERSION = 1


@dataclass(frozen=True)
class AuditEntry:
    id: str
    text: str
    source: str
    updated_at: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "text": self.text,
            "source": self.source,
            "updated_at": self.updated_at,
        }


def _dt_to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def open_readonly_connection(db_path: Path | str) -> sqlite3.Connection:
    """
    以 SQLite URI ``mode=ro`` 打开已有库；不创建父目录、不改 journal、不跑 schema。
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise MemoryStoreUnavailableError("failed to open readonly memory db") from exc
    conn.row_factory = sqlite3.Row
    return conn


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


def plan_digest(entries: Sequence[AuditEntry]) -> str:
    canonical = [
        {
            "id": e.id,
            "text": e.text,
            "source": e.source,
            "updated_at": e.updated_at,
        }
        for e in entries
    ]
    blob = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_confirmation_plan(
    *,
    db_path: Path | str,
    entries: Sequence[AuditEntry],
) -> dict[str, Any]:
    if not entries:
        raise MemoryValidationError("confirmation plan requires at least one entry")
    ids = [e.id for e in entries]
    if len(ids) != len(set(ids)):
        raise MemoryValidationError("confirmation plan contains duplicate ids")
    return {
        "version": PLAN_VERSION,
        "db": str(Path(db_path).resolve()),
        "entries": [e.to_public_dict() for e in entries],
        "digest": plan_digest(entries),
    }


def parse_confirmation_plan(raw: Any) -> Tuple[Path, List[AuditEntry], str]:
    if not isinstance(raw, Mapping):
        raise MemoryValidationError("plan must be a mapping")
    if raw.get("version") != PLAN_VERSION:
        raise MemoryValidationError("unsupported plan version")
    db = raw.get("db")
    if not isinstance(db, str) or not db.strip():
        raise MemoryValidationError("plan.db must be non-empty str")
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise MemoryValidationError("plan.entries must be a non-empty list")
    entries: List[AuditEntry] = []
    seen: set[str] = set()
    for item in entries_raw:
        if not isinstance(item, Mapping):
            raise MemoryValidationError("plan entry must be a mapping")
        mid = item.get("id")
        text = item.get("text")
        source = item.get("source")
        updated = item.get("updated_at")
        if not all(isinstance(v, str) and v for v in (mid, text, source, updated)):
            raise MemoryValidationError("plan entry fields must be non-empty str")
        assert isinstance(mid, str)
        if mid in seen:
            raise MemoryValidationError(f"plan contains duplicate id: {mid}")
        seen.add(mid)
        entries.append(
            AuditEntry(id=mid, text=text, source=source, updated_at=updated)
        )
    digest = raw.get("digest")
    if not isinstance(digest, str) or not digest.strip():
        raise MemoryValidationError("plan.digest must be non-empty str")
    expected = plan_digest(entries)
    if digest != expected:
        raise MemoryValidationError("plan digest mismatch")
    return Path(db), entries, digest


def verify_entries_against_db(
    conn: sqlite3.Connection, entries: Sequence[AuditEntry]
) -> None:
    """任一缺失或 text/source/updated_at 变化则失败。"""
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
    """
    单一事务条件删除；任一条不匹配则整批回滚并抛错（零删除）。
    """
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
