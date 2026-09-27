"""
文本交互会话模块。

提供纯文本 REPL 循环与带 TTS/语音切换的交互式会话。
默认非流式兼容；可启用增量显示与分句播报路径。
"""
from src.cli.help_text import print_help
from src.cli.voice_session import run_voice_session
from src.core.assistant import MikuAssistant
from src.core.dispatch_outcome import DispatchOutcome, ReplyStage


def _print_reply_streaming(
    assistant: MikuAssistant,
    user_input: str,
    *,
    use_tts: bool,
) -> None:
    """增量显示：完成时不重复打印全文；用 ``stage`` 区分生成/取消/播报失败。"""
    print("\nMiku: ", end="", flush=True)
    saw_delta = False

    def on_delta(delta: str) -> None:
        nonlocal saw_delta
        saw_delta = True
        print(delta, end="", flush=True)

    outcome: DispatchOutcome = assistant.process_text_input_streaming(
        user_input, use_tts=use_tts, on_delta=on_delta
    )
    if not saw_delta:
        print(outcome.text, end="", flush=True)
    elif outcome.stage is ReplyStage.PLAYBACK_FAILED:
        # 全文已通过增量展示；仅短提示，不重复正文
        print("\n[播报失败]", end="", flush=True)
    elif outcome.stage is ReplyStage.CANCELLED:
        print("\n[已取消]", end="", flush=True)
    elif outcome.stage is ReplyStage.GENERATION_FAILED and outcome.text:
        # 生成失败提示；正文若已增量显示过则仍附错误说明（区分角色台词）
        print(f"\n[生成失败] {outcome.text}", end="", flush=True)
    print()


def _print_reply_blocking(
    assistant: MikuAssistant,
    user_input: str,
    *,
    use_tts: bool,
) -> None:
    response = assistant.process_text_input(user_input, use_tts=use_tts)
    print(f"\nMiku: {response}")


def run_text_session(
    assistant: MikuAssistant,
    *,
    allow_voice_switch: bool = True,
    stream_reply: bool = False,
) -> None:
    """
    运行纯文本输入循环（默认不启用 TTS）。

    Args:
        assistant: 已初始化的助手实例。
        allow_voice_switch: 是否响应「开启语音模式」等切换指令。
        stream_reply: 是否使用增量显示路径。
    """
    print("\n[文本模式] 请输入您的问题:")
    if allow_voice_switch:
        print("提示: 输入'开启语音模式'可以切换到语音模式")
    if stream_reply:
        print("提示: 已启用增量显示（stream）")

    while True:
        try:
            user_input = input("\n您: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "退出"):
                break
            if user_input.lower() == "help":
                print_help()
                continue

            if allow_voice_switch and assistant.check_mode_switch(user_input) == "switch_to_voice":
                print("\n切换到语音模式...")
                run_voice_session(assistant, stream_reply=stream_reply)
                print("\n返回文本模式...")
                continue

            if stream_reply:
                _print_reply_streaming(assistant, user_input, use_tts=False)
            else:
                _print_reply_blocking(assistant, user_input, use_tts=False)

        except EOFError:
            break


def run_interactive_session(
    assistant: MikuAssistant, *, stream_reply: bool = False
) -> None:
    """
    运行交互式会话：支持 ``voice`` / ``tts`` / ``stream`` 命令及语音模式切换。

    Args:
        assistant: 已初始化的助手实例。
        stream_reply: 初始是否启用增量路径（可被 ``stream`` 命令切换）。
    """
    print("\n[交互式模式]")
    print("输入文本进行对话，或输入 'voice' 切换到语音输入")
    print("输入 'tts' 切换自动语音输出")
    print("输入 'stream' 切换增量显示与分句播报")
    print("提示: 输入'开启语音模式'或'关闭语音模式'可以切换模式")

    auto_tts = assistant.config.get("assistant.auto_tts", False)
    stream_on = bool(
        stream_reply or assistant.config.get("assistant.stream_reply", False)
    )
    print(f"增量显示: {'开启' if stream_on else '关闭'}")

    while True:
        try:
            user_input = input("\n您: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "退出"):
                break
            if user_input.lower() == "help":
                print_help()
                continue

            if assistant.check_mode_switch(user_input) == "switch_to_voice":
                print("\n切换到语音模式...")
                run_voice_session(assistant, stream_reply=stream_on)
                continue

            if user_input.lower() == "voice":
                run_voice_session(assistant, stream_reply=stream_on)
                continue

            if user_input.lower() == "tts":
                auto_tts = not auto_tts
                assistant.config.set("assistant.auto_tts", auto_tts)
                print(f"自动语音输出: {'开启' if auto_tts else '关闭'}")
                continue

            if user_input.lower() == "stream":
                stream_on = not stream_on
                assistant.config.set("assistant.stream_reply", stream_on)
                print(f"增量显示与分句播报: {'开启' if stream_on else '关闭'}")
                continue

            if stream_on:
                _print_reply_streaming(assistant, user_input, use_tts=auto_tts)
            else:
                _print_reply_blocking(assistant, user_input, use_tts=auto_tts)

        except (KeyboardInterrupt, EOFError):
            break
