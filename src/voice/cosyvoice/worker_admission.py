# -*- coding: utf-8 -*-
"""Runtime worker 资源准入（可 fake 单测；失败返回固定协议标签，零模型加载）。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from src.voice.cosyvoice.protocol import (
    ERR_ASSET_PIN,
    ERR_CUDA_UNAVAILABLE,
    ERR_INSUFFICIENT_RAM,
    ERR_INSUFFICIENT_VRAM,
    ERR_RESOURCE_UNOBSERVABLE,
)
from src.voice.cosyvoice.settings import CosyVoiceSettings
from src.voice.cosyvoice.worker_resources import (
    available_cuda_free_vram_mb,
    available_physical_ram_mb,
)


def apply_thread_env(cpu_threads: int) -> None:
    """在 import torch 之前设置 BLAS/OMP 环境（spawn env 亦应写入相同值）。"""
    n = max(1, min(4, int(cpu_threads)))
    import os

    os.environ["OMP_NUM_THREADS"] = str(n)
    os.environ["MKL_NUM_THREADS"] = str(n)
    os.environ["OPENBLAS_NUM_THREADS"] = str(n)
    os.environ["NUMEXPR_NUM_THREADS"] = str(n)


def apply_torch_thread_limits(cpu_threads: int) -> None:
    n = max(1, min(4, int(cpu_threads)))
    import torch

    torch.set_num_threads(n)
    torch.set_num_interop_threads(1)


def apply_cuda_memory_fraction(limit_mb: int, total_mb: int) -> None:
    import torch

    fraction = min(1.0, float(limit_mb) / max(1.0, float(total_mb)))
    torch.cuda.set_per_process_memory_fraction(fraction)


def run_resource_admission(
    settings: CosyVoiceSettings,
    *,
    import_torch: Callable[[], object] | None = None,
) -> Optional[str]:
    """
    RAM/VRAM/CUDA/线程与显存 fraction 准入。

    成功返回 ``None``；失败返回固定 ``ERR_*`` 标签（调用方不得加载模型）。
    """
    ram_mb = available_physical_ram_mb()
    if ram_mb is None:
        return ERR_RESOURCE_UNOBSERVABLE
    if ram_mb < settings.min_free_ram_mb:
        return ERR_INSUFFICIENT_RAM

    _torch_import = import_torch
    if _torch_import is None:
        try:
            import torch as _t

            _torch_import = lambda: _t  # noqa: E731
        except Exception:
            return ERR_CUDA_UNAVAILABLE

    try:
        torch = _torch_import()
    except Exception:
        return ERR_CUDA_UNAVAILABLE

    if not torch.cuda.is_available():
        return ERR_CUDA_UNAVAILABLE

    vram = available_cuda_free_vram_mb()
    if vram is None:
        return ERR_RESOURCE_UNOBSERVABLE
    free_mb, total_mb = vram
    if free_mb < settings.min_free_vram_mb:
        return ERR_INSUFFICIENT_VRAM

    try:
        apply_torch_thread_limits(settings.cpu_threads)
    except Exception:
        return ERR_RESOURCE_UNOBSERVABLE

    try:
        apply_cuda_memory_fraction(settings.cuda_memory_limit_mb, total_mb)
    except Exception:
        return ERR_RESOURCE_UNOBSERVABLE

    return None
