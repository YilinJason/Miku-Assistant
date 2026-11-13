# -*- coding: utf-8 -*-
"""RVC 一次性推理 CLI（在 .venv-rvc 环境中运行）。

本脚本由主进程通过 ``safe_subprocess_run`` 调用，将输入 WAV 经 RVC 模型转换为
目标音色后写出。适用于 Worker 未启用或 Worker 失败时的回退路径。

依赖：
    - 项目根目录下的独立虚拟环境 ``.venv-rvc``（或 config 中指定的 python_path）
    - ``rvc_python``、``scipy``、``torch`` 等（由 ``scripts/setup_voice_env.ps1`` 安装）

用法示例::

    .venv-rvc\\Scripts\\python.exe scripts/rvc_infer.py \\
        --input data/tmp/tts.wav \\
        --output data/tmp/rvc_out.wav \\
        --model src/voice/Nahida/nahida-2h-500epoch.pth \\
        --index src/voice/Nahida/added_IVF7975_Flat_nprobe_1_v2.index \\
        --device cuda:0 \\
        --pitch 0 \\
        --index-rate 0.75

成功时向 stdout 打印输出文件路径；失败时向 stderr 打印错误并返回非零退出码。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _early_join_owned_job() -> None:
    from src.core.owned_worker_job import worker_entry_join_owned_job

    worker_entry_join_owned_job()


def _patch_torch_load_for_fairseq() -> None:
    """修补 ``torch.load``，兼容 fairseq 加载 HuBERT 权重。

    PyTorch 2.6+ 默认 ``weights_only=True``，fairseq 的 checkpoint 无法以此模式
    加载。本函数在调用 RVC 库前将默认值改为 ``weights_only=False``，不改变其他参数。
    """
    import torch

    _orig = torch.load

    def _load(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return _orig(*args, **kwargs)

    torch.load = _load  # type: ignore[method-assign]


def main() -> int:
    """解析命令行参数，执行单次 RVC 推理并写出 WAV。

    Returns:
        0 表示成功；1 表示参数、依赖、推理或写文件失败。
    """
    _early_join_owned_job()
    from src.voice.rvc.vc_params import build_vc_kwargs

    parser = argparse.ArgumentParser(description="RVC voice conversion")
    parser.add_argument(
        "--input",
        required=True,
        help="输入音频文件路径（WAV，由上游 TTS 生成）",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="输出音频文件路径（转换后的 WAV）",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="RVC 模型权重文件路径（.pth）",
    )
    parser.add_argument(
        "--index",
        default="",
        help="FAISS 特征索引文件路径（.index，可选；缺失时 index_rate 视为 0）",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="推理设备：cpu | cuda:0；若指定 CUDA 但不可用则自动回退 cpu",
    )
    parser.add_argument(
        "--pitch",
        type=int,
        default=0,
        help="音调偏移（半音），对应 f0_up_key，通常 -12 ~ 12，默认 0",
    )
    parser.add_argument(
        "--index-rate",
        type=float,
        default=0.55,
        help="检索特征混合比例 0.0~1.0；无有效 index 时内部强制为 0，默认 0.55",
    )
    parser.add_argument(
        "--protect",
        type=float,
        default=0.5,
        help="protect 参数：0.5 关闭保护，更小更保护清辅音/气声",
    )
    parser.add_argument(
        "--filter-radius",
        type=int,
        default=3,
        help="Harvest f0 中值滤波半径（rmvpe 不使用）",
    )
    parser.add_argument(
        "--rms-mix-rate",
        type=float,
        default=0.75,
        help="源音量包络混合比",
    )
    parser.add_argument(
        "--f0-method",
        default="rmvpe",
        help="基频提取方法",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.model):
        print(f"Model not found: {args.model}", file=sys.stderr)
        return 1
    if not os.path.isfile(args.input):
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 1

    from src.voice.rvc.local_assets import RvcLocalAssetsError, ensure_rvc_python_local_assets

    try:
        assets = ensure_rvc_python_local_assets()
    except RvcLocalAssetsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    from src.voice.rvc.cpu_budget import apply_rvc_torch_thread_budget

    apply_rvc_torch_thread_budget()
    _patch_torch_load_for_fairseq()

    try:
        from rvc_python.configs.config import Config
        from rvc_python.modules.vc.modules import VC
        from scipy.io import wavfile
    except ImportError as exc:
        print(f"Missing rvc_python: {exc}", file=sys.stderr)
        return 1

    device = args.device
    if device.startswith("cuda"):
        try:
            import torch
            if not torch.cuda.is_available():
                device = "cpu"
        except ImportError:
            device = "cpu"

    lib_dir = assets.lib_dir

    config = Config(lib_dir, device)
    vc = VC(lib_dir, config)
    vc.get_vc(args.model, "v2")

    index_path = args.index.strip() if args.index and os.path.isfile(args.index) else ""

    rvc_cfg = {
        "index_path": index_path,
        "pitch": args.pitch,
        "index_rate": args.index_rate,
        "protect": args.protect,
        "filter_radius": args.filter_radius,
        "rms_mix_rate": args.rms_mix_rate,
        "f0_method": args.f0_method,
        "resample_sr": 0,
    }
    vc_kwargs = build_vc_kwargs(rvc_cfg)

    result = vc.vc_single(
        sid=0,
        input_audio_path=args.input,
        **vc_kwargs,
    )

    if isinstance(result, tuple):
        err, wav_opt = result
        if err:
            print(f"RVC error: {err}", file=sys.stderr)
            return 1
    else:
        wav_opt = result

    if wav_opt is None:
        print("RVC returned empty audio", file=sys.stderr)
        return 1

    wavfile.write(args.output, vc.tgt_sr, wav_opt)
    print(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
