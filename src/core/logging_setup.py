# -*- coding: utf-8 -*-
"""
应用日志初始化模块。

配置根 logger：控制台输出 + ``logs/`` 目录轮转落盘（受隐私与路径边界约束）。
日志级别与文件参数由 ``assistant.log_level`` / ``assistant.logging`` 控制。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, List, Optional

from src.core.log_privacy import (
    PrivacyRotatingFileHandler,
    max_size_mb_to_bytes,
    require_bool,
    require_logging_mapping,
    resolve_log_file_path,
    validate_backup_count,
    validate_log_file_name,
    validate_max_size_mb,
)
from src.core.paths import LOGS_DIR, ensure_runtime_dirs


def setup_app_logging(app_config) -> Optional[Path]:
    """
    根据应用配置初始化根日志处理器。

    先完成全部配置与路径验证，再创建文件 handler；失败不得在越界位置产生文件。
    应用自有 handler 带 ``_miku_app_owned`` 标记：关闭文件日志时移除并关闭；
    文件名变化时先成功创建新 handler 再关闭旧的；同路径参数更新持有 handler lock。

    Args:
        app_config: ``Config`` 实例或具备 ``get`` 方法的配置对象。

    Returns:
        启用文件日志时返回日志文件 ``Path``；``file_enabled=False`` 时返回 ``None``。
    """
    ensure_runtime_dirs()

    log_level_name = app_config.get("assistant.log_level", "INFO")
    level = getattr(logging, str(log_level_name).upper(), logging.INFO)
    log_cfg = require_logging_mapping(app_config.get("assistant.logging", {}) or {})

    file_enabled = require_bool(
        "assistant.logging.file_enabled",
        log_cfg["file_enabled"] if "file_enabled" in log_cfg else True,
    )
    include_tracebacks = require_bool(
        "assistant.logging.include_tracebacks",
        log_cfg["include_tracebacks"]
        if "include_tracebacks" in log_cfg
        else False,
    )

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(level)

    _ensure_console_handler(root, formatter)

    if not file_enabled:
        _remove_owned_file_handlers(root)
        return None

    # 全部校验完成后再创建 / 更新 handler
    file_name = validate_log_file_name(log_cfg.get("file_name", "miku.log"))
    max_size_mb = validate_max_size_mb(log_cfg.get("max_size_mb", 10))
    backup_count = validate_backup_count(log_cfg.get("backup_count", 5))
    log_path = resolve_log_file_path(LOGS_DIR, file_name)
    max_bytes = max_size_mb_to_bytes(max_size_mb)

    handler = _ensure_privacy_file_handler(
        root,
        log_path,
        max_bytes=max_bytes,
        backup_count=backup_count,
        include_tracebacks=include_tracebacks,
        formatter=formatter,
    )

    logging.getLogger(__name__).info(
        "日志文件已启用 name=%s max_size_mb=%s backup_count=%s include_tracebacks=%s",
        file_name,
        max_bytes / (1024 * 1024),
        handler.backupCount,
        bool(handler.include_tracebacks),
    )
    return log_path


def _ensure_console_handler(root: logging.Logger, formatter: logging.Formatter) -> None:
    for handler in root.handlers:
        if type(handler) is logging.StreamHandler:
            handler.setFormatter(formatter)
            return
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)


def _same_log_path(handler: logging.Handler, log_path: Path) -> bool:
    base = getattr(handler, "baseFilename", "")
    if not base:
        return False
    try:
        return Path(base).resolve() == log_path.resolve()
    except OSError:
        return False


def _is_owned_file_handler(handler: logging.Handler) -> bool:
    return bool(getattr(handler, "_miku_app_owned", False)) and isinstance(
        handler, RotatingFileHandler
    )


def _collect_owned_handlers(root: logging.Logger) -> List[logging.Handler]:
    return [h for h in list(root.handlers) if _is_owned_file_handler(h)]


def _close_and_remove(root: logging.Logger, handler: logging.Handler) -> None:
    root.removeHandler(handler)
    try:
        handler.close()
    except Exception:
        pass


def _remove_owned_file_handlers(root: logging.Logger) -> None:
    for handler in _collect_owned_handlers(root):
        _close_and_remove(root, handler)


def _ensure_privacy_file_handler(
    root: logging.Logger,
    log_path: Path,
    *,
    max_bytes: int,
    backup_count: int,
    include_tracebacks: bool,
    formatter: logging.Formatter,
) -> PrivacyRotatingFileHandler:
    """
    复用 / 原子更新 / 切换应用自有隐私 handler。

    - 同路径已有自有隐私 handler：持锁原子更新参数。
    - 路径变化：先完整创建新 handler 再关闭旧自有 handler；创建失败不破坏旧 handler。
    - 同路径非隐私 RotatingFileHandler：关闭并替换。
    """
    if max_bytes <= 0:
        raise ValueError("maxBytes 必须为正整数")

    owned = _collect_owned_handlers(root)
    same_path_privacy = [
        h
        for h in root.handlers
        if isinstance(h, PrivacyRotatingFileHandler) and _same_log_path(h, log_path)
    ]

    if same_path_privacy:
        primary = same_path_privacy[0]
        primary._miku_app_owned = True
        # 持有 handler lock，调用者只观察到完整配置
        with primary.lock:
            primary.maxBytes = max_bytes
            primary.backupCount = backup_count
            primary.include_tracebacks = bool(include_tracebacks)
            primary.setFormatter(formatter)
        for extra in _collect_owned_handlers(root):
            if extra is not primary:
                _close_and_remove(root, extra)
        for handler in list(root.handlers):
            if (
                handler is not primary
                and isinstance(handler, RotatingFileHandler)
                and _same_log_path(handler, log_path)
                and not _is_owned_file_handler(handler)
            ):
                _close_and_remove(root, handler)
        return primary

    # 路径变化或尚无隐私 handler：先创建成功，再移除旧自有
    new_handler = PrivacyRotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        include_tracebacks=include_tracebacks,
        trusted_logs_dir=log_path.parent,
    )
    new_handler._miku_app_owned = True
    new_handler.setFormatter(formatter)
    root.addHandler(new_handler)

    for handler in _collect_owned_handlers(root):
        if handler is not new_handler:
            _close_and_remove(root, handler)

    # 同路径非自有普通 RotatingFileHandler
    for handler in list(root.handlers):
        if (
            handler is not new_handler
            and isinstance(handler, RotatingFileHandler)
            and _same_log_path(handler, log_path)
            and not _is_owned_file_handler(handler)
        ):
            _close_and_remove(root, handler)

    return new_handler
