# -*- coding: utf-8 -*-
"""Paraformer runtime worker 子进程契约（仅 SafetyPolicy.spawn_local_asr_worker）。"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping

from src.core.paths import PROJECT_ROOT
from src.core.rvc_capabilities import reject_string_command
from src.voice.cosyvoice.path_trust import assert_trusted_repo_path
from src.voice.paraformer.assets import RUNTIME_WORKER_SCRIPT_REL, fixed_layout_paths
from src.voice.paraformer.settings import ParaformerSettings
from src.voice.paraformer.worker_env import build_asr_worker_env

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalAsrWorkerLaunch:
    parent_pid: int
    settings: ParaformerSettings


def _deny(reason: str) -> PermissionError:
    logger.warning("本地 ASR worker 能力拒绝: %s", reason)
    return PermissionError(f"本地 ASR worker 能力拒绝: {reason}")


def _require_strict_parent_pid(value: Any) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise _deny("parent_pid 必须是严格正整数")
    if value <= 0:
        raise _deny("parent_pid 无效")
    return value


def _require_launch(launch: Any) -> LocalAsrWorkerLaunch:
    if not isinstance(launch, LocalAsrWorkerLaunch):
        raise _deny("launch 类型无效")
    if not isinstance(launch.settings, ParaformerSettings):
        raise _deny("settings 类型无效")
    _require_strict_parent_pid(launch.parent_pid)
    if launch.parent_pid != os.getpid():
        raise _deny("parent_pid 与启动进程不一致")
    return launch


def build_runtime_worker_argv(
    python: Path,
    worker_script: Path,
    launch: LocalAsrWorkerLaunch,
    *,
    repo_root: Path | None = None,
) -> List[str]:
    root = repo_root or PROJECT_ROOT
    launch = _require_launch(launch)
    reject_string_command([str(python)])
    try:
        assert_trusted_repo_path(root, python, kind="file")
        assert_trusted_repo_path(root, worker_script, kind="file")
    except PermissionError as exc:
        raise _deny("路径不可信") from exc
    settings = launch.settings
    return [
        str(python.resolve()),
        str(worker_script.resolve()),
        "--parent-pid",
        str(launch.parent_pid),
        "--cpu-threads",
        str(settings.cpu_threads),
    ]


def build_runtime_popen_options(
    *,
    repo_root: Path,
    cpu_threads: int,
) -> Dict[str, Any]:
    layout = fixed_layout_paths(repo_root)
    env = build_asr_worker_env(
        repo_root=layout["repo_root"],
        model_dir=layout["model_dir"],
        cpu_threads=cpu_threads,
    )
    return {
        "cwd": str(layout["repo_root"]),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "env": env,
        "text": True,
        "encoding": "utf-8",
        "errors": "strict",
        "bufsize": 1,
        "shell": False,
        "close_fds": True,
    }


def merge_local_asr_popen_kwargs(
    fixed: Mapping[str, Any],
    user_kwargs: Mapping[str, Any],
) -> Dict[str, Any]:
    reject_string_command(user_kwargs.get("args"))
    if user_kwargs:
        key = next(iter(user_kwargs))
        raise _deny(f"不允许 caller popen 选项: {key}")
    return dict(fixed)


def expected_runtime_worker_script() -> Path:
    return PROJECT_ROOT / RUNTIME_WORKER_SCRIPT_REL
