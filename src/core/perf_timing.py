"""性能阶段计时：只记录 session/turn ID 与耗时，不记录正文。"""
from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Optional

DEFAULT_MAX_STAGED_RECORDS = 64
MAX_MAX_STAGED_RECORDS = 10_000


def _require_max_staged(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"max_staged_records must be int, got {type(value).__name__}"
        )
    if value < 1 or value > MAX_MAX_STAGED_RECORDS:
        raise ValueError(
            f"max_staged_records must be in 1..{MAX_MAX_STAGED_RECORDS}, got {value}"
        )
    return value


@dataclass
class StageTiming:
    name: str
    elapsed_ms: float
    cold: bool = False


@dataclass
class SessionTimer:
    """
    单会话有界阶段记录；日志不含用户/模型正文。

    冷/热：仅 ``counts_as_request=True`` 的阶段（生产请求边界）翻转。
    内存内 ``_stages`` 有硬上限，超出时淘汰最旧记录；日志仍每轮输出。
    """

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    cold_start: bool = True
    max_staged_records: int = DEFAULT_MAX_STAGED_RECORDS
    _stages: "OrderedDict[str, StageTiming]" = field(default_factory=OrderedDict)
    _turn_seq: int = 0

    def __post_init__(self) -> None:
        self.max_staged_records = _require_max_staged(self.max_staged_records)
        if not isinstance(self._stages, OrderedDict):
            self._stages = OrderedDict(self._stages)

    def new_turn_id(self) -> str:
        self._turn_seq += 1
        return f"{self.session_id}-t{self._turn_seq}"

    def record_stage(
        self,
        stage: str,
        elapsed_ms: float,
        *,
        turn_id: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        counts_as_request: bool = False,
    ) -> StageTiming:
        cold = self.cold_start
        timing = StageTiming(name=stage, elapsed_ms=float(elapsed_ms), cold=cold)
        key = f"{turn_id or '-'}::{stage}"
        if key in self._stages:
            del self._stages[key]
        self._stages[key] = timing
        while len(self._stages) > self.max_staged_records:
            self._stages.popitem(last=False)
        if counts_as_request and cold:
            self.cold_start = False
        log = logger or logging.getLogger("src.core.perf_timing")
        log.info(
            "[Perf] session=%s turn=%s stage=%s elapsed_ms=%.1f cold=%s",
            self.session_id,
            turn_id or "-",
            stage,
            timing.elapsed_ms,
            cold,
        )
        return timing

    def measure(
        self,
        stage: str,
        *,
        turn_id: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        counts_as_request: bool = False,
    ):
        """上下文管理器：测量 with 块耗时；取消/失败也在 __exit__ 闭合计时。"""
        timer = self

        class _Ctx:
            def __enter__(self_inner):
                self_inner._started = time.perf_counter()
                return self_inner

            def __exit__(self_inner, exc_type, exc, tb):
                elapsed = (time.perf_counter() - self_inner._started) * 1000.0
                timer.record_stage(
                    stage,
                    elapsed,
                    turn_id=turn_id,
                    logger=logger,
                    counts_as_request=counts_as_request,
                )
                return False

        return _Ctx()

    def stages(self) -> Dict[str, StageTiming]:
        return dict(self._stages)
