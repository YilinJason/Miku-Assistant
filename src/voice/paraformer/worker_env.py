# -*- coding: utf-8 -*-
"""Paraformer worker 最小环境 allowlist（CPU-only、离线）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

from src.voice.paraformer.assets import fixed_layout_paths


def build_asr_worker_env(
    *,
    repo_root: Path,
    model_dir: Path | None = None,
    cpu_threads: int,
) -> Dict[str, str]:
    if cpu_threads < 1 or cpu_threads > 4:
        raise ValueError("cpu_threads")
    layout = fixed_layout_paths(repo_root)
    expected_model = layout["model_dir"].resolve()
    model = (model_dir or expected_model).resolve()
    if model != expected_model:
        raise PermissionError("model_dir")
    root = layout["repo_root"].resolve()
    env: Dict[str, str] = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "PATH": os.environ.get("PATH", ""),
        "MIKU_REPO_ROOT": str(root),
        "MIKU_PARAFORMER_MODEL_DIR": str(model),
        "MIKU_PARAFORMER_CPU_THREADS": str(cpu_threads),
        "CUDA_VISIBLE_DEVICES": "-1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "FUNASR_DISABLE_UPDATE": "1",
        "OMP_NUM_THREADS": str(cpu_threads),
        "MKL_NUM_THREADS": str(cpu_threads),
        "OPENBLAS_NUM_THREADS": str(cpu_threads),
        "NUMEXPR_NUM_THREADS": "1",
        "TORCH_NUM_INTEROP_THREADS": "1",
        "TORCH_FORCE_WEIGHTS_ONLY_LOAD": "1",
        "PYTHONUTF8": "1",
    }
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "PYTHONPATH",
        "PYTHONHOME",
    ):
        env.pop(key, None)
    return env
