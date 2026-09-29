"""长期记忆领域对象（无 I/O）。"""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from src.memory.errors import MemoryValidationError

_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


class MemoryCategory(str, Enum):
    """记忆类别；未知库内值读取时兼容为 OTHER，不阻断整库。"""

    USER_PROFILE = "user_profile"
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    EVENT = "event"
    OTHER = "other"

    @classmethod
    def coerce(cls, value: object) -> "MemoryCategory":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                return cls.OTHER
        raise MemoryValidationError("category must be MemoryCategory or str")


class MemorySource(str, Enum):
    """来源真实性：持久层原样保存，不得把推断升级为用户确认。"""

    USER_CONFIRMED = "user_confirmed"
    MODEL_INFERRED = "model_inferred"

    @classmethod
    def parse(cls, value: object) -> "MemorySource":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError as exc:
                raise MemoryValidationError(f"invalid memory source: {value!r}") from exc
        raise MemoryValidationError("source must be MemorySource or str")


def normalize_dedupe_key(text: str) -> str:
    """
    精确去重键：去首尾空白、压缩连续空白、大小写折叠。

    不改写调用方持有的展示正文；中文与 Unicode 字符保留。
    """
    if not isinstance(text, str):
        raise MemoryValidationError("text must be str")
    collapsed = _WHITESPACE_RE.sub(" ", text.strip())
    return collapsed.casefold()


def _validate_unit_interval(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryValidationError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise MemoryValidationError(f"{name} must be finite")
    if number < 0.0 or number > 1.0:
        raise MemoryValidationError(f"{name} must be in [0.0, 1.0]")
    return number


def _validate_utc(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise MemoryValidationError(f"{name} must be datetime")
    if value.tzinfo is None:
        raise MemoryValidationError(f"{name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _validate_display_text(text: str) -> str:
    if not isinstance(text, str):
        raise MemoryValidationError("text must be str")
    # 仅用 strip 判断空正文；合法时原样返回（保留首尾与内部空白）
    if not text.strip():
        raise MemoryValidationError("text must be non-empty")
    return text


@dataclass(frozen=True)
class MemoryRecord:
    """已持久化的记忆条目；公开返回值均为不可变快照。"""

    id: str
    text: str
    category: MemoryCategory
    source: MemorySource
    importance: float
    confidence: float
    created_at: datetime
    updated_at: datetime
    dedupe_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise MemoryValidationError("id must be non-empty str")
        text = _validate_display_text(self.text)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "category", MemoryCategory.coerce(self.category))
        object.__setattr__(self, "source", MemorySource.parse(self.source))
        object.__setattr__(
            self, "importance", _validate_unit_interval("importance", self.importance)
        )
        object.__setattr__(
            self, "confidence", _validate_unit_interval("confidence", self.confidence)
        )
        created = _validate_utc("created_at", self.created_at)
        updated = _validate_utc("updated_at", self.updated_at)
        if updated < created:
            raise MemoryValidationError("updated_at must be >= created_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        expected_key = normalize_dedupe_key(text)
        if not isinstance(self.dedupe_key, str) or self.dedupe_key != expected_key:
            raise MemoryValidationError(
                "dedupe_key must equal normalize_dedupe_key(text)"
            )


@dataclass(frozen=True)
class NewMemory:
    """待写入草稿；构造时校验领域不变量。"""

    text: str
    category: MemoryCategory = MemoryCategory.OTHER
    source: MemorySource = MemorySource.USER_CONFIRMED
    importance: float = 0.5
    confidence: float = 1.0
    memory_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _validate_display_text(self.text))
        object.__setattr__(self, "category", MemoryCategory.coerce(self.category))
        object.__setattr__(self, "source", MemorySource.parse(self.source))
        object.__setattr__(
            self, "importance", _validate_unit_interval("importance", self.importance)
        )
        object.__setattr__(
            self, "confidence", _validate_unit_interval("confidence", self.confidence)
        )
        if self.memory_id is not None:
            if not isinstance(self.memory_id, str) or not self.memory_id.strip():
                raise MemoryValidationError("memory_id must be non-empty str when set")
            object.__setattr__(self, "memory_id", self.memory_id.strip())

    @property
    def dedupe_key(self) -> str:
        return normalize_dedupe_key(self.text)


@dataclass(frozen=True)
class MemoryWriteResult:
    """写入结果：新建或命中精确重复（``created=False``，不升级已有来源）。"""

    record: MemoryRecord
    created: bool


@dataclass(frozen=True)
class MemoryPruneResult:
    """有界 prune 统计；不含记忆正文。"""

    scanned: int = 0
    deleted: int = 0
    preserved_user_confirmed: int = 0
    truncated_batch: bool = False
    # 仍超 max_records，但无可淘汰（多为全 USER_CONFIRMED）
    over_capacity_unresolvable: bool = False


Clock = Callable[[], datetime]
IdFactory = Callable[[], str]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


def default_id_factory() -> str:
    return uuid.uuid4().hex
