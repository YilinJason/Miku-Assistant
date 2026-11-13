# -*- coding: utf-8 -*-
"""
CosyVoice-300M-Instruct 评估 worker（仅由 vendor CosyVoice Python 执行）。

一次加载模型，生成本套全部基础 WAV，写原子 JSON 报告后完全退出。
不绑定端口、不启动 WebUI、不播放、不访问麦克风，不导入主程序配置。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# scripts/ -> repo root
_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from cosyvoice_eval_suite import (  # noqa: E402
    EXPECTED_COUNT,
    MATCHA_DIR_REL,
    MODEL_DIR_REL,
    OUTPUT_DIR_REL,
    SAMPLE_CASES,
    SCHEMA_VERSION,
    SPEAKER,
    SUITE_CACHE_REL,
    VENDOR_COSYVOICE_REL,
    WETEXT_DIR_REL,
    verify_pinned_assets,
)


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(data, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _atomic_write_wav(path: Path, speech, sample_rate: int) -> int:
    """
    同目录原子写 WAV。

    ``*.wav.tmp`` 扩展名无法被 libsndfile 推断格式，必须显式 ``format=\"WAV\"``。
    """
    import numpy as np
    import soundfile as sf

    tmp = path.with_name(path.name + ".tmp")
    if hasattr(speech, "detach"):
        arr = speech.detach().cpu().numpy()
    else:
        arr = np.asarray(speech)
    if arr.ndim == 2:
        if arr.shape[0] <= 8 and arr.shape[0] < arr.shape[1]:
            arr = arr.T if arr.shape[0] > 1 else arr.reshape(-1)
        else:
            arr = arr.reshape(-1)
    arr = np.asarray(arr, dtype=np.float32).reshape(-1)
    if arr.size <= 0:
        raise RuntimeError("empty_audio_tensor")
    sf.write(
        str(tmp),
        arr,
        int(sample_rate),
        format="WAV",
        subtype="PCM_16",
    )
    info = sf.info(str(tmp))
    if (
        int(info.samplerate) <= 0
        or int(info.frames) <= 0
        or float(info.duration) <= 0.0
    ):
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            if tmp.exists():
                tmp.unlink()
        raise RuntimeError("invalid_wav_tmp")
    os.replace(str(tmp), str(path))
    return path.stat().st_size


def _resolve_under_repo(rel: str) -> Path:
    return (_REPO_ROOT / rel).resolve()


def _ensure_under(child: Path, parent: Path, *, label: str) -> Path:
    resolved = child.resolve()
    parent_r = parent.resolve()
    try:
        resolved.relative_to(parent_r)
    except ValueError as exc:
        raise RuntimeError(f"{label}_outside_allowed") from exc
    return resolved


def _apply_local_wetext(wetext_dir: Path) -> None:
    """将 wetext.wetext.snapshot_download 定向到本地目录，禁止文本前端触网。"""
    import wetext.wetext as wetext_mod

    target = str(wetext_dir.resolve())

    def _local_snapshot_download(*_a, **_k):
        return target

    wetext_mod.snapshot_download = _local_snapshot_download  # type: ignore[method-assign]


def _cuda_peaks_mb() -> tuple[Optional[float], Optional[float]]:
    try:
        import torch

        if not torch.cuda.is_available():
            return None, None
        alloc = torch.cuda.max_memory_allocated() / (1024.0 * 1024.0)
        reserved = torch.cuda.max_memory_reserved() / (1024.0 * 1024.0)
        return round(alloc, 1), round(reserved, 1)
    except Exception:
        return None, None


def run_worker(*, staging_dir: Path, report_path: Path) -> int:
    out_root = _resolve_under_repo(OUTPUT_DIR_REL)
    staging = _ensure_under(staging_dir, out_root, label="staging")
    report = _ensure_under(report_path, out_root, label="report")

    cosy_root = _resolve_under_repo(VENDOR_COSYVOICE_REL)
    matcha_root = _resolve_under_repo(MATCHA_DIR_REL)
    model_dir = _resolve_under_repo(MODEL_DIR_REL)
    wetext_dir = _resolve_under_repo(WETEXT_DIR_REL)
    cache_root = _resolve_under_repo(SUITE_CACHE_REL)

    if not cosy_root.is_dir():
        _err("error_type=MissingCosyVoiceRoot")
        return 2
    if not matcha_root.is_dir():
        _err("error_type=MissingMatchaDir")
        return 2
    if not model_dir.is_dir():
        _err("error_type=MissingModelDir")
        return 2
    if not wetext_dir.is_dir():
        _err("error_type=MissingWeTextDir")
        return 2

    # 模型加载前：实测 git / revision / bytes（fail closed）
    try:
        pin = verify_pinned_assets(
            cosyvoice_root=cosy_root,
            matcha_root=matcha_root,
            wetext_root=wetext_dir,
            model_dir=model_dir,
        )
    except Exception as exc:
        _err(f"error_type=AssetPinFailed:{type(exc).__name__}")
        return 2

    staging.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    (cache_root / "hf").mkdir(parents=True, exist_ok=True)
    (cache_root / "modelscope").mkdir(parents=True, exist_ok=True)
    (cache_root / "mpl").mkdir(parents=True, exist_ok=True)

    os.environ["PYTHONUTF8"] = "1"
    os.environ["HF_HOME"] = str(cache_root / "hf")
    os.environ["MODELSCOPE_CACHE"] = str(cache_root / "modelscope")
    os.environ["MPLCONFIGDIR"] = str(cache_root / "mpl")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    if str(cosy_root) not in sys.path:
        sys.path.insert(0, str(cosy_root))
    matcha = cosy_root / "third_party" / "Matcha-TTS"
    if matcha.is_dir() and str(matcha) not in sys.path:
        sys.path.insert(0, str(matcha))

    try:
        _apply_local_wetext(wetext_dir)
    except Exception:
        _err("error_type=WeTextPatchFailed")
        return 2

    try:
        import torch
        from cosyvoice.cli.cosyvoice import AutoModel
    except Exception:
        _err("error_type=CosyVoiceImportFailed")
        return 2

    if torch.cuda.is_available():
        try:
            torch.cuda.reset_peak_memory_stats()
        except Exception:
            pass

    t0 = time.perf_counter()
    try:
        model = AutoModel(model_dir=str(model_dir))
    except Exception:
        _err("error_type=ModelLoadFailed")
        return 2
    cold_load = time.perf_counter() - t0

    sample_rate = int(getattr(model, "sample_rate", 22050))
    samples_out: List[Dict[str, Any]] = []
    gen_t0 = time.perf_counter()

    for index, (label, text, instruct) in enumerate(SAMPLE_CASES, 1):
        out_wav = staging / f"{index:02d}_{label}_base.wav"
        for stale in (out_wav, Path(str(out_wav) + ".tmp")):
            try:
                if stale.exists() or stale.is_symlink():
                    stale.unlink()
            except OSError:
                _err("error_type=StaleCleanupFailed")
                return 2

        synth_t0 = time.perf_counter()
        try:
            chunks = []
            for item in model.inference_instruct(
                text, SPEAKER, instruct, stream=False
            ):
                speech = item.get("tts_speech")
                if speech is None:
                    raise RuntimeError("missing_tts_speech")
                chunks.append(speech)
            if not chunks:
                raise RuntimeError("empty_inference")
            if len(chunks) == 1:
                speech_tensor = chunks[0]
            else:
                speech_tensor = torch.cat(
                    [c if c.dim() == 1 else c.reshape(-1) for c in chunks],
                    dim=-1,
                )
            nbytes = _atomic_write_wav(out_wav, speech_tensor, sample_rate)
        except Exception:
            _err(f"error_type=SynthFailed label={label}")
            return 2
        synth_sec = time.perf_counter() - synth_t0

        if not out_wav.is_file() or out_wav.stat().st_size <= 0:
            _err(f"error_type=EmptyWav label={label}")
            return 2

        try:
            import soundfile as sf

            info = sf.info(str(out_wav))
            audio_sec = float(info.duration)
            if info.frames <= 0 or info.samplerate <= 0 or audio_sec <= 0:
                raise RuntimeError("bad_wav_info")
        except Exception:
            _err(f"error_type=WavInfoFailed label={label}")
            return 2

        samples_out.append(
            {
                "label": label,
                "synth_seconds": round(synth_sec, 3),
                "audio_seconds": round(audio_sec, 3),
                "bytes": int(nbytes),
            }
        )

    generation_seconds = time.perf_counter() - gen_t0
    alloc_mb, reserved_mb = _cuda_peaks_mb()

    if len(samples_out) != EXPECTED_COUNT:
        _err("error_type=SampleCountMismatch")
        return 2

    payload = {
        "schema_version": SCHEMA_VERSION,
        "speaker": SPEAKER,
        "cold_load_seconds": round(cold_load, 3),
        "generation_seconds": round(generation_seconds, 3),
        "cuda_peak_allocated_mb": alloc_mb,
        "cuda_peak_reserved_mb": reserved_mb,
        "samples": samples_out,
        **pin,
    }
    try:
        _atomic_write_json(report, payload)
    except Exception:
        _err("error_type=ReportWriteFailed")
        return 2
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="CosyVoice instruct eval worker（固定资产，无任意文本/路径）"
    )
    parser.add_argument(
        "--staging-dir",
        required=True,
        help="必须位于 data/test/voice-naturalness/cosyvoice-instruct/ 下",
    )
    parser.add_argument(
        "--report",
        required=True,
        help="worker JSON 报告路径（同上固定根目录下）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return run_worker(
            staging_dir=Path(args.staging_dir),
            report_path=Path(args.report),
        )
    except RuntimeError as exc:
        _err(f"error_type={type(exc).__name__}")
        return 2
    except Exception:
        _err("error_type=WorkerUnhandled")
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _err("error_type=KeyboardInterrupt")
        sys.exit(130)
