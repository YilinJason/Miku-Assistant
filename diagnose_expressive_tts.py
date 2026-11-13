# -*- coding: utf-8 -*-
"""
本地表达型 TTS 评估入口：CosyVoice-300M-Instruct → Nahida RVC。

默认 dry-run：零目录/文件创建、零子进程、零模型/RVC/设备/网络。
仅显式 ``--execute`` 才运行真实本地推理。不切换生产默认 TTS。

用法：
    py diagnose_expressive_tts.py
    py diagnose_expressive_tts.py --execute
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
_scripts = project_root / "scripts"
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

from cosyvoice_eval_suite import (  # noqa: E402
    COSYVOICE_COMMIT,
    COSYVOICE_PYTHON_REL,
    EXPECTED_COUNT,
    KILL_WAIT_SEC,
    LOCK_FILE_NAME,
    MATCHA_COMMIT,
    MATCHA_DIR_REL,
    MODEL_BYTES_EXPECTED,
    MODEL_DIR_REL,
    MODEL_REVISION,
    OUTPUT_DIR_REL,
    SAMPLE_CASES,
    SCHEMA_VERSION,
    SPEAKER,
    SUITE_CACHE_REL,
    TERMINATE_GRACE_SEC,
    VENDOR_COSYVOICE_REL,
    WETEXT_COMMIT,
    WETEXT_DIR_REL,
    WORKER_SCRIPT_REL,
    WORKER_TIMEOUT_SEC,
    staging_entry_allowed,
)
from src.core.config import Config
from src.voice.audio.temp_files import TempFileManager
from src.voice.cosyvoice.worker_env import (
    ENV_ALLOWLIST,
    build_worker_env as _compose_worker_env,
)
from src.voice.naturalness_helpers import (
    build_execute_safety_policy,
    path_lexists,
    purge_sample_artifacts,
    require_nonempty_audio,
)
from src.voice.rvc.health_check import check_rvc_health
from src.voice.tts import TTSEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("diagnose_expressive_tts")

# 兼容旧诊断测试对 allowlist 名的引用
_ENV_ALLOWLIST = ENV_ALLOWLIST


def _repo_root() -> Path:
    return project_root


def _display_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(_repo_root().resolve()).as_posix()
    except ValueError:
        return path.name


def _fixed_paths() -> Dict[str, Path]:
    root = _repo_root()
    return {
        "cosyvoice_python": (root / COSYVOICE_PYTHON_REL).resolve(),
        "worker_script": (root / WORKER_SCRIPT_REL).resolve(),
        "model_dir": (root / MODEL_DIR_REL).resolve(),
        "wetext_dir": (root / WETEXT_DIR_REL).resolve(),
        "matcha_dir": (root / MATCHA_DIR_REL).resolve(),
        "vendor_root": (root / VENDOR_COSYVOICE_REL).resolve(),
        "cache_root": (root / SUITE_CACHE_REL).resolve(),
        "output_dir": (root / OUTPUT_DIR_REL).resolve(),
    }


def resolve_assets() -> Tuple[Optional[Dict[str, Path]], str]:
    """解析固定资产；任一缺失则 fail-closed（在启动子进程前）。"""
    paths = _fixed_paths()
    checks = (
        ("cosyvoice_python", "file"),
        ("worker_script", "file"),
        ("model_dir", "dir"),
        ("wetext_dir", "dir"),
        ("matcha_dir", "dir"),
        ("vendor_root", "dir"),
    )
    for key, kind in checks:
        p = paths[key]
        ok = p.is_file() if kind == "file" else p.is_dir()
        if not ok:
            return None, f"missing_{key}"
    try:
        paths["worker_script"].relative_to(_repo_root().resolve())
        paths["output_dir"].relative_to((_repo_root() / "data" / "test").resolve())
    except ValueError:
        return None, "path_escape"
    return paths, ""


def build_worker_command(paths: Dict[str, Path], *, staging: Path, report: Path) -> List[str]:
    """固定绝对路径参数列表；shell=False。"""
    return [
        str(paths["cosyvoice_python"]),
        str(paths["worker_script"]),
        "--staging-dir",
        str(staging.resolve()),
        "--report",
        str(report.resolve()),
    ]


def build_worker_env(paths: Dict[str, Path]) -> Dict[str, str]:
    """薄代理：复用 ``src.voice.cosyvoice.worker_env``。"""
    return _compose_worker_env(cache_root=paths["cache_root"])


class EvalRunLock:
    """跨进程单实例锁；忙则立即 fail-closed（Windows 崩溃后由 OS 释放）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(self.path), flags)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, b"1")
            self._fd = fd
            return True
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return False

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def terminate_process(
    proc: subprocess.Popen,
    *,
    grace_sec: float = TERMINATE_GRACE_SEC,
    kill_wait_sec: float = KILL_WAIT_SEC,
) -> None:
    """有界 terminate → wait → kill。"""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace_sec)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=kill_wait_sec)
    except subprocess.TimeoutExpired:
        logger.debug("process_kill_wait_timeout")


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(str(tmp), str(path))


