# -*- coding: utf-8 -*-
"""CosyVoice 固定资产路径：词法布局 + 逐级拒绝 symlink/junction/reparse（stdlib only）。"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Literal, Optional

_REPARSE_ATTR = 0x400


def _path_exists_nofollow(path: Path) -> bool:
    try:
        return bool(path.exists(follow_symlinks=False))
    except TypeError:
        return path.exists() or path.is_symlink()
    except OSError:
        return False


def _path_is_dir_nofollow(path: Path) -> bool:
    try:
        return bool(path.is_dir(follow_symlinks=False))
    except TypeError:
        return path.is_dir()


def _path_is_file_nofollow(path: Path) -> bool:
    try:
        return bool(path.is_file(follow_symlinks=False))
    except TypeError:
        return path.is_file()


def is_reparse_or_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junc = getattr(path, "is_junction", None)
        if callable(is_junc) and is_junc():
            return True
    except OSError:
        return True
    try:
        st = os.lstat(path)
        attrs = getattr(st, "st_file_attributes", 0) or 0
        if attrs & _REPARSE_ATTR:
            return True
    except OSError:
        pass
    try:
        st = os.stat(path, follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0) or 0
        if attrs & _REPARSE_ATTR:
            return True
    except OSError:
        pass
    return False


def _assert_ordinary_dir(path: Path) -> None:
    if is_reparse_or_link(path):
        raise PermissionError("路径组件不得为 symlink/junction/reparse")
    if not _path_exists_nofollow(path):
        raise PermissionError("路径组件不存在")
    if not _path_is_dir_nofollow(path):
        raise PermissionError("路径组件必须是普通目录")
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise PermissionError("路径组件无法 stat") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise PermissionError("路径组件必须是普通目录")


def _assert_ordinary_file(path: Path) -> None:
    if is_reparse_or_link(path):
        raise PermissionError("路径不得为 symlink/junction/reparse")
    if not _path_exists_nofollow(path):
        raise PermissionError("路径不是普通文件")
    if not _path_is_file_nofollow(path):
        raise PermissionError("路径不是普通文件")
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise PermissionError("路径无法 stat") from exc
    if not stat.S_ISREG(st.st_mode):
        raise PermissionError("路径不是普通文件")


def _assert_existing_ancestors(path: Path, stop_at: Path) -> None:
    """自 path 向上至 stop_at：每个已存在组件须为普通目录（目标自身可为文件）。"""
    cur = Path(path)
    stop = Path(stop_at)
    while True:
        if (
            _path_exists_nofollow(cur)
            or cur.is_symlink()
            or is_reparse_or_link(cur)
        ):
            if cur == path and _path_is_file_nofollow(cur):
                _assert_ordinary_file(cur)
            else:
                _assert_ordinary_dir(cur)
        if cur == stop:
            _assert_ordinary_dir(stop)
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent


def assert_trusted_repo_path(
    repo_root: Path,
    target: Path,
    *,
    kind: Optional[Literal["file", "dir"]] = None,
) -> Path:
    """
    校验词法路径在仓库内；对已存在祖先与目标做 symlink/junction/reparse 拒绝。

    未创建的末级目录（如 cache）仅检查已有祖先，不 mkdir。
    """
    repo = Path(repo_root)
    lexical = Path(target)
    if not lexical.is_absolute():
        lexical = repo / lexical
    try:
        lexical.relative_to(repo)
    except ValueError as exc:
        raise PermissionError("路径逃出受信仓库") from exc

    _assert_existing_ancestors(lexical, repo)

    if kind == "file" and _path_exists_nofollow(lexical):
        _assert_ordinary_file(lexical)
    elif kind == "dir" and _path_exists_nofollow(lexical):
        _assert_ordinary_dir(lexical)

    try:
        resolved = lexical.resolve()
        resolved.relative_to(repo.resolve())
    except ValueError as exc:
        raise PermissionError("解析后路径逃出受信仓库") from exc
    except OSError as exc:
        raise PermissionError("路径无法解析") from exc
    return lexical


def seal_trusted_repo_root(raw: Path) -> Path:
    """封存用仓库根：拒绝 reparse/link，须为普通目录。"""
    root = Path(raw)
    if is_reparse_or_link(root):
        raise PermissionError("仓库根不得为 symlink/junction/reparse")
    try:
        resolved = root.resolve()
    except OSError as exc:
        raise PermissionError("仓库根无效") from exc
    if is_reparse_or_link(resolved):
        raise PermissionError("仓库根不得为 symlink/junction/reparse")
    _assert_ordinary_dir(resolved)
    return resolved
