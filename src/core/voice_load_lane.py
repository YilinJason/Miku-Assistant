"""App 语音加载唯一同步事务（单 owner 槽 + generation lane）；由 ``MikuAssistant`` 委托。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

from src.core.voice_activation import (
    UnpublishedVoice,
    bound_rvc_explicit_retry_needed,
    bound_tts_models_ready,
    edge_rvc_warmup_required,
    is_cosyvoice_tts,
    run_bound_rvc_explicit_retry,
    try_retire_unpublished_voice,
    voice_stack_load_ok,
    warmup_registered_tts_candidate,
)

if TYPE_CHECKING:
    from src.core.assistant import MikuAssistant, VoiceStackLoadResult


@dataclass
class _VoiceSnap:
    closing: bool
    tts: Any
    stt: Any
    tts_ready: bool
    load_failed: bool
    owner: Optional[UnpublishedVoice]
    autoload: bool


def _snap(app: MikuAssistant) -> _VoiceSnap:
    with app._lifecycle_lock:
        return _VoiceSnap(
            closing=app._lifecycle_closed(),
            tts=app.tts_engine,
            stt=app.stt_engine,
            tts_ready=bound_tts_models_ready(
                app.tts_engine,
                local_prepare_ok=app._local_voice_prepare_ok,
                resource_settings=app.resource_settings,
            ),
            load_failed=app._tts_load_failed,
            owner=app._voice_owner_slot,
            autoload=app._voice_autoload_enabled,
        )


def _stack_satisfied(*, want_tts: bool, want_stt: bool, snap: _VoiceSnap) -> bool:
    tts_ok = (not want_tts) or (snap.tts is not None and snap.tts_ready)
    stt_ok = (not want_stt) or (snap.stt is not None)
    return tts_ok and stt_ok


def _bound_edge_rvc_retry_pending(
    app: MikuAssistant,
    snap: _VoiceSnap,
    *,
    want_tts: bool,
    allow_retry: bool,
) -> bool:
    if not allow_retry or not want_tts or snap.tts is None:
        return False
    return bound_rvc_explicit_retry_needed(snap.tts, app.resource_settings)


def _run_bound_rvc_explicit_retry(app: MikuAssistant, tts_engine: Any) -> "VoiceStackLoadResult":
    return run_bound_rvc_explicit_retry(
        llm_client=app.llm_client,
        tts_engine=tts_engine,
        settings=app.resource_settings,
        cancel_event=app._voice_load_cancel,
        log=app.logger,
        local_prepare_ok=app._local_voice_prepare_ok,
        app=app,
    )


def _fail_block(app: MikuAssistant, *, allow_retry: bool) -> Optional[VoiceStackLoadResult]:
    from src.core.assistant import VoiceStackLoadResult

    if app._tts_load_failed and not allow_retry:
        return VoiceStackLoadResult(
            ok=False,
            errors=["TTS 先前加载失败；请输入 tts 显式重试"],
        )
    if app._voice_owner_slot is not None and not allow_retry:
        return VoiceStackLoadResult(
            ok=False,
            errors=["TTS 先前加载失败；请输入 tts 显式重试"],
        )
    return None


def _stop_rvc_for_owner(app: MikuAssistant) -> bool:
    return app.tts_engine is None


def _retire_owner_outside_locks(
    app: MikuAssistant, owner: UnpublishedVoice, *, stop_rvc: Optional[bool] = None
) -> bool:
    if owner.tts is None and owner.stt is None:
        with app._lifecycle_lock:
            if app._voice_owner_slot is owner:
                app._voice_owner_slot = None
        return True
    if stop_rvc is None:
        stop_rvc = _stop_rvc_for_owner(app)
    if not try_retire_unpublished_voice(owner, stop_rvc=stop_rvc):
        return False
    with app._lifecycle_lock:
        if app._voice_owner_slot is owner:
            app._voice_owner_slot = None
    return True


def _reserve_lane(app: MikuAssistant, *, label: str) -> tuple[Optional[Any], int]:
    from src.core.assistant import VoiceStackLoadResult

    with app._voice_loader_lock:
        waited = app._wait_while_voice_loading_locked(label=label)
        if waited is not None:
            return waited, 0
        with app._lifecycle_lock:
            if app._lifecycle_closed():
                return (
                    VoiceStackLoadResult(
                        ok=False,
                        errors=[f"助手正在关闭，无法加载{label}"],
                    ),
                    0,
                )
        app._voice_lane_ticket += 1
        ticket = app._voice_lane_ticket
        app._voice_lane_holder = ticket
        app._voice_loading = True
    return None, ticket


def _release_lane(app: MikuAssistant, ticket: int) -> None:
    if ticket <= 0:
        return
    with app._voice_loader_lock:
        if app._voice_lane_holder == ticket:
            app._voice_lane_holder = None
            app._voice_loading = False
            app._voice_load_cv.notify_all()


def _run_prepare(app: MikuAssistant, tts_engine: Any) -> VoiceStackLoadResult:
    from src.core.assistant import VoiceStackLoadResult

    ok_warm, warm_errors, warm_warnings = warmup_registered_tts_candidate(
        llm_client=app.llm_client,
        tts_engine=tts_engine,
        settings=app.resource_settings,
        cancel_event=app._voice_load_cancel,
        log=app.logger,
    )
    if ok_warm:
        return VoiceStackLoadResult(ok=True, warnings=list(warm_warnings))
    return VoiceStackLoadResult(
        ok=False,
        errors=warm_errors or ["语音预热未完成"],
    )


def _bound_prepare_pending(app: MikuAssistant, tts_engine: Any, *, tts_ready: bool) -> bool:
    if tts_engine is None or tts_ready:
        return False
    if is_cosyvoice_tts(tts_engine):
        return True
    return edge_rvc_warmup_required(tts_engine, app.resource_settings)


def _atomic_publish_owner(
    app: MikuAssistant,
    owner: UnpublishedVoice,
    *,
    move_tts: bool,
    move_stt: bool,
) -> tuple[bool, bool]:
    """
    锁内一次性 move 到 App；成功则清空 owner 引用并摘 slot。

    Returns:
        (published_ok, should_retire_unpublished)
    """
    with app._lifecycle_lock:
        if app._lifecycle_closed():
            return False, True
        if move_tts and app.tts_engine is not None:
            return False, True
        if move_stt and app.stt_engine is not None:
            return False, True
        if move_tts and owner.tts is not None:
            app.tts_engine = owner.tts
            app._owned_tts = True
            app._local_voice_prepare_ok = True
            owner.tts = None
        if move_stt and owner.stt is not None:
            app.stt_engine = owner.stt
            app._owned_stt = True
            owner.stt = None
        app._tts_load_failed = False
        if app._voice_owner_slot is owner:
            app._voice_owner_slot = None
    return True, False


def _set_voice_load_failed(app: MikuAssistant, *, tts_was_published: bool) -> None:
    if not tts_was_published and app.tts_engine is None:
        app._tts_load_failed = True


def ensure_voice_transaction(
    app: MikuAssistant,
    *,
    want_tts: bool,
    want_stt: bool,
    allow_retry: bool,
    lane_label: str,
) -> VoiceStackLoadResult:
    from src.core.assistant import VoiceStackLoadResult
    from src.core.log_privacy import exception_log_label

    snap = _snap(app)
    if snap.closing:
        return VoiceStackLoadResult(
            ok=False, errors=[f"助手正在关闭，无法加载{lane_label}"]
        )

    if _stack_satisfied(want_tts=want_tts, want_stt=want_stt, snap=snap) and not (
        _bound_edge_rvc_retry_pending(
            app, snap, want_tts=want_tts, allow_retry=allow_retry
        )
    ):
        return voice_stack_load_ok(
            snap.tts,
            resource_settings=app.resource_settings,
        )

    needs_slot = (want_tts and snap.tts is None) or (want_stt and snap.stt is None)
    if not snap.autoload and needs_slot:
        return VoiceStackLoadResult(
            ok=False,
            errors=[
                "语音栈未装配且注入路径禁止自动创建。"
                "请注入 tts_engine 或使用可懒加载的 startup_mode。"
            ],
        )
    early, ticket = _reserve_lane(app, label=lane_label)
    if early is not None:
        return early

    owner: Optional[UnpublishedVoice] = None
    published_moved = False
    result = VoiceStackLoadResult(ok=False, errors=["语音加载未完成"])
    try:
        blocked = _fail_block(app, allow_retry=allow_retry)
        if blocked is not None:
            result = blocked
            return result
        if allow_retry:
            with app._lifecycle_lock:
                app._tts_load_failed = False
                app._tts_fail_notified = False

        snap = _snap(app)
        if snap.closing:
            result = VoiceStackLoadResult(
                ok=False, errors=[f"助手正在关闭，无法加载{lane_label}"]
            )
            return result
        if _stack_satisfied(want_tts=want_tts, want_stt=want_stt, snap=snap) and not (
            _bound_edge_rvc_retry_pending(
                app, snap, want_tts=want_tts, allow_retry=allow_retry
            )
        ):
            result = voice_stack_load_ok(
                snap.tts,
                resource_settings=app.resource_settings,
            )
            published_moved = True
            return result

        if _bound_edge_rvc_retry_pending(
            app, snap, want_tts=want_tts, allow_retry=allow_retry
        ):
            result = _run_bound_rvc_explicit_retry(app, snap.tts)
            if not result.ok:
                return result
            snap = _snap(app)
            if _stack_satisfied(want_tts=want_tts, want_stt=want_stt, snap=snap):
                result = voice_stack_load_ok(
                    snap.tts,
                    resource_settings=app.resource_settings,
                )
                published_moved = True
                return result

        if snap.owner is not None:
            if not allow_retry:
                result = VoiceStackLoadResult(
                    ok=False,
                    errors=["TTS 先前加载失败；请输入 tts 显式重试"],
                )
                return result
            if not _retire_owner_outside_locks(app, snap.owner):
                result = VoiceStackLoadResult(
                    ok=False,
                    errors=["语音候选尚未回收完成，无法重试"],
                )
                return result
            snap = _snap(app)
            if snap.owner is not None:
                result = VoiceStackLoadResult(
                    ok=False,
                    errors=["语音候选尚未回收完成，无法重试"],
                )
                return result

        need_build_tts = want_tts and snap.tts is None
        need_build_stt = want_stt and snap.stt is None
        need_prepare_bound = want_tts and _bound_prepare_pending(
            app, snap.tts, tts_ready=snap.tts_ready
        )

        if need_prepare_bound:
            prep = _run_prepare(app, snap.tts)
            if not prep.ok:
                app._tts_load_failed = True
                result = prep
                return result
            with app._lifecycle_lock:
                if app._lifecycle_closed():
                    result = VoiceStackLoadResult(
                        ok=False, errors=["助手正在关闭，无法准备本地语音"]
                    )
                    return result
                app._local_voice_prepare_ok = True
                app._tts_load_failed = False
            snap = _snap(app)
            need_build_tts = False
            need_build_stt = want_stt and snap.stt is None
            if not need_build_stt:
                result = voice_stack_load_ok(
                    snap.tts,
                    resource_settings=app.resource_settings,
                    extra_warnings=list(prep.warnings),
                )
                published_moved = True
                return result

        if not need_build_tts and not need_build_stt:
            result = voice_stack_load_ok(
                snap.tts,
                resource_settings=app.resource_settings,
            )
            published_moved = True
            return result

        with app._lifecycle_lock:
            if app._voice_owner_slot is not None:
                result = VoiceStackLoadResult(
                    ok=False,
                    errors=["语音候选占用中，无法分配新对象"],
                )
                return result
            owner = UnpublishedVoice()
            app._voice_owner_slot = owner

        if need_build_tts:
            import src.core.assistant as _assistant_build

            tts_kwargs: dict = {}
            if app._tts_engine_cls is not None:
                tts_kwargs["tts_engine_cls"] = app._tts_engine_cls
            try:
                owner.tts = _assistant_build.build_tts_engine(
                    app.config,
                    app.safety_policy,
                    warm_rvc_on_init=False,
                    temp_file_manager=app._temp_file_manager,
                    **tts_kwargs,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                _set_voice_load_failed(app, tts_was_published=False)
                app.logger.error(
                    "TTS 构造失败 err=%s",
                    exception_log_label(exc),
                    exc_info=True,
                )
                result = VoiceStackLoadResult(
                    ok=False, errors=[f"TTS 初始化失败: {exc}"]
                )
                return result

        if need_build_stt:
            import src.core.assistant as _assistant_build

            stt_kwargs: dict = {}
            if app._stt_engine_cls is not None:
                stt_kwargs["stt_engine_cls"] = app._stt_engine_cls
            try:
                owner.stt = _assistant_build.build_stt_engine(
                    app.config,
                    safety_policy=app.safety_policy,
                    temp_file_manager=app._temp_file_manager,
                    **stt_kwargs,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                _set_voice_load_failed(
                    app, tts_was_published=app._bound_tts_ready()
                )
                result = VoiceStackLoadResult(
                    ok=False, errors=[f"语音栈初始化失败: {exc}"]
                )
                return result

        if owner.tts is not None:
            prep = _run_prepare(app, owner.tts)
            if not prep.ok:
                _set_voice_load_failed(app, tts_was_published=False)
                result = prep
                return result
            owner_prep_warnings = list(prep.warnings)
        else:
            owner_prep_warnings = []

        move_tts = need_build_tts and owner.tts is not None
        move_stt = need_build_stt and owner.stt is not None
        ok_pub, retire_unpub = _atomic_publish_owner(
            app, owner, move_tts=move_tts, move_stt=move_stt
        )
        if ok_pub:
            published_moved = True
            app.logger.info(
                "语音加载完成 session=%s label=%s",
                app.session_timer.session_id,
                lane_label,
            )
            result = voice_stack_load_ok(
                app.tts_engine,
                resource_settings=app.resource_settings,
                extra_warnings=owner_prep_warnings,
            )
            return result
        if retire_unpub:
            result = VoiceStackLoadResult(
                ok=False, errors=["助手正在关闭，已丢弃新建语音栈"],
            )
            return result
        result = VoiceStackLoadResult(ok=False, errors=["语音加载未完成"])
        return result
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        app.logger.error(
            "语音加载事务失败 err=%s",
            exception_log_label(exc),
            exc_info=True,
        )
        result = VoiceStackLoadResult(
            ok=False, errors=[f"语音加载失败: {exc}"]
        )
        return result
    finally:
        if owner is not None and not published_moved:
            try:
                if not _retire_owner_outside_locks(app, owner):
                    _set_voice_load_failed(
                        app,
                        tts_was_published=app.tts_engine is not None
                        and app._bound_tts_ready(),
                    )
            except Exception:
                _set_voice_load_failed(
                    app,
                    tts_was_published=app.tts_engine is not None
                    and app._bound_tts_ready(),
                )
        _release_lane(app, ticket)


def shutdown_retire_owner_in_lane(app: MikuAssistant) -> None:
    with app._voice_loader_lock:
        if app._voice_loading:
            return
        app._voice_lane_ticket += 1
        ticket = app._voice_lane_ticket
        app._voice_lane_holder = ticket
        app._voice_loading = True
    try:
        with app._lifecycle_lock:
            owner = app._voice_owner_slot
        if owner is not None:
            _retire_owner_outside_locks(app, owner)
    finally:
        _release_lane(app, ticket)
