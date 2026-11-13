# -*- coding: utf-8 -*-
"""语音自然度 A/B 共享安全 helper（purge / 非空检查 / TrustedRvc 封存）。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.core.config import Config
from src.core.paths import RUNTIME_DATA_DIRS
from src.core.safety import SafetyPolicy, TrustedRvcProfile
from src.voice.rvc.infer import resolve_rvc_python


def path_lexists(path: Path) -> bool:
    """路径仍存在（含 symlink；不跟随链接判断）。"""
    try:
        if path.is_symlink():
            return True
        return path.exists()
    except OSError:
        # 无法判定时视为仍占用，fail closed
        return True


def purge_sample_artifacts(temp_files, *paths: Path) -> None:
    """
    经 TempFileManager 白名单+所有权同步删除样本相关旧/半成品。

    任一路径登记、删除或删除后仍存在（含 symlink）→ 立即抛出；
    不得吞异常后继续 TTS/网络。
    """
    for path in paths:
        if not path_lexists(path):
            continue
        key = temp_files.adopt_existing(path)
        temp_files.remove(key)
        if path_lexists(path):
            raise RuntimeError("purge_incomplete")


def require_nonempty_audio(path: Path, *, stage: str) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"{stage}_empty_or_missing")


def build_execute_safety_policy(
    config: Config,
    rvc_config: dict,
) -> tuple[Optional[SafetyPolicy], Optional[dict], str]:
    """
    在任何 probe / 网络 / 目录 / TTS 之前解析并封存 TrustedRvcProfile。

    Returns:
        ``(policy, effective_rvc_config, error_message)``；成功时 error 为空串。
    """
    model_raw = rvc_config.get("model_path")
    if not model_raw:
        return None, None, "未找到 RVC 配置（voice.tts.rvc.model_path）"

    model_path = Path(model_raw)
    if not model_path.is_file():
        return None, None, f"模型文件不存在: {model_path}"

    try:
        python_path = resolve_rvc_python(rvc_config)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"无法解析 RVC Python: {type(exc).__name__}"

    if not python_path.is_file():
        return None, None, f"RVC 解释器不存在: {python_path}"

    index_raw = rvc_config.get("index_path")
    index_path: Optional[Path] = None
    if index_raw:
        candidate = Path(index_raw)
        if candidate.is_file():
            index_path = candidate

    effective = dict(rvc_config)
    effective["model_path"] = str(model_path.resolve())
    effective["python_path"] = str(python_path.resolve())
    if index_path is not None:
        effective["index_path"] = str(index_path.resolve())
    else:
        effective["index_path"] = ""

    profile = TrustedRvcProfile(
        python_path=python_path.resolve(),
        model_path=model_path.resolve(),
        index_path=index_path.resolve() if index_path is not None else None,
    )
    policy = SafetyPolicy.from_app_config(config, trusted_rvc_profile=profile)
    for directory in RUNTIME_DATA_DIRS:
        policy.register_file_delete_dir(directory)
    return policy, effective, ""
