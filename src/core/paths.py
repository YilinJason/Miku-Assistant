"""
项目路径常量与运行时数据目录工具。

定义项目根目录、日志/临时/测试输出路径，并提供目录创建与
受安全策略保护的临时文件生成 helpers。
"""
import os
import tempfile
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TMP_DIR = DATA_DIR / "tmp"
TEST_DIR = DATA_DIR / "test"
LOGS_DIR = PROJECT_ROOT / "logs"

# 运行时数据根目录（供清理器与安全策略注册）
RUNTIME_DATA_DIRS = (TMP_DIR, TEST_DIR, LOGS_DIR)


def ensure_runtime_dirs() -> None:
    """创建 ``logs``、``data/tmp``、``data/test`` 等运行时目录（幂等）。"""
    for directory in RUNTIME_DATA_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def create_app_temp_file(suffix: str) -> str:
    """
    在 ``data/tmp`` 下创建 ``miku_`` 前缀临时文件。

    该目录已注册为文件删除白名单，供 TTS 等模块安全写入临时音频。

    Args:
        suffix: 文件扩展名，可带或不带点号（如 ``wav`` 或 ``.wav``）。

    Returns:
        新建临时文件的绝对路径字符串。
    """
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    name = f"miku_{uuid.uuid4().hex}{suffix}"
    path = TMP_DIR / name
    path.touch(exist_ok=False)
    return str(path)


def get_test_output_dir() -> Path:
    """
    获取测试脚本输出目录并确保其存在。

    Returns:
        ``data/test`` 目录的 ``Path`` 对象。
    """
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    return TEST_DIR


def runtime_layout_summary() -> str:
    """
    生成启动时打印的运行时数据目录说明文本。

    Returns:
        多行字符串，列出日志、临时音频、测试输出路径。
    """
    return "\n".join(
        [
            "运行时数据目录:",
            f"  日志:     {LOGS_DIR}",
            f"  临时音频: {TMP_DIR}",
            f"  测试输出: {TEST_DIR}",
        ]
    )
