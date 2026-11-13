"""
CLI 应用入口模块。

解析命令行参数、初始化 ``MikuAssistant``、检查 Ollama 连通性，
并按 ``--mode`` 启动文本、语音或交互式会话。
"""
import argparse
import io
import logging
import sys
from pathlib import Path

from src.cli.text_session import run_interactive_session, run_text_session
from src.cli.voice_session import run_voice_session
from src.core.assistant import MikuAssistant


def _setup_console_encoding() -> None:
    """在 Windows 上将 stdout/stderr 重包装为 UTF-8，避免中文乱码。

    必须保留 ``line_buffering`` / ``write_through``：裸 ``TextIOWrapper`` 在
    Windows 控制台上常变成块缓冲，语音模式提示会晚于 logging 出现，看起来像卡死。
    """
    if sys.platform != "win32":
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        buffer = getattr(stream, "buffer", None)
        if stream is None or buffer is None:
            continue
        line_buffering = bool(getattr(stream, "line_buffering", False))
        if not line_buffering and callable(getattr(stream, "isatty", None)):
            try:
                line_buffering = bool(stream.isatty())
            except Exception:
                line_buffering = False
        setattr(
            sys,
            name,
            io.TextIOWrapper(
                buffer,
                encoding="utf-8",
                errors=getattr(stream, "errors", "replace") or "replace",
                line_buffering=line_buffering,
                write_through=True,
            ),
        )


def _ensure_project_path() -> None:
    """将项目根目录加入 ``sys.path``，支持从任意工作目录启动。"""
    project_root = Path(__file__).resolve().parent.parent.parent
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


_ensure_project_path()


def main() -> None:
    """
    CLI 主入口：解析参数、启动助手并进入对应会话模式。

    捕获 ``KeyboardInterrupt`` 与未处理异常，``finally`` 中调用 ``assistant.shutdown()``。
    """
    _setup_console_encoding()

    parser = argparse.ArgumentParser(description="Miku 桌面助手")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument(
        "--mode",
        type=str,
        choices=["text", "voice", "interactive"],
        default="interactive",
        help="运行模式: text / voice / interactive",
    )
    parser.add_argument("--auto-tts", action="store_true", help="自动将 LLM 回复转为语音")
    parser.add_argument(
        "--stream",
        action="store_true",
        help="启用增量显示与分句播报（无需等完整回答即可开始 TTS）",
    )
    args = parser.parse_args()

    assistant = None
    try:
        assistant = MikuAssistant(config_path=args.config, startup_mode=args.mode)
        if args.auto_tts:
            assistant.config.set("assistant.auto_tts", True)
        if args.stream:
            assistant.config.set("assistant.stream_reply", True)

        print("=" * 50)
        print("Miku 桌面助手已启动")
        print("=" * 50)
        print("输入 'quit' 或 'exit' 退出")
        print("输入 'help' 查看帮助")
        print("=" * 50)

        if assistant.llm_client:
            if assistant.llm_client.check_connection():
                if assistant.llm_client.check_model_available():
                    print(f"✓ Ollama 连接正常 (模型: {assistant.llm_client.model})")
                else:
                    print(f"✗ Ollama 模型不存在: {assistant.llm_client.model}")
                    models = assistant.llm_client.list_models()
                    if models:
                        print(f"  可用模型: {', '.join(models[:5])}")
                    print("  请修改 config/config.yaml 中的 ollama.model")
            else:
                print("✗ Ollama 连接失败，请确保 Ollama 服务正在运行")
                print("  提示: 运行 'ollama serve' 启动服务")

        stream_reply = bool(assistant.config.get("assistant.stream_reply", True))
        if args.mode == "text":
            run_text_session(assistant, stream_reply=stream_reply)
        elif args.mode == "voice":
            run_voice_session(assistant, standalone=True, stream_reply=stream_reply)
        else:
            run_interactive_session(assistant, stream_reply=stream_reply)

    except KeyboardInterrupt:
        print("\n\n正在退出...")
    except Exception as exc:
        from src.core.log_privacy import exception_log_label

        # 持久日志仅记安全标签；面向用户的 print 可保留正文
        logging.error(
            "程序错误 err=%s", exception_log_label(exc), exc_info=True
        )
        print(f"错误: {exc}")
    finally:
        if assistant:
            assistant.shutdown()


__all__ = ["main"]
