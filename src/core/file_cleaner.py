"""
文件清理模块。

按配置的保留时长与 glob 模式，定期清理 ``logs``、``data/tmp``、
``data/test`` 中的过期运行时文件；删除操作受 ``SafetyPolicy`` 约束。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Timer
from typing import List, Optional, TYPE_CHECKING

from src.core.paths import LOGS_DIR, TEST_DIR, TMP_DIR
from src.core.safety import Operation

if TYPE_CHECKING:
    from src.core.safety import SafetyPolicy

logger = logging.getLogger(__name__)


@dataclass
class CleanupRule:
    """
    单目录清理规则。

    定义目标目录、文件名匹配模式及最大保留时长（秒）。
    """

    name: str
    directory: Path
    patterns: List[str]
    max_age_seconds: float
    enabled: bool = True


class FileCleaner:
    """
    运行时文件定时清理器。

    支持从应用配置构建默认规则，并通过后台 ``Timer`` 周期性执行清理；
    手动 ``cleanup_now`` 可立即触发一轮。
    """

    def __init__(
        self,
        rules: Optional[List[CleanupRule]] = None,
        cleanup_interval_minutes: int = 10,
        safety_policy: Optional["SafetyPolicy"] = None,
    ):
        """
        初始化清理器。

        Args:
            rules: 清理规则列表；为 ``None`` 时使用空列表。
            cleanup_interval_minutes: 自动清理间隔（分钟）。
            safety_policy: 安全策略；用于校验 ``FILE_DELETE`` 权限与路径白名单。
        """
        self.rules = rules or []
        self.cleanup_interval_seconds = cleanup_interval_minutes * 60
        self.safety_policy = safety_policy
        self.logger = logging.getLogger(__name__)
        self._cleanup_timer: Optional[Timer] = None
        self._running = False

    @classmethod
    def from_config(cls, app_config, safety_policy: Optional["SafetyPolicy"] = None) -> "FileCleaner":
        """
        从 ``assistant.file_cleanup`` 配置节构建默认三目录清理规则。

        Args:
            app_config: 应用 ``Config`` 实例。
            safety_policy: 可选的安全策略实例。

        Returns:
            已填充临时音频、运行日志、测试输出规则的 ``FileCleaner`` 实例。
        """
        cleanup = app_config.get("assistant.file_cleanup", {}) or {}
        interval = int(cleanup.get("interval_minutes", 10))

        tmp_hours = float(
            cleanup.get("tmp_max_age_hours", cleanup.get("max_age_hours", 1.0))
        )
        logs_days = float(cleanup.get("logs_max_age_days", 1))
        test_hours = float(cleanup.get("test_max_age_hours", 72))

        rules = [
            CleanupRule(
                name="临时音频",
                directory=TMP_DIR,
                patterns=["miku_*.mp3", "miku_*.wav", "tmp*.mp3", "tmp*.wav"],
                max_age_seconds=tmp_hours * 3600,
            ),
            CleanupRule(
                name="运行日志",
                directory=LOGS_DIR,
                patterns=["miku.log*", "*.log"],
                max_age_seconds=logs_days * 86400,
            ),
            CleanupRule(
                name="测试输出",
                directory=TEST_DIR,
                patterns=["test_*.wav", "test_*.mp3", "miku_test_*"],
                max_age_seconds=test_hours * 3600,
            ),
        ]
        return cls(rules=rules, cleanup_interval_minutes=interval, safety_policy=safety_policy)

    def cleanup_all(self) -> int:
        """
        执行所有已启用规则的清理。

        Returns:
            本轮删除的文件总数。
        """
        total = 0
        for rule in self.rules:
            if rule.enabled:
                total += self._cleanup_rule(rule)
        return total

    def _cleanup_rule(self, rule: CleanupRule) -> int:
        """
        按单条规则扫描目录并删除过期文件。

        Args:
            rule: 清理规则实例。

        Returns:
            该规则下删除的文件数量。
        """
        if self.safety_policy and not self.safety_policy.is_allowed(Operation.FILE_DELETE):
            return 0
        if not rule.directory.exists():
            return 0

        deleted = 0
        now = time.time()
        for file_path in rule.directory.iterdir():
            if not file_path.is_file():
                continue
            if not _matches_any_pattern(file_path.name, rule.patterns):
                continue
            try:
                age = now - file_path.stat().st_mtime
                if age <= rule.max_age_seconds:
                    continue
                if self.safety_policy and not self.safety_policy.is_path_deletable(file_path):
                    self.logger.debug("跳过非白名单文件: %s", file_path)
                    continue
                file_path.unlink()
                deleted += 1
                self.logger.debug(
                    "删除过期[%s]: %s (%.1f小时)",
                    rule.name,
                    file_path.name,
                    age / 3600,
                )
            except OSError as exc:
                self.logger.warning("无法删除 %s: %s", file_path, exc)

        if deleted:
            self.logger.info("[%s] 清理完成，删除 %d 个文件", rule.name, deleted)
        return deleted

    def start_auto_cleanup(self) -> None:
        """启动后台定时清理；若已在运行则仅记录警告。"""
        if self._running:
            self.logger.warning("自动清理已在运行")
            return
        self._running = True
        self.logger.info(
            "启动自动清理（间隔 %.0f 分钟，规则 %d 条）",
            self.cleanup_interval_seconds / 60,
            len(self.rules),
        )
        for rule in self.rules:
            self.logger.info(
                "  - %s: %s | 保留 %s",
                rule.name,
                rule.directory,
                _format_retention(rule.max_age_seconds),
            )
        self._cleanup_all()
        self._schedule_next_cleanup()

    def _cleanup_all(self) -> None:
        """执行一轮清理并捕获异常，避免定时器线程崩溃。"""
        try:
            deleted = self.cleanup_all()
            if deleted > 0:
                self.logger.info("自动清理完成，共删除 %d 个过期文件", deleted)
        except Exception as exc:
            self.logger.error("自动清理失败: %s", exc)

    def _schedule_next_cleanup(self) -> None:
        """安排下一次定时清理（守护线程）。"""
        if not self._running:
            return
        self._cleanup_timer = Timer(self.cleanup_interval_seconds, self._cleanup_cycle)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def _cleanup_cycle(self) -> None:
        """定时器回调：清理完成后重新调度。"""
        if not self._running:
            return
        self._cleanup_all()
        self._schedule_next_cleanup()

    def stop_auto_cleanup(self) -> None:
        """停止自动清理并取消待执行的定时器。"""
        self._running = False
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None
        self.logger.info("自动清理已停止")

    def cleanup_now(self) -> int:
        """
        立即执行一轮全量清理（不受定时器影响）。

        Returns:
            删除的文件总数。
        """
        self.logger.info("执行手动清理...")
        return self.cleanup_all()


def _matches_any_pattern(filename: str, patterns: List[str]) -> bool:
    """
    判断文件名是否匹配任一 glob 或前缀模式。

    Args:
        filename: 待匹配文件名（不含目录）。
        patterns: glob 模式列表；以 ``*`` 结尾且无 leading ``*`` 时按前缀匹配。

    Returns:
        任一模式匹配时为 ``True``。
    """
    for pattern in patterns:
        if Path(filename).match(pattern):
            return True
        # miku_test_* 等前缀模式（Path.match 对纯前缀支持不完整）
        if pattern.endswith("*") and not pattern.startswith("*"):
            prefix = pattern[:-1]
            if filename.startswith(prefix):
                return True
    return False


def _format_retention(seconds: float) -> str:
    """
    将保留秒数格式化为人类可读字符串。

    Args:
        seconds: 保留时长（秒）。

    Returns:
        如 ``3天`` 或 ``1.5小时`` 的简短描述。
    """
    if seconds >= 86400:
        return f"{seconds / 86400:.0f}天"
    return f"{seconds / 3600:.1f}小时"
