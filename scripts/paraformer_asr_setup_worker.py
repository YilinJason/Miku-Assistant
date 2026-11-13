# -*- coding: utf-8 -*-
"""
Paraformer ASR 环境准备（单进程固定 mode；Win32 下先加入 supplied NativeJob）。

由 preparation 以 argv 列表启动；worker 从脚本路径推导仓库根与固定布局。
禁止 subprocess / 字符串 -c；pip/venv 在同一 Job 进程内执行。
"""
from __future__ import annotations

import argparse
import json
import runpy
import sys
import venv
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

SETUP_MODES = frozenset(
    {"venv_create", "pip_install", "env_verify", "pip_freeze"},
)


def _join_owned_job_before_work() -> None:
    if sys.platform != "win32":
        return
    from src.core.owned_worker_job import worker_entry_join_owned_job

    worker_entry_join_owned_job()


def _repo_root() -> Path:
    return _REPO_ROOT


def _run_venv_create(repo_root: Path) -> None:
    from src.voice.paraformer.assets import fixed_layout_paths

    layout = fixed_layout_paths(repo_root)
    venv_root = layout["asr_python"].parent.parent
    venv_root.mkdir(parents=True, exist_ok=True)
    builder = venv.EnvBuilder(symlinks=False, with_pip=True)
    builder.create(str(venv_root.resolve()))


def _run_pip_install(repo_root: Path) -> None:
    from src.voice.paraformer.preparation import (
        PIP_CMD_RETRIES,
        PIP_CMD_TIMEOUT_SEC,
        requirements_path,
    )

    req = requirements_path(repo_root)
    sys.argv = [
        "pip",
        "install",
        "--isolated",
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--progress-bar",
        "off",
        "--timeout",
        str(PIP_CMD_TIMEOUT_SEC),
        "--retries",
        str(PIP_CMD_RETRIES),
        "-r",
        str(req.resolve()),
    ]
    runpy.run_module("pip", run_name="__main__")


def _run_env_verify() -> None:
    from src.voice.paraformer.preparation import collect_asr_env_identity_from_interpreter

    payload = collect_asr_env_identity_from_interpreter()
    sys.stdout.write(json.dumps(payload, separators=(",", ":")))
    sys.stdout.flush()


def _run_pip_freeze() -> None:
    sys.argv = ["pip", "freeze"]
    runpy.run_module("pip", run_name="__main__")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", required=True, choices=sorted(SETUP_MODES))
    args = parser.parse_args(argv)

    _join_owned_job_before_work()

    repo_root = _repo_root()
    mode = str(args.mode)
    if mode == "venv_create":
        _run_venv_create(repo_root)
        return 0
    if mode == "pip_install":
        _run_pip_install(repo_root)
        return 0
    if mode == "env_verify":
        _run_env_verify()
        return 0
    if mode == "pip_freeze":
        _run_pip_freeze()
        return 0
    print("setup worker: invalid mode", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
