"""Miku 桌面助手 — 主程序入口。

将项目根目录加入 ``sys.path`` 后委托 ``src.cli.app.main`` 启动 CLI。
实际参数解析、会话模式（text / voice / interactive）及助手生命周期均在
``src.cli.app`` 中实现。

用法::

    python main.py                          # 默认 interactive 模式
    python main.py --mode voice             # 语音交互
    python main.py --config path/to.yaml    # 指定配置文件
    python main.py --auto-tts               # 强制开启 LLM 回复自动朗读

支持的启动入口为仓库根目录的 ``main.py``（委托 ``src.cli.app.main``）。
``python -m src.cli.app`` 当前仅加载模块、**不会**自动调用 ``main()``
（``src/cli/app.py`` 无 ``__main__`` 入口）；本阶段不新增模块入口。

嵌入调用方若需与 CLI 相同的 BLAS/OMP 单线程预算，须在导入 numpy/onnxruntime
之前显式调用 ``src.core.numeric_bootstrap.apply_main_numeric_thread_budget()``。
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def main() -> None:
    from src.core.numeric_bootstrap import apply_main_numeric_thread_budget

    apply_main_numeric_thread_budget()
    from src.cli.app import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
