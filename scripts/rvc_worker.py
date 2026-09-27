# -*- coding: utf-8 -*-
"""RVC 常驻推理 Worker：启动时加载模型，通过 stdin/stdout JSON 行协议接收任务。

与 ``rvc_infer.py`` 的一次性子进程模式相比，Worker 在进程存活期间只加载一次
模型，显著降低连续 TTS→RVC 流水线的延迟。主进程由 ``src.voice.rvc.infer`` 管理
其生命周期（启动、预热、下发任务、shutdown）。

协议概要：
    - 启动：解析 CLI 参数，加载模型后向 stdout 输出一行 JSON
      ``{"ready": true, "device": "...", "load_ms": ...}`` 或 ``{"ready": false, "error": "..."}``
    - 任务：每行一个 JSON 对象，至少包含 ``input``、``output`` 路径；可选 ``pitch``、``index_rate``
    - 控制：``{"cmd": "ping"}`` 返回 pong；``{"cmd": "warmup"}`` 执行一次
      不播放的短音频推理；``{"cmd": "shutdown"}`` 优雅退出
    - 响应：每任务一行 JSON，``ok`` 为 true 时含 ``output``、``infer_ms``、``total_ms``

须在 ``.venv-rvc`` 环境中运行，依赖与 ``rvc_infer.py`` 相同。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.voice.rvc.vc_params import build_vc_kwargs


def _patch_torch_load_for_fairseq() -> None:
    """修补 ``torch.load``，兼容 fairseq 加载 HuBERT 权重（见 rvc_infer 模块说明）。"""
    import torch

    _orig = torch.load

    def _load(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return _orig(*args, **kwargs)

    torch.load = _load  # type: ignore[method-assign]


def _emit_json(payload: dict) -> None:
    """向协议 stdout 输出一行 JSON（flush 确保主进程立即可读）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _redirect_stdout_to_stderr():
    """加载模型期间将 stdout 重定向到 stderr，避免库 print 污染 JSON 协议通道。"""
    return _StdoutRedirect(sys.stderr)


class _StdoutRedirect:
    def __init__(self, target):
        self._target = target
        self._saved = sys.stdout

    def __enter__(self):
        sys.stdout = self._target
        return self

    def __exit__(self, *args):
        sys.stdout = self._saved


def _load_vc(model_path: str, device: str):
    """加载 RVC ``VC`` 实例及实际使用的设备名。

    Args:
        model_path: RVC 模型权重文件路径（.pth）。
        device: 请求设备（cpu | cuda:0）；CUDA 不可用时回退为 cpu。

    Returns:
        ``(vc, device)`` 元组：已加载模型的 VC 对象与实际设备字符串。
    """
    from rvc_python.configs.config import Config
    from rvc_python.modules.vc.modules import VC
    from rvc_python.download_model import download_rvc_models

    if device.startswith("cuda"):
        import torch

        if not torch.cuda.is_available():
            device = "cpu"

    lib_dir = os.path.dirname(os.path.abspath(__import__("rvc_python").__file__))
    download_rvc_models(lib_dir)
    config = Config(lib_dir, device)
    vc = VC(lib_dir, config)
    vc.get_vc(model_path, "v2")
    return vc, device


def _convert(vc, req: dict, worker_cfg: dict) -> tuple[bool, str, float]:
    """根据 JSON 请求执行单次音色转换并写出 WAV。"""
    from scipy.io import wavfile

    input_path = req["input"]
    output_path = req["output"]
    if not os.path.isfile(input_path):
        return False, f"Input not found: {input_path}", 0.0

    pitch = int(req.get("pitch", worker_cfg.get("pitch", 0)))
    index_rate = req.get("index_rate")
    if index_rate is not None:
        index_rate = float(index_rate)

    vc_kwargs = build_vc_kwargs(
        worker_cfg,
        pitch=pitch,
        index_rate=index_rate,
    )

    t0 = time.perf_counter()
    result = vc.vc_single(
        sid=0,
        input_audio_path=input_path,
        **vc_kwargs,
    )
    infer_ms = (time.perf_counter() - t0) * 1000

    if isinstance(result, tuple):
        err, wav_opt = result
        if err:
            return False, str(err), infer_ms
    else:
        wav_opt = result

    if wav_opt is None:
        return False, "RVC returned empty audio", infer_ms

    wavfile.write(output_path, vc.tgt_sr, wav_opt)
    return True, output_path, infer_ms


def _warm_up_vc(vc, worker_cfg: dict) -> tuple[bool, str, float]:
    """用短正弦 WAV 触发一次完整推理，预热 CUDA/特征提取路径且不播放。"""
    import numpy as np
    from scipy.io import wavfile

    sample_rate = 16000
    duration_seconds = 1.0
    sample_count = int(sample_rate * duration_seconds)
    timeline = np.arange(sample_count, dtype=np.float32) / sample_rate
    # 低振幅有声信号比全静音更能稳定走过 F0 与声学特征路径。
    signal = (0.03 * np.sin(2.0 * np.pi * 220.0 * timeline)).astype(np.float32)

    with tempfile.TemporaryDirectory(prefix="miku-rvc-warmup-") as temp_dir:
        input_path = os.path.join(temp_dir, "input.wav")
        output_path = os.path.join(temp_dir, "output.wav")
        wavfile.write(input_path, sample_rate, signal)
        return _convert(
            vc,
            {
                "input": input_path,
                "output": output_path,
                "pitch": int(worker_cfg.get("pitch", 0)),
                "index_rate": float(worker_cfg.get("index_rate", 0.55)),
            },
            worker_cfg,
        )


