"""SQLite 长期记忆适配器：仅持久化与 schema，不调用模型或拼提示词。"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from src.memory.errors import (
    MemoryDataCorruptError,
    MemoryNotFoundError,
    MemorySchemaIncompatibleError,
    MemoryStoreUnavailableError,
    MemoryValidationError,
)
from src.memory.models import (
    Clock,
    IdFactory,
    MemoryCategory,
    MemoryPruneResult,
    MemoryRecord,
    MemorySource,
    MemoryWriteResult,
    NewMemory,
    default_clock,
    default_id_factory,
    normalize_dedupe_key,
)

SCHEMA_VERSION = 1

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY NOT NULL,
    text TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL,
    importance REAL NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE
)
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_memories_created_id
    ON memories (created_at ASC, id ASC)
"""

_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 500
# 允许 candidate_limit+1 的有界 has-more 探测，不得无界扫描
_LIST_HAS_MORE_PROBE_LIMIT = _LIST_MAX_LIMIT + 1


class SqliteMemoryStore:
    """
    单文件 SQLite 记忆库。

    持有一条连接并由 ``threading.RLock`` 串行化访问；写操作使用真实事务
    （非 autocommit）。本类不启动后台线程。模块导入不会打开本存储。
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Optional[Clock] = None,
        id_factory: Optional[IdFactory] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._path = Path(db_path)
        self._clock = clock or default_clock
        self._id_factory = id_factory or default_id_factory
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._closed = False
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def schema_version(self) -> int:
        with self._lock:
            conn = self._require_conn()
            try:
                return int(conn.execute("PRAGMA user_version").fetchone()[0])
            except sqlite3.Error as exc:
                self._logger.error("memory schema_version failed err=%s", type(exc).__name__)
                raise MemoryStoreUnavailableError(
                    "failed to read memory schema version"
                ) from exc

    def add(self, draft: NewMemory) -> MemoryWriteResult:
        if not isinstance(draft, NewMemory):
            raise MemoryValidationError("draft must be NewMemory")
        with self._lock:
            conn = self._require_conn()
            existing = self._fetch_by_dedupe(conn, draft.dedupe_key)
            if existing is not None:
                return MemoryWriteResult(record=existing, created=False)

            now = self._now()
            memory_id = draft.memory_id or self._id_factory()
            if not isinstance(memory_id, str) or not memory_id.strip():
                raise MemoryValidationError("id factory must return non-empty str")
            memory_id = memory_id.strip()
            if self._fetch_by_id(conn, memory_id) is not None:
                raise MemoryValidationError(f"memory id already exists: {memory_id}")

            record = MemoryRecord(
                id=memory_id,
                text=draft.text,
                category=draft.category,
                source=draft.source,
                importance=draft.importance,
                confidence=draft.confidence,
                created_at=now,
                updated_at=now,
                dedupe_key=draft.dedupe_key,
            )
            try:
                with self._transaction(conn):
                    conn.execute(
                        """
                        INSERT INTO memories (
                            id, text, category, source, importance, confidence,
                            created_at, updated_at, dedupe_key
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        self._row_params(record),
                    )
            except sqlite3.IntegrityError:
                raced = self._fetch_by_dedupe(conn, draft.dedupe_key)
                if raced is not None:
                    return MemoryWriteResult(record=raced, created=False)
                self._logger.error("memory insert integrity error id=%s", memory_id)
                raise MemoryStoreUnavailableError("failed to insert memory") from None
            except sqlite3.Error as exc:
                self._logger.error(
                    "memory insert failed id=%s err=%s", memory_id, type(exc).__name__
                )
                raise MemoryStoreUnavailableError("failed to insert memory") from exc
            return MemoryWriteResult(record=record, created=True)

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        mid = self._require_id(memory_id)
        with self._lock:
            conn = self._require_conn()
            return self._fetch_by_id(conn, mid)

    def list_memories(
        self,
        *,
        limit: int = _LIST_DEFAULT_LIMIT,
        offset: int = 0,
        newest_first: bool = False,
    ) -> Sequence[MemoryRecord]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise MemoryValidationError("limit must be a positive int")
        if limit > _LIST_HAS_MORE_PROBE_LIMIT:
            raise MemoryValidationError(
                f"limit must be <= {_LIST_HAS_MORE_PROBE_LIMIT}"
            )
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise MemoryValidationError("offset must be a non-negative int")
        if not isinstance(newest_first, bool):
            raise MemoryValidationError("newest_first must be bool")
        order_sql = (
            "ORDER BY created_at DESC, id DESC"
            if newest_first
            else "ORDER BY created_at ASC, id ASC"
        )
        with self._lock:
            conn = self._require_conn()
            try:
                rows = conn.execute(
                    f"""
                    SELECT id, text, category, source, importance, confidence,
                           created_at, updated_at, dedupe_key
                    FROM memories
                    {order_sql}
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            except sqlite3.Error as exc:
                self._logger.error("memory list failed err=%s", type(exc).__name__)
                raise MemoryStoreUnavailableError("failed to list memories") from exc
            return tuple(self._decode_row(row) for row in rows)

    def count_memories(self) -> int:
        with self._lock:
            conn = self._require_conn()
            try:
                row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            except sqlite3.Error as exc:
                self._logger.error("memory count failed err=%s", type(exc).__name__)
                raise MemoryStoreUnavailableError("failed to count memories") from exc
            return int(row[0]) if row else 0

    def prune_for_retention(
        self,
        *,
        max_records: int,
        max_age_days: Optional[float],
        preserve_user_confirmed: bool,
        batch_limit: int,
        now: Optional[datetime] = None,
    ) -> MemoryPruneResult:
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or max_records < 1
        ):
            raise MemoryValidationError("max_records must be a positive int")
        if (
            not isinstance(batch_limit, int)
            or isinstance(batch_limit, bool)
            or batch_limit < 1
        ):
            raise MemoryValidationError("batch_limit must be a positive int")
        if not isinstance(preserve_user_confirmed, bool):
            raise MemoryValidationError("preserve_user_confirmed must be bool")
        if max_age_days is not None:
            if isinstance(max_age_days, bool) or not isinstance(max_age_days, (int, float)):
                raise MemoryValidationError("max_age_days must be a number or None")
            if float(max_age_days) < 0:
                raise MemoryValidationError("max_age_days must be >= 0")

        clock_now = now if isinstance(now, datetime) else self._clock()
        if clock_now.tzinfo is None:
            clock_now = clock_now.replace(tzinfo=timezone.utc)

        deleted = 0
        preserved = 0
        scanned = 0
        truncated = False
        remaining_budget = batch_limit
        # 淘汰顺序：低重要度 → 旧时间 → id 稳定平局
        order_sql = "ORDER BY importance ASC, created_at ASC, id ASC"

        with self._lock:
            conn = self._require_conn()
            try:
                # 1) 年龄淘汰：在 SQL 中排除受保护行，共享全局剩余预算
                if (
                    max_age_days is not None
                    and float(max_age_days) > 0
                    and remaining_budget > 0
                ):
                    cutoff = clock_now.timestamp() - float(max_age_days) * 86400.0
                    cutoff_iso = datetime.fromtimestamp(
                        cutoff, tz=timezone.utc
                    ).isoformat()
                    if preserve_user_confirmed:
                        rows = conn.execute(
                            f"""
                            SELECT id FROM memories
                            WHERE created_at < ?
                              AND source != ?
                            {order_sql}
                            LIMIT ?
                            """,
                            (
                                cutoff_iso,
                                MemorySource.USER_CONFIRMED.value,
                                remaining_budget,
                            ),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            f"""
                            SELECT id FROM memories
                            WHERE created_at < ?
                            {order_sql}
                            LIMIT ?
                            """,
                            (cutoff_iso, remaining_budget),
                        ).fetchall()
                    scanned += len(rows)
                    age_budget = remaining_budget
                    ids = [str(row[0]) for row in rows]
                    if ids:
                        with self._transaction(conn):
                            conn.executemany(
                                "DELETE FROM memories WHERE id = ?",
                                [(i,) for i in ids],
                            )
                        deleted += len(ids)
                        remaining_budget -= len(ids)
                    if len(rows) >= age_budget:
                        truncated = True

                # 2) 数量淘汰：继续使用剩余预算
                total = int(
                    conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                )
                overflow = total - max_records
                if overflow > 0 and remaining_budget > 0:
                    take = min(overflow, remaining_budget)
                    if preserve_user_confirmed:
                        rows = conn.execute(
                            f"""
                            SELECT id FROM memories
                            WHERE source != ?
                            {order_sql}
                            LIMIT ?
                            """,
                            (MemorySource.USER_CONFIRMED.value, take),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            f"""
                            SELECT id FROM memories
                            {order_sql}
                            LIMIT ?
                            """,
                            (take,),
                        ).fetchall()
                    scanned += len(rows)
                    ids = [str(row[0]) for row in rows]
                    if ids:
                        with self._transaction(conn):
                            conn.executemany(
                                "DELETE FROM memories WHERE id = ?",
                                [(i,) for i in ids],
                            )
                        deleted += len(ids)
                        remaining_budget -= len(ids)
                    if overflow > len(ids):
                        truncated = True

                total_after = int(
                    conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                )
                over_capacity_unresolvable = False
                if total_after > max_records:
                    if preserve_user_confirmed:
                        protected = int(
                            conn.execute(
                                "SELECT COUNT(*) FROM memories WHERE source = ?",
                                (MemorySource.USER_CONFIRMED.value,),
                            ).fetchone()[0]
                        )
                        preserved = protected
                        deletable_left = total_after - protected
                        if deletable_left <= 0:
                            over_capacity_unresolvable = True
                            truncated = False
                        elif remaining_budget <= 0:
                            truncated = True
                    elif remaining_budget <= 0:
                        truncated = True
            except sqlite3.Error as exc:
                self._logger.error("memory prune failed err=%s", type(exc).__name__)
                raise MemoryStoreUnavailableError("failed to prune memories") from exc

        return MemoryPruneResult(
            scanned=scanned,
            deleted=deleted,
            preserved_user_confirmed=preserved,
            truncated_batch=truncated,
            over_capacity_unresolvable=over_capacity_unresolvable,
        )

    def vacuum_offline(self) -> None:
        """显式离线 VACUUM；可能长时间锁库，禁止在热路径调用。"""
        with self._lock:
            conn = self._require_conn()
            try:
                conn.execute("VACUUM")
            except sqlite3.Error as exc:
                self._logger.error("memory vacuum failed err=%s", type(exc).__name__)
                raise MemoryStoreUnavailableError("failed to vacuum memory db") from exc

    def update(
        self,
        memory_id: str,
        *,
        text: Optional[str] = None,
        category: Optional[object] = None,
        source: Optional[object] = None,
        importance: Optional[float] = None,
        confidence: Optional[float] = None,
    ) -> MemoryRecord:
        mid = self._require_id(memory_id)
        with self._lock:
            conn = self._require_conn()
            current = self._fetch_by_id(conn, mid)
            if current is None:
                raise MemoryNotFoundError(f"memory not found: {mid}")

            new_text = current.text if text is None else text
            new_category = current.category if category is None else category
            new_source = current.source if source is None else source
            new_importance = current.importance if importance is None else importance
            new_confidence = current.confidence if confidence is None else confidence

            draft = NewMemory(
                text=new_text,
                category=MemoryCategory.coerce(new_category),
                source=MemorySource.parse(new_source),
                importance=new_importance,
                confidence=new_confidence,
                memory_id=mid,
            )
            updated = MemoryRecord(
                id=mid,
                text=draft.text,
                category=draft.category,
                source=draft.source,
                importance=draft.importance,
                confidence=draft.confidence,
                created_at=current.created_at,
                updated_at=self._now(),
                dedupe_key=draft.dedupe_key,
            )
            if (
                updated.dedupe_key != current.dedupe_key
                and self._fetch_by_dedupe(conn, updated.dedupe_key) is not None
            ):
                raise MemoryValidationError(
                    "update would collide with an existing normalized memory"
                )
            try:
                with self._transaction(conn):
                    conn.execute(
                        """
                        UPDATE memories SET
                            text = ?, category = ?, source = ?,
                            importance = ?, confidence = ?,
                            updated_at = ?, dedupe_key = ?
                        WHERE id = ?
                        """,
                        (
                            updated.text,
                            updated.category.value,
                            updated.source.value,
                            updated.importance,
                            updated.confidence,
                            self._dt_to_iso(updated.updated_at),
                            updated.dedupe_key,
                            mid,
                        ),
                    )
            except sqlite3.Error as exc:
                self._logger.error(
                    "memory update failed id=%s err=%s", mid, type(exc).__name__
                )
                raise MemoryStoreUnavailableError("failed to update memory") from exc
            return updated

    def delete(self, memory_id: str) -> bool:
        mid = self._require_id(memory_id)
        with self._lock:
            conn = self._require_conn()
            try:
                with self._transaction(conn):
                    cur = conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
                return cur.rowcount > 0
            except sqlite3.Error as exc:
                self._logger.error(
                    "memory delete failed id=%s err=%s", mid, type(exc).__name__
                )
                raise MemoryStoreUnavailableError("failed to delete memory") from exc

    def clear(self) -> int:
        with self._lock:
            conn = self._require_conn()
            try:
                with self._transaction(conn):
                    cur = conn.execute("DELETE FROM memories")
                return int(cur.rowcount)
            except sqlite3.Error as exc:
                self._logger.error("memory clear failed err=%s", type(exc).__name__)
                raise MemoryStoreUnavailableError("failed to clear memories") from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conn = self._conn
            self._conn = None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error as exc:
                    self._logger.error("memory close failed err=%s", type(exc).__name__)

    def _open(self) -> None:
        conn: Optional[sqlite3.Connection] = None
        try:
            parent = self._path.parent
            if parent and str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
            # 默认 isolation_level=""：DML 开启真实事务；非 autocommit
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = DELETE")
            found = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if found > SCHEMA_VERSION:
                conn.close()
                conn = None
                raise MemorySchemaIncompatibleError(
                    "memory database schema is newer than this build supports",
                    found_version=found,
                    supported_version=SCHEMA_VERSION,
                )
            self._initialize_schema(conn, found_version=found)
            self._conn = conn
            conn = None
        except MemorySchemaIncompatibleError:
            raise
        except MemoryStoreUnavailableError:
            raise
        except (sqlite3.Error, OSError) as exc:
            self._logger.error("memory open failed err=%s", type(exc).__name__)
            raise MemoryStoreUnavailableError(
                f"failed to open memory database: {self._path}"
            ) from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _initialize_schema(self, conn: sqlite3.Connection, *, found_version: int) -> None:
        """在单一事务中建表/索引，仅成功后才提升 user_version。"""
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            if found_version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
            conn.commit()
        except sqlite3.Error:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise

    @contextmanager
    def _transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def _require_conn(self) -> sqlite3.Connection:
        if self._closed or self._conn is None:
            raise MemoryStoreUnavailableError("memory store is closed")
        return self._conn

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise MemoryValidationError("clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _require_id(memory_id: str) -> str:
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemoryValidationError("memory_id must be non-empty str")
        return memory_id.strip()

    @staticmethod
    def _dt_to_iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _dt_from_iso(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _row_params(self, record: MemoryRecord) -> tuple[Any, ...]:
        return (
            record.id,
            record.text,
            record.category.value,
            record.source.value,
            record.importance,
            record.confidence,
            self._dt_to_iso(record.created_at),
            self._dt_to_iso(record.updated_at),
            record.dedupe_key,
        )

    def _decode_row(self, row: sqlite3.Row) -> MemoryRecord:
        try:
            text = row["text"]
            return MemoryRecord(
                id=row["id"],
                text=text,
                category=MemoryCategory.coerce(row["category"]),
                source=MemorySource.parse(row["source"]),
                importance=float(row["importance"]),
                confidence=float(row["confidence"]),
                created_at=self._dt_from_iso(row["created_at"]),
                updated_at=self._dt_from_iso(row["updated_at"]),
                dedupe_key=row["dedupe_key"]
                if row["dedupe_key"]
                else normalize_dedupe_key(str(text)),
            )
        except MemoryValidationError as exc:
            raise MemoryDataCorruptError("memory row failed domain validation") from exc
        except (TypeError, ValueError, KeyError) as exc:
            raise MemoryDataCorruptError("memory row is corrupt or undecodable") from exc

    def _fetch_by_id(
        self, conn: sqlite3.Connection, memory_id: str
    ) -> Optional[MemoryRecord]:
        try:
            row = conn.execute(
                """
                SELECT id, text, category, source, importance, confidence,
                       created_at, updated_at, dedupe_key
                FROM memories WHERE id = ?
                """,
                (memory_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            self._logger.error("memory fetch by id failed err=%s", type(exc).__name__)
            raise MemoryStoreUnavailableError("failed to read memory") from exc
        return None if row is None else self._decode_row(row)

    def _fetch_by_dedupe(
        self, conn: sqlite3.Connection, dedupe_key: str
    ) -> Optional[MemoryRecord]:
        try:
            row = conn.execute(
                """
                SELECT id, text, category, source, importance, confidence,
                       created_at, updated_at, dedupe_key
                FROM memories WHERE dedupe_key = ?
                """,
                (dedupe_key,),
            ).fetchone()
        except sqlite3.Error as exc:
            self._logger.error(
                "memory fetch by dedupe failed err=%s", type(exc).__name__
            )
            raise MemoryStoreUnavailableError("failed to read memory") from exc
        return None if row is None else self._decode_row(row)
