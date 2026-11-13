# -*- coding: utf-8 -*-
"""CosyVoice 子进程最小环境（allowlist + 固定离线缓存路径）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Mapping

from src.core.owned_worker_job import JOB_ENV_NAME, JOB_ENV_PARENT_PID

# Windows/Python/CUDA 运行所需的非敏感系统项（显式 allowlist，不 copy 全环境）
ENV_ALLOWLIST: tuple[str, ...] = (
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "SystemDrive",
    "SYSTEMDRIVE",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_ARCHITEW6432",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "OS",
    "PROMPT",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "PUBLIC",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "COMMONPROGRAMFILES",
    "CommonProgramFiles(x86)",
    "CUDA_PATH",
    "CUDA_PATH_V11_8",
    "CUDA_PATH_V12_1",
    "CUDA_PATH_V12_2",
    "CUDA_PATH_V12_4",
    "CUDA_VISIBLE_DEVICES",
)

_BLOCKED_SUB = ("token", "key", "secret", "credential", "password", "proxy")


def build_worker_env(
    *,
    cache_root: Path,
    environ: Mapping[str, str] | None = None,
    cpu_threads: int = 4,
) -> Dict[str, str]:
    """
    最小权限环境：只复制 allowlist 系统项，再写入固定离线/缓存变量。

    显式不传播 PYTHONPATH/PYTHONHOME、代理，以及 token/key/secret/credential。
    """
    source = environ if environ is not None else os.environ
    env: Dict[str, str] = {}
    for key in ENV_ALLOWLIST:
        val = source.get(key)
        if val is not None:
            env[key] = val
    cache = cache_root.resolve()
    env["PYTHONUTF8"] = "1"
    env["HF_HOME"] = str(cache / "hf")
    env["MODELSCOPE_CACHE"] = str(cache / "modelscope")
    env["MPLCONFIGDIR"] = str(cache / "mpl")
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    n = max(1, min(4, int(cpu_threads)))
    env["OMP_NUM_THREADS"] = str(n)
    env["MKL_NUM_THREADS"] = str(n)
    env["OPENBLAS_NUM_THREADS"] = str(n)
    env["NUMEXPR_NUM_THREADS"] = str(n)
    for k in list(env):
        low = k.lower()
        if low in ("pythonpath", "pythonhome"):
            env.pop(k, None)
            continue
        if any(s in low for s in _BLOCKED_SUB):
            env.pop(k, None)
    env.pop(JOB_ENV_NAME, None)
    env.pop(JOB_ENV_PARENT_PID, None)
    return env
