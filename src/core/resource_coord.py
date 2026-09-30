"""按模式串行协调 Ollama / RVC 预热，以及退出时 best-effort 模型卸载请求。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Literal, Optional

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
    """
    wait = getattr(llm_client, "wait_for_warmup", None)
    try:
        if callable(wait):
            finished = wait(timeout=timeout)
            if finished:
                return WarmupWaitResult("completed")
            return WarmupWaitResult("timeout", "wait_for_warmup returned False")
        done = getattr(llm_client, "_warmup_done", None)
        if done is not None and hasattr(done, "wait"):
            finished = done.wait(timeout=max(1.0, float(timeout)) + 5.0)
            if finished:
                return WarmupWaitResult("completed")
            return WarmupWaitResult("timeout", "_warmup_done.wait timed out")
        return WarmupWaitResult("skipped", "no wait API")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        return WarmupWaitResult("failed", f"{type(exc).__name__}: {exc}")


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
        log.warning("Ollama 模型后台预热启动失败（正式请求仍可继续）: %s", exc)
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
        log.warning("RVC 预热失败（正式播报仍可继续）: %s", exc)
        return False


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
                "Ollama 预热等待超时，本轮跳过 RVC 预热以防并行抢 GPU detail=%s",
                result.detail,
            )
        elif result.status == "failed":
            log.warning(
                "Ollama 预热等待失败，本轮跳过 RVC 预热 detail=%s",
                result.detail,
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
        log.warning("退出时 keep_alive=0 卸载请求失败: %s", exc)
        return False
