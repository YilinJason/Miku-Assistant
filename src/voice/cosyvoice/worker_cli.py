# -*- coding: utf-8 -*-
"""Runtime worker 固定 argv 构建与严格解析（父/子共用）。"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from typing import List, Sequence

from src.voice.cosyvoice.settings import CosyVoiceSettings, _BOUNDS, _DEFAULTS


@dataclass(frozen=True)
class ParsedWorkerCli:
    parent_pid: int
    settings: CosyVoiceSettings


def _strict_int(value: str, *, path: str, lo: int, hi: int) -> int:
    if value.lower() in ("true", "false", "nan", "inf", "-inf"):
        raise ValueError(path)
    try:
        if "." in value or "e" in value.lower():
            f = float(value)
            if not math.isfinite(f) or not f.is_integer():
                raise ValueError(path)
            n = int(f)
        else:
            n = int(value, 10)
    except (ValueError, OverflowError):
        raise ValueError(path) from None
    if isinstance(n, bool):
        raise ValueError(path)
    if n < lo or n > hi:
        raise ValueError(path)
    return n


def build_settings_argv(settings: CosyVoiceSettings) -> List[str]:
    """将已校验 settings 展开为固定 CLI 标志（顺序稳定）。"""
    return [
        "--startup-timeout-sec",
        str(settings.startup_timeout_sec),
        "--synth-timeout-sec",
        str(settings.synth_timeout_sec),
        "--idle-timeout-sec",
        str(settings.idle_timeout_sec),
        "--max-text-chars",
        str(settings.max_text_chars),
        "--cpu-threads",
        str(settings.cpu_threads),
        "--min-free-ram-mb",
        str(settings.min_free_ram_mb),
        "--min-free-vram-mb",
        str(settings.min_free_vram_mb),
        "--cuda-memory-limit-mb",
        str(settings.cuda_memory_limit_mb),
    ]


def build_worker_cli_argv(
    parent_pid: int,
    settings: CosyVoiceSettings,
) -> List[str]:
    if isinstance(parent_pid, bool) or not isinstance(parent_pid, int) or parent_pid <= 0:
        raise ValueError("parent_pid")
    return ["--parent-pid", str(parent_pid)] + build_settings_argv(settings)


def parse_worker_cli(argv: Sequence[str]) -> ParsedWorkerCli:
    """严格解析 worker CLI；未知标志/非有限值一律拒绝。"""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--parent-pid", required=True)
    for key in _DEFAULTS:
        flag = "--" + key.replace("_", "-")
        parser.add_argument(flag, required=True)
    try:
        ns, extras = parser.parse_known_args(list(argv))
    except SystemExit as exc:
        raise ValueError("cli_parse") from exc
    if extras:
        raise ValueError("unknown_flag")

    fields: dict[str, int] = {}
    for key in _DEFAULTS:
        attr = key
        raw = getattr(ns, attr)
        lo, hi = _BOUNDS[key]
        fields[key] = _strict_int(str(raw), path=key, lo=lo, hi=hi)
    parent = _strict_int(str(ns.parent_pid), path="parent_pid", lo=1, hi=2**31 - 1)
    settings = CosyVoiceSettings(**fields)
    return ParsedWorkerCli(parent_pid=parent, settings=settings)
