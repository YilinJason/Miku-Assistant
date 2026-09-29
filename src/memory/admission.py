"""前台优先的小型提取/维护准入闸：不可抢占已发出的 Ollama HTTP。"""
from __future__ import annotations

import threading
from typing import Optional, Protocol, runtime_checkable

# 前台等待维护结束的单次 wait 切片；循环直到维护释放（处理虚假唤醒）
_FOREGROUND_WAIT_SLICE_S = 0.5


@runtime_checkable
class ExtractionAdmissionGate(Protocol):
    """
    后台提取与维护准入。

    已开始的 HTTP 请求无法可靠抢占；本闸只决定「能否开始」提取/维护。
    """

    def begin_foreground(self) -> None:
        """前台生成开始；可重入；维护未释放前不增加 foreground 计数。"""

    def end_foreground(self) -> None:
        """前台生成结束；与 begin 成对。"""

    def try_acquire_extract(self) -> bool:
        """前台或维护活跃时返回 False（失败开放跳过本轮提取）。"""

    def release_extract(self) -> None:
        """提取结束。"""

    def try_acquire_maintenance(self) -> bool:
        """
        原子获取维护 lease。

        前台、前台等待中、提取中或已有维护时失败；成功后与前台/提取互斥。
        """

    def release_maintenance(self) -> None:
        """维护结束；与 try_acquire_maintenance 成对，exactly-once。"""


class ForegroundAwareAdmissionGate:
    """
    计数闸：前台、提取、维护三者互斥启动。

    - ``try_acquire_maintenance``：foreground / pending / extracting / maintenance 任一非零则失败。
    - ``try_acquire_extract``：foreground、foreground-pending 或 maintenance 活跃则失败。
    - ``begin_foreground``：登记 pending，循环等待维护结束后再增加 foreground；
      释放维护后优先唤醒前台等待者。维护批量须保持有界以免长期阻塞。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._foreground = 0
        self._foreground_pending = 0
        self._extracting = 0
        self._maintenance = 0

    @property
    def foreground_count(self) -> int:
        with self._cond:
            return self._foreground

    @property
    def foreground_pending_count(self) -> int:
        with self._cond:
            return self._foreground_pending

    @property
    def extracting_count(self) -> int:
        with self._cond:
            return self._extracting

    @property
    def maintenance_count(self) -> int:
        with self._cond:
            return self._maintenance

    def begin_foreground(self) -> None:
        with self._cond:
            self._foreground_pending += 1
            try:
                while self._maintenance > 0:
                    self._cond.wait(timeout=_FOREGROUND_WAIT_SLICE_S)
                self._foreground += 1
            finally:
                self._foreground_pending -= 1

    def end_foreground(self) -> None:
        with self._cond:
            if self._foreground > 0:
                self._foreground -= 1
            self._cond.notify_all()

    def try_acquire_extract(self) -> bool:
        with self._cond:
            if (
                self._foreground > 0
                or self._foreground_pending > 0
                or self._maintenance > 0
            ):
                return False
            self._extracting += 1
            return True

    def release_extract(self) -> None:
        with self._cond:
            if self._extracting > 0:
                self._extracting -= 1
            self._cond.notify_all()

    def try_acquire_maintenance(self) -> bool:
        with self._cond:
            if (
                self._foreground > 0
                or self._foreground_pending > 0
                or self._extracting > 0
                or self._maintenance > 0
            ):
                return False
            self._maintenance = 1
            return True

    def release_maintenance(self) -> None:
        with self._cond:
            self._maintenance = 0
            # 优先唤醒等待中的前台
            self._cond.notify_all()


def optional_gate(gate: Optional[ExtractionAdmissionGate]) -> Optional[ExtractionAdmissionGate]:
    return gate
