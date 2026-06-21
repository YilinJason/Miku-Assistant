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
    """在 Windows 上将 stdout/stderr 重包装为 UTF-8，避免中文乱码。"""
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


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
    args = parser.parse_args()

    assistant = None
    try:
        assistant = MikuAssistant(config_path=args.config)
        if args.auto_tts:
            assistant.config.set("assistant.auto_tts", True)

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

        if args.mode == "text":
            run_text_session(assistant)
        elif args.mode == "voice":
            run_voice_session(assistant, standalone=True)
        else:
            run_interactive_session(assistant)

    except KeyboardInterrupt:
        print("\n\n正在退出...")
    except Exception as exc:
        logging.error("程序错误: %s", exc, exc_info=True)
        print(f"错误: {exc}")
    finally:
        if assistant:
            assistant.shutdown()


__all__ = ["main"]
