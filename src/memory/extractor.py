"""记忆提取器窄接口；导入与构造不得调用模型、联网或启动线程。"""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from src.memory.service_models import ExtractedMemory, MemoryTurnEvent


@runtime_checkable
class MemoryExtractor(Protocol):
    """同步提取接口；实现方可阻塞，但 ingestion 在后台 worker 中调用。"""

    def extract(self, turn: MemoryTurnEvent) -> Sequence[ExtractedMemory]:
        """从成功完成的对话轮提取候选记忆；不得声明 USER_CONFIRMED。"""
