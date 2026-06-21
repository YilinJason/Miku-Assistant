"""
持续语音监听会话模块。

封装 ``VoiceSession`` 循环：STT 持续识别 → 意图处理 → 可选 TTS，
支持独立 voice 模式与嵌套于文本/交互模式中的子会话。
"""
from typing import Callable, Iterable, Literal, Optional

from src.cli.voice_preflight import ensure_voice_ready
from src.core.assistant import MikuAssistant

SessionExit = Literal["exit", "switch_text"]

DEFAULT_STOP_KEYWORDS = ("退出语音", "退出", "返回", "quit voice", "exit voice")
VOICE_MODE_STOP_KEYWORDS = ("退出", "结束", "停止", "quit", "exit", "stop", "bye", "再见")


class VoiceSession:
    """
    可复用的持续语音对话循环。

    反复调用 STT 识别，将结果交给助手处理，直至命中停止词或模式切换指令。
    """

    def __init__(
        self,
        assistant: MikuAssistant,
        *,
        use_tts: bool = True,
        stop_keywords: Iterable[str] = DEFAULT_STOP_KEYWORDS,
        show_periodic_hint: bool = False,
    ):
        """
        配置语音会话行为。

        Args:
            assistant: 已初始化的助手实例。
            use_tts: 是否对 LLM/系统回复启用 TTS 播报。
            stop_keywords: 触发退出语音模式的口令集合（子串匹配，忽略大小写）。
            show_periodic_hint: 是否每 5 轮识别打印一次「继续监听」提示。
        """
        self.assistant = assistant
        self.use_tts = use_tts
        self.stop_keywords = tuple(stop_keywords)
        self.show_periodic_hint = show_periodic_hint

    def run(self) -> SessionExit:
        """
        进入持续监听主循环。

        Returns:
            ``"exit"`` 表示退出语音模式；``"switch_text"`` 表示用户请求切回文本模式。
        """
        listen_count = 0
        while True:
            try:
                if self.show_periodic_hint and listen_count > 0 and listen_count % 5 == 0:
                    print("继续监听中...（请说话）\n")

                text = self.assistant.process_voice_input(continuous=True)
                listen_count += 1
                if not text:
                    continue

                print(f"您说: {text}")

                if self.assistant.check_mode_switch(text) == "switch_to_text":
                    print("\n切换到文本模式...\n")
                    return "switch_text"

                text_lower = text.lower().strip()
                if any(keyword in text_lower for keyword in self.stop_keywords):
                    print("\n退出语音模式...\n")
                    return "exit"

                response = self.assistant.process_text_input(text, use_tts=self.use_tts)
                print(f"Miku: {response}\n")
                print("继续监听中...（请说话）\n")

            except KeyboardInterrupt:
                print("\n退出语音模式...\n")
                return "exit"
            except Exception as exc:
                print(f"\n发生错误: {exc}")
                print("继续监听中...\n")


def run_voice_session(
    assistant: MikuAssistant,
    *,
    standalone: bool = False,
    on_enter_text_mode: Optional[Callable[[], None]] = None,
) -> None:
    """
    启动语音会话；通过预检确保 STT/麦克风/Ollama 就绪。

    Args:
        assistant: 已初始化的助手实例。
        standalone: ``True`` 时为独立 ``--mode voice``：退出后可切文本再返回语音。
        on_enter_text_mode: 独立模式下切文本时的自定义回调；默认调用 ``run_text_session``。
    """
    if not ensure_voice_ready(assistant):
        print("语音模式启动失败，请根据上方提示修复后重试。")
        return

    if standalone:
        print("\n" + "=" * 50)
        print("[语音模式] 已启动持续监听")
        print("=" * 50)
        print("提示:")
        print("  - 直接说话即可，系统会自动识别并回复")
        print("  - 说'关闭语音模式'可以切换到文本模式")
        print("  - 说'退出'、'结束'、'停止'可以退出语音模式")
        print("  - 按 Ctrl+C 强制退出程序")
        print("=" * 50)
        print("\n正在监听中...（请说话）\n")

        while True:
            result = VoiceSession(
                assistant,
                use_tts=True,
                stop_keywords=VOICE_MODE_STOP_KEYWORDS,
                show_periodic_hint=True,
            ).run()
            if result == "switch_text":
                if on_enter_text_mode:
                    on_enter_text_mode()
                else:
                    from src.cli.text_session import run_text_session
                    run_text_session(assistant, allow_voice_switch=False)
                print("\n返回语音模式...")
                continue
            break
    else:
        print("\n" + "=" * 50)
        print("进入持续语音模式")
        print("=" * 50)
        print("提示: 直接说话即可，说'关闭语音模式'或'退出语音'返回文本模式")
        print("=" * 50)
        print("\n正在监听中...（请说话）\n")
        VoiceSession(assistant, use_tts=True).run()
