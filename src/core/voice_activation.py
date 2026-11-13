"""App 语音候选/关闭所有权与三模型 prepare 接线（同步 ensure，无后台加载线程）。"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from src.core.assembly import _shutdown_engine
from src.core.local_voice_warmup import STAGE_CANCELLED, prepare_local_voice
from src.core.resource_coord import run_coordinated_warmup
from src.core.resource_settings import ResourceSettings
from src.core.rvc_prepare_metadata import (
    RVC_PREPARE_READ_FAILED,
    RVC_PREPARE_USER_WARNING,
    RvcPrepareState,
    collect_rvc_fallback_warnings,
    read_rvc_prepare_state,
    rvc_prepare_failed_sticky,
    rvc_prepare_read_failed,
    rvc_prepare_verified_ready,
)
from src.core.shutdown_status import (
    engine_declares_aggregate_shutdown_status,
    engine_declares_local_cosyvoice_shutdown_status,
    verify_aggregate_shutdown_status,
)

LifecycleClosed = Callable[[], bool]

_LEGACY_TTS_ENGINES = frozenset({"gtts", "rvc"})


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    get_fn = getattr(config, "get", None)
    if callable(get_fn):
        return get_fn(key, default)
    return default


def _config_tts_engine_name(config: Any) -> str:
    tts_cfg = _config_get(config, "voice.tts", {}) or {}
    if not isinstance(tts_cfg, dict):
        return "edge-tts"
    engine = str(tts_cfg.get("engine", "edge-tts"))
    if engine in _LEGACY_TTS_ENGINES:
        return "edge-tts"
    return engine


def _config_has_rvc_model_path(config: Any) -> bool:
    get_rvc = getattr(config, "get_rvc_config", None)
    if callable(get_rvc):
        rvc = get_rvc()
        if isinstance(rvc, dict) and rvc.get("model_path"):
            return True
    rvc_cfg = _config_get(config, "voice.tts.rvc", {}) or {}
    if isinstance(rvc_cfg, dict) and rvc_cfg.get("model_path"):
        return True
    voice = _config_get(config, "voice", {}) or {}
    if isinstance(voice, dict):
        legacy = voice.get("rvc")
        if isinstance(legacy, dict) and legacy.get("model_path"):
            return True
    return False


def is_cosyvoice_tts(tts_engine: Any) -> bool:
    return getattr(tts_engine, "engine_type", None) == "cosyvoice"


def tts_engine_require_rvc(tts_engine: Any) -> bool:
    """RVC 是否启用：取引擎绑定 converter，而非 warmup 调度开关。"""
    rvc = getattr(tts_engine, "_rvc", None)
    if rvc is None:
        return False
    return bool(getattr(rvc, "enabled", False))


def bound_tts_engine_name(tts_engine: Any) -> str:
    """已绑定 TTS 的引擎标签（含 gtts/rvc→edge-tts 映射）。"""
    engine_type = getattr(tts_engine, "engine_type", None)
    if engine_type is None:
        return "edge-tts"
    engine = str(engine_type)
    if engine in _LEGACY_TTS_ENGINES:
        return "edge-tts"
    return engine


def bound_tts_supports_edge_rvc_warmup(tts_engine: Any) -> bool:
    if tts_engine is None or is_cosyvoice_tts(tts_engine):
        return False
    return bound_tts_engine_name(tts_engine) == "edge-tts"


def edge_rvc_warmup_required(
    tts_engine: Any,
    settings: ResourceSettings,
) -> bool:
    if tts_engine is None or is_cosyvoice_tts(tts_engine):
        return False
    if not settings.rvc_warmup_on_voice_start:
        return False
    if not bound_tts_supports_edge_rvc_warmup(tts_engine):
        return False
    return tts_engine_require_rvc(tts_engine)


def bound_edge_rvc_explicit_retry_needed(
    tts_engine: Any,
    resource_settings: Optional[ResourceSettings],
) -> bool:
    """已绑定 Edge 且 RVC 准备失败 sticky；仅配合 ``allow_retry=True`` 重试。"""
    if tts_engine is None or resource_settings is None:
        return False
    if not edge_rvc_warmup_required(tts_engine, resource_settings):
        return False
    state = read_rvc_prepare_state(tts_engine)
    if rvc_prepare_read_failed(state):
        return False
    return rvc_prepare_failed_sticky(state)


def bound_rvc_explicit_retry_needed(
    tts_engine: Any,
    resource_settings: Optional[ResourceSettings],
) -> bool:
    """已绑定 TTS 且 RVC 失败 sticky（Edge 或 Cosy+RVC）；须 ``allow_retry=True``。"""
    if tts_engine is None or resource_settings is None:
        return False
    if not tts_engine_require_rvc(tts_engine):
        return False
    if not resource_settings.rvc_warmup_on_voice_start:
        return False
    if is_cosyvoice_tts(tts_engine):
        state = read_rvc_prepare_state(tts_engine)
        if rvc_prepare_read_failed(state):
            return False
        return rvc_prepare_failed_sticky(state)
    return bound_edge_rvc_explicit_retry_needed(tts_engine, resource_settings)


def run_bound_rvc_explicit_retry(
    *,
    llm_client: Any,
    tts_engine: Any,
    settings: ResourceSettings,
    cancel_event: Any,
    log: logging.Logger,
    local_prepare_ok: bool,
    app: Any = None,
) -> "VoiceStackLoadResult":
    """显式 RVC 重试：Ollama 串行门槛 → retry →（Cosy）本地 prepare。"""
    from src.core.assistant import VoiceStackLoadResult
    from src.core.resource_coord import wait_ollama_warmup_succeeded_for_explicit_retry

    if _cancelled_explicit(cancel_event):
        return VoiceStackLoadResult(ok=False, errors=["语音加载已取消"])

    if not bound_rvc_explicit_retry_needed(tts_engine, settings):
        return VoiceStackLoadResult(ok=False, errors=["无需 RVC 显式重试"])

    need_serialize = (
        settings.serialize_gpu_warmup
        and settings.rvc_warmup_on_voice_start
        and tts_engine_require_rvc(tts_engine)
    )
    if need_serialize and settings.ollama_warmup_on_start and llm_client is not None:
        if _cancelled_explicit(cancel_event):
            return VoiceStackLoadResult(ok=False, errors=["语音加载已取消"])
        wait = wait_ollama_warmup_succeeded_for_explicit_retry(
            llm_client,
            timeout=float(settings.warmup_timeout),
            cancel_event=cancel_event,
        )
        if not wait.ok:
            detail = wait.detail or wait.status
            return VoiceStackLoadResult(
                ok=False,
                errors=[f"Ollama 预热未完成，无法重试 RVC ({detail})"],
            )

    if _cancelled_explicit(cancel_event):
        return VoiceStackLoadResult(ok=False, errors=["语音加载已取消"])

    retry = getattr(tts_engine, "retry_rvc_preparation", None)
    if not callable(retry):
        return VoiceStackLoadResult(ok=False, errors=["RVC 显式重试不可用"])
    try:
        ok = bool(retry(cancel_event=cancel_event))
    except (KeyboardInterrupt, SystemExit):
        raise
    if not ok:
        return VoiceStackLoadResult(ok=False, errors=["RVC 显式重试未完成"])

    state = read_rvc_prepare_state(tts_engine)
    if not rvc_prepare_verified_ready(state):
        return VoiceStackLoadResult(ok=False, errors=["RVC 显式重试未完成"])

    if _cancelled_explicit(cancel_event):
        return VoiceStackLoadResult(ok=False, errors=["语音加载已取消"])

    if is_cosyvoice_tts(tts_engine) and not local_prepare_ok:
        prepare_local = getattr(tts_engine, "prepare_local_tts", None)
        if not callable(prepare_local):
            return VoiceStackLoadResult(
                ok=False, errors=["CosyVoice 显式 prepare 不可用"]
            )
        try:
            cosy_ok = bool(prepare_local(cancel_event=cancel_event))
        except (KeyboardInterrupt, SystemExit):
            raise
        if not cosy_ok:
            return VoiceStackLoadResult(
                ok=False, errors=["CosyVoice 显式 prepare 未完成"]
            )
        if _cancelled_explicit(cancel_event):
            return VoiceStackLoadResult(ok=False, errors=["语音加载已取消"])

    if app is not None:
        with app._lifecycle_lock:
            if app._lifecycle_closed():
                return VoiceStackLoadResult(
                    ok=False, errors=["助手正在关闭，无法发布语音就绪"]
                )
            if app.tts_engine is not tts_engine:
                return VoiceStackLoadResult(
                    ok=False, errors=["语音加载事务已过期，无法发布就绪"]
                )
            if _cancelled_explicit(cancel_event):
                return VoiceStackLoadResult(ok=False, errors=["语音加载已取消"])
            if is_cosyvoice_tts(tts_engine):
                app._local_voice_prepare_ok = True

    if _cancelled_explicit(cancel_event):
        return VoiceStackLoadResult(ok=False, errors=["语音加载已取消"])

    return voice_stack_load_ok(
        tts_engine,
        resource_settings=settings,
    )


def _cancelled_explicit(cancel_event: Any) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def config_expects_edge_rvc_warmup(
    config: Any,
    settings: ResourceSettings,
) -> bool:
    if not settings.rvc_warmup_on_voice_start:
        return False
    engine = _config_tts_engine_name(config)
    if engine == "cosyvoice" or engine == "pyttsx3":
        return False
    if engine != "edge-tts":
        return False
    return _config_has_rvc_model_path(config)


def bound_tts_models_ready(
    tts_engine: Any,
    *,
    local_prepare_ok: bool,
    resource_settings: Optional[ResourceSettings] = None,
) -> bool:
    """已绑定 TTS 是否满足播报/语音栈就绪（Cosy 须真实 prepare 成功）。"""
    if tts_engine is None:
        return False
    if is_cosyvoice_tts(tts_engine):
        return bool(local_prepare_ok)
    if resource_settings is None:
        return True
    if not edge_rvc_warmup_required(tts_engine, resource_settings):
        return True
    state = read_rvc_prepare_state(tts_engine)
    if rvc_prepare_read_failed(state):
        return False
    if state is None:
        return True
    if state.inflight or not state.attempt_completed:
        return False
    return True


def tts_playback_warnings(
    tts_engine: Any,
    *,
    resource_settings: Optional[ResourceSettings] = None,
) -> List[str]:
    if tts_engine is None:
        return []
    if resource_settings is not None and not edge_rvc_warmup_required(
        tts_engine, resource_settings
    ):
        return []
    state = read_rvc_prepare_state(tts_engine)
    return collect_rvc_fallback_warnings(state)


def voice_stack_load_ok(
    tts_engine: Any,
    *,
    resource_settings: Optional[ResourceSettings] = None,
    extra_warnings: Optional[List[str]] = None,
) -> "VoiceStackLoadResult":
    from src.core.assistant import VoiceStackLoadResult

    merged: List[str] = []
    if extra_warnings:
        merged.extend(extra_warnings)
    for warn in tts_playback_warnings(tts_engine, resource_settings=resource_settings):
        if warn not in merged:
            merged.append(warn)
    return VoiceStackLoadResult(ok=True, warnings=merged)


def stt_shutdown_reports_complete(stt_engine: Any) -> bool:
    if stt_engine is None:
        return True
    checker = getattr(stt_engine, "is_shutdown_complete", None)
    if callable(checker):
        try:
            return checker() is True
        except Exception:
            return False
    getter = getattr(stt_engine, "get_shutdown_status", None)
    if callable(getter):
        try:
            status = getter()
        except Exception:
            return False
        if isinstance(status, dict):
            return status.get("complete") is True
    if getattr(stt_engine, "engine_type", None) == "paraformer":
        return False
    closer = getattr(stt_engine, "shutdown", None)
    return not callable(closer)


def tts_shutdown_reports_complete(tts_engine: Any) -> bool:
    """shutdown 后是否满足显式 status 契约；无 status API 的 Edge 等视为完成。"""
    if tts_engine is None:
        return True
    if engine_declares_aggregate_shutdown_status(tts_engine):
        getter = getattr(tts_engine, "get_shutdown_status", None)
        if not callable(getter):
            return False
        try:
            verify_aggregate_shutdown_status(getter())
        except Exception:
            return False
        return True
    if engine_declares_local_cosyvoice_shutdown_status(tts_engine):
        reader = getattr(tts_engine, "local_cosyvoice_shutdown_status", None)
        if not callable(reader):
            return False
        try:
            status = reader()
        except Exception:
            return False
        if status is None:
            return False
        return getattr(status, "complete", None) is True
    if is_cosyvoice_tts(tts_engine):
        return False
    return True


def warmup_registered_tts_candidate(
    *,
    llm_client: Any,
    tts_engine: Any,
    settings: ResourceSettings,
    cancel_event: Optional[threading.Event],
    log: logging.Logger,
) -> tuple[bool, List[str], List[str]]:
    """
    在 candidate 已登记、constructor 已返回后执行预热（锁外）。

    CosyVoice：``prepare_local_voice``；其他：资源协调器（Edge 路径）。
    """
    if cancel_event is not None and cancel_event.is_set():
        return False, ["语音加载已取消"], []
    if is_cosyvoice_tts(tts_engine):
        require_rvc = tts_engine_require_rvc(tts_engine)
        result = prepare_local_voice(
            llm_client,
            tts_engine,
            settings,
            require_rvc=require_rvc,
            cancel_event=cancel_event,
            log=log,
        )
        if cancel_event is not None and cancel_event.is_set():
            return False, ["语音加载已取消"], []
        if result.final_stage == STAGE_CANCELLED:
            return False, ["语音加载已取消"], []
        if not result.ok:
            return False, [f"本地语音准备未完成 stage={result.final_stage}"], []
        return True, [], []
    run_coordinated_warmup(
        llm_client=llm_client,
        tts_engine=tts_engine,
        settings=settings,
        log=log,
    )
    if cancel_event is not None and cancel_event.is_set():
        return False, ["语音加载已取消"], []
    if edge_rvc_warmup_required(tts_engine, settings):
        state = read_rvc_prepare_state(tts_engine)
        if rvc_prepare_read_failed(state):
            return False, ["本地语音准备未完成 stage=rvc"], []
        if state is None:
            return True, [], []
        if not state.attempt_completed or state.inflight:
            return False, ["本地语音准备未完成 stage=rvc"], []
        return True, [], collect_rvc_fallback_warnings(state)
    return True, [], []


@dataclass
class UnpublishedVoice:
    tts: Any = None
    stt: Any = None


def try_retire_unpublished_voice(
    unpublished: UnpublishedVoice,
    *,
    stop_rvc: bool,
) -> bool:
    """
    关闭未发布候选并校验 status；未完成则保留 ``unpublished`` 引用并返回 False。
    """
    if unpublished.tts is None and unpublished.stt is None:
        return True
    if unpublished.tts is not None:
        try:
            _shutdown_engine(unpublished.tts, stop_rvc=stop_rvc)
        except Exception:
            return False
        if not tts_shutdown_reports_complete(unpublished.tts):
            return False
    if unpublished.stt is not None:
        try:
            _shutdown_engine(unpublished.stt, stop_rvc=stop_rvc)
        except Exception:
            return False
        if not stt_shutdown_reports_complete(unpublished.stt):
            return False
    unpublished.tts = None
    unpublished.stt = None
    return True


def retire_pending_candidate(
    pending: Optional[UnpublishedVoice],
    *,
    stop_rvc: bool,
) -> bool:
    if pending is None:
        return True
    return try_retire_unpublished_voice(pending, stop_rvc=stop_rvc)


__all__ = [
    "RVC_PREPARE_READ_FAILED",
    "RVC_PREPARE_USER_WARNING",
    "RvcPrepareState",
    "UnpublishedVoice",
    "bound_tts_models_ready",
    "bound_edge_rvc_explicit_retry_needed",
    "bound_rvc_explicit_retry_needed",
    "config_expects_edge_rvc_warmup",
    "edge_rvc_warmup_required",
    "is_cosyvoice_tts",
    "read_rvc_prepare_state",
    "retire_pending_candidate",
    "run_bound_rvc_explicit_retry",
    "try_retire_unpublished_voice",
    "tts_engine_require_rvc",
    "tts_playback_warnings",
    "stt_shutdown_reports_complete",
    "tts_shutdown_reports_complete",
    "voice_stack_load_ok",
    "warmup_registered_tts_candidate",
]
