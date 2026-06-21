"""
文本交互会话模块。

提供纯文本 REPL 循环与带 TTS/语音切换的交互式会话，
均基于 ``MikuAssistant.process_text_input`` 处理用户输入。
"""
from src.cli.help_text import print_help
from src.cli.voice_session import run_voice_session
from src.core.assistant import MikuAssistant


def run_text_session(assistant: MikuAssistant, *, allow_voice_switch: bool = True) -> None:
    """
    运行纯文本输入循环（默认不启用 TTS）。

    Args:
        assistant: 已初始化的助手实例。
        allow_voice_switch: 是否响应「开启语音模式」等切换指令。
    """
    print("\n[文本模式] 请输入您的问题:")
    if allow_voice_switch:
        print("提示: 输入'开启语音模式'可以切换到语音模式")

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
                run_voice_session(assistant)
                print("\n返回文本模式...")
                continue

            response = assistant.process_text_input(user_input, use_tts=False)
            print(f"\nMiku: {response}")

        except EOFError:
            break


def run_interactive_session(assistant: MikuAssistant) -> None:
    """
    运行交互式会话：支持 ``voice`` / ``tts`` 命令及语音模式切换。

    Args:
        assistant: 已初始化的助手实例。
    """
    print("\n[交互式模式]")
    print("输入文本进行对话，或输入 'voice' 切换到语音输入")
    print("输入 'tts' 切换自动语音输出")
    print("提示: 输入'开启语音模式'或'关闭语音模式'可以切换模式")

    auto_tts = assistant.config.get("assistant.auto_tts", False)

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
                run_voice_session(assistant)
                continue

            if user_input.lower() == "voice":
                run_voice_session(assistant)
                continue

            if user_input.lower() == "tts":
                auto_tts = not auto_tts
                assistant.config.set("assistant.auto_tts", auto_tts)
                print(f"自动语音输出: {'开启' if auto_tts else '关闭'}")
                continue

            response = assistant.process_text_input(user_input, use_tts=auto_tts)
            print(f"\nMiku: {response}")

        except (KeyboardInterrupt, EOFError):
            break
