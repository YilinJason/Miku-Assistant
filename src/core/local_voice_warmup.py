"""独立三模型串行本地语音准备协调器（CosyVoice + Ollama + RVC）；由 voice_load_lane → voice_activation 调用。"""
from __future__ import annotations

import logging
import math
import threading
import time
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

from src.core.log_privacy import exception_log_label
from src.core.resource_settings import MAX_WARMUP_TIMEOUT_SEC, ResourceSettings

logger = logging.getLogger(__name__)

STAGE_UNSUPPORTED = "unsupported_config"
STAGE_CANCELLED = "cancelled"
STAGE_BUSY = "busy"
STAGE_OLLAMA = "ollama"
STAGE_RVC = "rvc"
STAGE_COSYVOICE = "cosyvoice"
STAGE_COMPLETED = "completed"

_WARMUP_POLL_SEC = 0.05
_MONOTONIC: Callable[[], float] = time.monotonic
_PREPARE_SINGLE_FLIGHT = threading.Lock()


@dataclass(frozen=True)
class LocalVoiceWarmupResult:
    """冻结的串行准备结果；仅阶段标签与 monotonic 耗时，无正文/路径。"""

    ok: bool
    final_stage: str
    total_elapsed_sec: float
    stage_elapsed_sec: Mapping[str, float] = field(default_factory=dict)
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stage_elapsed_sec",
            types.MappingProxyType(dict(self.stage_elapsed_sec)),
        )

    def to_report_dict(self) -> Dict[str, Any]:
        """可 JSON 序列化的固定字段报告（阶段耗时为普通 dict 副本）。"""
        return {
            "ok": self.ok,
            "final_stage": self.final_stage,
            "total_elapsed_sec": self.total_elapsed_sec,
            "stage_elapsed_sec": dict(self.stage_elapsed_sec),
            "detail": self.detail,
        }

    @property
    def skipped(self) -> bool:
        return self.final_stage == STAGE_UNSUPPORTED


def _is_cosyvoice_engine(tts_engine: Any) -> bool:
    return getattr(tts_engine, "engine_type", None) == "cosyvoice"


def _freeze_stage_elapsed(stage_elapsed: Dict[str, float]) -> Mapping[str, float]:
    return types.MappingProxyType(dict(stage_elapsed))


