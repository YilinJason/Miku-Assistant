# -*- coding: utf-8 -*-
"""RVC 子进程 CPU / BLAS / Torch 线程预算（模块本身仅 stdlib）。"""
from __future__ import annotations

import os
import sys
from typing import Dict, Mapping

_BLAS_THREAD_KEYS: tuple[str, ...] = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)
_BLAS_KEY_BY_FOLD: dict[str, str] = {k.casefold(): k for k in _BLAS_THREAD_KEYS}


def _strip_blas_thread_env_variants(env: Dict[str, str]) -> None:
    """去掉四键任意大小写变体，避免 Windows 环境下 duplicate 绕过预算。"""
    for key in list(env.keys()):
        if key.casefold() in _BLAS_KEY_BY_FOLD:
            del env[key]


def compute_rvc_cpu_thread_budget(*, cpu_count: int | None = None) -> int:
    """固定保守默认：max(1, min(4, (cpu_count or os.cpu_count() or 2) // 2))。"""
    ncpus = cpu_count if cpu_count is not None else (os.cpu_count() or 2)
    return max(1, min(4, ncpus // 2))


def child_env_with_rvc_cpu_budget(
    base: Mapping[str, str],
    *,
    cpu_count: int | None = None,
) -> Dict[str, str]:
    """
    从给定环境 copy 出子进程 env，强制写入 BLAS/OpenMP 线程键。

    不修改 ``base`` 或父 ``os.environ``；恶意超大值会被覆盖而非 setdefault。
    """
    env = dict(base)
    _strip_blas_thread_env_variants(env)
    n = compute_rvc_cpu_thread_budget(cpu_count=cpu_count)
    s = str(n)
    for key in _BLAS_THREAD_KEYS:
        env[key] = s
    return env


def apply_rvc_torch_thread_budget(*, cpu_count: int | None = None) -> int:
    """
    在 RVC 脚本子进程内、首次 ``import torch`` 之后、并行工作之前调用一次。

    仅设置 Torch 线程；BLAS/OpenMP 由主进程 spawn 前的 ``child_env_with_rvc_cpu_budget`` 注入。
    不修改 ``os.environ``。失败时受控退出，避免吞异常后以默认无限线程继续跑。
    """
    import torch

    n = compute_rvc_cpu_thread_budget(cpu_count=cpu_count)
    try:
        torch.set_num_threads(n)
        torch.set_num_interop_threads(1)
    except RuntimeError as exc:
        print(f"RVC CPU thread budget failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    return n
