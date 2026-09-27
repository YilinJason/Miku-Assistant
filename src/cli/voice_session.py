"""
持续语音监听会话模块。

封装 ``VoiceSession`` 循环：STT 持续识别 → 意图处理 → 可选 TTS，
支持独立 voice 模式与嵌套于文本/交互模式中的子会话。
支持串行轮流说话与全双工监听；全双工模式对助手回声和低信息转写做门控，
仅在确认用户有效插话后取消当前生成/播报。
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
from typing import Callable, Iterable, Literal, Optional

from src.cli.voice_preflight import ensure_voice_ready
from src.core.assistant import MikuAssistant
from src.core.dispatch_outcome import DispatchOutcome, ReplyStage
from src.core.speech_queue import PlaybackStatus
from src.voice.input_gate import VoiceInputGate
from src.voice.recognition import RecognitionResult, RecognitionStatus

SessionExit = Literal["exit", "switch_text", "unsafe_pause", "stt_error"]

DEFAULT_STOP_KEYWORDS = ("退出语音", "退出", "返回", "quit voice", "exit voice")
VOICE_MODE_STOP_KEYWORDS = ("退出", "结束", "停止", "quit", "exit", "stop", "bye", "再见")


class _ReplyTask:
    """VoiceSession 持有的一轮后台回复；只保存控制状态与回声参考文本。"""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.outcome: Optional[DispatchOutcome] = None
        self.error: Optional[Exception] = None
        self.finished_at = 0.0
        self._text_parts: list[str] = []
        self._lock = threading.Lock()
        self.reported = False

    def append_text(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            self._text_parts.append(delta)

    def text_snapshot(self) -> str:
        with self._lock:
            return "".join(self._text_parts)


class VoiceSession:
    """
    可复用的持续语音对话循环。

    反复调用 STT 识别，将结果交给助手处理，直至命中停止词或模式切换指令。
    串行模式等待本轮播报完成后再监听；全双工模式持续监听，并对助手回声、
    语气词和极短转写做门控，只让有效插话取消当前输出。
    """

    def __init__(
        self,
        assistant: MikuAssistant,
        *,
        use_tts: bool = True,
        stop_keywords: Iterable[str] = DEFAULT_STOP_KEYWORDS,
        show_periodic_hint: bool = False,
        stream_reply: Optional[bool] = None,
        duplex_listening: Optional[bool] = None,
    ):
        """
        配置语音会话行为。

        Args:
            assistant: 已初始化的助手实例。
            use_tts: 是否对 LLM/系统回复启用 TTS 播报。
            stop_keywords: 触发退出语音模式的口令集合（子串匹配，忽略大小写）。
            show_periodic_hint: 是否每 5 轮识别打印一次「继续监听」提示。
            stream_reply: 是否增量回复；``None`` 时读 ``assistant.stream_reply`` 配置。
            duplex_listening: 是否在回复期间继续监听并允许有效插话；``None`` 时读配置。
        """
        self.assistant = assistant
        self.use_tts = use_tts
        self.stop_keywords = tuple(stop_keywords)
        self.show_periodic_hint = show_periodic_hint
        self._last_unrecognized_notify = 0.0
        if stream_reply is None:
            stream_reply = bool(
                assistant.config.get("assistant.stream_reply", True)
            )
        self.stream_reply = bool(stream_reply)
        if duplex_listening is None:
            # 注入测试/旧配置未声明时保持原串行行为；生产默认配置显式开启。
            duplex_listening = bool(
                assistant.config.get("assistant.duplex_listening", False)
            )
        self.duplex_listening = bool(duplex_listening)
        self._duplex_listen_timeout = float(
            assistant.config.get("assistant.duplex_listen_timeout", 0.8)
        )
        self._duplex_phrase_time_limit = float(
            assistant.config.get("assistant.duplex_phrase_time_limit", 6.0)
        )
        self._duplex_echo_grace = float(
            assistant.config.get("assistant.duplex_echo_grace_seconds", 1.0)
        )
        self._duplex_interrupt_wait = float(
            assistant.config.get("assistant.duplex_interrupt_wait_timeout", 5.0)
        )
        self._input_gate = VoiceInputGate(
            min_interrupt_chars=int(
                assistant.config.get("assistant.duplex_min_interrupt_chars", 2)
            ),
            echo_match_ratio=float(
                assistant.config.get("assistant.duplex_echo_match_ratio", 0.72)
            ),
        )
        self._logger = getattr(assistant, "logger", logging.getLogger(__name__))

    def run(self) -> SessionExit:
        """按配置运行串行轮流说话或全双工监听循环。"""
        if self.duplex_listening:
            return self._run_duplex()
        return self._run_serial()

    def _run_serial(self) -> SessionExit:
        """
        串行监听：保留兼容行为，等待播报静默证明后才发起下一次识别。

        Returns:
            ``"exit"`` 表示退出语音模式；``"switch_text"`` 表示用户请求切回文本模式；
            ``"unsafe_pause"`` 表示播报未确认静默，为避免拾取助手声音而停止监听
            （不声称硬打断底层音频）；``"stt_error"`` 表示识别服务/设备不可用，
            为避免错误忙循环而暂停。
        """
        listen_count = 0
        last_hint_count = 0
        while True:
            try:
                if (
                    self.show_periodic_hint
                    and listen_count > 0
                    and listen_count % 5 == 0
                    and listen_count != last_hint_count
                ):
                    print("继续监听中...（请说话）\n")
                    last_hint_count = listen_count

                recognition = self._recognize_once()
                if recognition.status is RecognitionStatus.SILENCE_TIMEOUT:
                    continue
                if recognition.status is RecognitionStatus.UNRECOGNIZED:
                    self._notify_unrecognized()
                    continue
                if recognition.status is RecognitionStatus.CANCELLED:
                    print("\n语音识别已取消，退出语音模式...\n")
                    return "exit"
                if recognition.status is not RecognitionStatus.SUCCESS:
                    self._print_recognition_error(recognition)
                    return "stt_error"

                text = recognition.text
                listen_count += 1

                print(f"您说: {text}")

                if self.assistant.check_mode_switch(text) == "switch_to_text":
                    print("\n切换到文本模式...\n")
                    return "switch_text"

                text_lower = text.lower().strip()
                if any(keyword in text_lower for keyword in self.stop_keywords):
                    print("\n退出语音模式...\n")
                    return "exit"

                try:
                    if self.stream_reply:
                        outcome = self._handle_streaming_turn(text)
                    else:
                        outcome = self._handle_nonstreaming_turn(text)
                except Exception as exc:
                    # 任一路径异常后都无法从 CLI 证明静默，不得继续开麦。
                    # KeyboardInterrupt 不在 Exception 中，仍由外层退出分支处理。
                    print(f"\n发生错误: {exc}")
                    print(
                        "\n播报可能仍在进行，为避免拾取助手声音，"
                        "已暂停语音监听（未硬打断音频）\n"
                    )
                    return "unsafe_pause"

                if not outcome.safe_to_resume_listen:
                    print(
                        "\n播报可能仍在进行，为避免拾取助手声音，"
                        "已暂停语音监听（未硬打断音频）\n"
                    )
                    return "unsafe_pause"

                print("继续监听中...（请说话）\n")

            except KeyboardInterrupt:
                print("\n退出语音模式...\n")
                self._cancel_active_speech()
                return "exit"
            except Exception as exc:
                print(f"\n发生错误: {exc}")
                print("继续监听中...\n")

    def _run_duplex(self) -> SessionExit:
        """回复在受控后台任务中运行；当前线程始终独占麦克风并连续识别。"""
        active: Optional[_ReplyTask] = None
        listen_count = 0
        last_hint_count = 0
        try:
            while True:
                if (
                    self.show_periodic_hint
                    and listen_count > 0
                    and listen_count % 5 == 0
                    and listen_count != last_hint_count
                ):
                    print("实时监听中...（可直接插话）\n")
                    last_hint_count = listen_count

                self._report_reply_failure(active)
                recognition = self._recognize_once(
                    timeout=self._duplex_listen_timeout,
                    phrase_time_limit=self._duplex_phrase_time_limit,
                )
                now = time.monotonic()
                output_active = bool(active is not None and not active.done.is_set())
                echo_tail = bool(
                    active is not None
                    and active.done.is_set()
                    and active.finished_at > 0
                    and now - active.finished_at <= self._duplex_echo_grace
                )

                if recognition.status is RecognitionStatus.SILENCE_TIMEOUT:
                    continue
                if recognition.status is RecognitionStatus.UNRECOGNIZED:
                    if not output_active:
                        self._notify_unrecognized()
                    continue
                if recognition.status is RecognitionStatus.CANCELLED:
                    print("\n语音识别已取消，退出语音模式...\n")
                    return "exit"
                if recognition.status is not RecognitionStatus.SUCCESS:
                    self._print_recognition_error(recognition)
                    self._interrupt_reply(active)
                    return "stt_error"

                if active is not None and (output_active or echo_tail):
                    gate = self._input_gate.evaluate(
                        recognition.text,
                        assistant_text=active.text_snapshot(),
                        output_active=True,
                    )
                    if not gate.accepted:
                        self._logger.info(
                            "[语音门控] 忽略回复期间输入 reason=%s chars=%d",
                            gate.decision.value,
                            len(gate.normalized_text),
                        )
                        continue

                text = recognition.text
                listen_count += 1
                print(f"\n您说: {text}")

                # 有效插话先取消旧生成/播报；回声和低信息内容不会到达这里。
                if output_active and not self._interrupt_reply(active):
                    print("\n当前回复未能及时停止，已暂停语音会话以避免任务重叠。\n")
                    return "unsafe_pause"

                if self.assistant.check_mode_switch(text) == "switch_to_text":
                    print("\n切换到文本模式...\n")
                    return "switch_text"

                text_lower = text.lower().strip()
                if any(keyword in text_lower for keyword in self.stop_keywords):
                    print("\n退出语音模式...\n")
                    return "exit"

                active = self._start_reply_task(text)
                print("\n实时监听中...（可直接插话）\n")
        except KeyboardInterrupt:
            print("\n退出语音模式...\n")
            return "exit"
        finally:
            if active is not None and not active.done.is_set():
                self._interrupt_reply(active)

    def _start_reply_task(self, text: str) -> _ReplyTask:
        task = _ReplyTask()

        def _run() -> None:
            try:
                if self.stream_reply:
                    task.outcome = self._handle_streaming_turn(
                        text,
                        cancel_event=task.cancel,
                        on_delta_observer=task.append_text,
                    )
                else:
                    task.outcome = self._handle_nonstreaming_turn(
                        text,
                        cancel_event=task.cancel,
                        on_delta_observer=task.append_text,
                    )
            except Exception as exc:
                task.error = exc
            finally:
                task.finished_at = time.monotonic()
                task.done.set()

        task.thread = threading.Thread(
            target=_run,
            name="VoiceReplyTask",
            daemon=True,
        )
        task.thread.start()
        return task

    def _interrupt_reply(self, task: Optional[_ReplyTask]) -> bool:
        if task is None or task.done.is_set():
            return True
        task.cancel.set()
        interrupt = getattr(self.assistant, "interrupt_active_output", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception:
                self._logger.error("取消当前回复失败", exc_info=True)
        if task.done.wait(timeout=max(0.0, self._duplex_interrupt_wait)):
            if task.thread is not None:
                task.thread.join(timeout=0.2)
            return True
        self._logger.error(
            "全双工插话后回复任务未在 %.1fs 内停止",
            self._duplex_interrupt_wait,
        )
        return False

    def _report_reply_failure(self, task: Optional[_ReplyTask]) -> None:
        if task is None or not task.done.is_set() or task.reported:
            return
        task.reported = True
        if task.error is not None:
            self._logger.error("后台回复任务失败: %s", task.error)
            print(f"\n回复失败: {task.error}\n")

    def _recognize_once(
        self,
        *,
        timeout: Optional[float] = None,
        phrase_time_limit: Optional[float] = None,
    ) -> RecognitionResult:
        """调用助手结构化入口；兼容尚未迁移的助手替身。"""
        recognize = getattr(self.assistant, "recognize_voice_input", None)
        if callable(recognize):
            try:
                kwargs = {"continuous": True}
                parameters = inspect.signature(recognize).parameters
                accepts_kwargs = any(
                    item.kind is inspect.Parameter.VAR_KEYWORD
                    for item in parameters.values()
                )
                if accepts_kwargs or "timeout" in parameters:
                    kwargs["timeout"] = timeout
                if accepts_kwargs or "phrase_time_limit" in parameters:
                    kwargs["phrase_time_limit"] = phrase_time_limit
                result = recognize(**kwargs)
            except Exception as exc:
                return RecognitionResult.failure(
                    RecognitionStatus.INTERNAL_ERROR, str(exc)
                )
            if isinstance(result, RecognitionResult):
                return RecognitionResult.coerce(result)
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, "助手返回了无效识别结果"
            )

        try:
            text = self.assistant.process_voice_input(continuous=True)
        except Exception as exc:
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, str(exc)
            )
        if text == "STT 模块未初始化":
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, text
            )
        if isinstance(text, str) and text.strip():
            return RecognitionResult.success(text)
        if isinstance(text, str):
            return RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED)
        return RecognitionResult.failure(
            RecognitionStatus.INTERNAL_ERROR, "旧语音入口返回了非字符串"
        )

    @staticmethod
    def _print_recognition_error(result: RecognitionResult) -> None:
        """打印状态匹配的短提示；详情保留在结果中供日志/诊断使用。"""
        messages = {
            RecognitionStatus.SERVICE_ERROR: "语音识别服务不可用",
            RecognitionStatus.DEVICE_ERROR: "麦克风设备不可用",
            RecognitionStatus.NOT_READY: "语音识别模块尚未就绪",
            RecognitionStatus.INTERNAL_ERROR: "语音识别发生内部错误",
        }
        message = messages.get(result.status, "语音识别失败")
        print(f"\n[语音模式] {message}，已暂停监听。\n")

    def _notify_unrecognized(self) -> None:
        """节流“没听清”提示，延续旧 STT 连续模式行为。"""
        now = time.monotonic()
        if now - self._last_unrecognized_notify < 3.0:
            return
        self._last_unrecognized_notify = now
        print("\n[语音模式] 没听清，请再说一次…\n")

    def _handle_streaming_turn(
        self,
        text: str,
        *,
        cancel_event: Optional[threading.Event] = None,
        on_delta_observer: Optional[Callable[[str], None]] = None,
    ) -> DispatchOutcome:
        print("\nMiku: ", end="", flush=True)
        saw_delta = False

        def on_delta(delta: str) -> None:
            nonlocal saw_delta
            saw_delta = True
            if on_delta_observer is not None:
                on_delta_observer(delta)
            print(delta, end="", flush=True)

        outcome: DispatchOutcome = self.assistant.process_text_input_streaming(
            text,
            use_tts=self.use_tts,
            on_delta=on_delta,
            cancel_event=cancel_event,
            wait_for_playback=True,
        )
        self._print_stream_outcome(outcome, saw_delta=saw_delta)
        print()
        return outcome

    def _handle_nonstreaming_turn(
        self,
        text: str,
        *,
        cancel_event: Optional[threading.Event] = None,
        on_delta_observer: Optional[Callable[[str], None]] = None,
    ) -> DispatchOutcome:
        """整段显示回复，但仍等待受管理播报确认静默后再返回。"""
        process_streaming = getattr(
            self.assistant, "process_text_input_streaming", None
        )
        if callable(process_streaming):
            outcome = process_streaming(
                text,
                use_tts=self.use_tts,
                on_delta=on_delta_observer,
                cancel_event=cancel_event,
                wait_for_playback=True,
            )
            print(f"Miku: {outcome.text}\n")
            return outcome

        # 仅供尚未迁移的外部替身兼容；生产助手始终走上面的可等待入口。
        response = self.assistant.process_text_input(text, use_tts=self.use_tts)
        print(f"Miku: {response}\n")
        return DispatchOutcome.from_legacy_handler_text(response)

    def _print_stream_outcome(
        self, outcome: DispatchOutcome, *, saw_delta: bool
    ) -> None:
        if not saw_delta:
            print(outcome.text, end="", flush=True)

        if outcome.stage is ReplyStage.PLAYBACK_FAILED:
            detail = outcome.playback_status
            if detail is PlaybackStatus.TIMEOUT:
                print("\n[播报超时]", end="", flush=True)
            else:
                print("\n[播报失败]", end="", flush=True)
        elif outcome.stage is ReplyStage.CANCELLED:
            print("\n[已取消]", end="", flush=True)
        elif outcome.stage is ReplyStage.GENERATION_FAILED and outcome.text:
            if saw_delta:
                print(f"\n[生成失败] {outcome.text}", end="", flush=True)
            # 无增量时正文已打印

    def _cancel_active_speech(self) -> None:
        """取消当前生成/播报归属；不声称硬打断底层音频。"""
        interrupt = getattr(self.assistant, "interrupt_active_output", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception:
                pass


def run_voice_session(
    assistant: MikuAssistant,
    *,
    standalone: bool = False,
    on_enter_text_mode: Optional[Callable[[], None]] = None,
    stream_reply: Optional[bool] = None,
) -> None:
    """
    启动语音会话；通过预检确保 STT/麦克风/Ollama 就绪。

    Args:
        assistant: 已初始化的助手实例。
        standalone: ``True`` 时为独立 ``--mode voice``：退出后可切文本再返回语音。
        on_enter_text_mode: 独立模式下切文本时的自定义回调；默认调用 ``run_text_session``。
        stream_reply: 是否增量+等待播报；``None`` 时读配置 ``assistant.stream_reply``。
    """
    if not ensure_voice_ready(assistant):
        print("语音模式启动失败，请根据上方提示修复后重试。")
        return

    if stream_reply is None:
        stream_reply = bool(assistant.config.get("assistant.stream_reply", True))
    duplex_listening = bool(
        assistant.config.get("assistant.duplex_listening", False)
    )

    if standalone:
        print("\n" + "=" * 50)
        print("[语音模式] 已启动持续监听")
        print("=" * 50)
        print("提示:")
        print("  - 直接说话即可，系统会自动识别并回复")
        print("  - 说'关闭语音模式'可以切换到文本模式")
        print("  - 说'退出'、'结束'、'停止'可以退出语音模式")
        print("  - 按 Ctrl+C 强制退出程序")
        if duplex_listening:
            print("  - 已启用实时监听：助手回复时可直接插话，回声/低信息输入会被过滤")
        elif stream_reply:
            print("  - 已启用增量回复：本轮播报确认完成后继续听")
        print("=" * 50)
        print("\n正在监听中...（请说话）\n")

        while True:
            result = VoiceSession(
                assistant,
                use_tts=True,
                stop_keywords=VOICE_MODE_STOP_KEYWORDS,
                show_periodic_hint=True,
                stream_reply=stream_reply,
                duplex_listening=duplex_listening,
            ).run()
            if result == "switch_text":
                if on_enter_text_mode:
                    on_enter_text_mode()
                else:
                    from src.cli.text_session import run_text_session

                    run_text_session(
                        assistant,
                        allow_voice_switch=False,
                        stream_reply=stream_reply,
                    )
                print("\n返回语音模式...")
                continue
            break
    else:
        print("\n" + "=" * 50)
        print("进入持续语音模式")
        print("=" * 50)
        print("提示: 直接说话即可，说'关闭语音模式'或'退出语音'返回文本模式")
        if duplex_listening:
            print("提示: 已启用实时监听（回复时可插话；自动过滤回声/低信息输入）")
        elif stream_reply:
            print("提示: 已启用增量回复（本轮播报确认完成后继续听）")
        print("=" * 50)
        print("\n正在监听中...（请说话）\n")
        VoiceSession(
            assistant,
            use_tts=True,
            stream_reply=stream_reply,
            duplex_listening=duplex_listening,
        ).run()
