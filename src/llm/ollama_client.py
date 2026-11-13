"""
Ollama 客户端模块。

通过 HTTP API 调用本地 Ollama 服务的 ``/api/chat`` 与 ``/api/tags``。
会话历史由 ``ConversationHistory`` 管理；本类负责请求协议与连通性探测。
结构化整段结果见 ``generate_result``；``generate`` 为兼容字符串转换层。
增量接口见 ``generate_stream``；语音与文本会话可通过助手的流式编排入口使用。

``/api/chat`` 的动态 system 必须作为 ``messages`` 首条 ``role=system`` 发送；
不得使用 ``/api/generate`` 的顶层 ``system`` 字段（会被 Chat API 静默忽略）。
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests

from src.core.network_endpoint import (
    SealedModelEndpoint,
    authorize_model_endpoint,
    best_effort_close_response,
    enforce_no_redirect,
    reseal_model_endpoint,
)
from src.core.log_privacy import exception_log_label
from src.core.companion import compose_system_prompt, normalize_base_system
from src.core.model_memory_admission import (
    LocalModelMemoryAdmission,
    ModelMemoryAdmissionError,
    payload_exempt_from_model_admission,
)
from src.core.owned_thread import OwnedThreadTask
from src.core.resource_settings import (
    DEFAULT_FOREGROUND_WARMUP_WAIT_SEC,
    MAX_FOREGROUND_WARMUP_WAIT_SEC,
)
from src.llm.conversation_history import (
    DEFAULT_MAX_STORED_TURNS,
    ConversationHistory,
    require_max_stored_turns,
)
from src.llm.generate_result import GenerateResult, GenerateStatus
from src.llm.stream_events import StreamEvent


@dataclass(frozen=True)
class OllamaWarmupSnapshot:
    """只读后台预热代际快照；不含请求正文或模型回复。"""

    generation: int
    finished: bool
    succeeded: bool


class _WarmupPostAborted(Exception):
    """准入通过后、POST 前因 ``close_warmup`` 取消暖机加载。"""


class OllamaClient:
    """
    Ollama REST API 客户端。

    封装 chat 生成、连接/模型可用性探测；多轮上下文委托给
    ``ConversationHistory``（请求侧附带最近至多 10 条历史消息 + 当前用户输入）。
    非空 ``system`` 仅作为当前请求 ``messages[0]``，不写入短期历史。
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "Miku_Assistant:latest",
        timeout: int = 60,
        history: Optional[ConversationHistory] = None,
        keep_alive: Any = None,
        max_stored_turns: Optional[int] = None,
        *,
        safety_policy: Optional[Any] = None,
        sealed_endpoint: Optional[SealedModelEndpoint] = None,
        foreground_warmup_wait_sec: Optional[float] = None,
        base_system: Optional[str] = None,
        model_memory_guard: Optional[LocalModelMemoryAdmission] = None,
    ):
        """
        初始化 Ollama 客户端。

        Args:
            base_url: Ollama 服务根 URL（不含尾部斜杠会自动去除）。
            model: 默认使用的模型名称（可含 ``:tag``）。
            timeout: HTTP 请求超时时间（秒）。
            history: 可选会话历史；默认新建独立实例。显式注入时不改写该对象。
            keep_alive: 可选 Ollama 模型驻留时长，例如 ``"30m"``；``None``
                保持服务端默认值。
            max_stored_turns: 仅在未注入 ``history`` 时生效；默认
                ``DEFAULT_MAX_STORED_TURNS``。
            safety_policy: 装配共享策略；省略时仅允许 loopback 端点。
            sealed_endpoint: 已授权封存端点；传入时仍会按 ``base_url`` 重新分类
                并授权，绝不信任调用方声称的 ``kind``。
            foreground_warmup_wait_sec: 正式请求等待后台预热的短上限（秒）；
                与资源协调 ``resource.warmup.timeout`` 分离；默认 5，硬上限 10。
            base_system: 可选基础人格 system；在 ``_build_chat_messages`` 与
                调用方动态 system 合成恰好一条 ``role=system``；``None``/空保持兼容。
            model_memory_guard: 可选共享本地模型内存准入；``None`` 保持测试/库兼容。
        """
        if sealed_endpoint is not None:
            self._endpoint = reseal_model_endpoint(
                safety_policy, sealed_endpoint, context="ollama_client"
            )
        else:
            self._endpoint = authorize_model_endpoint(
                safety_policy, base_url, context="ollama_client"
            )
        self.base_url = self._endpoint.base_url
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.safety_policy = safety_policy
        self.logger = logging.getLogger(__name__)
        if history is not None:
            self._history = history
        else:
            turns = (
                DEFAULT_MAX_STORED_TURNS
                if max_stored_turns is None
                else require_max_stored_turns(
                    "max_stored_turns", max_stored_turns
                )
            )
            self._history = ConversationHistory(max_stored_turns=turns)
        self._warmup_lock = threading.Lock()
        self._warmup_state_cv = threading.Condition(self._warmup_lock)
        self._warmup_done = threading.Event()
        self._warmup_owner: Optional[OwnedThreadTask] = None
        self._warmup_ok: Optional[bool] = None
        self._warmup_active_generation = 0
        self._warmup_closing = False
        self._warmup_start_pending = False
        # 资源协调 wait_for_warmup(timeout=None) 的回退；不用于正式请求前台等待
        self._resource_warmup_wait_timeout = max(1.0, float(timeout)) + 5.0
        self._foreground_warmup_wait_sec = self._normalize_foreground_warmup_wait(
            foreground_warmup_wait_sec
        )
        self._base_system = normalize_base_system(base_system)
        self._model_touch_succeeded = False
        self._model_memory_guard = model_memory_guard

    def _enforce_model_post_admission(
        self, payload: Dict[str, Any], *, call_timeout: Optional[float] = None
    ) -> None:
        guard = self._model_memory_guard
        if guard is None or payload_exempt_from_model_admission(payload):
            return
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            model = self.model
        effective = (
            float(self.timeout)
            if call_timeout is None
            else float(call_timeout)
        )
        guard.require_model_post(
            model,
            call_timeout=effective,
            sealed_endpoint=self._endpoint,
        )

    @staticmethod
    def _resource_failure_detail(exc: ModelMemoryAdmissionError) -> str:
        return exc.user_message

    @staticmethod
    def _normalize_foreground_warmup_wait(value: Optional[float]) -> float:
        if value is None:
            return float(DEFAULT_FOREGROUND_WARMUP_WAIT_SEC)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                "foreground_warmup_wait_sec must be a finite number, "
                f"got {type(value).__name__}"
            )
        number = float(value)
        if (
            not math.isfinite(number)
            or number <= 0.0
            or number > float(MAX_FOREGROUND_WARMUP_WAIT_SEC)
        ):
            raise ValueError(
                "foreground_warmup_wait_sec must be a finite number in "
                f"(0, {MAX_FOREGROUND_WARMUP_WAIT_SEC:g}]"
            )
        return number

    @property
    def endpoint_kind(self) -> str:
        return self._endpoint.kind.value

    def _chat_url(self) -> str:
        return self._endpoint.api_url("/api/chat")

    def _tags_url(self) -> str:
        return self._endpoint.api_url("/api/tags")

    def _request_post(self, url: str, **kwargs: Any) -> Any:
        """所有 POST 禁止自动 redirect；3xx 先 close 再拒绝。chat 加载统一经内存准入。"""
        kwargs.setdefault("allow_redirects", False)
        if url == self._chat_url():
            payload = kwargs.get("json")
            if isinstance(payload, dict):
                raw_timeout = kwargs.get("timeout", self.timeout)
                effective = (
                    float(self.timeout)
                    if raw_timeout is None
                    else float(raw_timeout)
                )
                if not payload_exempt_from_model_admission(payload):
                    self._enforce_model_post_admission(
                        payload, call_timeout=effective
                    )
                    if payload.get("messages") == []:
                        with self._warmup_lock:
                            if self._warmup_closing:
                                raise _WarmupPostAborted()
        post = getattr(self, "_http_post", None) or requests.post
        response = post(url, **kwargs)
        enforce_no_redirect(response)
        return response

    def _request_get(self, url: str, **kwargs: Any) -> Any:
        """所有 GET 禁止自动 redirect；3xx 先 close 再拒绝。不吞 TypeError 重试。"""
        kwargs.setdefault("allow_redirects", False)
        get = getattr(self, "_http_get", None) or requests.get
        response = get(url, **kwargs)
        enforce_no_redirect(response)
        return response

    def _mark_model_touched(self) -> None:
        """记录本客户端曾成功触达模型（预热或正式请求）。"""
        self._model_touch_succeeded = True

    def model_touch_succeeded(self) -> bool:
        return bool(self._model_touch_succeeded)

    def _apply_runtime_options(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """把不改变对话内容的 Ollama 运行参数统一加入请求。"""
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        return payload

    def _build_chat_messages(
        self, prompt: str, system: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """
        构造 ``/api/chat`` 的 ``messages``：可选 system → 历史窗口 → 当前 user。

        - 将客户端 ``base_system`` 与调用方动态 ``system`` 合成**恰好一条**
          ``role=system``（基础人格在前，动态可信上下文在后）。
        - ``None`` 或空字符串不参与合成；两者皆无则不插入 system。
        - 返回独立列表/字典副本；不修改 ``ConversationHistory`` 内部记录。
        - 流式与非流式路径必须共用本边界，避免协议漂移。
        """
        messages = self._history.build_messages(prompt)
        composed = compose_system_prompt(self._base_system, system)
        if composed:
            messages.insert(0, {"role": "system", "content": composed})
        return messages

    def warm_up(self, *, timeout: Optional[float] = None) -> bool:
        """发送空 chat 请求预载模型；不读取或修改会话历史。"""
        with self._warmup_lock:
            if self._warmup_closing:
                return False
        url = self._chat_url()
        payload = self._apply_runtime_options(
            {"model": self.model, "messages": [], "stream": False}
        )
        effective_timeout = (
            float(self.timeout) if timeout is None else float(timeout)
        )
        started = time.perf_counter()
        response: Optional[requests.Response] = None
        try:
            response = self._request_post(
                url,
                json=payload,
                timeout=effective_timeout,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("done") is not True:
                raise ValueError("Ollama 预热响应缺少 done=true")
            self._mark_model_touched()
            self._log_response_metrics(data, stage="预热")
            self.logger.info(
                "[Ollama计时] 预热请求完成: %.0f ms",
                (time.perf_counter() - started) * 1000,
            )
            return True
        except _WarmupPostAborted:
            return False
        except ModelMemoryAdmissionError as exc:
            self.logger.warning(
                "Ollama 模型预热因资源准入被拒绝 reason=%s",
                exc.reason,
            )
            return False
        except Exception as exc:
            # 预热是性能优化；失败不影响稍后的正式请求与原错误契约。
            self.logger.warning(
                "Ollama 模型预热失败（正式请求仍可继续） err=%s",
                exception_log_label(exc),
            )
            return False
        finally:
            best_effort_close_response(response)

    def _warmup_lifecycle_busy_locked(self) -> bool:
        """调用方须已持有 ``_warmup_lock``。"""
        if self._warmup_start_pending:
            return True
        owner = self._warmup_owner
        if owner is None:
            return False
        snap = owner.snapshot()
        if snap.creator_pending:
            return True
        return not snap.quiet

    def _make_warmup_runner(
        self, generation: int, effective_timeout: float
    ) -> Callable[[threading.Event], None]:
        def _run(cancel: threading.Event) -> None:
            ok = False
            cancelled = False
            closing = False
            try:
                if cancel.is_set():
                    cancelled = True
                    return
                with self._warmup_lock:
                    closing = self._warmup_closing
                if closing:
                    return
                ok = self.warm_up(timeout=effective_timeout)
                if cancel.is_set():
                    cancelled = True
                    ok = False
                else:
                    with self._warmup_lock:
                        if self._warmup_closing:
                            closing = True
                            ok = False
            finally:
                with self._warmup_lock:
                    if generation != self._warmup_active_generation:
                        pass
                    else:
                        owner = self._warmup_owner
                        if owner is not None and owner.error is not None:
                            self._warmup_ok = False
                        elif cancelled or closing or self._warmup_closing:
                            self._warmup_ok = False
                        else:
                            self._warmup_ok = ok
                        self._warmup_done.set()
                        self._notify_warmup_waiters_locked()

        return _run

    def _notify_warmup_waiters_locked(self) -> None:
        """调用方须已持有 ``_warmup_lock``。"""
        self._warmup_state_cv.notify_all()

    def _prepare_new_warmup_generation_locked(
        self, effective_timeout: float
    ) -> tuple[OwnedThreadTask, int]:
        """在已持有 ``_warmup_lock`` 时发布新 owner（尚未 ``start``）。"""
        generation = self._warmup_active_generation + 1
        owner = OwnedThreadTask(
            self._make_warmup_runner(generation, effective_timeout),
            name="OllamaWarmup",
        )
        self._warmup_active_generation = generation
        self._warmup_done.clear()
        self._warmup_ok = None
        self._warmup_start_pending = True
        self._warmup_owner = owner
        return owner, generation

    def _start_published_warmup_owner(self, owner: OwnedThreadTask) -> bool:
        """``Thread.start`` 须在 warmup 锁外；``finally`` 清除 start_pending。"""
        ack = False
        try:
            ack = owner.start()
        finally:
            with self._warmup_lock:
                self._warmup_start_pending = False
                if self._warmup_closing and self._warmup_owner is owner:
                    owner.cancel()
                self._notify_warmup_waiters_locked()
        return ack

    def _warmup_work_finished_locked(self) -> bool:
        """调用方须已持有 ``_warmup_lock``；预热工作任务是否已结束。"""
        if self._warmup_start_pending:
            return False
        owner = self._warmup_owner
        if owner is None:
            return False
        snap = owner.snapshot()
        if snap.creator_pending or not snap.quiet:
            return False
        return self._warmup_done.is_set()

    def _warmup_snapshot_locked(self) -> OllamaWarmupSnapshot:
        """调用方须已持有 ``_warmup_lock``。"""
        owner = self._warmup_owner
        generation = self._warmup_active_generation
        finished = self._warmup_work_finished_locked()
        succeeded = (
            finished
            and bool(self._warmup_ok)
            and not self._warmup_closing
            and (owner is None or owner.error is None)
        )
        return OllamaWarmupSnapshot(
            generation=generation,
            finished=finished,
            succeeded=succeeded,
        )

    def warmup_snapshot(self) -> OllamaWarmupSnapshot:
        """返回当前预热代际的不可变快照。"""
        with self._warmup_lock:
            return self._warmup_snapshot_locked()

    def close_warmup(self, timeout: float = 2.0) -> bool:
        """
        永久结束本客户端的后台预热能力；有界等待 owner quiet。

        严格 ``True`` 表示无 pending creator/start 且 owner 已 quiet；``False`` 可重试。
        不卸载模型、不停止 Ollama 服务。
        """
        with self._warmup_lock:
            self._warmup_closing = True
            owner = self._warmup_owner
        if owner is not None:
            owner.cancel()
        if owner is None:
            with self._warmup_lock:
                if self._warmup_start_pending:
                    return False
            return True
        if not owner.join(timeout=timeout):
            return False
        with self._warmup_lock:
            if self._warmup_start_pending:
                return False
            if self._warmup_owner is not owner:
                return False
            snap = owner.snapshot()
            if snap.creator_pending:
                return False
            return snap.quiet

    def begin_fresh_warmup(self, *, timeout: Optional[float] = None) -> int:
        """
        原子开启或复用当前进行中的预热代际，并返回其 generation。

        与 ``start_warmup`` 分离，避免先返回 bool 再读快照产生竞态。
        """
        with self._warmup_lock:
            if self._warmup_closing:
                return 0
            if self._warmup_lifecycle_busy_locked():
                return self._warmup_active_generation
            if self._warmup_done.is_set():
                self._warmup_done.clear()
            effective_timeout = self.timeout if timeout is None else timeout
            self._resource_warmup_wait_timeout = (
                max(1.0, float(effective_timeout)) + 5.0
            )
            owner, generation = self._prepare_new_warmup_generation_locked(
                float(effective_timeout)
            )
        if not self._start_published_warmup_owner(owner):
            return 0
        return generation

    def start_warmup(
        self,
        *,
        timeout: Optional[float] = None,
        restart: bool = False,
    ) -> bool:
        """幂等启动后台预热；``restart=True`` 时仅在旧 owner quiet 后开启新代际。"""
        with self._warmup_lock:
            if self._warmup_closing:
                return False
            if self._warmup_lifecycle_busy_locked():
                return False
            if not restart:
                if self._warmup_done.is_set():
                    return False
                prior = self._warmup_owner
                if prior is not None:
                    snap = prior.snapshot()
                    if not snap.quiet:
                        return False
                    if snap.start_unknown or snap.alive or snap.entered:
                        return False
            elif self._warmup_done.is_set():
                self._warmup_done.clear()
                self._warmup_ok = None

            effective_timeout = self.timeout if timeout is None else timeout
            self._resource_warmup_wait_timeout = (
                max(1.0, float(effective_timeout)) + 5.0
            )
            owner, _generation = self._prepare_new_warmup_generation_locked(
                float(effective_timeout)
            )
        return self._start_published_warmup_owner(owner)

    def wait_for_warmup(self, *, timeout: Optional[float] = None) -> bool:
        """有界等待后台预热 owner quiet；供资源协调器串行 RVC 前调用。"""
        wait_timeout = (
            self._resource_warmup_wait_timeout
            if timeout is None
            else max(0.0, float(timeout))
        )
        deadline = time.monotonic() + wait_timeout
        while True:
            with self._warmup_lock:
                owner = self._warmup_owner
                if owner is None and not self._warmup_done.is_set():
                    return True
                if self._warmup_work_finished_locked():
                    return True
                if (
                    owner is not None
                    and not self._warmup_start_pending
                ):
                    snap = owner.snapshot()
                    if (
                        snap.quiet
                        and not snap.creator_pending
                        and not self._warmup_done.is_set()
                    ):
                        return True
                pending = self._warmup_start_pending
                join_owner = None if pending else owner
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                if pending or join_owner is None:
                    self._warmup_state_cv.wait(timeout=min(0.05, remaining))
                    continue
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            snap = join_owner.snapshot()
            if snap.quiet and not snap.creator_pending:
                with self._warmup_lock:
                    if self._warmup_work_finished_locked():
                        return True
                    if (
                        not self._warmup_done.is_set()
                        and join_owner is self._warmup_owner
                        and join_owner.snapshot().quiet
                    ):
                        return True
                continue
            if not join_owner.join(timeout=min(0.1, remaining)):
                with self._warmup_lock:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        return False
                    self._warmup_state_cv.wait(timeout=min(0.05, remaining))
                continue

    def release_owned_model(self) -> bool:
        """
        Best-effort：对共享 Ollama 中的指定模型发送 ``keep_alive=0``。

        可能影响同名模型的其他本地客户端；不停止 Ollama 服务。
        若本客户端从未成功触达模型，则不发 HTTP。
        """
        if not self._model_touch_succeeded:
            self.logger.info(
                "跳过 keep_alive=0：本客户端未曾成功触达模型"
            )
            return False
        url = self._chat_url()
        payload = {
            "model": self.model,
            "messages": [],
            "stream": False,
            "keep_alive": 0,
        }
        started = time.perf_counter()
        response: Optional[requests.Response] = None
        try:
            response = self._request_post(
                url, json=payload, timeout=min(30.0, float(self.timeout))
            )
            response.raise_for_status()
            self.logger.info(
                "[Ollama计时] best-effort keep_alive=0: %.0f ms "
                "（共享模型；可能影响其他客户端；未停止服务）",
                (time.perf_counter() - started) * 1000,
            )
            return True
        except Exception as exc:
            self.logger.warning(
                "keep_alive=0 卸载请求失败 err=%s", exception_log_label(exc)
            )
            return False
        finally:
            best_effort_close_response(response)

    def _wait_for_warmup(self) -> None:
        """
        正式请求前短等待后台预热（独立于资源协调 timeout）。

        超时后允许正式请求继续；可能短暂与尚未返回的空预热 HTTP 重叠，
        但不新建模型进程，由 Ollama 服务自行串行同模型请求。
        """
        with self._warmup_lock:
            if self._warmup_closing or self._warmup_work_finished_locked():
                return
            owner = self._warmup_owner
        if owner is None:
            return
        started = time.perf_counter()
        budget = self._foreground_warmup_wait_sec
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            with self._warmup_lock:
                if self._warmup_work_finished_locked():
                    break
                pending = self._warmup_start_pending
            if not pending:
                break
            time.sleep(min(0.02, deadline - time.monotonic()))
        remaining = max(0.0, deadline - time.monotonic())
        quiet = owner.join(timeout=remaining)
        status = "ready" if quiet else "timeout"
        if not quiet:
            self.logger.warning(
                "Ollama 后台预热等待超时，正式请求将独立继续 status=%s",
                status,
            )
        self.logger.info(
            "[Ollama计时] 等待后台预热: %.0f ms status=%s",
            (time.perf_counter() - started) * 1000,
            status,
        )

    @property
    def conversation_history(self) -> List[Dict[str, str]]:
        """
        兼容旧公开属性：返回可修改的历史副本。

        对返回列表或其中字典的原地修改**不会**影响内部记录。
        清空请使用 ``clear_history()``。
        默认构造的客户端历史相互隔离；显式注入同一 ``ConversationHistory``
        时会共享，``clear_history()`` 也会影响其他共享者。
        仓库检索未发现调用方依赖可变内部列表语义；兼容策略为返回副本而非静默保留可变引用。
        """
        return self._history.snapshot()

    def generate(
        self, prompt: str, stream: bool = False, system: Optional[str] = None
    ) -> str:
        """
        兼容接口：委托 ``generate_result``，再转为既有字符串约定。

        Returns:
            成功为模型原文；空回复为 ``\"\"``；失败为以 ``错误:`` 开头的提示。
            连接/超时/HTTP 仍为 ``错误: 无法连接到Ollama服务 (…)``；
            含 ``requests.exceptions.JSONDecodeError`` 在内的响应类失败为
            ``错误: {detail}``（相对第六小步前把 JSONDecodeError 误归入
            RequestException 连接文案的行为有变化，见 ADR）。
        """
        return self.generate_result(prompt, stream=stream, system=system).to_legacy_string()

    def generate_result(
        self, prompt: str, stream: bool = False, system: Optional[str] = None
    ) -> GenerateResult:
        """
        调用 chat API，返回结构化结果（自动附带最近对话历史）。

        仅 ``SUCCESS``（非空文本）时提交完整一轮历史。失败与空回复不修改历史。
        不根据文本是否以「错误:」开头判断成败。
        """
        url = self._chat_url()
        self._wait_for_warmup()
        messages = self._build_chat_messages(prompt, system=system)

        payload = self._apply_runtime_options({
            "model": self.model,
            "messages": messages,
            "stream": stream,
        })

        response: Optional[requests.Response] = None
        try:
            response = self._request_post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            self._mark_model_touched()

            if stream:
                text = self._aggregate_stream(response)
            else:
                data = response.json()
                self._log_response_metrics(data, stage="非流式")
                text = self._extract_non_stream_text(data)

            if text:
                result = GenerateResult.success(text)
                self._history.commit_turn(prompt, text)
                self.logger.debug(
                    "对话历史已更新，当前有 %s 条消息", len(self._history)
                )
                return result
            return GenerateResult.empty()

        except ModelMemoryAdmissionError as exc:
            self.logger.warning(
                "Ollama 生成因资源准入被拒绝 reason=%s", exc.reason
            )
            return GenerateResult.failure(
                GenerateStatus.RESOURCE_ERROR,
                self._resource_failure_detail(exc),
            )
        except PermissionError as exc:
            self.logger.error(
                "Ollama API请求失败 err=%s", exception_log_label(exc)
            )
            return GenerateResult.failure(
                GenerateStatus.HTTP_ERROR,
                self._exception_detail(exc, "HTTP redirect 被拒绝"),
            )
        except requests.exceptions.Timeout as exc:
            self.logger.error(
                "Ollama API请求失败 err=%s", exception_log_label(exc)
            )
            return GenerateResult.failure(
                GenerateStatus.TIMEOUT,
                self._exception_detail(exc, "请求超时"),
            )
        except requests.exceptions.HTTPError as exc:
            self.logger.error(
                "Ollama API请求失败 err=%s", exception_log_label(exc)
            )
            return GenerateResult.failure(
                GenerateStatus.HTTP_ERROR,
                self._exception_detail(exc, "HTTP 错误"),
            )
        except requests.exceptions.JSONDecodeError as exc:
            # 必须在宽泛 RequestException 之前：该类同时是 RequestException 子类
            self.logger.error(
                "生成回复时出错 err=%s", exception_log_label(exc)
            )
            return GenerateResult.failure(
                GenerateStatus.RESPONSE_ERROR,
                self._exception_detail(exc, "响应 JSON 解析失败"),
            )
        except requests.exceptions.RequestException as exc:
            self.logger.error(
                "Ollama API请求失败 err=%s", exception_log_label(exc)
            )
            return GenerateResult.failure(
                GenerateStatus.CONNECTION_ERROR,
                self._exception_detail(exc, "连接失败"),
            )
        except Exception as exc:
            self.logger.error(
                "生成回复时出错 err=%s", exception_log_label(exc)
            )
            return GenerateResult.failure(
                GenerateStatus.RESPONSE_ERROR,
                self._exception_detail(exc, "响应处理失败"),
            )
        finally:
            best_effort_close_response(response)

    def generate_stream(
        self, prompt: str, system: Optional[str] = None
    ) -> Iterator[StreamEvent]:
        """
        增量生成：真正逐段读取 NDJSON，产出 ``StreamEvent``。

        - ``TEXT_DELTA``：非空 ``message.content`` 增量（非终结）。
        - ``COMPLETED``：收到官方 ``done: true`` 后的成功终结；仅此时若全文非空
          才 ``commit_turn`` 一次。
        - ``FAILED``：缺完成标记、解析/结构错误、连接中断等；不提交历史。

        正常消费至结束恰有一个终结事件，之后不再产出。提前 ``close`` 迭代器或
        异常路径会关闭 HTTP 响应且不提交未完成轮次。

        不改变 ``generate`` / ``generate_result`` 聚合路径。
        """
        return self._iter_chat_stream(prompt, system=system)

    def _iter_chat_stream(
        self, prompt: str, system: Optional[str] = None
    ) -> Iterator[StreamEvent]:
        url = self._chat_url()
        self._wait_for_warmup()
        messages = self._build_chat_messages(prompt, system=system)
        payload: Dict[str, Any] = self._apply_runtime_options({
            "model": self.model,
            "messages": messages,
            "stream": True,
        })

        response: Optional[requests.Response] = None
        terminal_emitted = False
        assembled: List[str] = []
        committed = False

        def _fail(status: GenerateStatus, detail: str) -> StreamEvent:
            nonlocal terminal_emitted
            terminal_emitted = True
            return StreamEvent.failed(status, detail)

        def _complete() -> StreamEvent:
            nonlocal terminal_emitted, committed
            terminal_emitted = True
            full = "".join(assembled)
            if full and not committed:
                self._history.commit_turn(prompt, full)
                committed = True
                self.logger.debug(
                    "流式对话历史已更新，当前有 %s 条消息", len(self._history)
                )
            return StreamEvent.completed(full)

        try:
            try:
                response = self._request_post(
                    url, json=payload, timeout=self.timeout, stream=True
                )
                response.raise_for_status()
                self._mark_model_touched()
            except ModelMemoryAdmissionError as exc:
                yield _fail(
                    GenerateStatus.RESOURCE_ERROR,
                    self._resource_failure_detail(exc),
                )
                return
            except PermissionError as exc:
                yield _fail(
                    GenerateStatus.HTTP_ERROR,
                    self._exception_detail(exc, "HTTP redirect 被拒绝"),
                )
                return
            except requests.exceptions.Timeout as exc:
                yield _fail(
                    GenerateStatus.TIMEOUT,
                    self._exception_detail(exc, "请求超时"),
                )
                return
            except requests.exceptions.HTTPError as exc:
                yield _fail(
                    GenerateStatus.HTTP_ERROR,
                    self._exception_detail(exc, "HTTP 错误"),
                )
                return
            except requests.exceptions.RequestException as exc:
                yield _fail(
                    GenerateStatus.CONNECTION_ERROR,
                    self._exception_detail(exc, "连接失败"),
                )
                return

            try:
                for line in response.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as exc:
                        yield _fail(
                            GenerateStatus.RESPONSE_ERROR,
                            self._exception_detail(exc, "流式 JSON 解析失败"),
                        )
                        return

                    if not isinstance(data, dict):
                        yield _fail(
                            GenerateStatus.RESPONSE_ERROR,
                            f"流式行必须为对象，实际为 {type(data).__name__}",
                        )
                        return

                    # 部分 Ollama 错误以 {"error": "..."} 行返回
                    if "error" in data and data["error"]:
                        yield _fail(
                            GenerateStatus.RESPONSE_ERROR,
                            str(data["error"]),
                        )
                        return

                    # 交付本行增量前先校验 done（若存在必须为 bool）
                    if "done" in data:
                        done_val = data["done"]
                        if not isinstance(done_val, bool):
                            yield _fail(
                                GenerateStatus.RESPONSE_ERROR,
                                "done 必须为 bool，实际为 "
                                f"{type(done_val).__name__}",
                            )
                            return

                    if "message" in data:
                        message = data["message"]
                        if not isinstance(message, dict):
                            yield _fail(
                                GenerateStatus.RESPONSE_ERROR,
                                "message 必须为对象，实际为 "
                                f"{type(message).__name__}",
                            )
                            return
                        if "content" in message and message["content"] is not None:
                            content = message["content"]
                            if not isinstance(content, str):
                                yield _fail(
                                    GenerateStatus.RESPONSE_ERROR,
                                    "message.content 必须为 str，实际为 "
                                    f"{type(content).__name__}",
                                )
                                return
                            if content:
                                assembled.append(content)
                                yield StreamEvent.text_delta(content)

                    # 仅 done is True 完成；False 或缺失则继续
                    if data.get("done") is True:
                        self._log_response_metrics(data, stage="流式")
                        yield _complete()
                        return

                # 迭代正常结束但从未 done:true
                yield _fail(
                    GenerateStatus.RESPONSE_ERROR,
                    "流式响应缺少完成标记 done=true",
                )
            except requests.exceptions.Timeout as exc:
                if not terminal_emitted:
                    yield _fail(
                        GenerateStatus.TIMEOUT,
                        self._exception_detail(exc, "请求超时"),
                    )
            except requests.exceptions.RequestException as exc:
                if not terminal_emitted:
                    yield _fail(
                        GenerateStatus.CONNECTION_ERROR,
                        self._exception_detail(exc, "连接失败"),
                    )
            except GeneratorExit:
                # 消费方提前关闭：不提交、不补终结事件
                raise
            except Exception as exc:
                if not terminal_emitted:
                    yield _fail(
                        GenerateStatus.RESPONSE_ERROR,
                        self._exception_detail(exc, "流式响应处理失败"),
                    )
        finally:
            best_effort_close_response(response)

    @staticmethod
    def _exception_detail(exc: BaseException, fallback: str) -> str:
        """异常说明为空时使用稳定非空兜底，避免 failure 构造再次失败。"""
        detail = str(exc).strip()
        return detail if detail else fallback

    def _log_response_metrics(self, data: Any, *, stage: str) -> None:
        """记录 Ollama 终结对象中的阶段耗时；缺失/畸形字段只跳过。"""
        if not isinstance(data, dict):
            return

        def _ms(name: str) -> Optional[float]:
            value = data.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value) / 1_000_000.0
            return None

        total_ms = _ms("total_duration")
        load_ms = _ms("load_duration")
        prompt_ms = _ms("prompt_eval_duration")
        eval_ms = _ms("eval_duration")
        eval_count = data.get("eval_count")
        token_rate: Optional[float] = None
        if (
            isinstance(eval_count, (int, float))
            and not isinstance(eval_count, bool)
            and eval_ms is not None
            and eval_ms > 0
        ):
            token_rate = float(eval_count) / (eval_ms / 1000.0)
        self.logger.info(
            "[Ollama计时] %s total_ms=%s load_ms=%s prompt_ms=%s "
            "eval_ms=%s eval_tokens=%s tokens_per_s=%s",
            stage,
            _format_metric(total_ms),
            _format_metric(load_ms),
            _format_metric(prompt_ms),
            _format_metric(eval_ms),
            eval_count
            if isinstance(eval_count, (int, float))
            and not isinstance(eval_count, bool)
            else "n/a",
            _format_metric(token_rate),
        )

    @staticmethod
    def _extract_non_stream_text(data: Any) -> str:
        """
        从非流式 JSON 取出助手正文；仅接受 str。

        缺失 content 或 content 为 None 视为空字符串（EMPTY）。
        message 非 dict、content 为 list/number 等则抛错 → RESPONSE_ERROR。
        """
        if not isinstance(data, dict):
            raise TypeError(
                f"chat 响应必须为对象，实际为 {type(data).__name__}"
            )
        message = data.get("message", {})
        if message is None:
            return ""
        if not isinstance(message, dict):
            raise TypeError(
                f"message 必须为对象，实际为 {type(message).__name__}"
            )
        if "content" not in message or message["content"] is None:
            return ""
        content = message["content"]
        if not isinstance(content, str):
            raise TypeError(
                f"message.content 必须为 str，实际为 {type(content).__name__}"
            )
        return content

    def _aggregate_stream(self, response: requests.Response) -> str:
        """按行聚合流式响应为完整字符串（非端到端增量契约）。"""
        result = ""
        for line in response.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            if not isinstance(data, dict):
                raise TypeError(
                    f"流式行必须为对象，实际为 {type(data).__name__}"
                )
            if "message" in data:
                message = data["message"]
                # 只要存在 message 就必须是对象；非 dict 不得静默跳过以免提交部分回复
                if not isinstance(message, dict):
                    raise TypeError(
                        f"message 必须为对象，实际为 {type(message).__name__}"
                    )
                if "content" in message:
                    content = message["content"]
                    if not isinstance(content, str):
                        raise TypeError(
                            "message.content 必须为 str，实际为 "
                            f"{type(content).__name__}"
                        )
                    result += content
            if data.get("done", False):
                self._log_response_metrics(data, stage="聚合流式")
                break
        return result

    def clear_history(self) -> None:
        """清空本客户端所持会话历史；不影响其他默认独立实例。"""
        self._history.clear()
        self.logger.info("对话历史已清空")

    def check_connection(self) -> bool:
        """
        探测 Ollama 服务是否可达。

        Returns:
            ``GET /api/tags`` 成功时为 ``True``。
        """
        response: Optional[requests.Response] = None
        try:
            url = self._tags_url()
            response = self._request_get(url, timeout=5)
            response.raise_for_status()
            return True
        except Exception:
            return False
        finally:
            best_effort_close_response(response)

    def check_model_available(self) -> bool:
        """
        检查当前配置的模型是否已在 Ollama 中安装。

        Returns:
            模型名完全匹配或基础名（``:`` 前）匹配时为 ``True``。
        """
        response: Optional[requests.Response] = None
        try:
            url = self._tags_url()
            response = self._request_get(url, timeout=5)
            response.raise_for_status()
            models = response.json().get("models", [])
            names = {m.get("name", "") for m in models}
            if self.model in names:
                return True
            # 支持 name 带或不带 :latest 后缀的模糊匹配
            base = self.model.split(":")[0]
            return any(n.split(":")[0] == base for n in names)
        except Exception as exc:
            self.logger.warning(
                "检查模型列表失败 err=%s", exception_log_label(exc)
            )
            return False
        finally:
            best_effort_close_response(response)

    def list_models(self) -> List[str]:
        """
        列出 Ollama 已安装的全部模型名称。

        Returns:
            模型名字符串列表；请求失败时返回空列表。
        """
        response: Optional[requests.Response] = None
        try:
            url = self._tags_url()
            response = self._request_get(url, timeout=5)
            response.raise_for_status()
            return [m.get("name", "") for m in response.json().get("models", [])]
        except Exception:
            return []
        finally:
            best_effort_close_response(response)


def _format_metric(value: Optional[float]) -> str:
    """将可选性能数值格式化为稳定日志字段。"""
    return "n/a" if value is None else f"{value:.1f}"
