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
from typing import Any, Callable, Optional

from src.core.assembly import AssembledParts, assemble_default
from src.core.dispatch_outcome import DispatchOutcome, ReplyStage
from src.core.handlers.llm_handler import LLMIntentHandler
from src.core.router import IntentRouter
from src.core.router_factory import build_intent_router
from src.core.safety import SafetyPolicy
from src.core.sentence_splitter import IncrementalSentenceSplitter
from src.core.speech_queue import PlaybackStatus, TurnSpeechPlayer
from src.llm.generate_result import GenerateResult, GenerateStatus
from src.llm.stream_events import StreamEventKind
from src.voice.recognition import RecognitionResult, RecognitionStatus


@dataclass
class _GenerationTurn:
    """单次输入的不可复用轮次身份；取消事件仅属本轮。"""

    turn_id: int
    cancel: threading.Event = field(default_factory=threading.Event)
    speech_turn_id: int = 0


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
        单步异常记录后继续；整次流程只跑一轮，再次 ``shutdown()`` 不重复、不重试。
        全部成功才记「助手已关闭」；否则记「关闭流程已结束但部分资源释放失败」。
        由调用方管理（助手不关闭）：
            - ``config``、``safety_policy``、``llm_client``、``stt_engine``、
              ``windows_controller``、``chrome_searcher``、``router``
        注入路径不会为缺失的可选组件自动创建真实实现；未注入的槽位保持 ``None``。
        播报中退出会请求停止播放器并回收 RVC Worker；第三方阻塞音频后端仍只能尽力关闭。
    """

    def __init__(self, config_path: Optional[str] = None, *, _parts: Optional[AssembledParts] = None):
        """
        创建助手实例。

        Args:
            config_path: 可选配置路径；默认装配时使用。与 ``_parts`` 互斥于外部用法。
            _parts: 内部/工厂绑定用；外部请使用 ``from_dependencies``。

        Raises:
            Exception: 默认装配过程中核心模块初始化失败时向上抛出。
        """
        if _parts is not None:
            self._bind_parts(_parts)
            return

        try:
            parts = assemble_default(config_path)
        except Exception as exc:
            logging.getLogger(__name__).error("模块初始化失败: %s", exc, exc_info=True)
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
    ) -> "MikuAssistant":
        """
        显式注入依赖构造（无配置文件加载、无日志落盘、无目录创建、无引擎/清理器自动启动）。

        缺失的可选组件保持 ``None``，不会回退创建真实 ``TTSEngine`` / ``STTEngine`` 等。
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
        if parts.logger is not None:
            self.logger = parts.logger
        else:
            self.logger = logging.getLogger(__name__)
        # 关闭流程幂等：整次 shutdown 只执行一轮步骤，失败步骤不在再次调用时重试
        self._shutdown_flow_done = False
        self._speech_player = None
        self._shutting_down = False
        self._turn_seq = 0
        self._active_turn: Optional[_GenerationTurn] = None
        self._turn_lock = threading.Lock()

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
        if use_tts and self.tts_engine:
            # token 归属与播报 begin_turn 同一临界区；失去所有权则不得 begin_turn
            player = self._claim_speech_for_turn(turn, cancel_event)
        if not self._turn_is_active(turn, cancel_event):
            self._release_own_speech(turn)
            return DispatchOutcome.cancelled()

        splitter = IncrementalSentenceSplitter(
            max_buffer=int(self.config.get("assistant.stream_sentence_max_buffer", 500))
        )
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
            """入队；仅在本轮确有受管理播放器且句子被接受时标记可能在播。"""
            nonlocal speech_started
            ok = self._enqueue_sentence(player, turn, sentence, cancel_event)
            if (
                ok
                and player is not None
                and bool(turn.speech_turn_id)
                and sentence
                and sentence.strip()
            ):
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

    def _enqueue_sentence(
        self,
        player: Optional[TurnSpeechPlayer],
        turn: _GenerationTurn,
        sentence: str,
        external_cancel: Optional[threading.Event],
    ) -> bool:
        if player is None:
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
            self.strip_assistant_prefix(sentence),
            cancel_event=turn.cancel,
            extra_cancel=external_cancel,
        )

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
        if use_tts and outcome.allow_tts and self.tts_engine:
            tts_text = self.strip_assistant_prefix(response)
            if use_legacy_thread:
                try:
                    threading.Thread(
                        target=self.tts_engine.speak,
                        args=(tts_text,),
                        daemon=True,
                    ).start()
                except Exception as exc:
                    self.logger.error("启动音频播放线程失败: %s", exc)
                    self.tts_engine.speak(tts_text)
            else:
                self.tts_engine.speak(tts_text)

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

        if not (
            use_tts
            and outcome.allow_tts
            and self.tts_engine
            and outcome.text
            and self._turn_is_active(turn, cancel_event)
        ):
            return outcome

        player = self._claim_speech_for_turn(turn, cancel_event)
        if player is None or not turn.speech_turn_id:
            return outcome

        ok = player.enqueue(
            turn.speech_turn_id,
            self.strip_assistant_prefix(outcome.text),
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
                self.logger.error("语音识别失败: %s", exc)
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
            self.logger.error("语音识别失败: %s", exc)
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
            text: 待朗读内容；会自动去除 ``Miku: `` 前缀。
        """
        if self.tts_engine:
            self.tts_engine.speak(self.strip_assistant_prefix(text))
        else:
            self.logger.warning("TTS 模块未初始化")

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
                self.logger.error("停止当前语音输出失败: %s", exc, exc_info=True)
        return interrupted

    def shutdown(self) -> None:
        """
        优雅关闭（容错、顺序幂等）。

        顺序：TTS ``shutdown`` → 清理器 ``stop_auto_cleanup`` → ``cleanup_now`` → 角色 ``close``。
        某步普通异常会记录后继续后续步骤；若 ``stop_auto_cleanup`` 失败则跳过同清理器的
        ``cleanup_now``，仍尝试关闭角色。整次流程只执行一轮，再次调用不重复、不重试。

        成功时记录「助手已关闭」；任一步失败时记录「关闭流程已结束但部分资源释放失败」，
        不以无条件成功日志掩盖失败。不关闭调用方管理的组件。
        """
        if self._shutdown_flow_done:
            self.logger.debug("shutdown 流程已执行过，跳过重复调用")
            return

        self.logger.info("正在关闭助手...")
        self._shutting_down = True
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
                detail = f"{component}.{operation}: {exc}"
                failures.append(detail)
                self.logger.error(
                    "关闭步骤失败 component=%s operation=%s error=%s",
                    component,
                    operation,
                    exc,
                    exc_info=True,
                )
                return False

        if self.tts_engine:
            _run_step("tts_engine", "shutdown", self.tts_engine.shutdown)

        if self._speech_player is not None:
            player = self._speech_player
            self._speech_player = None
            player.stop_accepting()
            if not player.shutdown(timeout=2.0):
                failures.append(
                    "speech_player.shutdown: 有界等待超时（不声称底层播放已停止）"
                )

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
            else:
                self.logger.warning(
                    "file_cleaner.stop_auto_cleanup 失败，跳过 cleanup_now 以免与仍可能运行的清理任务重叠"
                )

        if self.character_interface:
            _run_step("character_interface", "close", self.character_interface.close)

        self._shutdown_flow_done = True

        if failures:
            self.logger.error(
                "助手关闭流程已结束，但部分资源释放失败（共 %d 项）: %s",
                len(failures),
                "; ".join(failures),
            )
        else:
            self.logger.info("助手已关闭")
