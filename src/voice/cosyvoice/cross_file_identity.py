# -*- coding: utf-8 -*-
"""跨进程可比的文件身份（CosyVoice 父子协议专用；不替代全局 capture_file_identity）。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


@dataclass(frozen=True)
class PortableFileIdentity:
    """``dev``/``ino`` 在父子进程间语义一致（Windows 为 FILE_ID_INFO，其它平台为 st_dev/st_ino）。"""

    dev: int
    ino: int


def portable_identity_equal(
    dev_a: int, ino_a: int, dev_b: int, ino_b: int
) -> bool:
    return int(dev_a) == int(dev_b) and int(ino_a) == int(ino_b)


def _identity_from_stat(st: os.stat_result) -> PortableFileIdentity:
    return PortableFileIdentity(dev=int(st.st_dev), ino=int(st.st_ino))


def _file_id_info_from_os_handle(fd: int) -> PortableFileIdentity:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FILE_ID_128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_byte * 16)]

    class FILE_ID_INFO(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_uint64),
            ("FileId", FILE_ID_128),
        ]

    _FileIdInfo = 18
    kernel32 = ctypes.windll.kernel32
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    get_info.restype = wintypes.BOOL

    handle = msvcrt.get_osfhandle(fd)
    if handle in (-1, 0):
        raise OSError("invalid_osfhandle")
    info = FILE_ID_INFO()
    ok = get_info(
        wintypes.HANDLE(handle),
        _FileIdInfo,
        ctypes.byref(info),
        wintypes.DWORD(ctypes.sizeof(info)),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "GetFileInformationByHandleEx")
    file_id = bytes(info.FileId.Identifier)
    ino = int.from_bytes(file_id, byteorder="little", signed=False)
    return PortableFileIdentity(dev=int(info.VolumeSerialNumber), ino=ino)


def capture_portable_file_identity(path: PathLike) -> PortableFileIdentity:
    """从路径读取 portable 身份（仅用于身份；调用方须保留既有信任边界检查）。"""
    if os.name != "nt":
        st = os.stat(path, follow_symlinks=False)
        return _identity_from_stat(st)
    fd = os.open(os.fspath(path), os.O_RDONLY | os.O_BINARY)
    try:
        return capture_portable_file_identity_fd(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def capture_portable_file_identity_fd(fd: int) -> PortableFileIdentity:
    if os.name != "nt":
        st = os.fstat(fd)
        return _identity_from_stat(st)
    return _file_id_info_from_os_handle(fd)
