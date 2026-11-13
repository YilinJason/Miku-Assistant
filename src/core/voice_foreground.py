"""本地语音前台有界 pending：单 Job owner 加载线程委托 ``ensure_voice_transaction``。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TYPE_CHECKING

from src.core.voice_activation import (
    bound_rvc_explicit_retry_needed,
    bound_tts_models_ready,
    config_expects_edge_rvc_warmup,
    edge_rvc_warmup_required,
    is_cosyvoice_tts,
    voice_stack_load_ok,
)

if TYPE_CHECKING:
    from src.core.assistant import MikuAssistant, VoiceStackLoadResult

_READY_FN = Callable[[], bool]


@dataclass
class _ForegroundLoadJob:
    """单次前台加载任务；generation 与 done 不跨代复用。"""

    generation: int
    kind: str
    allow_retry: bool
    done: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    start_attempted: bool = False
    start_ack: bool = False
    start_unknown: bool = False
    worker_entered: bool = False
    creator_done: bool = False
    terminal: bool = False
    result: Optional[VoiceStackLoadResult] = None


def configured_cosyvoice(app: MikuAssistant) -> bool:
    if app.tts_engine is not None and is_cosyvoice_tts(app.tts_engine):
        return True
    voice = app.config.get("voice") if app.config is not None else None
    if not isinstance(voice, dict):
        return False
    tts_cfg = voice.get("tts")
    if not isinstance(tts_cfg, dict):
        return False
    return tts_cfg.get("engine") == "cosyvoice"


def cosy_foreground_path(app: MikuAssistant) -> bool:
    """配置或已绑定 Cosy 时走有界 request（不依赖 autoload 总开关）。"""
    return configured_cosyvoice(app)


def configured_edge_rvc_foreground(app: MikuAssistant) -> bool:
    """默认 Edge+RVC 且配置要求 RVC 预热时走有界 request。"""
    if app.tts_engine is not None:
        return edge_rvc_warmup_required(app.tts_engine, app.resource_settings)
    return config_expects_edge_rvc_warmup(app.config, app.resource_settings)


def tts_foreground_bound_path(app: MikuAssistant) -> bool:
    """Cosy 或 Edge+RVC 预热路径使用同一前台 Job。"""
    return configured_cosyvoice(app) or configured_edge_rvc_foreground(app)


def _cosy_worker_readonly_ready(tts_engine: Any) -> bool:
    reader = getattr(tts_engine, "local_cosyvoice_worker_playback_ready", None)
    if not callable(reader):
        return False
    try:
        value = reader()
    except Exception:
        return False
    if value is True:
        return True
    if value is False:
        return False
    return False


def refresh_app_tts_playback_ready(app: MikuAssistant) -> None:
    tts = app.tts_engine
    if tts is None or not is_cosyvoice_tts(tts):
        return
    if not app._local_voice_prepare_ok:
        return
    if _cosy_worker_readonly_ready(tts):
        return
    with app._lifecycle_lock:
        if app.tts_engine is tts:
            app._local_voice_prepare_ok = False


def app_tts_playback_ready(app: MikuAssistant) -> bool:
    refresh_app_tts_playback_ready(app)
    if app.tts_engine is None:
        return False
    if is_cosyvoice_tts(app.tts_engine):
        return bool(app._local_voice_prepare_ok) and _cosy_worker_readonly_ready(
            app.tts_engine
        )
    return bound_tts_models_ready(
        app.tts_engine,
        local_prepare_ok=True,
        resource_settings=app.resource_settings,
    )


def app_voice_stack_ready(app: MikuAssistant) -> bool:
    if app.stt_engine is None:
        return False
    return app_tts_playback_ready(app)


def _result_ok_for_request(
    app: MikuAssistant,
    result: VoiceStackLoadResult,
    *,
    ready_fn: _READY_FN,
) -> VoiceStackLoadResult:
    from src.core.assistant import VoiceStackLoadResult

    if not result.ok:
        return result
    if ready_fn():
        return voice_stack_load_ok(
            app.tts_engine,
            resource_settings=app.resource_settings,
            extra_warnings=list(result.warnings),
        )
    return VoiceStackLoadResult(
        ok=False,
        pending=True,
        warnings=list(result.warnings),
    )


def _job_in_flight(job: Optional[_ForegroundLoadJob]) -> bool:
    if job is None:
        return False
    if not job.creator_done:
        return True
    if job.start_unknown:
        if not job.terminal:
            return True
        if job.thread is not None and job.thread.is_alive():
            return True
        return False
    if not job.terminal:
        return True
    if job.thread is not None and job.thread.is_alive():
        return True
    return False


def _job_reapable_locked(job: _ForegroundLoadJob) -> bool:
    if not job.creator_done:
        return False
    if job.start_unknown:
        if not job.terminal:
            return False
        if job.thread is not None and job.thread.is_alive():
            return False
        return True
    if not job.terminal:
        return False
    if job.thread is None:
        return True
    if job.thread.is_alive():
        return False
    return True


def _reap_finished_job_locked(app: MikuAssistant) -> None:
    job = app._fg_active_job
    if job is None:
        return
    if not _job_reapable_locked(job):
        return
    app._fg_active_job = None


def _reap_finished_job(app: MikuAssistant) -> None:
    with app._fg_lock:
        _reap_finished_job_locked(app)


def foreground_voice_load_blocks_shutdown(app: MikuAssistant) -> bool:
    with app._fg_lock:
        job = app._fg_active_job
        if job is None:
            return False
        return _job_in_flight(job)


def join_foreground_loader_bounded(app: MikuAssistant, timeout: float) -> bool:
    """锁外 join 活跃 Job 线程；True 仅当 job 静止（与 ``foreground_voice_load_blocks_shutdown`` 一致）。"""
    with app._fg_lock:
        job = app._fg_active_job
        if job is None:
            return True
        if not job.creator_done:
            return False
        thread = job.thread if (
            job.start_ack or job.start_unknown or job.worker_entered
        ) else None
    if thread is None:
        _reap_finished_job(app)
        return not foreground_voice_load_blocks_shutdown(app)
    if not thread.is_alive():
        if foreground_voice_load_blocks_shutdown(app):
            return False
        _reap_finished_job(app)
        return not foreground_voice_load_blocks_shutdown(app)
    thread.join(timeout=max(0.0, float(timeout)))
    if thread.is_alive():
        return False
    _reap_finished_job(app)
    return not foreground_voice_load_blocks_shutdown(app)


def _interrupt_engine(engine: Any) -> None:
    interrupt = getattr(engine, "interrupt", None)
    if callable(interrupt):
        try:
            interrupt()
        except Exception:
            pass


def _interrupt_registered_voice(app: MikuAssistant) -> None:
    app._voice_load_cancel.set()
    with app._lifecycle_lock:
        published = app.tts_engine
        owner = app._voice_owner_slot
        candidate = owner.tts if owner is not None else None
    engines: list[Any] = []
    if published is not None:
        engines.append(published)
    if candidate is not None and candidate is not published:
        engines.append(candidate)
    seen: set[int] = set()
    for engine in engines:
        key = id(engine)
        if key in seen:
            continue
        seen.add(key)
        _interrupt_engine(engine)


def notify_foreground_shutdown(app: MikuAssistant) -> None:
    with app._fg_lock:
        job = app._fg_active_job
        if job is not None:
            job.done.set()
    _interrupt_registered_voice(app)


def _pending_result(*, label: str) -> VoiceStackLoadResult:
    from src.core.assistant import VoiceStackLoadResult

    return VoiceStackLoadResult(
        ok=False,
        pending=True,
        warnings=[f"{label}仍在准备中（文本对话可用）"],
    )


def _wait_budget(app: MikuAssistant, *, wait: bool, wait_sec: Optional[float]) -> float:
    if not wait:
        return 0.0
    cap = float(app.resource_settings.foreground_warmup_wait_sec)
    if wait_sec is not None:
        cap = min(cap, max(0.0, float(wait_sec)))
    with app._fg_lock:
        deadline = app._fg_deadline_mono
    if deadline <= 0:
        return cap
    return min(cap, max(0.0, deadline - time.monotonic()))


def _sync_guard_immediate_fail(
    app: MikuAssistant, *, kind: str
) -> Optional[VoiceStackLoadResult]:
    """缺组件且禁止 autoload 构造时立即失败（不进 lane 长等）。"""
    from src.core.assistant import VoiceStackLoadResult

    with app._lifecycle_lock:
        closing = app._lifecycle_closed()
        autoload = app._voice_autoload_enabled
        tts = app.tts_engine
        stt = app.stt_engine
    if closing:
        return VoiceStackLoadResult(ok=False, errors=["助手正在关闭，无法加载语音栈"])
    need_tts = tts is None
    need_stt = kind == "voice_stack" and stt is None
    if (need_tts or need_stt) and not autoload:
        return VoiceStackLoadResult(
            ok=False,
            errors=[
                "语音栈未装配且注入路径禁止自动创建。"
                "请注入 tts_engine/stt_engine 或使用可懒加载的 startup_mode。"
            ],
        )
    return None


def _sticky_failure_result(app: MikuAssistant) -> Optional[VoiceStackLoadResult]:
    with app._fg_lock:
        cached = app._fg_sticky_failure
    if cached is not None:
        return cached
    if app._tts_load_failed:
        from src.core.assistant import VoiceStackLoadResult

        return VoiceStackLoadResult(
            ok=False,
            errors=["TTS 先前加载失败；请输入 tts 显式重试"],
        )
    return None


def _store_terminal_failure(app: MikuAssistant, result: VoiceStackLoadResult) -> None:
    if result.ok or result.pending:
        return
    with app._fg_lock:
        app._fg_sticky_failure = result
    with app._lifecycle_lock:
        app._tts_load_failed = True


def _worker_run(app: MikuAssistant, job: _ForegroundLoadJob) -> None:
    from src.core.assistant import VoiceStackLoadResult
    from src.core.log_privacy import exception_log_label

    with app._fg_lock:
        if app._fg_active_job is job:
            job.worker_entered = True

    result: VoiceStackLoadResult = VoiceStackLoadResult(
        ok=False, errors=["语音加载未完成"]
    )
    try:
        while True:
            with app._fg_lock:
                if app._fg_active_job is not job:
                    result = VoiceStackLoadResult(
                        ok=False, errors=["语音加载任务已被取代"]
                    )
                    break
                run_kind = job.kind
                retry = job.allow_retry
            if run_kind == "voice_stack":
                result = app.ensure_voice_stack()
            else:
                result = app.ensure_tts_for_playback(allow_retry=retry)
            if not result.ok:
                break
            with app._fg_lock:
                upgraded = job.kind == "voice_stack" and run_kind == "tts"
            if upgraded and not app_voice_stack_ready(app):
                continue
            if run_kind == "voice_stack" and not app_voice_stack_ready(app):
                result = VoiceStackLoadResult(
                    ok=False,
                    pending=True,
                    warnings=["语音栈仍在准备中（文本对话可用）"],
                )
                break
            if run_kind == "tts" and not app_tts_playback_ready(app):
                result = VoiceStackLoadResult(
                    ok=False,
                    pending=True,
                    warnings=["TTS 仍在准备中（文本对话可用）"],
                )
                break
            break
    except (KeyboardInterrupt, SystemExit):
        result = VoiceStackLoadResult(ok=False, errors=["语音加载已中断"])
    except Exception as exc:
        result = VoiceStackLoadResult(
            ok=False,
            errors=[f"语音加载失败: {exception_log_label(exc)}"],
        )
    finally:
        if not result.ok and not result.pending:
            _store_terminal_failure(app, result)
        with app._fg_lock:
            job.result = result
            job.terminal = True
        job.done.set()


def _creator_start_job(
    app: MikuAssistant,
    *,
    kind: str,
    allow_retry: bool,
) -> None:
    from src.core.assistant import VoiceStackLoadResult

    _reap_finished_job(app)
    with app._fg_lock:
        active = app._fg_active_job
        if _job_in_flight(active):
            if active is not None and kind == "voice_stack" and active.kind == "tts":
                active.kind = "voice_stack"
            return
        if app._fg_deadline_mono <= time.monotonic():
            app._fg_deadline_mono = (
                time.monotonic()
                + float(app.resource_settings.foreground_warmup_wait_sec)
            )
        app._fg_generation += 1
        gen = app._fg_generation
        job = _ForegroundLoadJob(
            generation=gen,
            kind=kind,
            allow_retry=allow_retry,
        )
        app._fg_active_job = job
        if allow_retry:
            app._fg_sticky_failure = None

    thread: Optional[threading.Thread] = None
    terminal_fail: Optional[VoiceStackLoadResult] = None
    pending_reraise: Optional[BaseException] = None
    try:
        thread = threading.Thread(
            target=_worker_run,
            args=(app, job),
            name=f"miku-voice-fg-{gen}",
            daemon=True,
        )
        with app._fg_lock:
            if app._fg_active_job is not job:
                return
            job.thread = thread
            job.start_attempted = True
        thread.start()
        with app._fg_lock:
            if app._fg_active_job is not job:
                return
            job.start_ack = True
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            pending_reraise = exc
            terminal_fail = VoiceStackLoadResult(
                ok=False, errors=["语音加载已中断"]
            )
        elif isinstance(exc, Exception):
            terminal_fail = VoiceStackLoadResult(
                ok=False,
                errors=["语音加载线程启动失败"],
            )
            _store_terminal_failure(app, terminal_fail)
        else:
            pending_reraise = exc
            terminal_fail = VoiceStackLoadResult(
                ok=False, errors=["语音加载已中断"]
            )
        with app._fg_lock:
            if app._fg_active_job is not job:
                if pending_reraise is not None:
                    raise pending_reraise
                return
            if thread is not None:
                job.thread = thread
                job.start_unknown = True
            elif terminal_fail is not None:
                job.result = terminal_fail
                job.terminal = True
    finally:
        with app._fg_lock:
            if app._fg_active_job is job:
                job.creator_done = True
        if job.terminal:
            job.done.set()
        if pending_reraise is not None:
            raise pending_reraise


def _active_job(app: MikuAssistant) -> Optional[_ForegroundLoadJob]:
    with app._fg_lock:
        return app._fg_active_job


def _fg_wait_outcome(
    app: MikuAssistant,
    *,
    wait: bool,
    wait_sec: Optional[float],
    label: str,
    ready_fn: _READY_FN,
) -> VoiceStackLoadResult:
    from src.core.assistant import VoiceStackLoadResult

    job = _active_job(app)
    wait_job = job
    budget = _wait_budget(app, wait=wait, wait_sec=wait_sec)
    if job is not None and budget > 0:
        job.done.wait(timeout=budget)
    with app._lifecycle_lock:
        if app._lifecycle_closed():
            return VoiceStackLoadResult(
                ok=False, errors=[f"助手正在关闭，无法加载{label}"]
            )
    cached: Optional[VoiceStackLoadResult] = None
    with app._fg_lock:
        if wait_job is not None:
            cached = wait_job.result
    _reap_finished_job(app)
    if cached is not None:
        if cached.pending:
            return cached
        if not cached.ok:
            return cached
        return _result_ok_for_request(app, cached, ready_fn=ready_fn)
    if ready_fn():
        if not (
            wait_job is not None
            and wait_job.allow_retry
            and bound_rvc_explicit_retry_needed(
                app.tts_engine, app.resource_settings
            )
        ):
            return voice_stack_load_ok(
                app.tts_engine,
                resource_settings=app.resource_settings,
            )
    job = _active_job(app)
    if _job_in_flight(job):
        return _pending_result(label=label)
    sticky = _sticky_failure_result(app)
    if sticky is not None:
        return sticky
    return _pending_result(label=label)


def _fg_request(
    app: MikuAssistant,
    *,
    kind: str,
    allow_retry: bool,
    wait: bool,
    wait_sec: Optional[float],
    label: str,
    ready_fn: _READY_FN,
) -> VoiceStackLoadResult:
    from src.core.assistant import VoiceStackLoadResult

    with app._lifecycle_lock:
        if app._lifecycle_closed():
            return VoiceStackLoadResult(
                ok=False, errors=[f"助手正在关闭，无法加载{label}"]
            )
    refresh_app_tts_playback_ready(app)
    if ready_fn():
        if not (
            allow_retry
            and bound_rvc_explicit_retry_needed(
                app.tts_engine, app.resource_settings
            )
        ):
            return voice_stack_load_ok(
                app.tts_engine,
                resource_settings=app.resource_settings,
            )

    guard = _sync_guard_immediate_fail(app, kind=kind)
    if guard is not None:
        return guard

    if not allow_retry:
        sticky = _sticky_failure_result(app)
        if sticky is not None:
            return sticky

    _creator_start_job(app, kind=kind, allow_retry=allow_retry)
    return _fg_wait_outcome(
        app,
        wait=wait,
        wait_sec=wait_sec,
        label=label,
        ready_fn=ready_fn,
    )


def request_tts_for_playback(
    app: MikuAssistant,
    *,
    allow_retry: bool = False,
    wait: bool = True,
    wait_sec: Optional[float] = None,
) -> VoiceStackLoadResult:
    if not tts_foreground_bound_path(app):
        return app.ensure_tts_for_playback(allow_retry=allow_retry)
    out = _fg_request(
        app,
        kind="tts",
        allow_retry=allow_retry,
        wait=wait,
        wait_sec=wait_sec,
        label="TTS",
        ready_fn=lambda: app_tts_playback_ready(app),
    )
    return _result_ok_for_request(
        app, out, ready_fn=lambda: app_tts_playback_ready(app)
    )


def request_voice_stack(
    app: MikuAssistant,
    *,
    wait: bool = True,
    wait_sec: Optional[float] = None,
) -> VoiceStackLoadResult:
    if not tts_foreground_bound_path(app):
        return app.ensure_voice_stack()
    out = _fg_request(
        app,
        kind="voice_stack",
        allow_retry=False,
        wait=wait,
        wait_sec=wait_sec,
        label="语音栈",
        ready_fn=lambda: app_voice_stack_ready(app),
    )
    return _result_ok_for_request(
        app, out, ready_fn=lambda: app_voice_stack_ready(app)
    )
