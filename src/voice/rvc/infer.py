"""
RVC 推理：优先常驻 Worker，失败时回退一次性专用能力子进程

协调 ``RVCWorkerClient`` 与 ``scripts/rvc_infer.py``（经 ``rvc_infer`` 能力）完成音色转换。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.paths import PROJECT_ROOT
from src.core.rvc_capabilities import RvcInferLaunch
from src.core.safety import Operation, SafetyPolicy
from src.voice.rvc.device import resolve_rvc_device
from src.voice.timing import log_elapsed

logger = logging.getLogger(__name__)


class RvcWorkerUnavailableError(RuntimeError):
    """常驻 Worker 无法按当前配置就绪（常见原因：旧子进程未确认退出）。"""


RVC_VENV_PYTHON = PROJECT_ROOT / ".venv-rvc" / "Scripts" / "python.exe"
RVC_INFER_SCRIPT = PROJECT_ROOT / "scripts" / "rvc_infer.py"

_worker_client: Optional["RVCWorkerClient"] = None
_worker_config_key: Optional[str] = None
# 非阻塞准入 gate：仅 try-acquire；持锁期间完成 stop/构造/发布，IO 在锁外由 client 自行完成。
_worker_lifecycle_gate = threading.Lock()


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


def _cancelled(cancel_event: Any) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _mark_client_globally_retired(client: Any) -> None:
    marker = getattr(client, "mark_globally_retired", None)
    if callable(marker):
        marker()


class _InferWorkLease:
    __slots__ = ("_lock", "_released")

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._lock.release()
            self._released = True


def _try_acquire_work_lease(client: Any) -> Any:
    acquirer = getattr(client, "try_acquire_work_lease", None)
    if callable(acquirer):
        return acquirer()
    lock = getattr(client, "_lock", None)
    if lock is None:
        return None
    if not lock.acquire(blocking=False):
        return None
    return _InferWorkLease(lock)


def _client_idle_flags(client: Any) -> bool:
    if getattr(client, "converting", False) is True:
        return False
    if getattr(client, "_pin_inflight", False) is True:
        return False
    if getattr(client, "_start_inflight", False) is True:
        return False
    return True


def _dispose_client_with_permanent_fence(client: Any) -> bool:
    """永久退休：先 fence 再 stop；quiet False 时不清 global（由调用方保留引用）。"""
    _mark_client_globally_retired(client)
    return _shutdown_existing_client(client)


def _stop_client_quiet_if_global(client: Any) -> bool:
    """在 lifecycle gate 内停止 ``client``；永久退休 fence 先于 stop。"""
    global _worker_client, _worker_config_key
    retiring_global = _worker_client is client
    if not _dispose_client_with_permanent_fence(client):
        return False
    if retiring_global and _worker_client is client:
        _worker_client = None
        _worker_config_key = None
    return True


def retire_rvc_worker_for_retry(
    rvc_config: Dict[str, Any],
    safety_policy: SafetyPolicy,
) -> bool:
    """
    显式重试前退休全局 Worker：须同 SafetyPolicy、同 config key 且 idle。

    Returns:
        无 Worker 或已确认 quiet 后为 True；busy/策略或配置不匹配为 False。
    """
    if not _worker_lifecycle_gate.acquire(blocking=False):
        return False
    try:
        client = _worker_client
        if client is None:
            return True
        key = _config_key(rvc_config)
        if _worker_config_key != key:
            return False
        if client.safety_policy is not safety_policy:
            return False
        lease = _try_acquire_work_lease(client)
        if lease is None:
            return False
        try:
            if not _client_idle_flags(client):
                return False
            return _stop_client_quiet_if_global(client)
        finally:
            release = getattr(lease, "release", None)
            if callable(release):
                release()
    finally:
        _worker_lifecycle_gate.release()


def _shutdown_existing_client(client: Any) -> bool:
    """停止既有客户端；仅在确认完成后方可丢弃全局引用。"""
    stop = getattr(client, "stop", None)
    if not callable(stop):
        return False
    was_converting = getattr(client, "converting", False) is True
    if was_converting:
        interrupt = getattr(client, "interrupt_current", None)
        if callable(interrupt):
            interrupt()
        else:
            result = stop()
            if result is False:
                return False
            return True if result is None else bool(result)
        return getattr(client, "last_stop_exited", False) is True
    result = stop()
    if result is False:
        return False
    return True if result is None else bool(result)


def get_rvc_worker(
    rvc_config: Dict[str, Any],
    safety_policy: Optional[SafetyPolicy] = None,
    *,
    temp_file_manager: Any = None,
    cancel_event: Any = None,
    for_explicit_retry: bool = False,
) -> "RVCWorkerClient":
    """
    获取或重建与当前配置匹配的 RVC Worker 客户端单例。

    必须显式提供共享 ``safety_policy``；策略实例变化时停止并重建。
    未提供策略时不得复用已授权 Worker。
    """
    global _worker_client, _worker_config_key
    from src.voice.rvc.worker import RVCWorkerClient

    if safety_policy is None:
        raise PermissionError("RVC Worker 需要显式共享 SafetyPolicy")

    if _cancelled(cancel_event):
        raise RvcWorkerUnavailableError("RVC Worker 获取已取消")

    if not _worker_lifecycle_gate.acquire(blocking=False):
        raise RvcWorkerUnavailableError("RVC Worker 生命周期操作进行中")

    try:
        key = _config_key(rvc_config)
        policy = safety_policy
        existing = _worker_client
        if for_explicit_retry and existing is not None:
            if _worker_config_key != key or existing.safety_policy is not policy:
                raise RvcWorkerUnavailableError(
                    "RVC 显式重试拒绝替换 foreign Worker"
                )
        rebuild = (
            existing is None
            or _worker_config_key != key
            or existing.safety_policy is not policy
        )
        if rebuild and existing is not None:
            if for_explicit_retry:
                raise RvcWorkerUnavailableError(
                    "RVC 显式重试拒绝替换 foreign Worker"
                )
            _mark_client_globally_retired(existing)
            if not _shutdown_existing_client(existing):
                logger.error(
                    "RVC Worker 停止失败，拒绝在未确认退出前重建客户端"
                )
                raise RvcWorkerUnavailableError(
                    "RVC Worker 停止失败，拒绝在未确认退出前重建客户端"
                )
            if _worker_client is existing:
                _worker_client = None
                _worker_config_key = None
        if rebuild:
            client = RVCWorkerClient(
                rvc_config,
                safety_policy=policy,
                temp_file_manager=temp_file_manager,
            )
            _worker_client = client
            _worker_config_key = key
            if _cancelled(cancel_event):
                _mark_client_globally_retired(client)
                if not _shutdown_existing_client(client):
                    return client
                if _worker_client is client:
                    _worker_client = None
                    _worker_config_key = None
                raise RvcWorkerUnavailableError("RVC Worker 获取已取消")
        else:
            client = _worker_client
            assert client is not None
            if temp_file_manager is not None:
                client.bind_temp_file_manager(temp_file_manager)
        if _cancelled(cancel_event):
            if _worker_client is client:
                if not _stop_client_quiet_if_global(client):
                    return client
                raise RvcWorkerUnavailableError("RVC Worker 获取已取消")
        return client
    finally:
        _worker_lifecycle_gate.release()


def rvc_worker_running() -> bool:
    """全局 Worker 子进程是否仍存活。单例已清空时为 False。"""
    client = _worker_client
    if client is None:
        return False
    return client.process_alive()


def stop_rvc_worker() -> bool:
    """
    停止全局 RVC Worker 并清空单例引用（TTS 关闭时调用）。

    Returns:
        确认没有残留 Worker 进程时为 True。调用方在 False 时不得启动下一个 Worker。
    """
    global _worker_client, _worker_config_key
    if not _worker_lifecycle_gate.acquire(blocking=False):
        return False
    try:
        client = _worker_client
        if client is None:
            return True
        _mark_client_globally_retired(client)
        if not _shutdown_existing_client(client):
            return False
        if _worker_client is client:
            _worker_client = None
            _worker_config_key = None
        return True
    finally:
        _worker_lifecycle_gate.release()


def _confirm_worker_stopped_before_subprocess() -> bool:
    """
    一次性 ``rvc_infer`` 子进程前确认没有仍存活的常驻 Worker。

    避免 Worker 与 fallback / worker_disabled 并行加载第二套模型。
    """
    if _worker_client is None:
        return True
    if rvc_worker_running():
        if stop_rvc_worker():
            return True
        logger.error(
            "RVC 一次性推理拒绝: 常驻 Worker 仍未退出，禁止并行冷启动"
        )
        return False
    return stop_rvc_worker()


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


def _quiesce_worker_after_cancel(worker: Any) -> None:
    if not _worker_lifecycle_gate.acquire(blocking=False):
        _shutdown_existing_client(worker)
        return
    try:
        if _worker_client is worker:
            _stop_client_quiet_if_global(worker)
        else:
            _shutdown_existing_client(worker)
    finally:
        _worker_lifecycle_gate.release()


def warm_rvc_worker(
    rvc_config: Dict[str, Any],
    safety_policy: Optional[SafetyPolicy] = None,
    *,
    temp_file_manager: Any = None,
    cancel_event: Any = None,
    for_explicit_retry: bool = False,
) -> bool:
    """
    预启动 Worker，使模型在进入语音模式前完成加载。

    Args:
        rvc_config: RVC 配置字典；无 ``model_path`` 或 ``worker_enabled=False`` 时跳过。
        safety_policy: 共享安全策略。

    Returns:
        启动成功返回 ``True``，否则 ``False``。
    """
    if not rvc_config.get("model_path"):
        return False
    worker_enabled = rvc_config.get("worker_enabled", True)
    if not worker_enabled:
        return False
    if safety_policy is None:
        return False
    if _cancelled(cancel_event):
        return False
    try:
        worker = get_rvc_worker(
            rvc_config,
            safety_policy=safety_policy,
            temp_file_manager=temp_file_manager,
            cancel_event=cancel_event,
            for_explicit_retry=for_explicit_retry,
        )
    except RvcWorkerUnavailableError:
        return False
    if _cancelled(cancel_event):
        _quiesce_worker_after_cancel(worker)
        return False
    if not worker.start():
        return False
    if _cancelled(cancel_event):
        _quiesce_worker_after_cancel(worker)
        return False
    if not rvc_config.get("worker_warmup_inference", True):
        if not worker.ready:
            logger.warning("RVC Worker 已启动但未就绪（进程未存活）")
            return False
        return True
    warmup_timeout = float(rvc_config.get("worker_warmup_timeout", 45.0))
    if _cancelled(cancel_event):
        _quiesce_worker_after_cancel(worker)
        return False
    if not worker.warm_up(timeout=warmup_timeout):
        logger.warning("RVC Worker 首次推理预热未完成")
        return False
    if _cancelled(cancel_event):
        _quiesce_worker_after_cancel(worker)
        return False
    if not worker.ready:
        logger.warning("RVC Worker 预热后未就绪（进程未存活）")
        return False
    return True


def ensure_policy_rvc_python(
    safety_policy: SafetyPolicy,
    rvc_config: Optional[Dict[str, Any]] = None,
) -> Path:
    """校验装配期已封存的 RVC Python；下层不得登记或改写。"""
    resolved = resolve_rvc_python(rvc_config)
    try:
        resolved = resolved.resolve()
    except OSError as exc:
        raise PermissionError("RVC 子进程能力拒绝: 解释器无效") from exc
    existing = safety_policy.rvc_python()
    if existing is None:
        raise PermissionError("RVC 子进程能力拒绝: 未封存解释器")
    if existing != resolved:
        raise PermissionError("RVC 子进程能力拒绝: 意外解释器")
    safety_policy.assert_sealed_rvc_python()
    return existing


def run_rvc_infer_subprocess(
    input_path: str,
    output_path: str,
    rvc_config: Dict[str, Any],
    safety_policy: Optional[SafetyPolicy] = None,
    *,
    temp_file_manager: Any = None,
) -> bool:
    """
    通过 ``rvc_infer`` 专用能力调用 ``scripts/rvc_infer.py``（Worker 失败时的回退）。

    必须显式共享策略与 TempFileManager；不回退通用 ``Operation.SUBPROCESS``。
    """
    if safety_policy is None:
        logger.error("RVC 一次性推理拒绝: 缺少显式 SafetyPolicy")
        return False
    if temp_file_manager is None:
        logger.error("RVC 一次性推理拒绝: 缺少 TempFileManager")
        return False
    policy = safety_policy
    try:
        ensure_policy_rvc_python(policy, rvc_config)
    except PermissionError:
        logger.error("RVC 一次性推理被安全策略拒绝")
        return False

    python = resolve_rvc_python(rvc_config)
    if not python.is_file():
        logger.error(
            "未找到 RVC 环境，请运行: powershell -ExecutionPolicy Bypass -File scripts/setup_voice_env.ps1"
        )
        return False

    model_path = rvc_config.get("model_path", "")
    if not model_path or not os.path.isfile(model_path):
        logger.error("RVC 模型不存在")
        return False

    # 仅校验装配期封存的 profile；下层不得补登记
    index_path = rvc_config.get("index_path")
    if index_path and not os.path.isfile(index_path):
        index_path = None
    if policy.rvc_model() is None:
        logger.error("RVC 一次性推理拒绝: 未封存模型 profile")
        return False

    from src.core.host_memory import STARTUP_MEMORY_USER_HINT, check_model_startup_memory

    _mem = check_model_startup_memory("rvc")
    if not _mem.allowed:
        logger.error(
            "RVC 一次性推理被拒绝 reason=%s physical_avail=%s commit_avail=%s "
            "required_phys=%s required_commit=%s. %s",
            _mem.reason,
            _mem.physical_available_mb,
            _mem.commit_available_mb,
            _mem.required_physical_mb,
            _mem.required_commit_mb,
            STARTUP_MEMORY_USER_HINT,
        )
        return False

    device = resolve_rvc_device(rvc_config, safety_policy=policy)

    launch = RvcInferLaunch(
        input_path=input_path,
        output_path=output_path,
        model_path=str(model_path),
        device=device,
        pitch=int(rvc_config.get("pitch", 0)),
        index_rate=float(rvc_config.get("index_rate", 0.55)),
        protect=float(rvc_config.get("protect", 0.5)),
        filter_radius=int(rvc_config.get("filter_radius", 3)),
        rms_mix_rate=float(rvc_config.get("rms_mix_rate", 0.75)),
        f0_method=str(rvc_config.get("f0_method", "rmvpe")),
        index_path=str(index_path) if index_path else None,
    )

    t0 = time.perf_counter()
    try:
        with log_elapsed("RVC 子进程推理(fallback)"):
            result = policy.run_rvc_infer(
                launch,
                temp_file_manager=temp_file_manager,
                capture_output=True,
                text=True,
                timeout=float(rvc_config.get("subprocess_timeout", 90.0)),
                encoding="utf-8",
                errors="ignore",
            )
    except PermissionError:
        logger.error("RVC 一次性推理被安全策略拒绝")
        return False
    except subprocess.TimeoutExpired:
        logger.error("RVC 一次性推理超时")
        return False
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if result.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
        logger.info("[TTS计时] RVC 子进程成功 total=%.0f ms", elapsed_ms)
        return True

    stderr_chars = len((result.stderr or result.stdout or "").strip())
    logger.error(
        "RVC 子进程失败 code=%s elapsed_ms=%.0f stderr_chars=%s",
        result.returncode,
        elapsed_ms,
        stderr_chars,
    )
    return False


def run_rvc_infer(
    input_path: str,
    output_path: str,
    rvc_config: Dict[str, Any],
    safety_policy: Optional[SafetyPolicy] = None,
    *,
    temp_file_manager: Any = None,
) -> bool:
    """
    执行 RVC 推理：优先 Worker，失败时回退专用一次性推理能力。

    必须显式共享策略；每次使用 Worker 前再次要求 ``rvc_worker``。
    """
    if safety_policy is None:
        logger.error("RVC 推理拒绝: 缺少显式 SafetyPolicy")
        return False

    # 策略实例变化时先停掉旧授权 Worker，即使新策略将拒绝能力
    global _worker_client
    if _worker_client is not None and _worker_client.safety_policy is not safety_policy:
        if not stop_rvc_worker():
            return False

    worker_enabled = rvc_config.get("worker_enabled", True)
    if worker_enabled:
        try:
            safety_policy.require(Operation.RVC_WORKER, "rvc_worker")
        except PermissionError:
            logger.error("RVC Worker 被策略拒绝")
            return False
        try:
            worker = get_rvc_worker(
                rvc_config,
                safety_policy=safety_policy,
                temp_file_manager=temp_file_manager,
            )
        except RvcWorkerUnavailableError:
            return False
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
        logger.warning("RVC Worker 失败，按配置回退到一次性推理能力")
        if not _confirm_worker_stopped_before_subprocess():
            return False
    elif not _confirm_worker_stopped_before_subprocess():
        return False

    return run_rvc_infer_subprocess(
        input_path,
        output_path,
        rvc_config,
        safety_policy,
        temp_file_manager=temp_file_manager,
    )
