# -*- coding: utf-8 -*-
"""
CosyVoice 生产 runtime worker 专用子进程契约。

调用方只能通过 ``SafetyPolicy.spawn_local_tts_worker`` 启动；不得旁路通用 subprocess。
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping

from src.core.paths import PROJECT_ROOT
from src.core.rvc_capabilities import reject_string_command
from src.voice.cosyvoice.path_trust import assert_trusted_repo_path
from src.voice.cosyvoice.assets import RUNTIME_WORKER_SCRIPT_REL, fixed_layout_paths
from src.voice.cosyvoice.settings import CosyVoiceSettings
from src.voice.cosyvoice.worker_cli import build_worker_cli_argv
from src.voice.cosyvoice.worker_env import build_worker_env

logger = logging.getLogger(__name__)

_RUNTIME_WORKER_SCRIPT = PROJECT_ROOT / RUNTIME_WORKER_SCRIPT_REL


@dataclass(frozen=True)
class LocalTtsWorkerLaunch:
    """runtime worker 启动参数：parent_pid + 已校验 settings。"""

    parent_pid: int
    settings: CosyVoiceSettings


def _deny(reason: str) -> PermissionError:
    logger.warning("本地 TTS worker 能力拒绝: %s", reason)
    return PermissionError(f"本地 TTS worker 能力拒绝: {reason}")


def _resolve_regular_file(
    path: Path,
    *,
    label: str,
    repo_root: Path | None = None,
) -> Path:
    root = repo_root or PROJECT_ROOT
    try:
        assert_trusted_repo_path(root, path, kind="file")
    except PermissionError as exc:
        raise _deny(f"{label} 路径不可信") from exc
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise _deny(f"{label} 路径无法解析") from exc
    if not resolved.is_file():
        raise _deny(f"{label} 不是普通文件")
    return resolved


def _require_strict_parent_pid(value: Any) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise _deny("parent_pid 必须是严格正整数")
    if value <= 0:
        raise _deny("parent_pid 无效")
    return value


def expected_runtime_worker_script() -> Path:
    return _resolve_regular_file(_RUNTIME_WORKER_SCRIPT, label="runtime worker 脚本")


def build_runtime_worker_argv(
    python: Path,
    worker_script: Path,
    launch: LocalTtsWorkerLaunch,
    *,
    repo_root: Path | None = None,
) -> List[str]:
    root = repo_root or PROJECT_ROOT
    _resolve_regular_file(python, label="CosyVoice 解释器", repo_root=root)
    script = _resolve_regular_file(
        worker_script, label="runtime worker 脚本", repo_root=root
    )
    pid = _require_strict_parent_pid(launch.parent_pid)
    if not isinstance(launch.settings, CosyVoiceSettings):
        raise _deny("settings 无效")
    cli_tail = build_worker_cli_argv(pid, launch.settings)
    return [str(python), "-u", str(script)] + cli_tail


def build_runtime_popen_options(
    *,
    cache_root: Path,
    vendor_root: Path,
    environ: Mapping[str, str] | None = None,
    cpu_threads: int = 4,
) -> Dict[str, Any]:
    """固定 cwd/env/stdio；调用方不得覆盖这些键。"""
    return {
        "cwd": str(vendor_root.resolve()),
        "env": build_worker_env(
            cache_root=cache_root, environ=environ, cpu_threads=cpu_threads
        ),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "strict",
        "bufsize": 1,
        "shell": False,
    }


def merge_local_tts_popen_kwargs(
    fixed: Mapping[str, Any],
    user_kwargs: Mapping[str, Any],
) -> Dict[str, Any]:
    reject_string_command(user_kwargs.get("args"))
    if user_kwargs:
        key = next(iter(user_kwargs))
        raise _deny(f"不允许 caller popen 选项: {key}")
    return dict(fixed)


def layout_for_repo(repo_root: Path | None = None) -> dict[str, Path]:
    root = repo_root or PROJECT_ROOT
    return fixed_layout_paths(root)
