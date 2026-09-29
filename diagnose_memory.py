# -*- coding: utf-8 -*-
"""长期记忆真实文本验收入口（默认 dry-run，显式 --execute 才访问本机 Ollama）。

始终使用合成对话与临时数据库；不污染正式记忆，不启动语音/设备。

用法::

    python diagnose_memory.py
    python diagnose_memory.py --execute
    python diagnose_memory.py --execute --report tmp/memory-report.json
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent))

from src.core.config import Config
from src.memory.diagnostic import (
    EXIT_CONTRACT,
    EXIT_PASS,
    EXIT_QUALITY,
    format_console_summary,
    run_diagnostic,
)
from src.memory.errors import MemoryValidationError


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="长期记忆合成文本验收（默认 dry-run，不联网）"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="显式对本机 Ollama 发起真实 HTTP；使用临时 SQLite",
    )
    parser.add_argument("--config", type=str, default=None, help="可选配置文件路径")
    parser.add_argument("--base-url", type=str, default=None, help="仅本次覆盖 base_url")
    parser.add_argument("--model", type=str, default=None, help="仅本次覆盖 model")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="仅本次覆盖 extractor/HTTP timeout（秒）",
    )
    parser.add_argument(
        "--keep-alive",
        type=str,
        default=None,
        help="仅本次覆盖诊断 keep_alive（默认 1m，不继承主聊天 30m）",
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=None,
        help="仅本次覆盖提取器 num_ctx（默认 inherit/null，不写入 options）",
    )
    parser.add_argument(
        "--num-predict",
        type=int,
        default=None,
        help="仅本次覆盖提取器 num_predict",
    )
    parser.add_argument(
        "--report",
        type=str,
        default=None,
        help="可选 JSON 报告路径；未指定则只打印控制台摘要",
    )
    return parser


def _load_config(path: str | None) -> Config:
    if path is None:
        return Config()
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise SystemExit(f"config not found: {cfg_path}")
    return Config(str(cfg_path))


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        config = _load_config(args.config)
        report = run_diagnostic(
            config,
            execute=bool(args.execute),
            base_url=args.base_url,
            model=args.model,
            timeout=args.timeout,
            keep_alive=args.keep_alive,
            num_ctx=args.num_ctx,
            num_predict=args.num_predict,
            report_path=Path(args.report) if args.report else None,
        )
    except MemoryValidationError as exc:
        print(f"参数/配置错误: {exc}", file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"诊断失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CONTRACT

    print(format_console_summary(report))
    return int(report.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
