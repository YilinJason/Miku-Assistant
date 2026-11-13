# -*- coding: utf-8 -*-
"""
Paraformer-zh 本地 ASR 环境准备（.venv-asr + vendor/paraformer-zh）。

默认只读 ``--check``；仅 ``--execute`` 创建虚拟环境、pip 安装与 HF pin 下载。
占用本地磁盘，无按分钟 API 费用。不修改主程序 / RVC / Cosy 依赖。

用法::

    py scripts/prepare_paraformer.py --check
    py scripts/prepare_paraformer.py --execute

Codex 监督下由 tmp/codex_asr_owned_gate_2026_11_05.py 以 ``--execute`` 调用；
须在 pip/venv 前加入 supplied NativeJob（``worker_entry_join_owned_job``）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.core.paths import PROJECT_ROOT  # noqa: E402
from src.voice.paraformer.assets import MODEL_REVISION, SETUP_USER_HINT  # noqa: E402
from src.voice.paraformer.preparation import (  # noqa: E402
    REQUIREMENTS_ASR_REL,
    run_check,
    run_execute_setup,
)


def _print_check(repo_root: Path) -> int:
    report = run_check(repo_root)
    print("=" * 60)
    print("Paraformer ASR 准备（--check，只读）")
    print("=" * 60)
    print(f"model_revision: {MODEL_REVISION}")
    print(f"requirements: {REQUIREMENTS_ASR_REL}")
    print()
    for item in report.components:
        mark = "OK" if item.ok else "MISSING"
        detail = f" ({item.detail})" if item.detail else ""
        print(f"  [{mark}] {item.name}{detail}")
    print()
    if report.ok:
        print("就绪：可运行 diagnose_asr.py --execute 做固定音频验收。")
    else:
        print("未就绪：" + report.hint)
        print("显式安装: py scripts/prepare_paraformer.py --execute")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check",
        action="store_true",
        help="只读检查 .venv-asr 与 vendor 资产（不安装/不下载）",
    )
    group.add_argument(
        "--execute",
        action="store_true",
        help="在显式监督下创建环境、安装依赖并下载 pin 模型",
    )
    args = parser.parse_args(argv)
    repo_root = PROJECT_ROOT
    if args.check:
        return _print_check(repo_root)
    code, report_path = run_execute_setup(repo_root)
    if report_path is not None:
        print(f"setup_report={report_path.relative_to(repo_root).as_posix()}")
    if code == 0:
        print("Paraformer ASR 环境准备完成。")
    else:
        print("Paraformer ASR 环境准备失败；详见 setup_report。")
        print(SETUP_USER_HINT)
    return code


def _configure_utf8_stdio() -> None:
    if sys.platform != "win32":
        return
    import io

    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


if __name__ == "__main__":
    _configure_utf8_stdio()
    raise SystemExit(main())
