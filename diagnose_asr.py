# -*- coding: utf-8 -*-
"""
Paraformer 固定音频诊断（无麦克风 / 无 TTS·RVC·Ollama / 无正式记忆）。

默认 dry-run；``--execute`` 使用生产 ParaformerWorkerClient 与 STTEngine 包装
对只读 WAV 做有界推理与资源观测。报告写入 ``tmp/``（exclusive 文件名）。

用法::

    py diagnose_asr.py
    py diagnose_asr.py --execute

监督入口在 ``--execute`` 时调用 ``worker_entry_join_owned_job``（经 maybe_join_owned_job）。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.host_memory import read_host_memory_snapshot  # noqa: E402
from src.core.paths import PROJECT_ROOT, TMP_DIR as SHARED_MIKU_WAV_DIR  # noqa: E402
from src.voice.paraformer.client import ParaformerWorkerClient  # noqa: E402
from src.voice.paraformer.preparation import (  # noqa: E402
    DIAGNOSTIC_HARD_DEADLINE_SEC,
    DIAGNOSTIC_PID_SETTLE_SEC,
    FIXED_DIAGNOSTIC_CLIP_RELS,
    MAX_DIAGNOSTIC_CLIPS,
    MODEL_REVISION,
    SCHEMA_VERSION,
    SETUP_USER_HINT,
    WARM_DIAGNOSTIC_REPEATS,
    build_diagnostic_safety_policy,
    check_host_memory_for_setup,
    collect_tree_pids,
    finalize_diagnostic_verdict,
    load_pcm_16k_mono,
    maybe_join_owned_job,
    min_transcribe_budget_sec,
    percentile,
    resolve_diagnostic_clips,
    run_check,
    snapshot_shared_miku_wavs,
    stt_recognition_success,
    transcribe_outcome_success,
    try_tree_sample,
    verify_nahida_reference,
    verify_pids_gone_with_settle,
)
from src.voice.paraformer.settings import ParaformerSettings  # noqa: E402
from src.voice.stt import STTEngine  # noqa: E402

logger = logging.getLogger("diagnose_asr")


def _configure_cli_stdio_and_logging() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                try:
                    reconfigure(encoding="utf-8")
                except Exception:
                    pass
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

# 诊断报告仅写入仓库 ``tmp/``；共享 miku_*.wav 元数据观测使用 ``data/tmp/``。
DIAGNOSTIC_REPORT_DIR = PROJECT_ROOT / "tmp"


def print_dry_run_plan() -> int:
    report = run_check(PROJECT_ROOT)
    print("=" * 60)
    print("Paraformer 固定音频诊断（dry-run）")
    print("=" * 60)
    print()
    print("本模式不启动 worker、不读麦克风、不写共享 data/tmp。")
    print("真实验收请显式传入 --execute（Codex 180s 硬监督）。")
    print("日志不写入语音转写；详细结果仅进入 tmp 报告。")
    print()
    print(f"schema_version: {SCHEMA_VERSION}")
    print(f"model_revision: {MODEL_REVISION}")
    print(f"max_clips: {MAX_DIAGNOSTIC_CLIPS}")
    print(f"hard_deadline_sec: {DIAGNOSTIC_HARD_DEADLINE_SEC}")
    print()
    print("固定片段（存在则参与，最多 12 路）：")
    for rel in FIXED_DIAGNOSTIC_CLIP_RELS:
        print(f"  - {rel}")
    print()
    print("准备状态：")
    for item in report.components:
        mark = "OK" if item.ok else "MISSING"
        print(f"  [{mark}] {item.name}")
    if not report.ok:
        print()
        print(SETUP_USER_HINT)
    print()
    print("提示: py diagnose_asr.py --execute")
    return 0


def _exclusive_report_path() -> Path:
    DIAGNOSTIC_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"paraformer_asr_diagnostic_{uuid.uuid4().hex}.json"
    return DIAGNOSTIC_REPORT_DIR / name


def _write_report(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def run_execute(
    *,
    clock: Callable[[], float] = time.monotonic,
    perf_counter: Callable[[], float] = time.perf_counter,
    client_cls: type = ParaformerWorkerClient,
    stt_engine_cls: type = STTEngine,
) -> int:
    maybe_join_owned_job()
    started = clock()
    deadline = started + DIAGNOSTIC_HARD_DEADLINE_SEC
    report_path = _exclusive_report_path()
    wav_before = snapshot_shared_miku_wavs(SHARED_MIKU_WAV_DIR)
    mem_before = read_host_memory_snapshot()
    nahida = verify_nahida_reference(PROJECT_ROOT)

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model_revision": MODEL_REVISION,
        "complete": False,
        "nahida_reference": {"ok": nahida.ok, "detail": nahida.detail},
        "shared_miku_wav_count_before": len(wav_before),
        "inference_all_ok": False,
        "wrapper_ok": False,
        "report_written": False,
        "shutdown_complete": False,
        "stt_engine_shutdown_complete": False,
        "shared_wav_metadata_unchanged": False,
        "owned_pids_gone": False,
        "hard_deadline_hit": False,
    }

    client: Optional[ParaformerWorkerClient] = None
    stt_engine: Optional[STTEngine] = None
    tree_samples: List[Dict[str, Any]] = []
    warm_latencies_ms: List[float] = []
    cold_ms: Optional[float] = None
    clip_rows: List[Dict[str, Any]] = []
    launcher_pids: List[int] = []
    exit_code = 1

    def _remaining() -> float:
        return deadline - clock()

    def _deadline_exceeded() -> bool:
        if _remaining() <= 0:
            payload["hard_deadline_hit"] = True
            return True
        return False

    try:
        policy, pol_err = build_diagnostic_safety_policy(PROJECT_ROOT)
        if policy is None:
            payload["failure"] = pol_err
        else:
            prep = run_check(PROJECT_ROOT)
            if not prep.ok:
                payload["failure"] = "not_prepared"
                payload["check"] = [
                    {"name": c.name, "ok": c.ok, "detail": c.detail}
                    for c in prep.components
                ]
            elif not check_host_memory_for_setup().ok:
                payload["failure"] = "host_memory"
            elif not nahida.ok:
                payload["failure"] = "nahida_reference"
            else:
                clips = resolve_diagnostic_clips(PROJECT_ROOT)
                if not clips:
                    payload["failure"] = "no_clips"
                else:
                    settings = ParaformerSettings(cpu_threads=2)
                    client = client_cls(settings, policy)
                    all_ok = True

                    for index, clip in enumerate(clips):
                        if _deadline_exceeded():
                            all_ok = False
                            payload["failure"] = payload.get("failure") or "hard_deadline"
                            break
                        rel = clip.relative_to(PROJECT_ROOT).as_posix()
                        cold = index == 0
                        budget = min_transcribe_budget_sec(cold=cold)
                        if _remaining() < budget:
                            all_ok = False
                            payload["hard_deadline_hit"] = True
                            payload["failure"] = payload.get("failure") or "deadline_budget"
                            break
                        try:
                            pcm, duration = load_pcm_16k_mono(clip)
                        except Exception as exc:  # noqa: BLE001
                            all_ok = False
                            clip_rows.append(
                                {
                                    "path": rel,
                                    "ok": False,
                                    "stage": type(exc).__name__,
                                }
                            )
                            continue
                        t1 = perf_counter()
                        outcome = client.transcribe(pcm)
                        elapsed_ms = (perf_counter() - t1) * 1000.0
                        ok = transcribe_outcome_success(outcome)
                        if not ok:
                            all_ok = False
                        if index == 0:
                            cold_ms = elapsed_ms
                        else:
                            warm_latencies_ms.append(elapsed_ms)
                        clip_rows.append(
                            {
                                "path": rel,
                                "ok": ok,
                                "duration_sec": round(duration, 3),
                                "latency_ms": round(elapsed_ms, 2),
                                "error_label": getattr(outcome, "error_label", "") or "",
                                "audit_text_norm": (
                                    outcome.text.strip()
                                    if ok and isinstance(outcome.text, str)
                                    else ""
                                ),
                            }
                        )
                        launcher = getattr(client, "_proc", None)
                        launcher_pid = getattr(launcher, "pid", None)
                        if launcher_pid:
                            pid = int(launcher_pid)
                            launcher_pids.append(pid)
                            sample = try_tree_sample(pid, PROJECT_ROOT)
                            if sample is not None and len(tree_samples) < 64:
                                tree_samples.append({"phase": f"clip_{index}", **sample})
                        del launcher

                    if all_ok and not _deadline_exceeded():
                        pcm_warm, _ = load_pcm_16k_mono(clips[0])
                        for rep in range(WARM_DIAGNOSTIC_REPEATS):
                            if _deadline_exceeded():
                                all_ok = False
                                payload["failure"] = payload.get("failure") or "hard_deadline"
                                break
                            if _remaining() < min_transcribe_budget_sec(cold=False):
                                all_ok = False
                                payload["failure"] = payload.get("failure") or "deadline_budget"
                                break
                            t1 = perf_counter()
                            warm_out = client.transcribe(pcm_warm)
                            elapsed_ms = (perf_counter() - t1) * 1000.0
                            if not transcribe_outcome_success(warm_out):
                                all_ok = False
                            else:
                                warm_latencies_ms.append(elapsed_ms)

                    wrapper_ok = False
                    if all_ok and not _deadline_exceeded():
                        if _remaining() < min_transcribe_budget_sec(cold=False):
                            all_ok = False
                            payload["failure"] = payload.get("failure") or "deadline_budget"
                        else:
                            import speech_recognition as sr

                            stt_engine = stt_engine_cls(
                                engine="paraformer",
                                safety_policy=policy,
                                paraformer_settings=settings,
                            )
                            stt_engine._recognizer = sr.Recognizer()
                            stt_engine._paraformer_client = client
                            pcm0, _ = load_pcm_16k_mono(clips[0])
                            audio = sr.AudioData(pcm0, 16000, 2)
                            result = stt_engine._recognize_paraformer_result(audio)
                            wrapper_ok = stt_recognition_success(result)
                            if not wrapper_ok:
                                all_ok = False

                    payload["inference_all_ok"] = all_ok is True
                    payload["wrapper_ok"] = wrapper_ok is True
                    payload["cold_startup_ms"] = round(cold_ms or 0.0, 2)
                    payload["warm_p50_ms"] = percentile(warm_latencies_ms, 50)
                    payload["warm_p95_ms"] = percentile(warm_latencies_ms, 95)
                    payload["clip_count"] = len(clip_rows)
                    payload["clips"] = clip_rows
                    payload["tree_samples"] = tree_samples
    except Exception as exc:  # noqa: BLE001
        payload["failure"] = payload.get("failure") or type(exc).__name__
        payload["failure_detail"] = str(exc)
        logger.debug("diagnostic_execute_error", exc_info=True)
    finally:
        if client is not None:
            try:
                payload["shutdown_complete"] = client.shutdown() is True
            except Exception:
                payload["shutdown_complete"] = False
        if stt_engine is not None:
            try:
                payload["stt_engine_shutdown_complete"] = stt_engine.shutdown() is True
            except Exception:
                payload["stt_engine_shutdown_complete"] = False
        wav_after = snapshot_shared_miku_wavs(SHARED_MIKU_WAV_DIR)
        mem_after = read_host_memory_snapshot()
        payload["shared_miku_wav_count_after"] = len(wav_after)
        payload["shared_wav_metadata_unchanged"] = wav_before == wav_after
        payload["memory_before"] = (
            vars(mem_before) if mem_before is not None else None
        )
        payload["memory_after"] = vars(mem_after) if mem_after is not None else None
        payload["elapsed_sec"] = round(clock() - started, 3)
        payload["report"] = report_path.relative_to(PROJECT_ROOT).as_posix()
        tree_pids = collect_tree_pids(
            tree_samples,
            seed=launcher_pids,
        )
        payload["owned_pids"] = tree_pids
        payload["owned_pids_gone"] = (
            verify_pids_gone_with_settle(
                tree_pids,
                clock=clock,
                settle_sec=DIAGNOSTIC_PID_SETTLE_SEC,
            )
            is True
        )
        payload["pid_settle_sec"] = DIAGNOSTIC_PID_SETTLE_SEC
        complete, exit_code = finalize_diagnostic_verdict(
            payload, require_report_written=False
        )
        report_payload = dict(payload)
        report_payload["complete"] = complete
        payload["complete"] = complete
        try:
            report_payload["report_written"] = True
            _write_report(report_path, report_payload)
            payload["report_written"] = True
        except Exception:
            payload["report_written"] = False
            payload["complete"] = False
            exit_code = 1
            logger.debug("report_write_failed", exc_info=True)
        if payload.get("report_written") is True:
            complete, exit_code = finalize_diagnostic_verdict(payload)
            payload["complete"] = complete
        else:
            payload["complete"] = False
            exit_code = 1
        print(
            json.dumps(
                {k: v for k, v in payload.items() if k != "clips"},
                ensure_ascii=False,
            )
        )
    return exit_code


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_cli_stdio_and_logging()
    parser = argparse.ArgumentParser(add_help=True)
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--execute", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.execute:
        return run_execute()
    return print_dry_run_plan()


if __name__ == "__main__":
    raise SystemExit(main())