def _finite_number(value: Any, *, allow_none: bool = False) -> bool:
    if value is None:
        return allow_none
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _inspect_wav(path: Path) -> Tuple[int, float, int]:
    """返回 (samplerate, duration_sec, frames)；失败抛错。"""
    import soundfile as sf

    info = sf.info(str(path))
    sr = int(info.samplerate)
    frames = int(info.frames)
    duration = float(info.duration)
    if sr <= 0 or frames <= 0 or duration <= 0 or not math.isfinite(duration):
        raise RuntimeError("wav_meta_invalid")
    return sr, duration, frames


def validate_worker_report(report: dict, *, staging: Path) -> Optional[str]:
    """校验 worker JSON 与 staging WAV 的完整契约。"""
    if not isinstance(report, dict):
        return "report_not_object"
    if report.get("schema_version") != SCHEMA_VERSION:
        return "schema_version"
    for exp_key, act_key, expected in (
        ("cosyvoice_commit_expected", "cosyvoice_commit_actual", COSYVOICE_COMMIT),
        ("matcha_commit_expected", "matcha_commit_actual", MATCHA_COMMIT),
        ("wetext_commit_expected", "wetext_commit_actual", WETEXT_COMMIT),
        ("model_revision_expected", "model_revision_actual", MODEL_REVISION),
    ):
        if report.get(exp_key) != expected or report.get(act_key) != expected:
            return f"mismatch_{act_key}"
    if report.get("model_bytes_expected") != MODEL_BYTES_EXPECTED:
        return "mismatch_model_bytes_expected"
    if report.get("model_bytes_actual") != MODEL_BYTES_EXPECTED:
        return "mismatch_model_bytes_actual"
    if report.get("speaker") != SPEAKER:
        return "mismatch_speaker"

    for key in ("cold_load_seconds", "generation_seconds"):
        val = report.get(key)
        if not _finite_number(val) or float(val) < 0:
            return f"bad_{key}"
    for key in ("cuda_peak_allocated_mb", "cuda_peak_reserved_mb"):
        val = report.get(key)
        if val is None:
            continue
        if not _finite_number(val) or float(val) < 0:
            return f"bad_{key}"

    samples = report.get("samples")
    if not isinstance(samples, list) or len(samples) != EXPECTED_COUNT:
        return "sample_count"
    expected_labels = [c[0] for c in SAMPLE_CASES]
    got_labels = [s.get("label") for s in samples if isinstance(s, dict)]
    if got_labels != expected_labels:
        return "label_order"

    for index, s in enumerate(samples, 1):
        if not isinstance(s, dict):
            return "sample_not_object"
        label = s.get("label")
        if not isinstance(label, str):
            return "sample_label"
        for key in ("synth_seconds", "audio_seconds"):
            val = s.get(key)
            if not _finite_number(val) or float(val) < 0:
                return f"bad_{key}"
        if float(s["audio_seconds"]) <= 0:
            return "audio_seconds_nonpositive"
        nbytes = s.get("bytes")
        if not isinstance(nbytes, int) or isinstance(nbytes, bool) or nbytes <= 0:
            return "sample_bytes"
        wav = staging / f"{index:02d}_{label}_base.wav"
        if not wav.is_file():
            return "wav_missing"
        actual_size = wav.stat().st_size
        if actual_size != nbytes:
            return "bytes_mismatch"
        try:
            _inspect_wav(wav)
        except Exception:
            return "wav_unreadable"
    return None


def cleanup_owned_staging(temp_files: TempFileManager, staging: Path) -> None:
    """仅清理本次拥有的 staging；失败抛错（不得 ignore_errors 继续）。"""
    if not path_lexists(staging):
        return
    if not staging.is_dir():
        raise RuntimeError("staging_not_dir")
    for child in list(staging.iterdir()):
        name = child.name
        if not staging_entry_allowed(name):
            raise RuntimeError("staging_unexpected_entry")
        if child.is_dir():
            raise RuntimeError("staging_nested_dir")
        purge_sample_artifacts(temp_files, child)
    staging.rmdir()
    if path_lexists(staging):
        raise RuntimeError("staging_still_exists")