def _valid_warmup_timeout(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if (
        not math.isfinite(number)
        or number <= 0.0
        or number > float(MAX_WARMUP_TIMEOUT_SEC)
    ):
        return None
    return number


def _unsupported_reason(
    settings: ResourceSettings,
    *,
    require_rvc: bool,
) -> Optional[str]:
    if not settings.serialize_gpu_warmup:
        return "serialize_gpu_warmup"
    if settings.warmup_order != "ollama_then_rvc":
        return "warmup_order"
    if not settings.ollama_warmup_on_start:
        return "ollama_warmup_on_start"
    if require_rvc and not settings.rvc_warmup_on_voice_start:
        return "rvc_warmup_on_voice_start"
    return None


def _cancelled(
    *,
    stage: str,
    started_mono: float,
    stage_started_mono: float,
    stage_elapsed: Dict[str, float],
) -> LocalVoiceWarmupResult:
    stage_elapsed[stage] = max(0.0, _MONOTONIC() - stage_started_mono)
    return LocalVoiceWarmupResult(
        ok=False,
        final_stage=STAGE_CANCELLED,
        total_elapsed_sec=max(0.0, _MONOTONIC() - started_mono),
        stage_elapsed_sec=_freeze_stage_elapsed(stage_elapsed),
        detail="cancelled",
    )


def _failure(
    *,
    stage: str,
    started_mono: float,
    stage_started_mono: float,
    stage_elapsed: Dict[str, float],
    detail: str = "",
) -> LocalVoiceWarmupResult:
    stage_elapsed[stage] = max(0.0, _MONOTONIC() - stage_started_mono)
    return LocalVoiceWarmupResult(
        ok=False,
        final_stage=stage,
        total_elapsed_sec=max(0.0, _MONOTONIC() - started_mono),
        stage_elapsed_sec=_freeze_stage_elapsed(stage_elapsed),
        detail=detail,
    )


def _strict_positive_generation(value: Any) -> Optional[int]:
    if type(value) is not int or isinstance(value, bool):
        return None
    if value <= 0:
        return None
    return value


def _strict_bool_field(value: Any) -> Optional[bool]:
    if type(value) is not bool:
        return None
    return value


def _safe_begin_generation(
    begin_fn: Callable[..., Any],
    *,
    warmup_timeout: float,
) -> Optional[int]:
    try:
        raw = begin_fn(timeout=float(warmup_timeout))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None
    return _strict_positive_generation(raw)


def _safe_warmup_snapshot(
    snap_fn: Callable[[], Any],
) -> Optional[tuple[int, bool, bool]]:
    try:
        snap = snap_fn()
        generation = _strict_positive_generation(getattr(snap, "generation", None))
        finished = _strict_bool_field(getattr(snap, "finished", None))
        succeeded = _strict_bool_field(getattr(snap, "succeeded", None))
        if generation is None or finished is None or succeeded is None:
            return None
        return generation, finished, succeeded
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None


def _wait_fresh_ollama(
    llm_client: Any,
    *,
    deadline_mono: float,
    warmup_timeout: float,
    cancel_event: Optional[threading.Event],
) -> tuple[bool, str]:
    snap_fn = getattr(llm_client, "warmup_snapshot", None)
    begin_fn = getattr(llm_client, "begin_fresh_warmup", None)
    if not callable(snap_fn) or not callable(begin_fn):
        return False, "ollama_warmup_api_missing"

    target_generation = _safe_begin_generation(
        begin_fn,
        warmup_timeout=warmup_timeout,
    )
    if target_generation is None:
        return False, "ollama_warmup_not_started"

    while _MONOTONIC() < deadline_mono:
        if cancel_event is not None and cancel_event.is_set():
            return False, "cancelled"
        snap = _safe_warmup_snapshot(snap_fn)
        if snap is None:
            return False, "ollama_warmup_snapshot_failed"
        generation, finished, succeeded = snap
        if generation != target_generation:
            return False, "ollama_warmup_superseded"
        if finished:
            if succeeded:
                return True, ""
            return False, "ollama_warmup_failed"
        remaining = deadline_mono - _MONOTONIC()
        if remaining <= 0.0:
            break
        wait_slice = min(_WARMUP_POLL_SEC, remaining)
        if cancel_event is not None:
            if cancel_event.wait(timeout=wait_slice):
                return False, "cancelled"
        else:
            time.sleep(wait_slice)

    return False, "ollama_warmup_timeout"


def prepare_local_voice(
    llm_client: Any,
    tts_engine: Any,
    settings: ResourceSettings,
    *,
    require_rvc: bool = True,
    cancel_event: Optional[threading.Event] = None,
    log: Optional[logging.Logger] = None,
) -> LocalVoiceWarmupResult:
    """
    串行准备：本轮 Ollama 空请求成功 → RVC（可跳过）→ CosyVoice ``prepare_local_tts``。

    不接管 engine 生命周期；失败时由调用方保留 owner 并回收。
    """
    if not _PREPARE_SINGLE_FLIGHT.acquire(blocking=False):
        return LocalVoiceWarmupResult(
            ok=False,
            final_stage=STAGE_BUSY,
            total_elapsed_sec=0.0,
            stage_elapsed_sec={},
            detail="prepare_in_progress",
        )

    try:
        return _prepare_local_voice_body(
            llm_client,
            tts_engine,
            settings,
            require_rvc=require_rvc,
            cancel_event=cancel_event,
            log=log,
        )
    finally:
        _PREPARE_SINGLE_FLIGHT.release()


def _prepare_local_voice_body(
    llm_client: Any,
    tts_engine: Any,
    settings: ResourceSettings,
    *,
    require_rvc: bool,
    cancel_event: Optional[threading.Event],
    log: Optional[logging.Logger],
) -> LocalVoiceWarmupResult:
    log = log or logger
    started_mono = _MONOTONIC()
    stage_elapsed: Dict[str, float] = {}

    if type(require_rvc) is not bool:
        return LocalVoiceWarmupResult(
            ok=False,
            final_stage=STAGE_UNSUPPORTED,
            total_elapsed_sec=0.0,
            stage_elapsed_sec={},
            detail="require_rvc_invalid",
        )

    warmup_timeout = _valid_warmup_timeout(settings.warmup_timeout)
    if warmup_timeout is None:
        return LocalVoiceWarmupResult(
            ok=False,
            final_stage=STAGE_UNSUPPORTED,
            total_elapsed_sec=0.0,
            stage_elapsed_sec={},
            detail="warmup_timeout_invalid",
        )

    if cancel_event is not None and cancel_event.is_set():
        return _cancelled(
            stage=STAGE_UNSUPPORTED,
            started_mono=started_mono,
            stage_started_mono=started_mono,
            stage_elapsed=stage_elapsed,
        )

    reason = _unsupported_reason(settings, require_rvc=require_rvc)
    if reason is not None:
        return LocalVoiceWarmupResult(
            ok=False,
            final_stage=STAGE_UNSUPPORTED,
            total_elapsed_sec=0.0,
            stage_elapsed_sec={},
            detail=reason,
        )

    if not _is_cosyvoice_engine(tts_engine):
        return LocalVoiceWarmupResult(
            ok=False,
            final_stage=STAGE_UNSUPPORTED,
            total_elapsed_sec=0.0,
            stage_elapsed_sec={},
            detail="engine_not_cosyvoice",
        )

    prepare_local = getattr(tts_engine, "prepare_local_tts", None)
    if not callable(prepare_local):
        return LocalVoiceWarmupResult(
            ok=False,
            final_stage=STAGE_UNSUPPORTED,
            total_elapsed_sec=0.0,
            stage_elapsed_sec={},
            detail="prepare_local_tts_missing",
        )

    deadline_mono = started_mono + warmup_timeout

    # --- Ollama ---
    if cancel_event is not None and cancel_event.is_set():
        return _cancelled(
            stage=STAGE_OLLAMA,
            started_mono=started_mono,
            stage_started_mono=started_mono,
            stage_elapsed=stage_elapsed,
        )
    ollama_started = _MONOTONIC()
    ok_ollama, ollama_detail = _wait_fresh_ollama(
        llm_client,
        deadline_mono=deadline_mono,
        warmup_timeout=warmup_timeout,
        cancel_event=cancel_event,
    )
    stage_elapsed[STAGE_OLLAMA] = max(0.0, _MONOTONIC() - ollama_started)
    if not ok_ollama:
        if ollama_detail == "cancelled":
            return _cancelled(
                stage=STAGE_OLLAMA,
                started_mono=started_mono,
                stage_started_mono=ollama_started,
                stage_elapsed=stage_elapsed,
            )
        log.warning(
            "本地语音准备：Ollama 本轮预热未成功 detail=%s",
            ollama_detail or "unknown",
        )
        return _failure(
            stage=STAGE_OLLAMA,
            started_mono=started_mono,
            stage_started_mono=ollama_started,
            stage_elapsed=stage_elapsed,
            detail=ollama_detail or "ollama_failed",
        )

    # --- RVC ---
    if require_rvc:
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled(
                stage=STAGE_RVC,
                started_mono=started_mono,
                stage_started_mono=_MONOTONIC(),
                stage_elapsed=stage_elapsed,
            )
        rvc_started = _MONOTONIC()
        prepare_rvc = getattr(tts_engine, "prepare_rvc", None)
        if not callable(prepare_rvc):
            return _failure(
                stage=STAGE_RVC,
                started_mono=started_mono,
                stage_started_mono=rvc_started,
                stage_elapsed=stage_elapsed,
                detail="prepare_rvc_missing",
            )
        try:
            rvc_ok = bool(prepare_rvc())
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            log.warning(
                "本地语音准备：RVC 预热异常 err=%s",
                exception_log_label(exc),
            )
            return _failure(
                stage=STAGE_RVC,
                started_mono=started_mono,
                stage_started_mono=rvc_started,
                stage_elapsed=stage_elapsed,
                detail=exception_log_label(exc),
            )
        stage_elapsed[STAGE_RVC] = max(0.0, _MONOTONIC() - rvc_started)
        if not rvc_ok:
            return _failure(
                stage=STAGE_RVC,
                started_mono=started_mono,
                stage_started_mono=rvc_started,
                stage_elapsed=stage_elapsed,
                detail="rvc_not_ready",
            )

    # --- CosyVoice ---
    if cancel_event is not None and cancel_event.is_set():
        return _cancelled(
            stage=STAGE_COSYVOICE,
            started_mono=started_mono,
            stage_started_mono=_MONOTONIC(),
            stage_elapsed=stage_elapsed,
        )
    cosy_started = _MONOTONIC()
    try:
        cosy_ok = bool(prepare_local(cancel_event=cancel_event))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.warning(
            "本地语音准备：CosyVoice prepare 异常 err=%s",
            exception_log_label(exc),
        )
        return _failure(
            stage=STAGE_COSYVOICE,
            started_mono=started_mono,
            stage_started_mono=cosy_started,
            stage_elapsed=stage_elapsed,
            detail=exception_log_label(exc),
        )
    stage_elapsed[STAGE_COSYVOICE] = max(0.0, _MONOTONIC() - cosy_started)
    if not cosy_ok:
        return _failure(
            stage=STAGE_COSYVOICE,
            started_mono=started_mono,
            stage_started_mono=cosy_started,
            stage_elapsed=stage_elapsed,
            detail="cosy_prepare_false",
        )
    if cancel_event is not None and cancel_event.is_set():
        return _cancelled(
            stage=STAGE_COSYVOICE,
            started_mono=started_mono,
            stage_started_mono=cosy_started,
            stage_elapsed=stage_elapsed,
        )

    total = max(0.0, _MONOTONIC() - started_mono)
    log.info(
        "本地语音串行准备完成 total_elapsed_sec=%.3f stages=%s",
        total,
        ",".join(sorted(stage_elapsed.keys())),
    )
    return LocalVoiceWarmupResult(
        ok=True,
        final_stage=STAGE_COMPLETED,
        total_elapsed_sec=total,
        stage_elapsed_sec=_freeze_stage_elapsed(stage_elapsed),
    )
