"""
语音链路各阶段耗时日志

为 TTS / RVC 合成与播放提供上下文计时器与多阶段汇总。
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


@contextmanager
def log_elapsed(stage: str, *, extra: Optional[str] = None) -> Iterator[None]:
    """
    记录单个阶段的耗时（毫秒）。

    Args:
        stage: 阶段名称，写入日志前缀 ``[TTS计时]``。
        extra: 可选附加说明，括号形式追加在耗时后。

    Yields:
        无；在 ``with`` 块结束时写入耗时日志。
    """
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000
        suffix = f" ({extra})" if extra else ""
        logger.info("[TTS计时] %s: %.0f ms%s", stage, ms, suffix)


class StageTimer:
    """汇总多阶段耗时的计时器，用于单段或流水线朗读。"""

    def __init__(self, label: str = "TTS总耗时"):
        """
        初始化阶段计时器。

        Args:
            label: 汇总日志中的总标签名。
        """
        self.label = label
        self._t0 = time.perf_counter()
        self.stages: dict[str, float] = {}

    def mark(self, stage: str) -> None:
        """
        标记当前时刻相对起点的累计毫秒数。

        Args:
            stage: 阶段名称，作为 ``stages`` 字典键。
        """
        self.stages[stage] = (time.perf_counter() - self._t0) * 1000

    def finish(self) -> None:
        """输出总耗时及各已标记阶段的明细日志。"""
        total_ms = (time.perf_counter() - self._t0) * 1000
        parts = ", ".join(f"{k}={v:.0f}ms" for k, v in self.stages.items())
        logger.info("[TTS计时] %s: %.0f ms (%s)", self.label, total_ms, parts or "无分阶段数据")
