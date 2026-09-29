"""长期记忆应用层领域对象（无 I/O、无线程）。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence, Tuple

from src.memory.errors import MemoryValidationError
from src.memory.models import (
    MemoryCategory,
    MemoryRecord,
    _validate_display_text,
    _validate_unit_interval,
)


class MemoryTurnOutcome(str, Enum):
    """对话轮次结局；仅 SUCCESS 且双方正文非空可进入提取队列。"""

    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def parse(cls, value: object) -> "MemoryTurnOutcome":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError as exc:
                raise MemoryValidationError(f"invalid turn outcome: {value!r}") from exc
        raise MemoryValidationError("outcome must be MemoryTurnOutcome or str")


@dataclass(frozen=True)
class MemoryTurnEvent:
    """一次对话轮的应用事件；不代表已写入长期记忆。"""

    turn_id: str
    user_text: str
    assistant_text: str
    outcome: MemoryTurnOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise MemoryValidationError("turn_id must be non-empty str")
        object.__setattr__(self, "turn_id", self.turn_id.strip())
        if not isinstance(self.user_text, str):
            raise MemoryValidationError("user_text must be str")
        if not isinstance(self.assistant_text, str):
            raise MemoryValidationError("assistant_text must be str")
        object.__setattr__(self, "outcome", MemoryTurnOutcome.parse(self.outcome))

    @property
    def is_extractable(self) -> bool:
        """是否允许进入异步提取队列。"""
        return (
            self.outcome is MemoryTurnOutcome.SUCCESS
            and bool(self.user_text.strip())
            and bool(self.assistant_text.strip())
        )


@dataclass(frozen=True)
class ExtractedMemory:
    """提取器产出的候选事实；不得声明 USER_CONFIRMED，写库时统一 MODEL_INFERRED。"""

    text: str
    category: MemoryCategory = MemoryCategory.OTHER
    importance: float = 0.5
    confidence: float = 0.5

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _validate_display_text(self.text))
        object.__setattr__(self, "category", MemoryCategory.coerce(self.category))
        object.__setattr__(
            self, "importance", _validate_unit_interval("importance", self.importance)
        )
        object.__setattr__(
            self, "confidence", _validate_unit_interval("confidence", self.confidence)
        )


@dataclass(frozen=True)
class ScoredMemory:
    """检索命中：记忆快照 + 确定性相关度分数。"""

    record: MemoryRecord
    score: float

    def __post_init__(self) -> None:
        if not isinstance(self.record, MemoryRecord):
            raise MemoryValidationError("record must be MemoryRecord")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise MemoryValidationError("score must be a number")
        score = float(self.score)
        if not math.isfinite(score):
            raise MemoryValidationError("score must be finite")
        if score <= 0.0:
            raise MemoryValidationError("score must be positive")
        object.__setattr__(self, "score", score)


@dataclass(frozen=True)
class MemoryRecallResult:
    """确定性检索结果；公开集合均为不可变快照。"""

    hits: Tuple[ScoredMemory, ...] = ()
    candidate_count: int = 0
    truncated_candidates: bool = False
    error_type: Optional[str] = None

    def __post_init__(self) -> None:
        hits = tuple(self.hits)
        for item in hits:
            if not isinstance(item, ScoredMemory):
                raise MemoryValidationError("hits must contain ScoredMemory")
        object.__setattr__(self, "hits", hits)
        if (
            not isinstance(self.candidate_count, int)
            or isinstance(self.candidate_count, bool)
            or self.candidate_count < 0
        ):
            raise MemoryValidationError("candidate_count must be a non-negative int")
        if not isinstance(self.truncated_candidates, bool):
            raise MemoryValidationError("truncated_candidates must be bool")
        if self.error_type is not None and (
            not isinstance(self.error_type, str) or not self.error_type.strip()
        ):
            raise MemoryValidationError("error_type must be non-empty str when set")

    @property
    def records(self) -> Tuple[MemoryRecord, ...]:
        return tuple(hit.record for hit in self.hits)

    @property
    def empty(self) -> bool:
        return not self.hits


@dataclass(frozen=True)
class MemoryContext:
    """有预算的结构化记忆上下文块；本轮不注入模型请求。"""

    rendered: str
    entries_used: int = 0
    chars_used: int = 0
    truncated_entry: bool = False
    skipped_entries: int = 0
    error_type: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.rendered, str):
            raise MemoryValidationError("rendered must be str")
        for name in ("entries_used", "chars_used", "skipped_entries"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise MemoryValidationError(f"{name} must be a non-negative int")
        if not isinstance(self.truncated_entry, bool):
            raise MemoryValidationError("truncated_entry must be bool")
        if self.error_type is not None and (
            not isinstance(self.error_type, str) or not self.error_type.strip()
        ):
            raise MemoryValidationError("error_type must be non-empty str when set")
        if self.chars_used != len(self.rendered):
            object.__setattr__(self, "chars_used", len(self.rendered))

    @property
    def empty(self) -> bool:
        return not self.rendered


@dataclass(frozen=True)
class IngestionStopStatus:
    """ingestion 停止结果；超时不得谎称 worker 已退出；drained 由实际 pending 得出。"""

    discarded: int = 0
    timed_out: bool = False
    worker_alive: bool = False
    drained: bool = True
    pending: int = 0

    def __post_init__(self) -> None:
        for name in ("discarded", "pending"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise MemoryValidationError(f"{name} must be a non-negative int")
        for name in ("timed_out", "worker_alive", "drained"):
            if not isinstance(getattr(self, name), bool):
                raise MemoryValidationError(f"{name} must be bool")


def empty_recall_result(*, error_type: Optional[str] = None) -> MemoryRecallResult:
    return MemoryRecallResult(error_type=error_type)


def empty_memory_context(*, error_type: Optional[str] = None) -> MemoryContext:
    return MemoryContext(rendered="", error_type=error_type)


def as_scored_sequence(hits: Sequence[ScoredMemory]) -> Tuple[ScoredMemory, ...]:
    return tuple(hits)
