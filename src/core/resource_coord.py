"""按模式串行协调 Ollama / RVC 预热，以及退出时 best-effort 模型卸载请求。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Literal, Optional

from src.core.log_privacy import exception_log_label
from src.core.resource_settings import ResourceSettings

logger = logging.getLogger(__name__)

WarmupWaitStatus = Literal["completed", "timeout", "failed", "skipped"]


@dataclass(frozen=True)
class WarmupWaitResult:
    status: WarmupWaitStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("completed", "skipped")


def _wait_ollama_warmup(llm_client: Any, *, timeout: float) -> WarmupWaitResult:
    """
    有界等待 Ollama 预热结束。

    普通异常转为 ``failed``（不使装配失败）；``KeyboardInterrupt`` / ``SystemExit`` 上抛。
    ``detail`` 仅稳定状态标签，不含 ``str(exc)``。
    """
    wait = getattr(llm_client, "wait_for_warmup", None)
    try:
        if callable(wait):
            finished = wait(timeout=timeout)
            if finished:
                return WarmupWaitResult("completed")
            return WarmupWaitResult("timeout", "wait_for_warmup_false")
        done = getattr(llm_client, "_warmup_done", None)
        if done is not None and hasattr(done, "wait"):
            finished = done.wait(timeout=max(1.0, float(timeout)) + 5.0)
            if finished:
                return WarmupWaitResult("completed")
            return WarmupWaitResult("timeout", "warmup_event_timeout")
        return WarmupWaitResult("skipped", "no_wait_api")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        return WarmupWaitResult("failed", exception_log_label(exc))


def start_ollama_warmup(
    llm_client: Any,
    *,
    settings: ResourceSettings,
    log: Optional[logging.Logger] = None,
) -> bool:
    """按配置启动 Ollama 后台预热；返回是否新启动了线程。"""
    log = log or logger
    if not settings.ollama_warmup_on_start or llm_client is None:
        return False
    start = getattr(llm_client, "start_warmup", None)
    if not callable(start):
        return False
    try:
        started = start(timeout=float(settings.warmup_timeout))
        if started is not False:
            log.info("Ollama 模型后台预热已启动（资源协调）")
            return bool(started)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.warning(
            "Ollama 模型后台预热启动失败（正式请求仍可继续） err=%s",
            exception_log_label(exc),
        )
    return False


def prepare_rvc_if_needed(
    tts_engine: Any,
    *,
    settings: ResourceSettings,
    log: Optional[logging.Logger] = None,
) -> bool:
    """对已构造的 TTS 执行延迟 RVC 健康检查与 Worker 预热。"""
    log = log or logger
    if not settings.rvc_warmup_on_voice_start or tts_engine is None:
        return False
    prepare = getattr(tts_engine, "prepare_rvc", None)
    if not callable(prepare):
        return False
    try:
        ok = prepare()
        log.info("RVC 预热协调完成 ok=%s", ok)
        return bool(ok)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.warning(
            "RVC 预热失败（正式播报仍可继续） err=%s",
            exception_log_label(exc),
        )
        return False


def wait_ollama_warmup_succeeded_for_explicit_retry(
    llm_client: Any,
    *,
    timeout: float,
    cancel_event: Any = None,
) -> WarmupWaitResult:
    """
    显式 RVC 重试专用：须 ``begin_fresh_warmup`` + ``warmup_snapshot`` 证明 succeeded。

    不接受仅 ``wait_for_warmup`` 返回 True；缺 API 时 fail-closed。
    """
    from src.core.local_voice_warmup import _MONOTONIC, _wait_fresh_ollama

    if cancel_event is not None and cancel_event.is_set():
        return WarmupWaitResult("failed", "cancelled")
    snap_fn = getattr(llm_client, "warmup_snapshot", None)
    begin_fn = getattr(llm_client, "begin_fresh_warmup", None)
    if not callable(snap_fn) or not callable(begin_fn):
        return WarmupWaitResult("failed", "ollama_warmup_api_missing")
    deadline = _MONOTONIC() + float(timeout)
    ok, detail = _wait_fresh_ollama(
        llm_client,
        deadline_mono=deadline,
        warmup_timeout=float(timeout),
        cancel_event=cancel_event,
    )
    if ok:
        return WarmupWaitResult("completed")
    if detail == "cancelled":
        return WarmupWaitResult("failed", detail)
    return WarmupWaitResult("failed", detail or "ollama_warmup_failed")


def run_coordinated_warmup(
    *,
    llm_client: Any,
    tts_engine: Any,
    settings: ResourceSettings,
    log: Optional[logging.Logger] = None,
    events: Optional[List[str]] = None,
) -> None:
    """
    按 ``resource.warmup`` 串行或顺序执行预热。

    ``serialize_gpu=True`` 且 Ollama 等待未确认完成时：**本轮不启动 RVC**，
    避免与仍可能占用 GPU 的 Ollama runner 重叠。
    """
    log = log or logger
    trail = events if events is not None else []

    def do_ollama(*, wait: bool) -> WarmupWaitResult:
        started = start_ollama_warmup(llm_client, settings=settings, log=log)
        trail.append("ollama_start" if started else "ollama_skip")
        if not wait or not settings.ollama_warmup_on_start or llm_client is None:
            return WarmupWaitResult("skipped")
        result = _wait_ollama_warmup(llm_client, timeout=settings.warmup_timeout)
        trail.append(f"ollama_wait_{result.status}")
        if result.status == "timeout":
            log.warning(
                "Ollama 预热等待超时，本轮跳过 RVC 预热以防并行抢 GPU status=%s",
                result.detail or result.status,
            )
        elif result.status == "failed":
            log.warning(
                "Ollama 预热等待失败，本轮跳过 RVC 预热 err=%s",
                result.detail or result.status,
            )
        return result

    def do_rvc(*, allowed: bool) -> None:
        if not allowed:
            trail.append("rvc_skipped_serialize")
            return
        ok = prepare_rvc_if_needed(tts_engine, settings=settings, log=log)
        trail.append("rvc_ok" if ok else "rvc_skip")

    need_rvc = (
        settings.rvc_warmup_on_voice_start
        and tts_engine is not None
        and callable(getattr(tts_engine, "prepare_rvc", None))
    )
    serialize = settings.serialize_gpu_warmup and need_rvc

    if settings.warmup_order == "rvc_then_ollama":
        do_rvc(allowed=True)
        do_ollama(wait=False)
    else:
        wait_result = do_ollama(wait=serialize)
        allow_rvc = (not serialize) or wait_result.ok
        do_rvc(allowed=allow_rvc)


def release_owned_ollama_model(
    llm_client: Any,
    *,
    settings: ResourceSettings,
    log: Optional[logging.Logger] = None,
) -> bool:
    """
    Best-effort：对配置中的模型发送 ``keep_alive=0``。

    不声称独占所有权；可能影响同名模型的其他本地客户端。
    仅在显式 ``unload_on_exit`` 且本客户端曾成功触达模型时发出请求。
    """
    log = log or logger
    if not settings.ollama_unload_on_exit or llm_client is None:
        return False
    touched = getattr(llm_client, "model_touch_succeeded", None)
    if callable(touched):
        if not bool(touched()):
            log.info("本客户端未曾成功触达模型，跳过 keep_alive=0 卸载请求")
            return False
    elif hasattr(llm_client, "_model_touch_succeeded"):
        if not bool(getattr(llm_client, "_model_touch_succeeded")):
            log.info("本客户端未曾成功触达模型，跳过 keep_alive=0 卸载请求")
            return False
    release = getattr(llm_client, "release_owned_model", None)
    if not callable(release):
        log.warning("LLM 客户端不支持 release_owned_model，跳过退出卸载")
        return False
    try:
        ok = bool(release())
        log.info(
            "已发出对共享 Ollama 指定模型的 best-effort keep_alive=0 请求 ok=%s "
            "（可能影响同名模型其他客户端；未停止 Ollama 服务）",
            ok,
        )
        return ok
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.warning(
            "退出时 keep_alive=0 卸载请求失败 err=%s",
            exception_log_label(exc),
        )
        return False
