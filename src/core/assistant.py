"""
Miku 助手主类模块。

业务编排（文本/语音输入、TTS、关闭）与默认依赖装配分离：
默认路径经 ``assemble_default``；测试/嵌入路径经 ``from_dependencies``。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from src.core.assembly import (
    AssembledParts,
    assemble_default,
    build_stt_engine,
    build_tts_engine,
    build_voice_engines,
)
from src.core.dispatch_outcome import DispatchOutcome, ReplyStage
from src.core.handlers.llm_handler import LLMIntentHandler
from src.core.log_privacy import exception_log_label
from src.core.perf_timing import SessionTimer
from src.core.resource_coord import release_owned_ollama_model
from src.core.resource_settings import ResourceSettings, parse_resource_settings
from src.core.router import IntentRouter
from src.core.router_factory import build_intent_router
from src.core.safety import SafetyPolicy
from src.core.sentence_splitter import IncrementalSentenceSplitter
from src.core.speech_queue import PlaybackStatus, TurnSpeechPlayer
from src.core.startup_mode import StartupMode, normalize_startup_mode
from src.llm.generate_result import GenerateResult, GenerateStatus
from src.llm.stream_events import StreamEventKind
from src.voice.recognition import RecognitionResult, RecognitionStatus
from src.voice.speech_projection import SpeechProjector, project_for_speech
from src.core.shutdown_status import (
    engine_declares_aggregate_shutdown_status,
    engine_declares_local_cosyvoice_shutdown_status,
    verify_aggregate_shutdown_status,
)
from src.core.voice_load_lane import ensure_voice_transaction, shutdown_retire_owner_in_lane
from src.core.voice_activation import (
    UnpublishedVoice,
    bound_rvc_explicit_retry_needed,
    bound_tts_models_ready,
    is_cosyvoice_tts,
    warmup_registered_tts_candidate,
)


@dataclass
class VoiceStackLoadResult:
    """首次进入语音时懒加载结果；失败为结构化报告，不抛裸异常。"""

    ok: bool = False
    pending: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = ["语音栈加载:"]
        if self.pending:
            lines.append("  状态: 准备中")
        else:
            lines.append(f"  状态: {'通过' if self.ok else '未通过'}")
        for err in self.errors:
            lines.append(f"  [错误] {err}")
        for warn in self.warnings:
            lines.append(f"  [警告] {warn}")
        return "\n".join(lines)


# single-flight 等待者的总 deadline（秒）；超时返回结构化失败，不重置循环
VOICE_LOAD_WAIT_SECONDS = 30.0


@dataclass
class _GenerationTurn:
    """单次输入的不可复用轮次身份；取消事件仅属本轮。"""

    turn_id: int
    cancel: threading.Event = field(default_factory=threading.Event)
    speech_turn_id: int = 0
    speech_projector: Any = field(default=None, repr=False)


class MikuAssistant:
    """
    Miku 桌面助手主类。

    默认 ``MikuAssistant(config_path=...)`` 行为与重构前一致（加载配置并创建引擎）。
    离线测试请使用 ``from_dependencies``，避免触发真实设备与服务初始化。

    生命周期（shutdown，行为修复：容错 + 顺序幂等）
        由助手关闭（若实例上非 ``None``），顺序：
            - ``tts_engine.shutdown()``
            - ``file_cleaner.stop_auto_cleanup()``；成功后再 ``cleanup_now()``
              （停止失败则跳过末次清理，仍关闭角色）
            - ``character_interface.close()``
        单步异常记录后继续；已完成步骤不重复执行，未实际完成的关闭步骤可在后续 ``shutdown()`` 中重试。
        全部成功时记「助手已关闭」；否则记错误级关闭摘要（含可再次 ``shutdown()`` 重试），不以成功日志掩盖失败。
        由调用方管理（助手不关闭）：
            - ``config``、``safety_policy``、``llm_client``、``stt_engine``、
              ``windows_controller``、``chrome_searcher``、``router``
        注入路径不会为缺失的可选组件自动创建真实实现；未注入的槽位保持 ``None``。
        播报中退出会请求停止播放器并回收 RVC Worker；第三方阻塞音频后端仍只能尽力关闭。
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        *,
        startup_mode: object = None,
        _parts: Optional[AssembledParts] = None,
    ):
        """
        创建助手实例。

        Args:
            config_path: 可选配置路径；默认装配时使用。与 ``_parts`` 互斥于外部用法。
            startup_mode: ``text`` / ``voice`` / ``interactive``；装配前可知。
                ``None`` 时默认 ``interactive``（与 CLI 一致）。
            _parts: 内部/工厂绑定用；外部请使用 ``from_dependencies``。

        Raises:
            Exception: 默认装配过程中核心模块初始化失败时向上抛出。
        """
        if _parts is not None:
            self._bind_parts(_parts)
            return

        try:
            mode = normalize_startup_mode(
                startup_mode, default="interactive"
            )
            parts = assemble_default(config_path, startup_mode=mode)
        except Exception as exc:
            logging.getLogger(__name__).error(
                "模块初始化失败 err=%s",
                exception_log_label(exc),
                exc_info=True,
            )
            raise
        self._bind_parts(parts)

    @classmethod
    def from_dependencies(
        cls,
        config: Any,
        *,
        safety_policy: Optional[SafetyPolicy] = None,
        llm_client: Any = None,
        tts_engine: Any = None,
        stt_engine: Any = None,
        windows_controller: Any = None,
        chrome_searcher: Any = None,
        character_interface: Any = None,
        file_cleaner: Any = None,
        router: Optional[IntentRouter] = None,
        log_file: Any = None,
        logger: Optional[logging.Logger] = None,
        memory_runtime: Any = None,
    ) -> "MikuAssistant":
        """
        显式注入依赖构造（无配置文件加载、无日志落盘、无目录创建、无引擎/清理器自动启动）。

        缺失的可选组件保持 ``None``，不会回退创建真实 ``TTSEngine`` / ``STTEngine`` 等。
        ``memory_runtime`` 可选注入；注入路径不得自动创建真实数据库、HTTP 或线程。
        若未传入 ``router``，则用已注入的系统/搜索/LLM 依赖调用 ``build_intent_router``。
        若未传入 ``safety_policy``，则 ``SafetyPolicy.from_app_config(config)``（仅读内存配置）。
        """
        policy = safety_policy if safety_policy is not None else SafetyPolicy.from_app_config(config)
        intent_router = router if router is not None else build_intent_router(
            windows_controller=windows_controller,
            chrome_searcher=chrome_searcher,
            llm_client=llm_client,
            safety_policy=policy,
        )
        parts = AssembledParts(
            config=config,
            safety_policy=policy,
            log_file=log_file,
            llm_client=llm_client,
            tts_engine=tts_engine,
            stt_engine=stt_engine,
            windows_controller=windows_controller,
            chrome_searcher=chrome_searcher,
            character_interface=character_interface,
            file_cleaner=file_cleaner,
            router=intent_router,
            logger=logger,
            memory_runtime=memory_runtime,
            startup_mode="interactive",
            resource_settings=None,
            voice_autoload_enabled=False,
        )
        return cls(_parts=parts)

    def _bind_parts(self, parts: AssembledParts) -> None:
        """将装配结果绑定到实例属性（默认路径与注入路径共用）。"""
        self.config = parts.config
        self.safety_policy = parts.safety_policy
        self.log_file = parts.log_file
        self.llm_client = parts.llm_client
        self.tts_engine = parts.tts_engine
        self.stt_engine = parts.stt_engine
        self.windows_controller = parts.windows_controller
        self.chrome_searcher = parts.chrome_searcher
        self.character_interface = parts.character_interface
        self.file_cleaner = parts.file_cleaner
        self.router = parts.router
        self.memory_runtime = getattr(parts, "memory_runtime", None)
        self._model_memory_admission = getattr(
            parts, "model_memory_admission", None
        )
        self._temp_file_manager = getattr(parts, "temp_file_manager", None)
        self.temp_file_manager = self._temp_file_manager
        self.startup_mode: StartupMode = getattr(parts, "startup_mode", "interactive")
        self.resource_settings: ResourceSettings = (
            getattr(parts, "resource_settings", None)
            or parse_resource_settings(parts.config)
        )
        self._voice_autoload_enabled = bool(
            getattr(parts, "voice_autoload_enabled", False)
        )
        self._tts_engine_cls = getattr(parts, "tts_engine_cls", None)
        self._stt_engine_cls = getattr(parts, "stt_engine_cls", None)
        if parts.logger is not None:
            self.logger = parts.logger
        else:
            self.logger = logging.getLogger(__name__)
        # 关闭流程：已完成组件 exactly-once；未完成项（含播报超时）可有界重试
        self._shutdown_flow_done = False
        self._non_memory_shutdown_done = False
        self._speech_shutdown_done = False
        self._tts_shutdown_done = False
        self._stt_shutdown_done = False
        self._temp_shutdown_done = False
        self._file_cleaner_shutdown_done = False
        self._character_shutdown_done = False
        self._model_memory_guard_shutdown_done = False
        self._llm_warmup_shutdown_done = False
        self._llm_warmup_owner = getattr(parts, "llm_warmup_owner", None)
        self._speech_player = None
        self._shutting_down = False
        self._turn_seq = 0
        self._active_turn: Optional[_GenerationTurn] = None
        self._turn_lock = threading.Lock()
        self.session_timer = SessionTimer()
        # 语音懒加载：lifecycle 标记 closing；loader mutex 做 single-flight（构造不持 lifecycle）
        self._lifecycle_lock = threading.RLock()
        self._voice_loader_lock = threading.Lock()
        self._voice_load_cv = threading.Condition(self._voice_loader_lock)
        self._voice_loading = False
        self._owned_tts = False
        self._owned_stt = False
        self._tts_load_failed = False
        self._tts_fail_notified = False
        self._local_voice_prepare_ok = False
        self._voice_load_cancel = threading.Event()
        self._voice_owner_slot: Optional[UnpublishedVoice] = None
        self._voice_lane_ticket = 0
        self._voice_lane_holder: Optional[int] = None
        self._fg_lock = threading.Lock()
        self._fg_generation = 0
        self._fg_active_job: Optional[Any] = None
        self._fg_sticky_failure: Optional[VoiceStackLoadResult] = None
        self._fg_deadline_mono = 0.0

    def _lifecycle_closed(self) -> bool:
        return bool(self._shutting_down or self._non_memory_shutdown_done)

    def _wake_voice_loaders(self) -> None:
        """关闭开始后唤醒所有 loader 等待者；调用方不得持有 lifecycle lock。"""
        with self._voice_loader_lock:
            self._voice_load_cv.notify_all()

    def _wait_while_voice_loading_locked(
        self, *, label: str
    ) -> Optional[VoiceStackLoadResult]:
        """
        在已持有 ``_voice_loader_lock`` 时等待 single-flight。

        使用单一有限 deadline；超时或 closing 返回失败，否则返回 ``None`` 表示可继续。
        """
        deadline = time.monotonic() + VOICE_LOAD_WAIT_SECONDS
        while self._voice_loading:
            with self._lifecycle_lock:
                if self._lifecycle_closed():
                    return VoiceStackLoadResult(
                        ok=False, errors=[f"助手正在关闭，无法加载{label}"]
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return VoiceStackLoadResult(
                    ok=False, errors=[f"语音加载等待超时（{label}）"]
                )
            self._voice_load_cv.wait(timeout=remaining)
        return None

    def _bound_tts_ready(self) -> bool:
        return bound_tts_models_ready(
            self.tts_engine,
            local_prepare_ok=self._local_voice_prepare_ok,
            resource_settings=self.resource_settings,
        )

    def _edge_playback_satisfied(self) -> bool:
        tts = self.tts_engine
        if tts is None or is_cosyvoice_tts(tts):
            return False
        from src.core.voice_activation import edge_rvc_warmup_required

        if edge_rvc_warmup_required(tts, self.resource_settings):
            return self._bound_tts_ready()
        return True

    def ensure_tts_for_playback(self, *, allow_retry: bool = False) -> VoiceStackLoadResult:
        """懒加载 TTS（可选显式重试）。"""
        with self._lifecycle_lock:
            if self._lifecycle_closed():
                return VoiceStackLoadResult(
                    ok=False, errors=["助手正在关闭，无法加载 TTS"]
                )
            if self.tts_engine is not None and not is_cosyvoice_tts(self.tts_engine):
                if self._edge_playback_satisfied():
                    if not (
                        allow_retry
                        and bound_rvc_explicit_retry_needed(
                            self.tts_engine, self.resource_settings
                        )
                    ):
                        from src.core.voice_activation import voice_stack_load_ok

                        return voice_stack_load_ok(
                            self.tts_engine,
                            resource_settings=self.resource_settings,
                        )
        return ensure_voice_transaction(
            self,
            want_tts=True,
            want_stt=False,
            allow_retry=allow_retry,
            lane_label="TTS",
        )

    def ensure_voice_stack(self) -> VoiceStackLoadResult:
        """确保 STT/TTS 就绪（同步事务）。"""
        with self._lifecycle_lock:
            if self._lifecycle_closed():
                return VoiceStackLoadResult(
                    ok=False, errors=["助手正在关闭，无法加载语音栈"]
                )
            if (
                not self._voice_autoload_enabled
                and self.stt_engine is not None
                and self.tts_engine is None
            ):
                return VoiceStackLoadResult(
                    ok=True,
                    warnings=["TTS 引擎未初始化（识别预检可通过，播报将不可用）"],
                )
            if (
                self.tts_engine is not None
                and self.stt_engine is not None
                and not is_cosyvoice_tts(self.tts_engine)
            ):
                if self._edge_playback_satisfied():
                    from src.core.voice_activation import voice_stack_load_ok

                    return voice_stack_load_ok(
                        self.tts_engine,
                        resource_settings=self.resource_settings,
                    )
        return ensure_voice_transaction(
            self,
            want_tts=True,
            want_stt=True,
            allow_retry=False,
            lane_label="语音栈",
        )

    def request_tts_for_playback(
        self,
        *,
        allow_retry: bool = False,
        wait: bool = True,
        wait_sec: Optional[float] = None,
    ) -> VoiceStackLoadResult:
        from src.core.voice_foreground import request_tts_for_playback

        return request_tts_for_playback(
            self,
            allow_retry=allow_retry,
            wait=wait,
            wait_sec=wait_sec,
        )

    def request_voice_stack(
        self,
        *,
        wait: bool = True,
        wait_sec: Optional[float] = None,
    ) -> VoiceStackLoadResult:
        from src.core.voice_foreground import request_voice_stack

        return request_voice_stack(self, wait=wait, wait_sec=wait_sec)

    def _playback_tts_ready(self) -> bool:
        from src.core.voice_foreground import app_tts_playback_ready

        return app_tts_playback_ready(self)

    @staticmethod
    def check_mode_switch(text: str) -> Optional[str]:
        """
        检测用户输入是否包含语音/文本模式切换指令。

        Args:
            text: 用户原始输入文本。

        Returns:
            ``"switch_to_text"``、``"switch_to_voice"`` 或 ``None``（无切换意图）。
        """
        if not text:
            return None
        text_lower = text.lower().strip()
        text_compact = text_lower.replace(" ", "")

        to_text = ("关闭语音模式", "退出语音模式", "关闭语音", "退出语音")
        to_voice = (
            "开启语音模式", "打开语音模式", "启动语音模式", "进入语音模式",
            "开启语音", "打开语音", "voice mode", "voice",
        )

        if any(k in text_compact for k in to_text):
            return "switch_to_text"
        if any(k in text_compact or k == text_lower for k in to_voice):
            return "switch_to_voice"
        return None

    @staticmethod
    def strip_assistant_prefix(text: str) -> str:
        """
        去除助手回复前缀（如 ``Miku: ``），便于 TTS 朗读。

        Args:
            text: 可能带前缀的回复文本。

        Returns:
            去除已知前缀后的文本；无匹配前缀则原样返回。
        """
        for prefix in ("Miku: ", "miku: "):
            if text.startswith(prefix):
                return text[len(prefix):]
        return text

    def process_text_input(self, text: str, use_tts: bool = None) -> str:
        """
        处理文本输入：路由意图、按结果许可可选 TTS、同步角色模型。

        非流式兼容入口；每次输入只路由/执行一次。
        """
        if self._shutting_down:
            return "助手正在关闭"
        turn = self._begin_generation_turn(external_cancel=None)
        if turn is None:
            return "助手正在关闭"
        perf_turn = self.session_timer.new_turn_id()
        with self.session_timer.measure(
            "request",
            turn_id=perf_turn,
            logger=self.logger,
            counts_as_request=True,
        ):
            outcome = self._dispatch_text(text)
            if use_tts is None:
                use_tts = self.config.get("assistant.auto_tts", True)
            if self._turn_is_active(turn, None):
                self._apply_outcome_side_effects(
                    outcome, use_tts=use_tts, use_legacy_thread=True, turn=turn
                )
        return outcome.text

    def process_text_input_streaming(
        self,
        text: str,
        use_tts: bool = None,
        *,
        on_delta: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        wait_for_playback: bool = False,
        playback_timeout: Optional[float] = None,
    ) -> DispatchOutcome:
        """
        可选增量路径：系统/搜索/LLM 各 handler 每输入只执行一次。

        ``wait_for_playback=True`` 时等待本轮播报终结再返回；默认 False 保持
        文本/交互非阻塞播报。``safe_to_resume_listen`` 仅为串行语音模式提供
        播放静默证明；全双工模式使用独立输入门控，不用它开关麦克风。
        """
        if self._shutting_down:
            return DispatchOutcome.module_unavailable("助手正在关闭")
        if cancel_event is not None and cancel_event.is_set():
            return DispatchOutcome.cancelled()

        if use_tts is None:
            use_tts = self.config.get("assistant.auto_tts", True)

        turn = self._begin_generation_turn(external_cancel=cancel_event)
        if turn is None:
            if self._shutting_down:
                return DispatchOutcome.module_unavailable("助手正在关闭")
            return DispatchOutcome.cancelled()

        perf_turn = self.session_timer.new_turn_id()
        _perf_t0 = time.perf_counter()
        try:
            route = self._route_streaming(text)
            if not self._turn_is_active(turn, cancel_event):
                return DispatchOutcome.cancelled()

            if route[0] == "outcome":
                outcome = route[1]
                assert isinstance(outcome, DispatchOutcome)
                if not self._turn_is_active(turn, cancel_event):
                    return DispatchOutcome.cancelled()
                # 系统/搜索处理器不是 LLM token 流，但全双工回声门控仍需在播报前
                # 得到同一份展示文本；CLI 也因此保持统一的增量展示契约。
                if on_delta and outcome.text:
                    on_delta(outcome.text)
                    if not self._turn_is_active(turn, cancel_event):
                        return DispatchOutcome.cancelled()
                if wait_for_playback:
                    return self._speak_outcome_tracked(
                        outcome,
                        turn=turn,
                        use_tts=use_tts,
                        cancel_event=cancel_event,
                        playback_timeout=playback_timeout,
                    )
                self._apply_outcome_side_effects(
                    outcome, use_tts=use_tts, use_legacy_thread=True, turn=turn
                )
                return outcome

            llm = self.llm_client
            assert llm is not None
            player = None
            if use_tts:
                tts_ready = self.request_tts_for_playback(wait=False)
                if (
                    (not tts_ready.ok or tts_ready.pending)
                    and not self._tts_fail_notified
                ):
                    self._tts_fail_notified = True
                    self.logger.warning(
                        "流式播报跳过：TTS 不可用 error_count=%s",
                        len(tts_ready.errors),
                    )
            if use_tts and self._playback_tts_ready():
                # token 归属与播报 begin_turn 同一临界区；失去所有权则不得 begin_turn
                player = self._claim_speech_for_turn(turn, cancel_event)
            if not self._turn_is_active(turn, cancel_event):
                self._release_own_speech(turn)
                return DispatchOutcome.cancelled()

            _stream_max_buf = int(
                self.config.get("assistant.stream_sentence_max_buffer", 500)
            )
            splitter = IncrementalSentenceSplitter(
                max_buffer=_stream_max_buf,
                first_clause_min_chars=min(12, _stream_max_buf),
            )
            turn.speech_projector = SpeechProjector()
            stream = llm.generate_stream(text)
            stream_iter = iter(stream)
            generation_started = time.perf_counter()
            first_delta_logged = False
            assembled_display: list = []
            outcome = DispatchOutcome(
                text="",
                allow_tts=False,
                allow_character=False,
                stage=ReplyStage.GENERATION_FAILED,
            )
            aborted = False
            generation_committed = False
            speech_started = False
            received_terminal = False

            def _try_enqueue(sentence: str) -> bool:
                """入队投影后文本；空投影视为成功跳过，不置 speech_started。"""
                nonlocal speech_started
                spoken = self._project_sentence_for_turn(turn, sentence)
                if not spoken:
                    return True
                ok = self._enqueue_spoken(player, turn, spoken, cancel_event)
                if ok and player is not None and bool(turn.speech_turn_id):
                    speech_started = True
                return ok

            def _flush_projector() -> bool:
                nonlocal speech_started
                projector = turn.speech_projector
                if projector is None:
                    return True
                spoken = projector.finish()
                if not spoken:
                    return True
                ok = self._enqueue_spoken(player, turn, spoken, cancel_event)
                if ok and player is not None and bool(turn.speech_turn_id):
                    speech_started = True
                return ok

            def _abort_own(message: str) -> DispatchOutcome:
                nonlocal aborted
                aborted = True
                splitter.discard()
                turn.cancel.set()
                self._release_own_speech(turn)
                return DispatchOutcome.cancelled(
                    message, safe_to_resume_listen=not speech_started
                )

            def _consumer_failed_ours() -> bool:
                return (
                    player is not None
                    and bool(turn.speech_turn_id)
                    and player.turn_id == turn.speech_turn_id
                    and player.consumer_failed
                )

            try:
                while True:
                    # 主动 next 之前检查：取消后不得再拉取（避免客户端 _complete/commit）
                    if not self._turn_is_active(turn, cancel_event):
                        outcome = _abort_own("本轮已取消")
                        break
                    if _consumer_failed_ours():
                        if generation_committed:
                            self._release_own_speech(turn)
                            outcome = DispatchOutcome.playback_failed(
                                "".join(assembled_display)
                            )
                        else:
                            outcome = _abort_own("播报消费者失败，已停止本轮生成")
                        break

                    try:
                        event = next(stream_iter)
                    except StopIteration:
                        break

                    if event.kind is StreamEventKind.TEXT_DELTA:
                        delta = event.delta or ""
                        if delta and not first_delta_logged:
                            first_delta_logged = True
                            self.logger.info(
                                "[对话计时] Ollama 首段: %.0f ms",
                                (time.perf_counter() - generation_started) * 1000,
                            )
                        assembled_display.append(delta)
                        if on_delta and self._turn_is_active(turn, cancel_event):
                            on_delta(delta)
                        # 回调后立即再检；不得继续切句入队或拉取下一事件
                        if not self._turn_is_active(turn, cancel_event):
                            outcome = _abort_own("本轮已取消")
                            break
                        if _consumer_failed_ours():
                            outcome = _abort_own("播报消费者失败，已停止本轮生成")
                            break
                        for sentence in splitter.push(delta):
                            if not _try_enqueue(sentence):
                                outcome = _abort_own("播报入队失败或已取消，本轮终止")
                                break
                        if aborted:
                            break

                    elif event.kind is StreamEventKind.COMPLETED:
                        received_terminal = True
                        self.logger.info(
                            "[对话计时] Ollama 生成完成: %.0f ms",
                            (time.perf_counter() - generation_started) * 1000,
                        )
                        # 终结事件：客户端可能已在产出 COMPLETED 时 commit；立即处理并结束
                        full = event.text or "".join(assembled_display)
                        generation_ok = (
                            event.status is GenerateStatus.SUCCESS and bool(full)
                        )
                        if generation_ok:
                            generation_committed = True

                        playback_failed = False
                        for sentence in splitter.finish():
                            if not _try_enqueue(sentence):
                                playback_failed = True
                                break
                        if not playback_failed and not _flush_projector():
                            playback_failed = True

                        if playback_failed or _consumer_failed_ours():
                            self._release_own_speech(turn)
                            if generation_committed:
                                outcome = DispatchOutcome.playback_failed(full)
                            else:
                                outcome = _abort_own("播报入队失败或已取消，本轮终止")
                            break

                        if not self._turn_is_active(turn, cancel_event):
                            self._release_own_speech(turn)
                            if generation_committed:
                                outcome = DispatchOutcome.playback_failed(full)
                            else:
                                outcome = _abort_own("本轮已取消")
                            break

                        if generation_ok:
                            outcome = DispatchOutcome(
                                text=full,
                                allow_tts=True,
                                allow_character=True,
                                stage=ReplyStage.OK,
                            )
                            if (
                                self._turn_is_active(turn, cancel_event)
                                and self.character_interface
                                and self.character_interface._initialized
                            ):
                                self.character_interface.send_message(full)
                        else:
                            splitter.discard()
                            self._release_own_speech(turn)
                            outcome = DispatchOutcome(
                                text="",
                                allow_tts=False,
                                allow_character=False,
                                stage=ReplyStage.GENERATION_FAILED,
                                safe_to_resume_listen=not speech_started,
                            )
                        break

                    elif event.kind is StreamEventKind.FAILED:
                        received_terminal = True
                        splitter.discard()
                        turn.cancel.set()
                        self._release_own_speech(turn)
                        detail = event.error_detail or "生成失败"
                        status = event.status or GenerateStatus.RESPONSE_ERROR
                        legacy = GenerateResult.failure(status, detail).to_legacy_string()
                        outcome = DispatchOutcome(
                            text=legacy,
                            allow_tts=False,
                            allow_character=False,
                            stage=ReplyStage.GENERATION_FAILED,
                            safe_to_resume_listen=not speech_started,
                        )
                        break

                # 无 COMPLETED/FAILED 的正常耗尽：结构化生成失败（非成功）
                if not aborted and not received_terminal:
                    splitter.discard()
                    turn.cancel.set()
                    self._release_own_speech(turn)
                    legacy = GenerateResult.failure(
                        GenerateStatus.RESPONSE_ERROR, "流未正常结束"
                    ).to_legacy_string()
                    outcome = DispatchOutcome(
                        text=legacy,
                        allow_tts=False,
                        allow_character=False,
                        stage=ReplyStage.GENERATION_FAILED,
                        safe_to_resume_listen=not speech_started,
                    )
            except Exception as exc:
                # 普通异常：CAS 释放后转为窄契约结果；不得证明静默时不可恢复监听
                splitter.discard()
                turn.cancel.set()
                self._release_own_speech(turn)
                outcome = DispatchOutcome(
                    text=f"错误: {exc}",
                    allow_tts=False,
                    allow_character=False,
                    stage=ReplyStage.GENERATION_FAILED,
                    safe_to_resume_listen=not speech_started,
                )
            except BaseException:
                splitter.discard()
                turn.cancel.set()
                self._release_own_speech(turn)
                raise
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        self.logger.debug("关闭生成流时忽略异常", exc_info=True)

            if wait_for_playback:
                if outcome.stage is ReplyStage.PLAYBACK_FAILED:
                    if player is not None and turn.speech_turn_id:
                        player.mark_production_finished(turn.speech_turn_id)
                elif outcome.stage in (
                    ReplyStage.CANCELLED,
                    ReplyStage.GENERATION_FAILED,
                ):
                    # 已在 abort/fail/无终结耗尽路径 CAS 释放；兜底清理无播报归属
                    if player is not None and turn.speech_turn_id and not speech_started:
                        self._release_own_speech(turn)
                else:
                    outcome = self._await_stream_playback(
                        outcome,
                        turn=turn,
                        player=player,
                        cancel_event=cancel_event,
                        playback_timeout=playback_timeout,
                    )
            return outcome
        finally:
            self.session_timer.record_stage(
                "request_stream",
                (time.perf_counter() - _perf_t0) * 1000.0,
                turn_id=perf_turn,
                logger=self.logger,
                counts_as_request=True,
            )

    def _begin_generation_turn(
        self, *, external_cancel: Optional[threading.Event]
    ) -> Optional[_GenerationTurn]:
        """
        创建新轮次并置位旧轮 cancel；与播报 CAS 清理同在 ``_turn_lock`` 短临界区。

        CAS 失败不得 fallback ``invalidate``。无旧播报归属时也不无条件 invalidate。
        """
        if self._shutting_down:
            return None
        if external_cancel is not None and external_cancel.is_set():
            return None
        with self._turn_lock:
            if self._shutting_down:
                return None
            self._turn_seq += 1
            turn = _GenerationTurn(turn_id=self._turn_seq)
            prev = self._active_turn
            self._active_turn = turn
            prev_speech_id = 0
            if prev is not None:
                prev.cancel.set()
                prev_speech_id = prev.speech_turn_id
                prev.speech_turn_id = 0
            player = self._speech_player
            if player is not None and prev_speech_id:
                player.compare_and_invalidate(prev_speech_id)
            return turn

    def _claim_speech_for_turn(
        self,
        turn: _GenerationTurn,
        external_cancel: Optional[threading.Event],
    ) -> Optional[TurnSpeechPlayer]:
        """
        在 ``_turn_lock`` 内确认仍为本轮所有者后再 ``begin_turn`` 并写入归属。

        失去所有权时返回 None，不得调用无条件 invalidate/begin_turn。
        """
        with self._turn_lock:
            if self._shutting_down or turn.cancel.is_set():
                return None
            if external_cancel is not None and external_cancel.is_set():
                turn.cancel.set()
                return None
            if self._active_turn is not turn:
                return None
            if self.tts_engine is None:
                return None
            if not self._playback_tts_ready():
                return None
            player = self._ensure_speech_player()
            if self._active_turn is not turn or turn.cancel.is_set():
                return None
            turn.speech_turn_id = player.begin_turn()
            return player

    def _turn_is_active(
        self,
        turn: _GenerationTurn,
        external_cancel: Optional[threading.Event],
    ) -> bool:
        if self._shutting_down or turn.cancel.is_set():
            return False
        if external_cancel is not None and external_cancel.is_set():
            turn.cancel.set()
            return False
        return True

    def _release_own_speech(self, turn: _GenerationTurn) -> None:
        """仅 CAS 清理本轮播报；失败则放弃（不得无条件 invalidate）。"""
        player = self._speech_player
        speech_id = turn.speech_turn_id
        if player is None or not speech_id:
            return
        if player.compare_and_invalidate(speech_id):
            turn.speech_turn_id = 0

    def _route_streaming(self, text: str):
        """单次遍历 handler，避免系统/搜索重复执行。"""
        router = self.router
        if router is None:
            return ("outcome", DispatchOutcome.module_unavailable("路由器未初始化"))
        if not hasattr(router, "handlers"):
            return ("outcome", self._dispatch_text(text))

        for handler in router.handlers:
            if isinstance(handler, LLMIntentHandler):
                llm = self.llm_client
                if llm is not None and hasattr(llm, "generate_stream"):
                    return ("stream", handler)
                outcome = handler.handle_outcome(text)
                if outcome is None:
                    continue
                return ("outcome", outcome)
            outcome = handler.handle_outcome(text)
            if outcome is not None:
                return ("outcome", outcome)
        return (
            "outcome",
            DispatchOutcome(text="", allow_tts=False, allow_character=False),
        )

    def _project_sentence_for_turn(self, turn: _GenerationTurn, sentence: str) -> str:
        """对本轮句子做朗读投影；无 projector 时退回完整投影。"""
        raw = self.strip_assistant_prefix(sentence)
        projector = turn.speech_projector
        if projector is not None:
            return projector.feed(raw)
        return project_for_speech(raw)

    def _enqueue_spoken(
        self,
        player: Optional[TurnSpeechPlayer],
        turn: _GenerationTurn,
        spoken: str,
        external_cancel: Optional[threading.Event],
    ) -> bool:
        """已投影文本入队；空串成功跳过。"""
        if player is None:
            return True
        if not spoken or not spoken.strip():
            return True
        if not self._turn_is_active(turn, external_cancel):
            return False
        if turn.speech_turn_id and (
            player.turn_id != turn.speech_turn_id
            or player.consumer_failed
            or player.stopped
        ):
            return False
        return player.enqueue(
            turn.speech_turn_id,
            spoken,
            cancel_event=turn.cancel,
            extra_cancel=external_cancel,
        )

    def _enqueue_sentence(
        self,
        player: Optional[TurnSpeechPlayer],
        turn: _GenerationTurn,
        sentence: str,
        external_cancel: Optional[threading.Event],
    ) -> bool:
        spoken = self._project_sentence_for_turn(turn, sentence)
        return self._enqueue_spoken(player, turn, spoken, external_cancel)

    def _ensure_speech_player(self) -> TurnSpeechPlayer:
        if self._shutting_down:
            raise RuntimeError("助手正在关闭，无法创建播报队列")
        if self._speech_player is None:
            self._speech_player = TurnSpeechPlayer(
                self.tts_engine,
                maxsize=int(self.config.get("assistant.stream_speech_queue_size", 8)),
                poll_interval=float(
                    self.config.get("assistant.stream_speech_poll_interval", 0.05)
                ),
                logger=self.logger,
            )
        return self._speech_player

    def _apply_outcome_side_effects(
        self,
        outcome: DispatchOutcome,
        *,
        use_tts: bool,
        use_legacy_thread: bool,
        turn: Optional[_GenerationTurn] = None,
    ) -> None:
        if self._shutting_down:
            return
        if turn is not None and turn.cancel.is_set():
            return
        response = outcome.text
        if use_tts and outcome.allow_tts:
            if self.tts_engine is None and self._voice_autoload_enabled:
                load = self.request_tts_for_playback(wait=False)
                if (
                    (not load.ok or load.pending)
                    and not self._tts_fail_notified
                ):
                    self._tts_fail_notified = True
                    self.logger.warning(
                        "自动播报跳过：TTS 不可用 error_count=%s",
                        len(load.errors),
                    )
            if self._playback_tts_ready():
                spoken = project_for_speech(self.strip_assistant_prefix(response))
                if not spoken:
                    pass
                elif use_legacy_thread:
                    if turn is None:
                        self.logger.warning("非流播报跳过：缺少轮次上下文")
                    else:
                        try:
                            player = self._claim_speech_for_turn(turn, None)
                            if player is not None and turn.speech_turn_id:
                                ok = player.enqueue(
                                    turn.speech_turn_id,
                                    spoken,
                                    cancel_event=turn.cancel,
                                    extra_cancel=None,
                                )
                                if ok:
                                    player.mark_production_finished(turn.speech_turn_id)
                                else:
                                    self._release_own_speech(turn)
                                    self.logger.warning("非流播报入队失败")
                        except BaseException as exc:
                            if turn.speech_turn_id:
                                self._release_own_speech(turn)
                            if not isinstance(exc, Exception):
                                raise
                            self.logger.error(
                                "非流播报失败 err=%s",
                                exception_log_label(exc),
                            )
                else:
                    self.tts_engine.speak(spoken)

        if (
            outcome.allow_character
            and self.character_interface
            and self.character_interface._initialized
        ):
            self.character_interface.send_message(response)

    def _speak_outcome_tracked(
        self,
        outcome: DispatchOutcome,
        *,
        turn: _GenerationTurn,
        use_tts: bool,
        cancel_event: Optional[threading.Event],
        playback_timeout: Optional[float],
    ) -> DispatchOutcome:
        """系统/搜索等非流结果：经受管理队列播报并可等待完成。"""
        if (
            outcome.allow_character
            and self.character_interface
            and self.character_interface._initialized
            and self._turn_is_active(turn, cancel_event)
        ):
            self.character_interface.send_message(outcome.text)

        if use_tts and outcome.allow_tts and outcome.text and self._turn_is_active(
            turn, cancel_event
        ):
            if self.tts_engine is None and self._voice_autoload_enabled:
                self.request_tts_for_playback(wait=False)
        if not (
            use_tts
            and outcome.allow_tts
            and self._playback_tts_ready()
            and outcome.text
            and self._turn_is_active(turn, cancel_event)
        ):
            return outcome

        player = self._claim_speech_for_turn(turn, cancel_event)
        if player is None or not turn.speech_turn_id:
            return outcome

        spoken = project_for_speech(self.strip_assistant_prefix(outcome.text))
        ok = player.enqueue(
            turn.speech_turn_id,
            spoken,
            cancel_event=turn.cancel,
            extra_cancel=cancel_event,
        )
        player.mark_production_finished(turn.speech_turn_id)
        if not ok:
            self._release_own_speech(turn)
            return DispatchOutcome.playback_failed(
                outcome.text, playback_status=PlaybackStatus.FAILED
            )
        return self._await_stream_playback(
            DispatchOutcome(
                text=outcome.text,
                allow_tts=True,
                allow_character=outcome.allow_character,
                stage=ReplyStage.OK,
            ),
            turn=turn,
            player=player,
            cancel_event=cancel_event,
            playback_timeout=playback_timeout,
        )

    def _await_stream_playback(
        self,
        outcome: DispatchOutcome,
        *,
        turn: _GenerationTurn,
        player: Optional[TurnSpeechPlayer],
        cancel_event: Optional[threading.Event],
        playback_timeout: Optional[float],
    ) -> DispatchOutcome:
        """生成已结束后等待本轮播报；失败/超时不撤销已提交历史。"""
        if player is None or not turn.speech_turn_id:
            return outcome
        if outcome.stage in (ReplyStage.CANCELLED, ReplyStage.GENERATION_FAILED):
            return outcome

        player.mark_production_finished(turn.speech_turn_id)
        timeout = playback_timeout
        if timeout is None:
            timeout = float(
                self.config.get("assistant.stream_playback_wait_timeout", 120.0)
            )
        wait_cancel = cancel_event if cancel_event is not None else turn.cancel
        status = player.wait_for_turn(
            turn.speech_turn_id,
            timeout=timeout,
            cancel_event=wait_cancel,
        )
        if status is PlaybackStatus.COMPLETED:
            return DispatchOutcome(
                text=outcome.text,
                allow_tts=outcome.allow_tts,
                allow_character=outcome.allow_character,
                stage=outcome.stage,
                playback_status=status,
                safe_to_resume_listen=True,
            )
        # TIMEOUT / FAILED / STOPPED / INVALIDATED：CAS 释放本轮，丢弃未开始句子；
        # 已进入 speak 的音频可能仍在，调用方不得恢复 STT。
        self._release_own_speech(turn)
        if status in (
            PlaybackStatus.FAILED,
            PlaybackStatus.TIMEOUT,
            PlaybackStatus.STOPPED,
        ):
            return DispatchOutcome.playback_failed(
                outcome.text,
                playback_status=status,
                safe_to_resume_listen=False,
            )
        # INVALIDATED（含等待阶段外部取消）
        return DispatchOutcome(
            text=outcome.text,
            allow_tts=False,
            allow_character=False,
            stage=ReplyStage.CANCELLED,
            playback_status=status,
            safe_to_resume_listen=False,
        )

    def _dispatch_text(self, text: str) -> DispatchOutcome:
        """路由一次输入；优先结构化入口，兼容仅有 ``dispatch`` 的测试替身。"""
        if not self.router:
            return DispatchOutcome.module_unavailable("路由器未初始化")

        dispatch_outcome = getattr(self.router, "dispatch_outcome", None)
        if callable(dispatch_outcome):
            outcome = dispatch_outcome(text)
            if outcome is None:
                return DispatchOutcome(text="", allow_tts=False, allow_character=False)
            return outcome

        response = self.router.dispatch(text)
        if response is None:
            return DispatchOutcome(text="", allow_tts=False, allow_character=False)
        return DispatchOutcome.from_legacy_handler_text(response)

    def begin_voice_input_session(self) -> tuple[bool, str]:
        engine = self.stt_engine
        if engine is None:
            return True, ""
        starter = getattr(engine, "start_continuous_input", None)
        if not callable(starter):
            return True, ""
        try:
            return starter()
        except Exception as exc:
            self.logger.error(
                "连续采集启动失败 err=%s", exception_log_label(exc)
            )
            return False, "连续采集启动失败"

    def end_voice_input_session(self) -> bool:
        engine = self.stt_engine
        if engine is None:
            return True
        stopper = getattr(engine, "stop_continuous_input", None)
        if not callable(stopper):
            return True
        try:
            return stopper() is True
        except Exception as exc:
            self.logger.error(
                "连续采集停止失败 err=%s", exception_log_label(exc)
            )
            return False

    def recognize_voice_input(
        self,
        continuous: bool = False,
        *,
        cancel_event: Optional[threading.Event] = None,
        timeout: Optional[float] = None,
        phrase_time_limit: Optional[float] = None,
    ) -> RecognitionResult:
        """通过 STT 返回结构化识别结果，兼容仅实现 ``listen`` 的旧依赖。"""
        if cancel_event is not None and cancel_event.is_set():
            return RecognitionResult.cancelled("识别开始前已取消")
        if not self.stt_engine:
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "STT 模块未初始化"
            )

        recognize = getattr(self.stt_engine, "recognize", None)
        if callable(recognize):
            try:
                kwargs = {
                    "continuous": continuous,
                    "cancel_event": cancel_event,
                }
                # 不把新增的可选参数以 None 强塞给旧注入 provider，保留原接口兼容。
                if timeout is not None:
                    kwargs["timeout"] = timeout
                if phrase_time_limit is not None:
                    kwargs["phrase_time_limit"] = phrase_time_limit
                result = recognize(**kwargs)
            except Exception as exc:
                self.logger.error(
                    "语音识别失败 err=%s", exception_log_label(exc)
                )
                return RecognitionResult.failure(
                    RecognitionStatus.INTERNAL_ERROR, str(exc)
                )
            if cancel_event is not None and cancel_event.is_set():
                return RecognitionResult.cancelled("识别完成时已取消")
            return RecognitionResult.coerce(result)

        # 渐进兼容：旧注入对象只有字符串 listen，空串无法再推断具体原因。
        try:
            text = self.stt_engine.listen(continuous=continuous)
        except Exception as exc:
            self.logger.error(
                "语音识别失败 err=%s", exception_log_label(exc)
            )
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, str(exc)
            )
        if cancel_event is not None and cancel_event.is_set():
            return RecognitionResult.cancelled("识别完成时已取消")
        if isinstance(text, str) and text.strip():
            return RecognitionResult.success(text)
        if isinstance(text, str):
            return RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED)
        self.logger.error("语音识别失败: 旧 STT listen 返回了非字符串")
        return RecognitionResult.failure(
            RecognitionStatus.INTERNAL_ERROR, "旧 STT listen 返回了非字符串"
        )

    def process_voice_input(self, continuous: bool = False) -> str:
        """
        通过 STT 引擎采集并识别语音输入。

        Args:
            continuous: 是否持续监听模式（由 STT 引擎解释）。

        Returns:
            识别到的文本；STT 未初始化或识别失败时返回提示或空字符串。
        """
        if not self.stt_engine:
            return "STT 模块未初始化"
        result = self.recognize_voice_input(continuous=continuous)
        if result.status is RecognitionStatus.SUCCESS:
            return result.text
        return ""

    def speak(self, text: str) -> None:
        """
        直接调用 TTS 朗读文本（同步阻塞）。

        Args:
            text: 待朗读内容；会自动去除 ``Miku: `` 前缀并做朗读投影。
        """
        if not self.tts_engine:
            self.logger.warning("TTS 模块未初始化")
            return
        spoken = project_for_speech(self.strip_assistant_prefix(text))
        if not spoken:
            return
        self.tts_engine.speak(spoken)
    def interrupt_active_output(self) -> bool:
        """取消当前生成与待播队列，并尽力停止已经开始的音频输出。"""
        interrupted = False
        speech_id = 0
        player = None
        with self._turn_lock:
            turn = self._active_turn
            if turn is not None:
                turn.cancel.set()
                speech_id = turn.speech_turn_id
                turn.speech_turn_id = 0
                interrupted = True
            player = self._speech_player

        if player is not None and speech_id:
            player.compare_and_invalidate(speech_id)

        interrupt_tts = getattr(self.tts_engine, "interrupt", None)
        if callable(interrupt_tts):
            try:
                interrupt_tts()
                interrupted = True
            except Exception as exc:
                self.logger.error(
                    "停止当前语音输出失败 err=%s",
                    exception_log_label(exc),
                    exc_info=True,
                )
        return interrupted

    def shutdown(self) -> None:
        """
        优雅关闭（容错、顺序幂等）。

        顺序：memory runtime → TTS ``shutdown`` → 清理器 → 角色 ``close``。
        某步普通异常会记录后继续后续步骤。已完成步骤不重复执行；
        任一未实际完成的关闭步骤可在后续 ``shutdown()`` 中重试。

        成功时记录「助手已关闭」；任一步失败时记录「关闭流程已结束但部分资源释放失败」，
        不以无条件成功日志掩盖失败。不关闭调用方管理的组件。
        """
        if self._shutdown_flow_done:
            self.logger.debug("shutdown 流程已执行过，跳过重复调用")
            return

        self.logger.info("正在关闭助手...")
        with self._lifecycle_lock:
            self._shutting_down = True
            self._voice_load_cancel.set()
        # 先释放 lifecycle，再唤醒 loader 等待者（不得在持 lifecycle 时等 loader）
        self._wake_voice_loaders()
        from src.core.voice_foreground import (
            join_foreground_loader_bounded,
            notify_foreground_shutdown,
        )

        notify_foreground_shutdown(self)
        join_foreground_loader_bounded(self, 0.3)
        with self._turn_lock:
            if self._active_turn is not None:
                self._active_turn.cancel.set()
        failures: list[str] = []

        def _run_step(component: str, operation: str, action) -> bool:
            """执行一步关闭；成功返回 True，失败记录后返回 False。"""
            try:
                action()
                return True
            except Exception as exc:
                detail = f"{component}.{operation}: {exception_log_label(exc)}"
                failures.append(detail)
                self.logger.error(
                    "关闭步骤失败 component=%s operation=%s err=%s",
                    component,
                    operation,
                    exception_log_label(exc),
                    exc_info=True,
                )
                return False

        def _close_memory() -> bool:
            if self.memory_runtime is None:
                return True
            runtime = self.memory_runtime
            if getattr(runtime, "is_closed", False):
                return True

            def _do_close() -> None:
                status = runtime.close()
                if status.worker_alive or status.timed_out or not status.drained:
                    raise RuntimeError(
                        "memory close incomplete "
                        f"worker_alive={status.worker_alive} pending={status.pending} "
                        f"timed_out={status.timed_out} drained={status.drained}"
                    )

            return _run_step("memory_runtime", "close", _do_close)

        # 重试路径：非 memory 已全部完成时，仅补完 memory
        if self._non_memory_shutdown_done:
            _run_step(
                "voice_owner",
                "retire",
                lambda: shutdown_retire_owner_in_lane(self),
            )
            memory_ok = _close_memory()
            if memory_ok:
                self._shutdown_flow_done = True
                self.logger.info("助手 memory 关闭重试已完成")
            else:
                self.logger.error(
                    "助手 memory 关闭仍未完成（将允许再次 shutdown 重试）: %s",
                    "; ".join(failures) if failures else "incomplete",
                )
            return

        memory_ok = _close_memory()

        if not getattr(self, "_llm_warmup_shutdown_done", False):
            owner = getattr(self, "_llm_warmup_owner", None)
            if owner is None:
                self._llm_warmup_shutdown_done = True
            elif callable(getattr(type(owner), "close_warmup", None)):

                def _close_llm_warmup() -> None:
                    if owner.close_warmup(timeout=2.0) is not True:
                        raise RuntimeError("llm_warmup_owner.close_warmup incomplete")
                    self._llm_warmup_shutdown_done = True

                _run_step("llm_warmup_owner", "close_warmup", _close_llm_warmup)
            else:

                def _missing_close_contract() -> None:
                    raise RuntimeError(
                        "llm_warmup_owner present but type lacks close_warmup"
                    )

                _run_step(
                    "llm_warmup_owner",
                    "close_warmup",
                    _missing_close_contract,
                )

        _run_step(
            "voice_owner",
            "retire",
            lambda: shutdown_retire_owner_in_lane(self),
        )

        # 仅释放本程序 keep_alive 驻留；不停止用户的 Ollama 服务
        release_owned_ollama_model(
            self.llm_client,
            settings=self.resource_settings,
            log=self.logger,
        )

        # F6/F14：speech → TTS → TempFileManager；speech 未完成则不得关闭其依赖
        if not self._speech_shutdown_done:
            if self._speech_player is not None:
                player = self._speech_player
                player.stop_accepting()
                if player.shutdown(timeout=2.0):
                    self._speech_player = None
                    self._speech_shutdown_done = True
                else:
                    failures.append(
                        "speech_player.shutdown: 有界等待超时（不声称底层播放已停止）"
                    )
            else:
                self._speech_shutdown_done = True

        # speech 未完成：本轮跳过 STT/TTS/Temp；memory/cleaner/character 可继续
        if self._speech_shutdown_done and not self._stt_shutdown_done:
            if self.stt_engine:

                def _stt_shutdown_and_verify() -> None:
                    engine = self.stt_engine
                    shutdown = getattr(engine, "shutdown", None)
                    if not callable(shutdown):
                        return
                    if shutdown() is not True:
                        raise RuntimeError("stt_engine.shutdown incomplete")
                    from src.core.voice_activation import stt_shutdown_reports_complete

                    if not stt_shutdown_reports_complete(engine):
                        raise RuntimeError("stt_engine shutdown status incomplete")

                if _run_step("stt_engine", "shutdown", _stt_shutdown_and_verify):
                    self._stt_shutdown_done = True
            else:
                self._stt_shutdown_done = True

        if (
            self._speech_shutdown_done
            and self._stt_shutdown_done
            and not self._tts_shutdown_done
        ):
            if self.tts_engine:

                def _tts_shutdown_and_verify() -> None:
                    engine = self.tts_engine
                    engine.shutdown()
                    if engine_declares_aggregate_shutdown_status(engine):
                        getter = getattr(engine, "get_shutdown_status")
                        if not callable(getter):
                            raise RuntimeError(
                                "tts_shutdown: get_shutdown_status not callable"
                            )
                        verify_aggregate_shutdown_status(getter())
                        return
                    if engine_declares_local_cosyvoice_shutdown_status(engine):
                        reader = getattr(engine, "local_cosyvoice_shutdown_status")
                        if not callable(reader):
                            raise RuntimeError(
                                "local_cosyvoice_shutdown: status not callable"
                            )
                        cosy_status = reader()
                        if cosy_status is None:
                            raise RuntimeError(
                                "local_cosyvoice_shutdown: status missing (None)"
                            )
                        if getattr(cosy_status, "complete", None) is not True:
                            raise RuntimeError("local_cosyvoice_shutdown: incomplete")
                        return
                    engine_type = getattr(engine, "engine_type", None)
                    if engine_type == "cosyvoice":
                        raise RuntimeError(
                            "local_cosyvoice_shutdown: status missing (noncallable)"
                        )

                if _run_step("tts_engine", "shutdown", _tts_shutdown_and_verify):
                    self._tts_shutdown_done = True
            else:
                self._tts_shutdown_done = True

        if (
            self._speech_shutdown_done
            and self._stt_shutdown_done
            and self._tts_shutdown_done
            and not self._temp_shutdown_done
        ):
            with self._voice_loader_lock:
                voice_loading = self._voice_loading
            with self._lifecycle_lock:
                voice_pending = self._voice_owner_slot is not None
            from src.core.voice_foreground import foreground_voice_load_blocks_shutdown

            fg_loading = foreground_voice_load_blocks_shutdown(self)
            if voice_loading or voice_pending or fg_loading:
                failures.append(
                    "temp_file_manager.shutdown: voice stack loader still in flight"
                )
            else:

                def _retire_policy_rvc_runs() -> None:
                    if not (
                        self.safety_policy.retire_rvc_runs(timeout=2.0) is True
                        and self.safety_policy.rvc_runs_quiescent() is True
                    ):
                        raise RuntimeError(
                            "safety_policy: 单次 RVC run 未静止，保留 TempFileManager 供重试"
                        )

                policy_retire_ok = _run_step(
                    "safety_policy", "retire_rvc_runs", _retire_policy_rvc_runs
                )
                if policy_retire_ok and self._temp_file_manager is not None:
                    mgr = self._temp_file_manager

                    def _shutdown_temp() -> None:
                        status = mgr.shutdown(timeout=2.0)
                        if not getattr(status, "complete", False):
                            raise RuntimeError(
                                "temp_file_manager shutdown incomplete "
                                f"owned={getattr(status, 'owned', '?')} "
                                f"pending={getattr(status, 'pending', '?')} "
                                f"inflight={getattr(status, 'inflight', '?')} "
                                f"worker_alive={getattr(status, 'worker_alive', '?')} "
                                f"active_io_leases={getattr(status, 'active_io_leases', '?')}"
                            )

                    if _run_step("temp_file_manager", "shutdown", _shutdown_temp):
                        self._temp_shutdown_done = True
                elif policy_retire_ok:
                    self._temp_shutdown_done = True

        if not self._file_cleaner_shutdown_done:
            if self.file_cleaner:
                stop_ok = _run_step(
                    "file_cleaner",
                    "stop_auto_cleanup",
                    self.file_cleaner.stop_auto_cleanup,
                )
                if stop_ok:
                    _run_step(
                        "file_cleaner",
                        "cleanup_now",
                        self.file_cleaner.cleanup_now,
                    )
                    self._file_cleaner_shutdown_done = True
                else:
                    self.logger.warning(
                        "file_cleaner.stop_auto_cleanup 失败，跳过 cleanup_now 以免与仍可能运行的清理任务重叠"
                    )
            else:
                self._file_cleaner_shutdown_done = True

        if not self._character_shutdown_done:
            if self.character_interface:
                if _run_step(
                    "character_interface", "close", self.character_interface.close
                ):
                    self._character_shutdown_done = True
            else:
                self._character_shutdown_done = True

        if not getattr(self, "_model_memory_guard_shutdown_done", False):
            guard = getattr(self, "_model_memory_admission", None)
            if guard is None:
                self._model_memory_guard_shutdown_done = True
            else:

                def _close_model_memory_guard() -> None:
                    if guard.close(timeout=2.0) is not True:
                        raise RuntimeError(
                            "model_memory_admission close incomplete (not quiet)"
                        )

                if _run_step(
                    "model_memory_admission",
                    "close",
                    _close_model_memory_guard,
                ):
                    self._model_memory_guard_shutdown_done = True

        non_memory_ok = (
            self._speech_shutdown_done
            and self._stt_shutdown_done
            and self._tts_shutdown_done
            and self._temp_shutdown_done
            and self._file_cleaner_shutdown_done
            and self._character_shutdown_done
            and getattr(self, "_model_memory_guard_shutdown_done", True)
            and getattr(self, "_llm_warmup_shutdown_done", True)
        )
        if non_memory_ok:
            self._non_memory_shutdown_done = True
        if non_memory_ok and memory_ok:
            self._shutdown_flow_done = True

        if failures:
            self.logger.error(
                "助手关闭流程未完成，部分资源释放失败（共 %d 项，可再次 shutdown 重试）: %s",
                len(failures),
                "; ".join(failures),
            )
        elif memory_ok and non_memory_ok:
            self.logger.info("助手已关闭")
        elif non_memory_ok:
            self.logger.error(
                "助手非 memory 资源已回收，但 memory 关闭未完成（可再次 shutdown 重试）"
            )
        else:
            self.logger.error(
                "助手关闭未完成（incomplete，可再次 shutdown 重试）"
            )