def _parent_is_alive(parent_pid: int) -> bool:
    """无副作用地检查父进程；权限拒绝时保守视为仍存活。"""
    if parent_pid <= 0:
        return True
    if sys.platform == "win32":
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        error_access_denied = 5
        kernel32 = ctypes.windll.kernel32
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(synchronize, False, parent_pid)
        if not handle:
            return kernel32.GetLastError() == error_access_denied
        try:
            return wait_for_single_object(handle, 0) == wait_timeout
        finally:
            close_handle(handle)
    try:
        os.kill(parent_pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def _start_parent_watchdog(parent_pid: int) -> None:
    """父进程消失时立即结束 Worker，避免异常退出后继续占用 CPU/GPU。"""
    if parent_pid <= 0:
        return

    def _watch() -> None:
        while _parent_is_alive(parent_pid):
            time.sleep(0.5)
        os._exit(3)

    threading.Thread(target=_watch, name="RVCParentWatchdog", daemon=True).start()


def main() -> int:
    """Worker 主循环：加载模型后从 stdin 读取 JSON 行任务直至 shutdown。

    CLI 参数在启动时固定模型与默认推理选项；单任务可通过 JSON 覆盖 pitch/index_rate。

    Returns:
        模型加载失败返回 1；正常 shutdown 返回 0。
    """
    parser = argparse.ArgumentParser(description="RVC persistent worker")
    parser.add_argument(
        "--model",
        required=True,
        help="RVC 模型权重文件路径（.pth），Worker 生命周期内仅此模型",
    )
    parser.add_argument(
        "--index",
        default="",
        help="全局 FAISS 索引路径（.index）；空字符串表示不使用索引",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="推理设备：cpu | cuda:0；CUDA 不可用时自动回退 cpu",
    )
    parser.add_argument(
        "--pitch",
        type=int,
        default=0,
        help="默认音调偏移（半音）；任务 JSON 可 per-request 覆盖",
    )
    parser.add_argument(
        "--index-rate",
        type=float,
        default=0.55,
        help="默认 index_rate；任务 JSON 可 per-request 覆盖",
    )
    parser.add_argument(
        "--protect",
        type=float,
        default=0.5,
        help="protect 参数，越高越保护清辅音、减轻伪影",
    )
    parser.add_argument(
        "--filter-radius",
        type=int,
        default=3,
        help="中值滤波半径，用于 f0 平滑",
    )
    parser.add_argument(
        "--rms-mix-rate",
        type=float,
        default=0.75,
        help="源音量包络混合比，略低于 1 听感更自然",
    )
    parser.add_argument(
        "--f0-method",
        default="rmvpe",
        help="基频提取方法，如 rmvpe、harvest",
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=0,
        help="宿主进程 PID；宿主异常退出时 Worker 自动终止，0 表示不监视",
    )
    args = parser.parse_args()

    _start_parent_watchdog(args.parent_pid)

    if not os.path.isfile(args.model):
        _emit_json({"ready": False, "error": f"Model not found: {args.model}"})
        return 1

    _patch_torch_load_for_fairseq()

    try:
        t0 = time.perf_counter()
        with _redirect_stdout_to_stderr():
            vc, device = _load_vc(args.model, args.device)
        load_ms = (time.perf_counter() - t0) * 1000
    except Exception as exc:
        _emit_json({"ready": False, "error": str(exc)})
        return 1

    index_path = args.index.strip() if args.index else ""
    worker_cfg = {
        "index_path": index_path,
        "pitch": args.pitch,
        "index_rate": args.index_rate,
        "protect": args.protect,
        "filter_radius": args.filter_radius,
        "rms_mix_rate": args.rms_mix_rate,
        "f0_method": args.f0_method,
        "resample_sr": 0,
    }
    _emit_json(
        {
            "ready": True,
            "device": device,
            "load_ms": round(load_ms, 1),
        }
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit_json({"ok": False, "error": f"Invalid JSON: {exc}"})
            continue

        if req.get("cmd") == "shutdown":
            _emit_json({"ok": True, "shutdown": True})
            break

        if req.get("cmd") == "ping":
            _emit_json({"ok": True, "pong": True})
            continue

        if req.get("cmd") == "warmup":
            t0 = time.perf_counter()
            try:
                with _redirect_stdout_to_stderr():
                    ok, detail, infer_ms = _warm_up_vc(vc, worker_cfg)
                total_ms = (time.perf_counter() - t0) * 1000
                payload = {
                    "ok": ok,
                    "warmup": True,
                    "infer_ms": round(infer_ms, 1),
                    "total_ms": round(total_ms, 1),
                }
                if not ok:
                    payload["error"] = detail
                _emit_json(payload)
            except Exception as exc:
                _emit_json({"ok": False, "warmup": True, "error": str(exc)})
            continue

        t0 = time.perf_counter()
        with _redirect_stdout_to_stderr():
            ok, detail, infer_ms = _convert(vc, req, worker_cfg)
        total_ms = (time.perf_counter() - t0) * 1000
        if ok:
            _emit_json(
                {
                    "ok": True,
                    "output": detail,
                    "infer_ms": round(infer_ms, 1),
                    "total_ms": round(total_ms, 1),
                }
            )
        else:
            _emit_json(
                {
                    "ok": False,
                    "error": detail,
                    "infer_ms": round(infer_ms, 1),
                    "total_ms": round(total_ms, 1),
                }
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
