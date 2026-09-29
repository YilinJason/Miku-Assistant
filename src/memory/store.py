"""长期记忆存储 Protocol。"""
from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from src.memory.models import MemoryPruneResult, MemoryRecord, MemoryWriteResult, NewMemory


@runtime_checkable
class MemoryStore(Protocol):
    """可替换的长期记忆持久化接口。"""

    def add(self, draft: NewMemory) -> MemoryWriteResult:
        """新增；精确规范化重复时返回已有记录且 ``created=False``。"""

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        """按 ID 读取；不存在返回 None。"""

    def list_memories(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        newest_first: bool = False,
    ) -> Sequence[MemoryRecord]:
        """有界列表；默认 created_at ASC, id ASC；``newest_first=True`` 时倒序。"""

    def count_memories(self) -> int:
        """返回当前记录总数。"""

    def prune_for_retention(
        self,
        *,
        max_records: int,
        max_age_days: Optional[float],
        preserve_user_confirmed: bool,
        batch_limit: int,
        now: Optional[object] = None,
    ) -> MemoryPruneResult:
        """
        按保留策略有界删除。

        默认不删除 USER_CONFIRMED；超量时按 created_at ASC, id ASC 淘汰可删记录。
        不在此方法内执行 VACUUM。
        """

    def vacuum_offline(self) -> None:
        """显式离线维护：可能耗时的 VACUUM；禁止在用户请求热路径调用。"""

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
        """显式更新指定字段；不隐式升级来源。"""

    def delete(self, memory_id: str) -> bool:
        """删除单条；不存在返回 False。"""

    def clear(self) -> int:
        """清空全部；返回删除条数。"""

    def close(self) -> None:
        """关闭存储；可重复调用。"""
