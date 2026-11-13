"""
RVC 推理设备解析

根据配置与 CUDA 可用性选择 ``cpu`` 或 ``cuda:*`` 设备。
探测经 ``rvc_probe`` 专用能力，不直接调用 ``subprocess``。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.core.rvc_capabilities import RvcProbeKind
from src.core.safety import SafetyPolicy

logger = logging.getLogger(__name__)


def resolve_rvc_device(
    rvc_config: Optional[Dict[str, Any]] = None,
    *,
    safety_policy: Optional[SafetyPolicy] = None,
) -> str:
    """
    解析 RVC 推理设备字符串。

    - ``auto`` / 空：CUDA 可用时用 ``cuda:0``，否则 ``cpu``
    - ``cuda:*``：不可用时回退 ``cpu``

    Args:
        rvc_config: RVC 配置字典，读取 ``device`` 字段。
        safety_policy: 共享安全策略；探测走 ``rvc_probe``。

    Returns:
        PyTorch 设备字符串，如 ``cuda:0`` 或 ``cpu``。
    """
    rvc_config = rvc_config or {}
    requested = str(rvc_config.get("device", "auto")).strip().lower() or "auto"
    policy = safety_policy

    if requested in ("auto", "default"):
        if _cuda_available(rvc_config, safety_policy=policy):
            logger.info("[RVC] 设备: cuda:0（自动检测）")
            return "cuda:0"
        logger.info("[RVC] 设备: cpu（未检测到 CUDA）")
        return "cpu"

    if requested.startswith("cuda"):
        if _cuda_available(rvc_config, safety_policy=policy):
            logger.info("[RVC] 设备: %s", requested)
            return requested
        logger.warning("[RVC] 请求 CUDA 但不可用，回退 cpu")
        return "cpu"

    logger.info("[RVC] 设备: %s", requested)
    return requested


def _cuda_available(
    rvc_config: Optional[Dict[str, Any]] = None,
    *,
    safety_policy: Optional[SafetyPolicy] = None,
) -> bool:
    """
    在 RVC 专用 Python 环境中探测 ``torch.cuda.is_available()``。

    Returns:
        CUDA 可用返回 ``True``，否则 ``False``。
    """
    from src.voice.rvc.infer import ensure_policy_rvc_python, resolve_rvc_python

    if safety_policy is None:
        # 无共享策略时不另建旁路策略，也不直接 subprocess
        return False
    python = resolve_rvc_python(rvc_config)
    if not python or not python.is_file():
        return False
    try:
        ensure_policy_rvc_python(safety_policy, rvc_config)
        result = safety_policy.run_rvc_probe(
            RvcProbeKind.CUDA_AVAILABLE,
            capture_output=True,
            timeout=30,
            encoding="utf-8",
            errors="ignore",
        )
        return result.returncode == 0
    except PermissionError:
        logger.debug("CUDA 探测被安全策略拒绝")
        return False
    except Exception:
        return False
