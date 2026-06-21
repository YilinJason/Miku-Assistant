"""
应用日志初始化模块。

配置根 logger：控制台输出 + ``logs/`` 目录轮转落盘（``RotatingFileHandler``），
日志级别与文件参数由 ``assistant.log_level`` / ``assistant.logging`` 控制。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.paths import LOGS_DIR, ensure_runtime_dirs


def setup_app_logging(app_config) -> Optional[Path]:
    """
    根据应用配置初始化根日志处理器。

    Args:
        app_config: ``Config`` 实例或具备 ``get`` 方法的配置对象。

    Returns:
        启用文件日志时返回日志文件 ``Path``；``file_enabled=False`` 时返回 ``None``。
    """
    ensure_runtime_dirs()

    log_level_name = app_config.get("assistant.log_level", "INFO")
    level = getattr(logging, str(log_level_name).upper(), logging.INFO)
    log_cfg: Dict[str, Any] = app_config.get("assistant.logging", {}) or {}

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(level)

    _ensure_console_handler(root, formatter)

    if not log_cfg.get("file_enabled", True):
        return None

    log_path = LOGS_DIR / str(log_cfg.get("file_name", "miku.log"))
    if not _has_handler_for_file(root, log_path):
        max_bytes = int(float(log_cfg.get("max_size_mb", 10)) * 1024 * 1024)
        backup_count = int(log_cfg.get("backup_count", 5))
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    logging.getLogger(__name__).info("日志文件: %s", log_path)
    return log_path


def _ensure_console_handler(root: logging.Logger, formatter: logging.Formatter) -> None:
    """
    确保根 logger 存在标准输出 StreamHandler（避免重复添加）。

    Args:
        root: 根 logger 实例。
        formatter: 应用于控制台 handler 的格式器。
    """
    for handler in root.handlers:
        # 排除 RotatingFileHandler（其亦为 StreamHandler 子类）
        if type(handler) is logging.StreamHandler and not isinstance(
            handler, RotatingFileHandler
        ):
            handler.setFormatter(formatter)
            return
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)


def _has_handler_for_file(root: logging.Logger, log_path: Path) -> bool:
    """
    检查根 logger 是否已绑定指向同一文件的 RotatingFileHandler。

    Args:
        root: 根 logger 实例。
        log_path: 目标日志文件路径。

    Returns:
        已存在等价 handler 时为 ``True``。
    """
    target = str(log_path.resolve())
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler):
            base = getattr(handler, "baseFilename", "")
            if base and str(Path(base).resolve()) == target:
                return True
    return False
