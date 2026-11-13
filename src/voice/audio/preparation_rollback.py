"""音频准备失败时的逐路径 TFM 回滚意图（不直接 unlink）。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.voice.audio.temp_files import TempFileManager


def request_owned_paths_rollback(
    manager: "TempFileManager",
    *paths: str,
    primary: BaseException | None = None,
) -> None:
    """
    对已分配路径逐个 ``remove``；首个清理失败仍继续其它路径。

    ``primary`` 非 ``None`` 时清理阶段任意 ``BaseException`` 不覆盖主异常；
    无主异常时首个清理失败会向上抛出。
    """
    cleanup_error: BaseException | None = None
    for path in paths:
        if not path:
            continue
        try:
            manager.remove(path)
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
    if primary is not None:
        return
    if cleanup_error is not None:
        raise cleanup_error
