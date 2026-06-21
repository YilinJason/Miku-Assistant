# -*- coding: utf-8 -*-
"""RVC（Nahida 音色）环境诊断入口脚本。

读取 ``config/config.yaml`` 中的 ``voice.tts.rvc`` 配置，检查模型文件、索引、
``.venv-rvc`` 推理环境及 ``rvc_python`` 导入是否正常。不执行实际音色转换。

用法::

    python diagnose_rvc.py

通过时提示可运行 ``test_rvc_conversion.py`` 试听；未通过时提示运行
``scripts/setup_voice_env.ps1`` 初始化 RVC 环境。
"""
import io
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent))

from src.core.config import Config
from src.voice.rvc.health_check import check_rvc_health


def main() -> None:
    """加载配置、执行 RVC 健康检查并打印人类可读报告。

    无命令行参数；退出码始终为 0（仅诊断，不因未就绪而以非零码终止）。
    """
    print("=" * 50)
    print("Nahida 语音模型诊断")
    print("=" * 50)
    print()

    cfg = Config()
    report = check_rvc_health(cfg.get_rvc_config())
    print(report.summary())
    print()

    if report.ok:
        print("✅ 就绪。运行 py test_rvc_conversion.py 试听效果。")
    else:
        print("❌ 未就绪。首次使用请运行:")
        print("   powershell -ExecutionPolicy Bypass -File scripts/setup_voice_env.ps1")
    print()


if __name__ == "__main__":
    main()
