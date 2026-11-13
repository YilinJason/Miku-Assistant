# -*- coding: utf-8 -*-
"""跨进程 CosyVoice 模型锁（Windows msvcrt；忙则零 torch 导入）。"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO, Optional

from src.voice.cosyvoice.path_trust import is_reparse_or_link

_LOCK_HANDLE: Optional[BinaryIO] = None
_LEGACY_EMPTY_MAX_BYTES = 64


def _is_ordinary_existing_lock(path: Path) -> bool:
    if is_reparse_or_link(path):
        return False
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _lock_payload_valid(size: int, payload: bytes) -> bool:
    if size == 1:
        return True
    if size == 0:
        return True
    if size > _LEGACY_EMPTY_MAX_BYTES:
        return False
    return all(b == 0 for b in payload)


def _normalize_locked_payload(handle: BinaryIO) -> bool:
    try:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(0)
        if size == 1:
            return True
        if size == 0:
            handle.write(b"\0")
            handle.flush()
            return True
        if size > _LEGACY_EMPTY_MAX_BYTES:
            return False
        payload = handle.read(size)
        if not _lock_payload_valid(size, payload):
            return False
        handle.seek(0)
        handle.truncate(1)
        handle.write(b"\0")
        handle.flush()
        return True
    except OSError:
        return False


def _release_handle_quietly(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass


def try_acquire_model_lock(lock_path: Path) -> bool:
    """
    非阻塞获取模型锁；成功返回 True 并保持句柄至 ``release_model_lock``。

    在持有 OS 字节锁之前不 truncate/改写锁文件；仅在同 inode 句柄上把
    0 字节或少量全 NUL 遗留锁迁移为 1 字节。
    """
    global _LOCK_HANDLE
    if _LOCK_HANDLE is not None:
        return True
    if os.name != "nt":
        return False
    try:
        import msvcrt
    except ImportError:
        return False
    parent = lock_path.parent
    if not parent.is_dir():
        return False

    handle: Optional[BinaryIO] = None
    try:
        if not lock_path.exists():
            try:
                fd = os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                )
                handle = os.fdopen(fd, "r+b")
            except FileExistsError:
                pass
        if handle is None:
            if not _is_ordinary_existing_lock(lock_path):
                return False
            try:
                handle = open(lock_path, "r+b")
            except OSError:
                return False
    except OSError:
        return False

    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        _release_handle_quietly(handle)
        return False

    try:
        handle_st = os.fstat(handle.fileno())
    except OSError:
        _release_handle_quietly(handle)
        return False
    if not stat.S_ISREG(handle_st.st_mode):
        _release_handle_quietly(handle)
        return False
    try:
        path_st = os.stat(lock_path, follow_symlinks=False)
    except OSError:
        _release_handle_quietly(handle)
        return False
    if path_st.st_dev != handle_st.st_dev or path_st.st_ino != handle_st.st_ino:
        _release_handle_quietly(handle)
        return False

    if not _normalize_locked_payload(handle):
        _release_handle_quietly(handle)
        return False

    _LOCK_HANDLE = handle
    return True


def release_model_lock() -> None:
    global _LOCK_HANDLE
    handle = _LOCK_HANDLE
    _LOCK_HANDLE = None
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass
