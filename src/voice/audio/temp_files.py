"""
临时文件安全删除

TTS / RVC 链路产生的临时音频在播放后清理，受 ``SafetyPolicy`` 白名单与重试策略约束。
"""
import logging
import os
import threading
import time
from typing import Optional

from src.core.safety import Operation, SafetyPolicy


class TempFileManager:
    """带重试与安全策略检查的临时文件清理器。"""

    def __init__(self, safety_policy: Optional[SafetyPolicy] = None):
        """
        初始化临时文件管理器。

        Args:
            safety_policy: 安全策略；删除路径须在白名单内且具备 ``FILE_DELETE`` 权限。
        """
        self.logger = logging.getLogger(__name__)
        self.safety_policy = safety_policy or SafetyPolicy()

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
        删除单个临时文件，遇占用则指数退避重试。

        Args:
            file_path: 待删除文件路径。
            max_retries: 最大重试次数。
            delay: 首次重试基础延迟（秒），后续按 attempt 递增。
        """
        if not file_path or not os.path.exists(file_path):
            return

        if not self.safety_policy.is_path_deletable(file_path):
            if not self.safety_policy.is_allowed(Operation.FILE_DELETE):
                self.logger.debug("文件删除未授权，跳过: %s", file_path)
            else:
                self.logger.warning("路径不在删除白名单内，跳过: %s", file_path)
            return

        for attempt in range(max_retries):
            try:
                os.remove(file_path)
                self.logger.debug("成功删除临时文件: %s", file_path)
                return
            except OSError as exc:
                if self._is_file_locked_error(exc) and attempt < max_retries - 1:
                    time.sleep(delay * (attempt + 1))
                    continue
                if self._is_file_locked_error(exc):
                    self.logger.debug("临时文件仍被占用，稍后重试: %s", file_path)
                    self.remove_delayed(file_path)
                    return
                self.logger.warning("删除临时文件失败: %s - %s", file_path, exc)
                return

    def remove_delayed(self, file_path: str, delay: float = 3.0, max_retries: int = 5) -> None:
        """
        在后台线程中延迟删除文件（适用于播放器尚未释放句柄）。

        Args:
            file_path: 待删除文件路径。
            delay: 首次尝试前的等待秒数。
            max_retries: 延迟线程内的最大重试次数。
        """
        def _remove():
            time.sleep(delay)
            for attempt in range(max_retries):
                if not file_path or not os.path.exists(file_path):
                    return
                try:
                    os.remove(file_path)
                    self.logger.debug("延迟删除成功: %s", file_path)
                    return
                except OSError as exc:
                    if self._is_file_locked_error(exc) and attempt < max_retries - 1:
                        time.sleep(2.0 * (attempt + 1))
                    else:
                        if os.path.exists(file_path):
                            self.logger.debug("延迟删除仍失败，留给定时清理: %s", file_path)
                        return

        threading.Thread(target=_remove, daemon=True).start()

    def remove_many(self, *paths: str) -> None:
        """
        批量删除多个临时文件。

        Args:
            *paths: 可变数量的文件路径；空字符串会被跳过。
        """
        for path in paths:
            if path:
                self.remove(path)
