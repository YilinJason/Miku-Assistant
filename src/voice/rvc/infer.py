"""
RVC 推理：优先常驻 Worker，失败时回退一次性子进程

协调 ``RVCWorkerClient`` 与 ``scripts/rvc_infer.py`` 子进程完成音色转换。
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.paths import PROJECT_ROOT
from src.core.safety import SafetyPolicy, safe_subprocess_run
from src.voice.rvc.device import resolve_rvc_device
from src.voice.timing import log_elapsed

logger = logging.getLogger(__name__)

RVC_VENV_PYTHON = PROJECT_ROOT / ".venv-rvc" / "Scripts" / "python.exe"
RVC_INFER_SCRIPT = PROJECT_ROOT / "scripts" / "rvc_infer.py"

_worker_client: Optional["RVCWorkerClient"] = None
_worker_config_key: Optional[str] = None


def resolve_rvc_python(rvc_config: Optional[Dict[str, Any]] = None) -> Path:
    """
    解析 RVC 专用 Python 解释器路径。

    Args:
        rvc_config: 可选配置；若含 ``python_path`` 则优先使用。

    Returns:
        Windows 下默认 ``.venv-rvc/Scripts/python.exe``，Linux/macOS 为 ``bin/python``。
    """
    rvc_config = rvc_config or {}
    custom = rvc_config.get("python_path")
    if custom:
        return Path(custom)
    if sys.platform == "win32":
        return RVC_VENV_PYTHON
    return PROJECT_ROOT / ".venv-rvc" / "bin" / "python"


def is_rvc_runtime_ready(rvc_config: Optional[Dict[str, Any]] = None) -> bool:
    """
    检查 RVC 推理运行时是否已安装（解释器 + 推理脚本均存在）。

    Args:
        rvc_config: RVC 配置字典。

    Returns:
        就绪返回 ``True``。
    """
    python = resolve_rvc_python(rvc_config)
    return python.is_file() and RVC_INFER_SCRIPT.is_file()


def _config_key(rvc_config: Dict[str, Any]) -> str:
    """根据配置生成 Worker 缓存键；CUDA 探测只在新 Worker 构造时执行一次。"""
    requested_device = (
        str(rvc_config.get("device", "auto")).strip().lower() or "auto"
    )
    return "|".join(
        [
            str(rvc_config.get("model_path", "")),
            str(rvc_config.get("index_path", "")),
            requested_device,
            str(rvc_config.get("pitch", 0)),
            str(rvc_config.get("index_rate", 0.55)),
            str(rvc_config.get("protect", 0.5)),
            str(rvc_config.get("filter_radius", 3)),
            str(rvc_config.get("rms_mix_rate", 0.75)),
            str(rvc_config.get("f0_method", "rmvpe")),
        ]
    )


def get_rvc_worker(rvc_config: Dict[str, Any]) -> "RVCWorkerClient":
    """
    获取或重建与当前配置匹配的 RVC Worker 客户端单例。

    Args:
        rvc_config: RVC 配置字典。

    Returns:
        已绑定配置的 ``RVCWorkerClient`` 实例。
    """
    global _worker_client, _worker_config_key
    from src.voice.rvc.worker import RVCWorkerClient

    key = _config_key(rvc_config)
    if _worker_client is None or _worker_config_key != key:
        if _worker_client:
            _worker_client.stop()
        _worker_client = RVCWorkerClient(rvc_config)
        _worker_config_key = key
    return _worker_client


def stop_rvc_worker() -> None:
    """停止全局 RVC Worker 并清空单例引用（TTS 关闭时调用）。"""
    global _worker_client, _worker_config_key
    if _worker_client:
        # 先推进操作代际，使尚未发送、正在等待互斥锁的旧转换也失效。
        _worker_client.interrupt_current()
        _worker_client.stop()
    _worker_client = None
    _worker_config_key = None


def interrupt_rvc_worker() -> bool:
    """插话时取消活跃 RVC 工作；Worker 空闲则保留已加载模型供下一轮复用。"""
    worker = _worker_client
    if worker is None:
        return False
    interrupted = worker.interrupt_current()
    if interrupted:
        logger.info("RVC Worker 活跃任务已终止；客户端保留并可按需重启")
    else:
        logger.debug("RVC Worker 当前空闲，插话不卸载模型")
    return interrupted


def warm_rvc_worker(rvc_config: Dict[str, Any]) -> bool:
    """
    预启动 Worker，使模型在进入语音模式前完成加载。

    Args:
        rvc_config: RVC 配置字典；无 ``model_path`` 或 ``worker_enabled=False`` 时跳过。

    Returns:
        启动成功返回 ``True``，否则 ``False``。
    """
    if not rvc_config.get("model_path"):
        return False
    worker_enabled = rvc_config.get("worker_enabled", True)
    if not worker_enabled:
        return False
    worker = get_rvc_worker(rvc_config)
    if not worker.start():
        return False
    if not rvc_config.get("worker_warmup_inference", True):
        return True
    warmup_timeout = float(rvc_config.get("worker_warmup_timeout", 45.0))
    if not worker.warm_up(timeout=warmup_timeout):
        # 预热只是性能优化；模型已加载时仍允许真实请求继续。
        logger.warning("RVC Worker 首次推理预热未完成，真实请求仍可继续")
    return True


def run_rvc_infer_subprocess(
    input_path: str,
    output_path: str,
    rvc_config: Dict[str, Any],
    safety_policy: Optional[SafetyPolicy] = None,
) -> bool:
    """
    通过一次性子进程调用 ``scripts/rvc_infer.py`` 完成推理（Worker 失败时的回退）。

    Args:
        input_path: 输入音频路径。
        output_path: 输出 wav 路径。
        rvc_config: RVC 配置字典。
        safety_policy: 安全策略；子进程执行时使用。

    Returns:
        推理成功且输出文件非空返回 ``True``。
    """
    python = resolve_rvc_python(rvc_config)
    if not python.is_file():
        logger.error(
            "未找到 RVC 环境 %s，请运行: powershell -ExecutionPolicy Bypass -File scripts/setup_voice_env.ps1",
            python,
        )
        return False

    model_path = rvc_config.get("model_path", "")
    if not model_path or not os.path.isfile(model_path):
        logger.error("RVC 模型不存在: %s", model_path)
        return False

    device = resolve_rvc_device(rvc_config)
    cmd = [
        str(python),
        str(RVC_INFER_SCRIPT),
        "--input", input_path,
        "--output", output_path,
        "--model", model_path,
        "--device", device,
        "--pitch", str(int(rvc_config.get("pitch", 0))),
        "--index-rate", str(float(rvc_config.get("index_rate", 0.55))),
        "--protect", str(float(rvc_config.get("protect", 0.5))),
        "--filter-radius", str(int(rvc_config.get("filter_radius", 3))),
        "--rms-mix-rate", str(float(rvc_config.get("rms_mix_rate", 0.75))),
        "--f0-method", str(rvc_config.get("f0_method", "rmvpe")),
    ]

    index_path = rvc_config.get("index_path")
    if index_path and os.path.isfile(index_path):
        cmd.extend(["--index", index_path])

    t0 = time.perf_counter()
    with log_elapsed("RVC 子进程推理(fallback)"):
        result = safe_subprocess_run(
            safety_policy or SafetyPolicy(),
            cmd,
            capture_output=True,
            text=True,
            timeout=float(rvc_config.get("subprocess_timeout", 90.0)),
            encoding="utf-8",
            errors="ignore",
            context="rvc_infer",
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if result.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
        logger.info("[TTS计时] RVC 子进程成功 total=%.0f ms", elapsed_ms)
        return True

    stderr = (result.stderr or result.stdout or "").strip()[:600]
    logger.error("RVC 子进程失败 (code=%s, %.0f ms): %s", result.returncode, elapsed_ms, stderr)
    return False


def run_rvc_infer(
    input_path: str,
    output_path: str,
    rvc_config: Dict[str, Any],
    safety_policy: Optional[SafetyPolicy] = None,
) -> bool:
    """
    执行 RVC 推理：优先 Worker，失败时回退子进程。

    Args:
        input_path: 输入音频路径。
        output_path: 输出 wav 路径。
        rvc_config: RVC 配置字典。
        safety_policy: 安全策略；子进程回退时使用。

    Returns:
        转换成功返回 ``True``。
    """
    worker_enabled = rvc_config.get("worker_enabled", True)
    if worker_enabled:
        worker = get_rvc_worker(rvc_config)
        if worker.convert(
            input_path,
            output_path,
            timeout=float(rvc_config.get("worker_infer_timeout", 45.0)),
        ):
            return True
        if not rvc_config.get("fallback_on_worker_failure", False):
            logger.error(
                "RVC Worker 失败；为避免反复冷启动造成资源风暴，本轮回退默认音色"
            )
            return False
        logger.warning("RVC Worker 失败，按配置回退到一次性子进程")

    return run_rvc_infer_subprocess(input_path, output_path, rvc_config, safety_policy)
