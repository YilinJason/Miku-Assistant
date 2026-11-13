# -*- coding: utf-8 -*-
"""跨平台父进程存活探测（Windows 须声明完整 ctypes 签名，避免 64 位 HANDLE 截断）。"""
from __future__ import annotations

import os
import sys


def parent_process_is_alive(parent_pid: int) -> bool:
    """无副作用；权限拒绝时保守视为仍存活。"""
    if parent_pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        error_access_denied = 5
        kernel32 = ctypes.windll.kernel32
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(synchronize, False, parent_pid)
        if not handle:
            return kernel32.GetLastError() == error_access_denied
        try:
            return wait_for_single_object(handle, 0) == wait_timeout
        finally:
            close_handle(handle)
    try:
        os.kill(parent_pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