def print_dry_run_plan() -> int:
    print("=" * 60)
    print("表达型本地 TTS 评估（dry-run）")
    print("=" * 60)
    print()
    print("本模式不创建目录/文件、不启动子进程、不加载模型/RVC、不访问设备/网络。")
    print("真实 A/B 请显式传入 --execute。")
    print("约束：本地 CosyVoice 推理阶段不应联网；CosyVoice 与 RVC 严格串行；不会自动播放。")
    print("隐私：仅打印代码内固定非敏感样本；不切换生产默认 TTS。")
    print()
    print(f"schema_version: {SCHEMA_VERSION}")
    print(f"speaker: {SPEAKER}")
    print(f"cosyvoice_commit: {COSYVOICE_COMMIT}")
    print(f"matcha_commit: {MATCHA_COMMIT}")
    print(f"wetext_commit: {WETEXT_COMMIT}")
    print(f"model_revision: {MODEL_REVISION}")
    print(f"model_bytes_expected: {MODEL_BYTES_EXPECTED}")
    print(f"计划输出目录: {OUTPUT_DIR_REL}")
    print(f"worker: {WORKER_SCRIPT_REL}")
    print(f"cosyvoice_python: {COSYVOICE_PYTHON_REL}")
    print(f"model_dir: {MODEL_DIR_REL}")
    print(f"wetext_dir: {WETEXT_DIR_REL}")
    print(f"样本数: {EXPECTED_COUNT}")
    print()
    print("固定样本：")
    for index, (label, text, instruct) in enumerate(SAMPLE_CASES, 1):
        print(f"  [{index:02d}_{label}]")
        print(f"    text={text!r}")
        print(f"    instruct={instruct!r}")
    print()
    print("提示: py diagnose_expressive_tts.py --execute")
    return 0


