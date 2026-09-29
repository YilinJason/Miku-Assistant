"""
临时文件安全删除

TTS / RVC 链路产生的临时音频在播放后清理，受 ``SafetyPolicy`` 白名单与重试策略约束。
延迟删除使用单一有界队列与一个后台 Worker，禁止一文件一线程无限增长。
Worker 等待均可被 shutdown 中断。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Optional, Tuple

from src.core.safety import Operation, SafetyPolicy

DEFAULT_DELAYED_QUEUE_CAPACITY = 64
MAX_DELAYED_QUEUE_CAPACITY = 1024
# 前台同步删除：最多快速尝试次数，避免长时间卡住播放路径
FOREGROUND_REMOVE_ATTEMPTS = 2


class TempFileManager:
    """带重试与安全策略检查的临时文件清理器。"""

    def __init__(
        self,
        safety_policy: Optional[SafetyPolicy] = None,
        *,
        delayed_queue_capacity: int = DEFAULT_DELAYED_QUEUE_CAPACITY,
    ):
        """
        初始化临时文件管理器。

        Args:
            safety_policy: 安全策略；删除路径须在白名单内且具备 ``FILE_DELETE`` 权限。
            delayed_queue_capacity: 延迟删除队列容量；满时丢弃并留给定时清理器。
        """
        if (
            not isinstance(delayed_queue_capacity, int)
            or isinstance(delayed_queue_capacity, bool)
            or delayed_queue_capacity < 1
        ):
            raise ValueError("delayed_queue_capacity must be a positive int")
        if delayed_queue_capacity > MAX_DELAYED_QUEUE_CAPACITY:
            raise ValueError(
                f"delayed_queue_capacity must be <= {MAX_DELAYED_QUEUE_CAPACITY}"
            )

        self.logger = logging.getLogger(__name__)
        self.safety_policy = safety_policy or SafetyPolicy()
        self._delayed_queue: queue.Queue[Tuple[str, float, int]] = queue.Queue(
            maxsize=delayed_queue_capacity
        )
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self._stop = threading.Event()
        self._shutdown_done = False

    @staticmethod
    def _is_file_locked_error(exc: BaseException) -> bool:
        """
        判断异常是否由文件被占用引起（Windows winerror 32 等）。

        Args:
            exc: 删除文件时捕获的异常。

        Returns:
            若为文件锁定类错误则返回 ``True``。
        """
        if isinstance(exc, PermissionError):
            return True
        if isinstance(exc, OSError):
            if getattr(exc, "winerror", None) == 32:
                return True
            if getattr(exc, "errno", None) in (13, 32):
                return True
        return False

    def remove(self, file_path: str, max_retries: int = 8, delay: float = 0.5) -> None:
        """
        删除单个临时文件。

        前台路径仅做很少量快速尝试；遇占用立即转入有界延迟队列，
        避免同步 sleep 卡住播放/请求线程。``max_retries``/``delay`` 保留兼容签名，
        前台不再使用长退避。
        """
        del max_retries, delay  # 兼容旧调用方；前台不再长时间阻塞
        if not file_path or not os.path.exists(file_path):
            return

        if not self.safety_policy.is_path_deletable(file_path):
            if not self.safety_policy.is_allowed(Operation.FILE_DELETE):
                self.logger.debug("文件删除未授权，跳过: %s", file_path)
            else:
                self.logger.warning("路径不在删除白名单内，跳过: %s", file_path)
            return

        for _attempt in range(FOREGROUND_REMOVE_ATTEMPTS):
            try:
                os.remove(file_path)
                self.logger.debug("成功删除临时文件: %s", file_path)
                return
            except OSError as exc:
                if self._is_file_locked_error(exc):
                    self.logger.debug("临时文件仍被占用，稍后重试: %s", file_path)
                    self.remove_delayed(file_path)
                    return
                self.logger.warning("删除临时文件失败: %s - %s", file_path, exc)
                return

    def remove_delayed(
        self, file_path: str, delay: float = 3.0, max_retries: int = 5
    ) -> None:
        """
        将文件放入单一有界延迟删除队列（单一 Worker）。

        Args:
            file_path: 待删除文件路径。
            delay: 首次尝试前的等待秒数。
            max_retries: Worker 内的最大重试次数。
        """
        if not file_path or self._stop.is_set():
            return
        self._ensure_worker()
        try:
            self._delayed_queue.put_nowait((file_path, float(delay), int(max_retries)))
        except queue.Full:
            self.logger.debug(
                "延迟删除队列已满，留给定时清理: %s", file_path
            )

    def remove_many(self, *paths: str) -> None:
        """
        批量删除多个临时文件。

        Args:
            *paths: 可变数量的文件路径；空字符串会被跳过。
        """
        for path in paths:
            if path:
                self.remove(path)

    def pending_delayed(self) -> int:
        """当前延迟队列中的待处理项数。"""
        return self._delayed_queue.qsize()

    def shutdown(self, timeout: float = 2.0) -> None:
        """停止延迟删除 Worker；exactly-once / 可重复调用。"""
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)
        self._shutdown_done = True

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._stop.is_set():
                return
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._shutdown_done = False
            worker = threading.Thread(
                target=self._delayed_worker_loop,
                name="TempFileDelayedDelete",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def _wait_interruptible(self, seconds: float) -> bool:
        """等待至多 ``seconds``；若已 stop 返回 True。"""
        return self._stop.wait(timeout=max(0.0, seconds))

    def _delayed_worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                file_path, delay, max_retries = self._delayed_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if self._wait_interruptible(delay):
                    return
                for attempt in range(max(1, max_retries)):
                    if self._stop.is_set():
                        return
                    if not file_path or not os.path.exists(file_path):
                        break
                    try:
                        if not self.safety_policy.is_path_deletable(file_path):
                            break
                        os.remove(file_path)
                        self.logger.debug("延迟删除成功: %s", file_path)
                        break
                    except OSError as exc:
                        if self._is_file_locked_error(exc) and attempt < max_retries - 1:
                            if self._wait_interruptible(2.0 * (attempt + 1)):
                                return
                            continue
                        if os.path.exists(file_path):
                            self.logger.debug(
                                "延迟删除仍失败，留给定时清理: %s", file_path
                            )
                        break
            finally:
                self._delayed_queue.task_done()
