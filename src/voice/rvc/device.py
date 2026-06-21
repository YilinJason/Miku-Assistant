"""
RVC 推理设备解析

根据配置与 CUDA 可用性选择 ``cpu`` 或 ``cuda:*`` 设备。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def resolve_rvc_device(rvc_config: Optional[Dict[str, Any]] = None) -> str:
    """
    解析 RVC 推理设备字符串。

    - ``auto`` / 空：CUDA 可用时用 ``cuda:0``，否则 ``cpu``
    - ``cuda:*``：不可用时回退 ``cpu``

    Args:
        rvc_config: RVC 配置字典，读取 ``device`` 字段。

    Returns:
        PyTorch 设备字符串，如 ``cuda:0`` 或 ``cpu``。
    """
    rvc_config = rvc_config or {}
    requested = str(rvc_config.get("device", "auto")).strip().lower() or "auto"

    if requested in ("auto", "default"):
        if _cuda_available():
            logger.info("[RVC] 设备: cuda:0（自动检测）")
            return "cuda:0"
        logger.info("[RVC] 设备: cpu（未检测到 CUDA）")
        return "cpu"

    if requested.startswith("cuda"):
        if _cuda_available():
            logger.info("[RVC] 设备: %s", requested)
            return requested
        logger.warning("[RVC] 请求 %s 但 CUDA 不可用，回退 cpu", requested)
        return "cpu"

    logger.info("[RVC] 设备: %s", requested)
    return requested


def _cuda_available() -> bool:
    """
    在 RVC 专用 Python 环境中探测 ``torch.cuda.is_available()``。

    Returns:
        CUDA 可用返回 ``True``，否则 ``False``。
    """
    python = _resolve_probe_python()
    if not python:
        return False
    try:
        import subprocess

        result = subprocess.run(
            [str(python), "-c", "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)"],
            capture_output=True,
            timeout=30,
            encoding="utf-8",
            errors="ignore",
        )
        return result.returncode == 0
    except Exception:
        return False


def _resolve_probe_python():
    """解析用于 CUDA 探测的 Python 解释器路径。"""
    try:
        from src.voice.rvc.infer import resolve_rvc_python

        return resolve_rvc_python()
    except Exception:
        return None
