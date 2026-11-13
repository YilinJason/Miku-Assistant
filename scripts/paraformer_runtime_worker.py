# -*- coding: utf-8 -*-
"""
Paraformer 生产 ASR runtime worker（仅 .venv-asr + LOCAL_ASR_WORKER 启动）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PROTOCOL_STDOUT = sys.stdout


def _early_join_owned_job() -> None:
    from src.core.owned_worker_job import worker_entry_join_owned_job

    worker_entry_join_owned_job()


def _emit(obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    _PROTOCOL_STDOUT.write(line + "\n")
    _PROTOCOL_STDOUT.flush()


def _redirect_third_party_stdout():
    from src.voice.cosyvoice.worker_runtime import redirect_stdout_to_stderr

    return redirect_stdout_to_stderr()


def _load_model(model_dir: Path, *, ncpu: int):
    from src.voice.paraformer.network_guard import install_outbound_network_guard

    install_outbound_network_guard()
    import torch
    from funasr import AutoModel

    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    with torch.inference_mode():
        return AutoModel(
            model=str(model_dir),
            device="cpu",
            ncpu=ncpu,
            disable_update=True,
            disable_pbar=True,
            trust_remote_code=False,
        )


def _normalize_transcribe_text(result: Any) -> str:
    if not result:
        return ""
    item = result[0]
    if not isinstance(item, dict):
        return ""
    text = item.get("text")
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    return stripped if stripped else ""


def _transcribe(model, pcm: bytes, sample_rate: int) -> str:
    import numpy as np
    import torch

    if sample_rate != 16000:
        raise ValueError("sample_rate")
    arr = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
    with torch.inference_mode(), _redirect_third_party_stdout():
        result = model.generate(input=arr, batch_size=1)
    return _normalize_transcribe_text(result)


def main() -> int:
    _early_join_owned_job()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--cpu-threads", type=int, required=True)
    args, _ = parser.parse_known_args()
    if args.cpu_threads < 1 or args.cpu_threads > 4:
        _emit({"schema": 1, "msg": "error", "error": "schema_invalid"})
        return 2
    from src.core.parent_process import parent_process_is_alive

    parent_pid = int(args.parent_pid)
    if parent_pid <= 0 or not parent_process_is_alive(parent_pid):
        return 2

    def _watch() -> None:
        while parent_process_is_alive(parent_pid):
            time.sleep(0.5)
        os._exit(3)

    threading.Thread(target=_watch, name="ParaformerParentWatchdog", daemon=True).start()

    model_dir_raw = os.environ.get("MIKU_PARAFORMER_MODEL_DIR", "")
    repo_root_raw = os.environ.get("MIKU_REPO_ROOT", "")
    if not model_dir_raw or not repo_root_raw:
        _emit({"schema": 1, "msg": "error", "error": "asset_invalid"})
        return 2
    model_dir = Path(model_dir_raw)
    repo_root = Path(repo_root_raw)
    from src.voice.paraformer.assets import verify_pinned_assets

    try:
        verify_pinned_assets(model_dir=model_dir, repo_root=repo_root)
    except RuntimeError:
        _emit({"schema": 1, "msg": "error", "error": "asset_invalid"})
        return 2

    inflight = {"id": "", "cancel": False}
    lock = threading.Lock()
    try:
        with _redirect_third_party_stdout():
            model = _load_model(model_dir, ncpu=args.cpu_threads)
    except Exception:
        _emit({"schema": 1, "msg": "error", "error": "worker_start_failed"})
        return 2

    _emit({"schema": 1, "msg": "ready", "status": "ready"})

    from src.voice.cosyvoice.bounded_line import BoundedLineTooLongError, read_bounded_line
    from src.voice.paraformer.protocol import (
        MAX_INBOUND_JSON_LINE_CHARS,
        MSG_TRANSCRIBE_RESULT,
        REQ_CANCEL,
        REQ_SHUTDOWN,
        REQ_TRANSCRIBE,
        STATUS_EMPTY,
        STATUS_FAILED,
        STATUS_OK,
        decode_transcribe_pcm,
        parse_json_line,
        validate_inbound_request,
    )

    while True:
        try:
            line = read_bounded_line(sys.stdin, MAX_INBOUND_JSON_LINE_CHARS)
        except BoundedLineTooLongError:
            _emit({"schema": 1, "msg": "error", "error": "protocol_error"})
            continue
        if line is None:
            break
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = parse_json_line(stripped + "\n", max_chars=MAX_INBOUND_JSON_LINE_CHARS)
            validate_inbound_request(msg)
        except ValueError:
            _emit({"schema": 1, "msg": "error", "error": "schema_invalid"})
            continue
        req = msg.get("req")
        if req == REQ_SHUTDOWN:
            _emit({"schema": 1, "msg": "shutdown_ack"})
            return 0
        if req == REQ_CANCEL:
            rid = msg.get("request_id")
            with lock:
                if rid == inflight["id"]:
                    inflight["cancel"] = True
            continue
        if req != REQ_TRANSCRIBE:
            continue
        rid = msg["request_id"]
        with lock:
            if inflight["id"]:
                _emit(
                    {
                        "schema": 1,
                        "msg": "error",
                        "request_id": rid,
                        "error": "request_busy",
                    }
                )
                continue
            inflight["id"] = rid
            inflight["cancel"] = False
        try:
            pcm = decode_transcribe_pcm(msg)
        except ValueError:
            _emit(
                {
                    "schema": 1,
                    "msg": "error",
                    "request_id": rid,
                    "error": "pcm_invalid",
                }
            )
            with lock:
                inflight["id"] = ""
            continue
        with lock:
            if inflight["cancel"]:
                _emit(
                    {
                        "schema": 1,
                        "msg": "error",
                        "request_id": rid,
                        "error": "cancelled",
                    }
                )
                inflight["id"] = ""
                continue
        try:
            text = _transcribe(model, pcm, 16000)
        except Exception:
            _emit(
                {
                    "schema": 1,
                    "msg": "transcribe_result",
                    "request_id": rid,
                    "status": STATUS_FAILED,
                    "text": "",
                }
            )
            with lock:
                inflight["id"] = ""
            continue
        status = STATUS_OK if text else STATUS_EMPTY
        _emit(
            {
                "schema": 1,
                "msg": MSG_TRANSCRIBE_RESULT,
                "request_id": rid,
                "status": status,
                "text": text[:4096],
            }
        )
        with lock:
            inflight["id"] = ""
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
