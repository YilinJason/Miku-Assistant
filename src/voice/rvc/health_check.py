"""
RVC 环境检查（模型文件 + 独立推理环境）

启动 TTS 前校验模型路径、推理脚本与 ``.venv-rvc`` 是否就绪。
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.voice.rvc.infer import RVC_INFER_SCRIPT, is_rvc_runtime_ready, resolve_rvc_python


@dataclass
class RVCHealthReport:
    """RVC 健康检查结果。"""

    ok: bool = False
    model_path: Optional[str] = None
    index_path: Optional[str] = None
    python_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        """
        生成人类可读的多行检查摘要。

        Returns:
            含状态、路径、错误与警告的文本。
        """
        lines = ["RVC 健康检查:"]
        lines.append(f"  状态: {'通过' if self.ok else '未通过'}")
        if self.model_path:
            lines.append(f"  模型: {self.model_path}")
        if self.index_path:
            lines.append(f"  索引: {self.index_path}")
        if self.python_path:
            lines.append(f"  推理环境: {self.python_path}")
        for err in self.errors:
            lines.append(f"  [错误] {err}")
        for warn in self.warnings:
            lines.append(f"  [警告] {warn}")
        return "\n".join(lines)


def check_rvc_health(rvc_config: Optional[Dict[str, Any]] = None) -> RVCHealthReport:
    """
    执行 RVC 环境与模型文件完整性检查。

    Args:
        rvc_config: RVC 配置字典；``None`` 视为空配置。

    Returns:
        填充了 ``ok``、路径与错误/警告列表的 ``RVCHealthReport``。
    """
    report = RVCHealthReport()
    rvc_config = rvc_config or {}

    model_path = rvc_config.get("model_path")
    index_path = rvc_config.get("index_path")
    python_path = resolve_rvc_python(rvc_config)

    report.model_path = model_path
    report.index_path = index_path
    report.python_path = str(python_path)

    if not model_path:
        report.errors.append("未配置 voice.tts.rvc.model_path")
        return report

    if not os.path.isfile(model_path):
        report.errors.append(f"模型文件不存在: {model_path}")
        return report

    if index_path and not os.path.isfile(index_path):
        report.warnings.append(f"索引文件不存在，将不使用: {index_path}")

    if not RVC_INFER_SCRIPT.is_file():
        report.errors.append("缺少 scripts/rvc_infer.py")
        return report

    if not is_rvc_runtime_ready(rvc_config):
        report.errors.append(
            "RVC 推理环境未安装。运行一次: "
            "powershell -ExecutionPolicy Bypass -File scripts/setup_voice_env.ps1"
        )
        return report

    if not _can_import_rvc_python(python_path):
        report.errors.append("RVC 环境中无法导入 rvc_python，请重新运行 setup_voice_env.ps1")
        return report

    report.ok = True
    return report


def _can_import_rvc_python(python_path) -> bool:
    """
    在 RVC 专用环境中尝试导入 ``rvc_python.infer.RVCInference``。

    Args:
        python_path: RVC 专用 Python 可执行文件路径。

    Returns:
        导入成功返回 ``True``。
    """
    try:
        result = subprocess.run(
            [str(python_path), "-c", "from rvc_python.infer import RVCInference"],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="ignore",
        )
        return result.returncode == 0
    except Exception:
        return False


def ensure_rvc_ready(rvc_config: Optional[Dict[str, Any]], logger=None) -> RVCHealthReport:
    """
    检查 RVC 就绪状态并按结果写日志。

    Args:
        rvc_config: RVC 配置字典。
        logger: 可选日志器；未通过时写入 ``report.summary()``。

    Returns:
        与 ``check_rvc_health`` 相同的 ``RVCHealthReport``。
    """
    report = check_rvc_health(rvc_config)
    if logger:
        if report.ok:
            logger.info("RVC 健康检查通过")
        else:
            logger.error(report.summary())
    return report