def run_execute(
    config: Config,
    *,
    popen=subprocess.Popen,
    tts_engine_cls=TTSEngine,
    timeline: Optional[List[str]] = None,
    lock_cls=EvalRunLock,
) -> int:
    """
    CosyVoice worker → 校验 → RVC。失败返回非零。

    ``timeline`` 供离线测试记录事件顺序（worker_exit / rvc_init / first_rvc）。
    """
    events = timeline if timeline is not None else []
    t_start = time.perf_counter()
    paths, err = resolve_assets()
    if paths is None:
        print(f"错误: 固定资产不可用 ({err})")
        return 1

    rvc_config = config.get_rvc_config()
    if not rvc_config:
        print("错误: 未找到 RVC 配置（voice.tts.rvc）")
        return 1

    safety, effective_rvc, seal_err = build_execute_safety_policy(config, rvc_config)
    if safety is None or effective_rvc is None:
        print(f"错误: {seal_err}")
        return 1

    out_dir = paths["output_dir"]
    lock_path = out_dir / LOCK_FILE_NAME
    run_lock = lock_cls(lock_path)
    if not run_lock.acquire():
        print("错误: 另一评估实例正在运行（单实例锁忙）")
        return 1

    staging: Optional[Path] = None
    worker_report_path: Optional[Path] = None
    parent_report_path = out_dir / "report.json"
    tts_engine: Optional[TTSEngine] = None
    temp_files: Optional[TempFileManager] = None
    proc: Optional[subprocess.Popen] = None
    worker_exit: Optional[int] = None
    success_count = 0
    sample_rows: List[Dict[str, Any]] = []
    worker_payload: Optional[dict] = None
    staging_cleaned = False

    def _publish_failure_report(*, reason: str) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "complete": False,
            "success_count": success_count,
            "expected_count": EXPECTED_COUNT,
            "worker_exit_code": worker_exit,
            "total_seconds": round(time.perf_counter() - t_start, 3),
            "failure_reason": reason,
            "cosyvoice_commit_expected": COSYVOICE_COMMIT,
            "matcha_commit_expected": MATCHA_COMMIT,
            "wetext_commit_expected": WETEXT_COMMIT,
            "model_revision_expected": MODEL_REVISION,
            "model_bytes_expected": MODEL_BYTES_EXPECTED,
            "speaker": SPEAKER,
            "samples": sample_rows,
            "worker": worker_payload,
        }
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(parent_report_path, payload)
        except Exception:
            logger.debug("failure_report_write_failed")

    try:
        print("=" * 60)
        print("表达型本地 TTS 评估（--execute）")
        print("=" * 60)
        print()
        print(f"输出目录: {_display_rel(out_dir)}")
        print("阶段: CosyVoice worker（完成后完全退出）→ RVC")
        print()

        out_dir.mkdir(parents=True, exist_ok=True)
        safety.register_file_delete_dir(out_dir)
        temp_files = TempFileManager(safety_policy=safety, default_dir=out_dir)

        staging = out_dir / f"_staging_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        if path_lexists(staging):
            print("错误: staging 路径已存在")
            _publish_failure_report(reason="staging_exists")
            return 1
        staging.mkdir(parents=True, exist_ok=False)
        safety.register_file_delete_dir(staging)
        worker_report_path = staging / "worker_report.json"

        for sub in ("hf", "modelscope", "mpl"):
            (paths["cache_root"] / sub).mkdir(parents=True, exist_ok=True)

        cmd = build_worker_command(
            paths, staging=staging, report=worker_report_path
        )
        env = build_worker_env(paths)
        proc = popen(
            cmd,
            shell=False,
            env=env,
            cwd=str(paths["vendor_root"]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            worker_exit = proc.wait(timeout=WORKER_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            terminate_process(proc)
            worker_exit = proc.poll()
            events.append("worker_timeout")
            print("错误: worker 超时")
            _publish_failure_report(reason="worker_timeout")
            return 1

        events.append("worker_exit")
        if worker_exit != 0:
            print(f"错误: worker 非零退出 code={worker_exit}")
            _publish_failure_report(reason="worker_nonzero")
            return 1

        assert worker_report_path is not None
        if not worker_report_path.is_file():
            print("错误: worker 报告缺失")
            _publish_failure_report(reason="worker_report_missing")
            return 1

        try:
            worker_payload = json.loads(
                worker_report_path.read_text(encoding="utf-8")
            )
        except Exception:
            print("错误: worker 报告无法解析")
            _publish_failure_report(reason="worker_report_parse")
            return 1

        v_err = validate_worker_report(worker_payload, staging=staging)
        if v_err:
            print(f"错误: worker 报告校验失败 ({v_err})")
            _publish_failure_report(reason=f"worker_report_{v_err}")
            return 1

        report = check_rvc_health(effective_rvc, safety_policy=safety)
        print(report.summary())
        print()
        if not report.ok:
            print("错误: RVC 环境未就绪")
            _publish_failure_report(reason="rvc_health")
            return 1

        events.append("rvc_init")
        tts_config = config.get("voice.tts", {}) or {}
        tts_engine = tts_engine_cls(
            engine=tts_config.get("engine", "edge-tts"),
            language=tts_config.get("language", "zh-CN"),
            voice=tts_config.get("voice", "zh-CN-XiaoxiaoNeural"),
            rvc_config=effective_rvc,
            safety_policy=safety,
            warm_rvc_on_init=True,
            temp_file_manager=temp_files,
        )

        worker_by_label = {
            s["label"]: s for s in worker_payload["samples"]
        }

        for index, (label, _text, _inst) in enumerate(SAMPLE_CASES, 1):
            stage = "purge"
            src_base = staging / f"{index:02d}_{label}_base.wav"
            dest_base = out_dir / f"{index:02d}_{label}_base.wav"
            dest_rvc = out_dir / f"{index:02d}_{label}_rvc.wav"
            try:
                # F9：最终 A/B 不长期登记；RVC 输入必须是已 adopt 的 staging 路径
                stage = "purge"
                purge_sample_artifacts(temp_files, dest_base, dest_rvc)

                stage = "adopt"
                owned_src = temp_files.adopt_existing(src_base)
                require_nonempty_audio(Path(owned_src), stage="staging_base")

                stage = "rvc"
                if "first_rvc" not in events:
                    events.append("first_rvc")
                rvc_t0 = time.perf_counter()
                rvc_path = tts_engine.apply_rvc_model(owned_src)
                rvc_sec = time.perf_counter() - rvc_t0
                if not rvc_path or not Path(rvc_path).is_file():
                    raise RuntimeError("rvc_failed")
                require_nonempty_audio(Path(rvc_path), stage="rvc_out")

                stage = "copy_final"
                shutil.copy2(owned_src, dest_base)
                require_nonempty_audio(dest_base, stage="final_base")
                shutil.copy2(rvc_path, dest_rvc)
                require_nonempty_audio(dest_rvc, stage="rvc_dest")

                w = worker_by_label[label]
                sample_rows.append(
                    {
                        "label": label,
                        "base_bytes": dest_base.stat().st_size,
                        "rvc_bytes": dest_rvc.stat().st_size,
                        "cosyvoice_synth_seconds": w.get("synth_seconds"),
                        "audio_seconds": w.get("audio_seconds"),
                        "rvc_seconds": round(rvc_sec, 3),
                    }
                )
                success_count += 1
                print(f"[{index}/{EXPECTED_COUNT}] {label} ok")
            except Exception as exc:
                print(
                    f"[{index}/{EXPECTED_COUNT}] {label} 失败 "
                    f"stage={stage} err={type(exc).__name__}"
                )
                if stage != "purge":
                    try:
                        purge_sample_artifacts(temp_files, dest_base, dest_rvc)
                    except Exception as purge_exc:
                        logger.debug(
                            "post_fail_purge_incomplete err=%s",
                            type(purge_exc).__name__,
                        )
                _publish_failure_report(reason=f"rvc_stage_{stage}")
                return 1

        if success_count != EXPECTED_COUNT:
            _publish_failure_report(reason="incomplete_samples")
            return 1

        # F7：成功报告前必须确认本次 staging 已清理；失败不得标 complete
        assert staging is not None and temp_files is not None
        try:
            cleanup_owned_staging(temp_files, staging)
            staging_cleaned = True
            events.append("staging_cleaned")
        except Exception:
            print("错误: staging 清理失败")
            _publish_failure_report(reason="staging_cleanup")
            return 1

        final_payload = {
            "schema_version": SCHEMA_VERSION,
            "complete": True,
            "success_count": success_count,
            "expected_count": EXPECTED_COUNT,
            "worker_exit_code": worker_exit,
            "total_seconds": round(time.perf_counter() - t_start, 3),
            "speaker": SPEAKER,
            "cold_load_seconds": worker_payload.get("cold_load_seconds"),
            "generation_seconds": worker_payload.get("generation_seconds"),
            "cuda_peak_allocated_mb": worker_payload.get(
                "cuda_peak_allocated_mb"
            ),
            "cuda_peak_reserved_mb": worker_payload.get(
                "cuda_peak_reserved_mb"
            ),
            "samples": sample_rows,
            "worker": worker_payload,
            "cosyvoice_commit_expected": worker_payload.get(
                "cosyvoice_commit_expected"
            ),
            "cosyvoice_commit_actual": worker_payload.get(
                "cosyvoice_commit_actual"
            ),
            "matcha_commit_expected": worker_payload.get(
                "matcha_commit_expected"
            ),
            "matcha_commit_actual": worker_payload.get(
                "matcha_commit_actual"
            ),
            "wetext_commit_expected": worker_payload.get(
                "wetext_commit_expected"
            ),
            "wetext_commit_actual": worker_payload.get(
                "wetext_commit_actual"
            ),
            "model_revision_expected": worker_payload.get(
                "model_revision_expected"
            ),
            "model_revision_actual": worker_payload.get(
                "model_revision_actual"
            ),
            "model_bytes_expected": worker_payload.get(
                "model_bytes_expected"
            ),
            "model_bytes_actual": worker_payload.get("model_bytes_actual"),
        }
        atomic_write_json(parent_report_path, final_payload)
        print()
        print(
            f"完成: {success_count}/{EXPECTED_COUNT}；"
            f"报告: {_display_rel(parent_report_path)}"
        )
        return 0
    except Exception as exc:
        print(f"执行失败: {type(exc).__name__}")
        _publish_failure_report(reason=type(exc).__name__)
        return 1
    finally:
        if proc is not None and proc.poll() is None:
            terminate_process(proc)
        if tts_engine is not None:
            try:
                tts_engine.shutdown(stop_rvc=True)
            except Exception:
                logger.exception("TTS/RVC shutdown failed")
        # 失败/取消路径：尽力回收未清理的 staging（成功路径已在 return 前完成）
        if (
            staging is not None
            and temp_files is not None
            and not staging_cleaned
        ):
            try:
                cleanup_owned_staging(temp_files, staging)
                staging_cleaned = True
            except Exception as cleanup_exc:
                logger.debug(
                    "staging_cleanup_failed err=%s",
                    type(cleanup_exc).__name__,
                )
        if temp_files is not None:
            try:
                temp_files.shutdown(timeout=0.5)
            except Exception:
                logger.debug("temp_files_shutdown_failed")
        run_lock.release()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="CosyVoice-Instruct → Nahida RVC 表达型评估（默认 dry-run）"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="运行真实本地 CosyVoice 与 RVC（固定资产，无任意文本/路径）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.execute:
        return print_dry_run_plan()

    try:
        config = Config()
    except Exception as exc:
        print(f"配置加载失败: {type(exc).__name__}")
        return 1
    return run_execute(config)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(1)
