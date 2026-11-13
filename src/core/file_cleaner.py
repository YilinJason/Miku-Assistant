"""
文件清理模块。

按配置的保留时长与 glob 模式，定期清理 ``data/tmp``、``logs``、
``data/test`` 中的过期运行时文件；删除操作受 ``SafetyPolicy`` 约束。
不扫描仓库根 ``tmp/`` 或未知系统临时目录。
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Timer
from typing import Any, Callable, Dict, Iterator, List, Optional, TYPE_CHECKING

from src.core.paths import LOGS_DIR, TEST_DIR, TMP_DIR
from src.core.safety import Operation

if TYPE_CHECKING:
    from src.core.safety import SafetyPolicy

logger = logging.getLogger(__name__)

DEFAULT_MAX_SCAN_FILES = 500
DEFAULT_MAX_DELETE_FILES = 100
MAX_MAX_SCAN_FILES = 50_000
MAX_MAX_DELETE_FILES = 10_000
DEFAULT_INTERVAL_MINUTES = 10
MAX_INTERVAL_MINUTES = 24 * 60
# 活动 sweep 未耗尽时，用短延迟继续下一批，而非等完整清理间隔
DEFAULT_CONTINUE_DELAY_S = 0.05

ScandirFn = Callable[[Any], Any]


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


@dataclass
class _ActiveSweep:
    """FileCleaner 拥有的跨批次 scandir 活动游标。"""

    key: str
    directory: Path
    ctx: Any
    iterator: Iterator[Any]
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        close = getattr(self.ctx, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass


def _require_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


def _require_positive_int(name: str, value: Any, *, hard_max: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int")
    if value > hard_max:
        raise ValueError(f"{name} must be <= {hard_max}")
    return value


def _require_positive_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return number


class FileCleaner:
    """
    运行时文件定时清理器。

    启动时只调度后台 Timer，不同步执行无界扫描。每轮扫描/删除有上限，
    且同一实例同一时刻只允许一轮清理在跑。

    目录枚举使用 ``os.scandir`` 流式读取；跨短批次保存同一 iterator，
    每次 ``next()`` 均计入硬扫描预算，避免无界前缀跳过。
    """

    def __init__(
        self,
        rules: Optional[List[CleanupRule]] = None,
        cleanup_interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
        safety_policy: Optional["SafetyPolicy"] = None,
        *,
        max_scan_files: int = DEFAULT_MAX_SCAN_FILES,
        max_delete_files: int = DEFAULT_MAX_DELETE_FILES,
        cleanup_on_start: bool = False,
        scandir_fn: Optional[ScandirFn] = None,
        continue_delay_seconds: float = DEFAULT_CONTINUE_DELAY_S,
    ):
        interval = _require_positive_int(
            "cleanup_interval_minutes",
            cleanup_interval_minutes,
            hard_max=MAX_INTERVAL_MINUTES,
        )
        max_scan = _require_positive_int(
            "max_scan_files", max_scan_files, hard_max=MAX_MAX_SCAN_FILES
        )
        max_delete = _require_positive_int(
            "max_delete_files", max_delete_files, hard_max=MAX_MAX_DELETE_FILES
        )
        on_start = _require_bool("cleanup_on_start", cleanup_on_start)
        if (
            isinstance(continue_delay_seconds, bool)
            or not isinstance(continue_delay_seconds, (int, float))
            or not math.isfinite(float(continue_delay_seconds))
            or float(continue_delay_seconds) < 0
        ):
            raise ValueError("continue_delay_seconds must be a finite number >= 0")

        self.rules = rules or []
        self.cleanup_interval_seconds = interval * 60
        self.safety_policy = safety_policy
        self.max_scan_files = max_scan
        self.max_delete_files = max_delete
        self.cleanup_on_start = on_start
        self._continue_delay_s = float(continue_delay_seconds)
        self._scandir: ScandirFn = scandir_fn or os.scandir
        self.logger = logging.getLogger(__name__)
        self._cleanup_timer: Optional[Timer] = None
        self._running = False
        self._cycle_lock = Lock()
        self._sweep_lock = Lock()
        # 稳定规则身份 → 活动 scandir（跨短批次续扫，不从头跳前缀）
        self._active_sweeps: Dict[str, _ActiveSweep] = {}

    @classmethod
    def from_config(cls, app_config, safety_policy: Optional["SafetyPolicy"] = None) -> "FileCleaner":
        """
        从 ``assistant.file_cleanup`` 配置节构建默认三目录清理规则。

        布尔与数值严格校验；拒绝字符串 ``"false"`` 被当成 True、负数夹成默认值。
        """
        cleanup = app_config.get("assistant.file_cleanup", {}) or {}
        if not isinstance(cleanup, dict):
            raise ValueError("assistant.file_cleanup must be a mapping")

        interval = _require_positive_int(
            "assistant.file_cleanup.interval_minutes",
            cleanup.get("interval_minutes", DEFAULT_INTERVAL_MINUTES),
            hard_max=MAX_INTERVAL_MINUTES,
        )
        tmp_hours = _require_positive_number(
            "assistant.file_cleanup.tmp_max_age_hours",
            cleanup.get("tmp_max_age_hours", cleanup.get("max_age_hours", 1.0)),
        )
        logs_days = _require_positive_number(
            "assistant.file_cleanup.logs_max_age_days",
            cleanup.get("logs_max_age_days", 1),
        )
        test_hours = _require_positive_number(
            "assistant.file_cleanup.test_max_age_hours",
            cleanup.get("test_max_age_hours", 72),
        )
        max_scan = _require_positive_int(
            "assistant.file_cleanup.max_scan_files",
            cleanup.get("max_scan_files", DEFAULT_MAX_SCAN_FILES),
            hard_max=MAX_MAX_SCAN_FILES,
        )
        max_delete = _require_positive_int(
            "assistant.file_cleanup.max_delete_files",
            cleanup.get("max_delete_files", DEFAULT_MAX_DELETE_FILES),
            hard_max=MAX_MAX_DELETE_FILES,
        )
        cleanup_on_start = _require_bool(
            "assistant.file_cleanup.cleanup_on_start",
            cleanup.get("cleanup_on_start", False),
        )

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
        return cls(
            rules=rules,
            cleanup_interval_minutes=interval,
            safety_policy=safety_policy,
            max_scan_files=max_scan,
            max_delete_files=max_delete,
            cleanup_on_start=cleanup_on_start,
        )

    @staticmethod
    def _rule_key(rule: CleanupRule) -> str:
        """按对象身份 + 目录规范路径隔离；同名规则互不污染。"""
        try:
            resolved = str(Path(rule.directory).resolve())
        except OSError:
            resolved = str(rule.directory)
        return f"{id(rule)}|{resolved}|{rule.name}"

    def cleanup_all(self) -> int:
        """
        执行所有已启用规则的有界清理。

        Returns:
            本轮删除的文件总数。
        """
        if not self._cycle_lock.acquire(blocking=False):
            self.logger.debug("清理已在进行，跳过本轮")
            return 0
        try:
            return self._cleanup_all_bounded()
        except Exception:
            # 异常不得永久持有 scandir；exactly-once close 全部活动 sweep
            self._close_all_sweeps()
            raise
        finally:
            self._cycle_lock.release()

    def _cleanup_all_bounded(self) -> int:
        total = 0
        scanned = 0
        enabled_keys = set()
        for rule in self.rules:
            if not rule.enabled:
                self._close_sweep(self._rule_key(rule))
                continue
            enabled_keys.add(self._rule_key(rule))
            if total >= self.max_delete_files:
                break
            if scanned >= self.max_scan_files:
                break
            deleted, scanned_delta = self._cleanup_rule(
                rule,
                scan_budget=self.max_scan_files - scanned,
                delete_budget=self.max_delete_files - total,
            )
            total += deleted
            scanned += scanned_delta
        # 移除已不在规则列表中的活动 sweep
        with self._sweep_lock:
            stale = [k for k in self._active_sweeps if k not in enabled_keys]
        for key in stale:
            self._close_sweep(key)
        return total

    def _has_active_sweeps(self) -> bool:
        with self._sweep_lock:
            return any(not s.closed for s in self._active_sweeps.values())

    def _close_sweep(self, key: str) -> None:
        with self._sweep_lock:
            sweep = self._active_sweeps.pop(key, None)
        if sweep is not None:
            sweep.close()

    def _close_all_sweeps(self) -> None:
        with self._sweep_lock:
            sweeps = list(self._active_sweeps.values())
            self._active_sweeps.clear()
        for sweep in sweeps:
            sweep.close()

    def _get_or_open_sweep(self, rule: CleanupRule) -> Optional[_ActiveSweep]:
        key = self._rule_key(rule)
        with self._sweep_lock:
            existing = self._active_sweeps.get(key)
            if existing is not None and not existing.closed:
                return existing
            if existing is not None:
                self._active_sweeps.pop(key, None)

        if not rule.directory.exists():
            return None
        try:
            ctx = self._scandir(rule.directory)
            iterator = iter(ctx)
        except OSError as exc:
            from src.core.log_privacy import exception_log_label

            self.logger.warning(
                "无法扫描清理目录 rule=%s err=%s",
                rule.name,
                exception_log_label(exc),
            )
            return None
        sweep = _ActiveSweep(
            key=key,
            directory=Path(rule.directory),
            ctx=ctx,
            iterator=iterator,
        )
        with self._sweep_lock:
            # 竞态下已有则关闭新建的
            other = self._active_sweeps.get(key)
            if other is not None and not other.closed:
                sweep.close()
                return other
            self._active_sweeps[key] = sweep
            return sweep

    def _cleanup_rule(
        self,
        rule: CleanupRule,
        *,
        scan_budget: int,
        delete_budget: int,
    ) -> tuple[int, int]:
        """
        按单条规则有界流式扫描并删除过期文件。

        每次底层 ``next()`` 均计入 ``scanned``；跨批次复用同一 scandir。

        Returns:
            ``(deleted, scanned)``。
        """
        if scan_budget <= 0 or delete_budget <= 0:
            return 0, 0
        if self.safety_policy and not self.safety_policy.is_allowed(Operation.FILE_DELETE):
            return 0, 0

        sweep = self._get_or_open_sweep(rule)
        if sweep is None:
            return 0, 0

        deleted = 0
        scanned = 0
        now = time.time()
        exhausted = False
        try:
            while scanned < scan_budget and deleted < delete_budget:
                try:
                    entry = next(sweep.iterator)
                except StopIteration:
                    exhausted = True
                    break
                scanned += 1  # 每次 next 都必须计入硬预算
                try:
                    is_file = entry.is_file(follow_symlinks=False)
                except TypeError:
                    is_file = entry.is_file()
                except OSError:
                    continue
                if not is_file:
                    continue
                name = entry.name
                if not _matches_any_pattern(name, rule.patterns):
                    continue
                try:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except TypeError:
                        st = entry.stat()
                    except OSError:
                        continue
                    age = now - st.st_mtime
                    if age <= rule.max_age_seconds:
                        continue
                    file_path = Path(entry.path)
                    if self.safety_policy and not self.safety_policy.is_path_deletable(
                        file_path
                    ):
                        self.logger.debug(
                            "跳过非白名单文件 rule=%s", rule.name
                        )
                        continue
                    file_path.unlink()
                    deleted += 1
                    self.logger.debug(
                        "删除过期 rule=%s age_hours=%.1f",
                        rule.name,
                        age / 3600,
                    )
                except OSError as exc:
                    from src.core.log_privacy import exception_log_label

                    self.logger.warning(
                        "无法删除过期文件 rule=%s err=%s",
                        rule.name,
                        exception_log_label(exc),
                    )
        except OSError as exc:
            from src.core.log_privacy import exception_log_label

            self.logger.warning(
                "扫描目录失败 rule=%s err=%s",
                rule.name,
                exception_log_label(exc),
            )
            self._close_sweep(sweep.key)
            return deleted, scanned

        if exhausted:
            self._close_sweep(sweep.key)
        # 未耗尽：保留活动 sweep，由短延迟续批或下次 cleanup_now 继续

        if deleted:
            self.logger.info("[%s] 清理完成，删除 %d 个文件", rule.name, deleted)
        return deleted, scanned

    def start_auto_cleanup(self) -> None:
        """启动后台定时清理；启动线程不同步扫描目录。"""
        if self._running:
            self.logger.warning("自动清理已在运行")
            return
        self._running = True
        self.logger.info(
            "启动自动清理（间隔 %.0f 分钟，规则 %d 条，扫描上限 %d，删除上限 %d）",
            self.cleanup_interval_seconds / 60,
            len(self.rules),
            self.max_scan_files,
            self.max_delete_files,
        )
        for rule in self.rules:
            self.logger.info(
                "  - rule=%s retention=%s",
                rule.name,
                _format_retention(rule.max_age_seconds),
            )
        first_delay = 0.01 if self.cleanup_on_start else float(self.cleanup_interval_seconds)
        self._schedule_next_cleanup(delay=first_delay)

    def _cleanup_all(self) -> None:
        """执行一轮清理并捕获异常，避免定时器线程崩溃。"""
        try:
            deleted = self.cleanup_all()
            if deleted > 0:
                self.logger.info("自动清理完成，共删除 %d 个过期文件", deleted)
        except Exception as exc:
            from src.core.log_privacy import exception_log_label

            self.logger.error(
                "自动清理失败 err=%s", exception_log_label(exc)
            )
            self._close_all_sweeps()

    def _schedule_next_cleanup(self, *, delay: Optional[float] = None) -> None:
        """安排下一次定时清理（守护线程）。"""
        if not self._running:
            return
        wait = self.cleanup_interval_seconds if delay is None else delay
        self._cleanup_timer = Timer(wait, self._cleanup_cycle)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def _cleanup_cycle(self) -> None:
        """定时器回调：清理后若仍有活动 sweep 则短延迟续批。"""
        if not self._running:
            return
        self._cleanup_all()
        if not self._running:
            return
        if self._has_active_sweeps():
            self._schedule_next_cleanup(delay=self._continue_delay_s)
        else:
            self._schedule_next_cleanup()

    def stop_auto_cleanup(self) -> None:
        """停止自动清理、取消定时器并关闭全部活动 scandir。"""
        self._running = False
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None
        self._close_all_sweeps()
        self.logger.info("自动清理已停止")

    def cleanup_now(self) -> int:
        """
        立即执行一轮有界清理（不受定时器影响；仍受扫描/删除上限约束）。

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
