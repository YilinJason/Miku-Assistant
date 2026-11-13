"""Win64 PERFORMANCE_INFORMATION injection helpers (mirrors production ABI)."""
from __future__ import annotations

import ctypes
import sys
from typing import Callable, Optional


def assert_win64_performance_abi() -> None:
    from src.core.host_memory import performance_information_type

    pi = performance_information_type()
    assert ctypes.sizeof(pi) == 104
    assert pi.cb.offset == 0
    assert pi.PageSize.offset == 80
    assert pi.HandleCount.offset == 88
    assert pi.ThreadCount.offset == 96


def populate_performance_information(
    info: object,
    *,
    page_size: int,
    physical_available_pages: int,
    physical_total_pages: int,
    commit_limit_pages: int,
    commit_total_pages: int,
) -> None:
    from src.core.host_memory import performance_information_type

    info.cb = ctypes.sizeof(performance_information_type())  # type: ignore[attr-defined]
    info.PageSize = page_size
    info.PhysicalAvailable = physical_available_pages
    info.PhysicalTotal = physical_total_pages
    info.CommitLimit = commit_limit_pages
    info.CommitTotal = commit_total_pages
    info.SystemCache = 0
    info.KernelTotal = 0
    info.KernelPaged = 0
    info.KernelNonpaged = 0
    info.HandleCount = 1
    info.ProcessCount = 1
    info.ThreadCount = 1


def make_get_performance_info_injector(
    *,
    page_size: int,
    physical_available_pages: int,
    physical_total_pages: int,
    commit_limit_pages: int,
    commit_total_pages: int,
) -> Callable:
    from src.core.host_memory import performance_information_type

    PI = performance_information_type()

    def fake_get(info_ptr, _cb):
        info = ctypes.cast(info_ptr, ctypes.POINTER(PI)).contents
        populate_performance_information(
            info,
            page_size=page_size,
            physical_available_pages=physical_available_pages,
            physical_total_pages=physical_total_pages,
            commit_limit_pages=commit_limit_pages,
            commit_total_pages=commit_total_pages,
        )
        return True

    return fake_get


def snapshot_from_injected_pages(
    *,
    page_size: int,
    physical_available_pages: int,
    physical_total_pages: int,
    commit_limit_pages: int,
    commit_total_pages: int,
):
    if sys.platform != "win32":
        return None
    from src.core.host_memory import (
        performance_information_type,
        snapshot_from_performance_information,
    )

    pi = performance_information_type()
    info = pi()
    populate_performance_information(
        info,
        page_size=page_size,
        physical_available_pages=physical_available_pages,
        physical_total_pages=physical_total_pages,
        commit_limit_pages=commit_limit_pages,
        commit_total_pages=commit_total_pages,
    )
    return snapshot_from_performance_information(info)
