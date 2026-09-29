"""面向对话的记忆查询门面：检索 + 预算格式化；失败开放；不注入提示词。"""
from __future__ import annotations

import logging
from typing import Optional

from src.memory.context_format import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ENTRIES,
    format_memory_context,
)
from src.memory.errors import MemoryValidationError
from src.memory.lexical_recall import DEFAULT_CANDIDATE_LIMIT, LexicalMemoryRecall
from src.memory.service_models import MemoryContext, empty_memory_context
from src.memory.store import MemoryStore


class MemoryQueryService:
    """
    对当前输入执行确定性检索并生成有预算的上下文块。

    本轮只生成块，不写入系统提示词或用户 prompt，不调用模型。
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        recaller: Optional[LexicalMemoryRecall] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._store = store
        self._logger = logger or logging.getLogger(__name__)
        self._recall = recaller or LexicalMemoryRecall(store, logger=self._logger)

    def build_context(
        self,
        query: str,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_chars: int = DEFAULT_MAX_CHARS,
        recall_limit: Optional[int] = None,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> MemoryContext:
        if not isinstance(query, str):
            raise MemoryValidationError("query must be str")
        limit = max_entries if recall_limit is None else recall_limit
        result = self._recall.recall(
            query,
            limit=limit,
            candidate_limit=candidate_limit,
        )
        if result.error_type:
            return empty_memory_context(error_type=result.error_type)
        if result.empty:
            return empty_memory_context()
        return format_memory_context(
            result.records,
            max_entries=max_entries,
            max_chars=max_chars,
        )
