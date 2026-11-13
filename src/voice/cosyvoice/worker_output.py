# -*- coding: utf-8 -*-
"""Worker 侧输出路径校验与 CosyVoice generator 写 WAV。"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from src.core.paths import PROJECT_ROOT, TMP_DIR
from src.voice.cosyvoice.cross_file_identity import (
    capture_portable_file_identity,
    capture_portable_file_identity_fd,
    portable_identity_equal,
)
from src.voice.cosyvoice.path_trust import assert_trusted_repo_path, is_reparse_or_link
from src.voice.cosyvoice.protocol import MAX_AUDIO_BYTES, MAX_AUDIO_SECONDS

_WAV_PCM16_HEADER_RESERVE = 44


def _lexical_under_root(path: Path, root: Path) -> bool:
    if not path.is_absolute():
        return False
    if ".." in path.parts:
        return False
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    if ".." in rel.parts:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _same_open_identity(path: Path, fd: int) -> bool:
    try:
        id_p = capture_portable_file_identity(path)
        id_f = capture_portable_file_identity_fd(fd)
    except OSError:
        return False
    return portable_identity_equal(id_p.dev, id_p.ino, id_f.dev, id_f.ino)


def validate_worker_output_path(
    path_str: str,
    dev: int,
    ino: int,
    *,
    repo_root: Path | None = None,
    tmp_root: Path | None = None,
) -> Optional[Path]:
    """词法校验 ``data/tmp`` 内普通文件且 dev/ino 与父登记一致（不先 resolve 跟随链接）。"""
    repo = repo_root or PROJECT_ROOT
    tmp = tmp_root or TMP_DIR
    try:
        lexical = Path(path_str)
    except (TypeError, ValueError):
        return None
    if not _lexical_under_root(lexical, tmp):
        return None
    try:
        assert_trusted_repo_path(repo, lexical, kind="file")
    except PermissionError:
        return None
    if is_reparse_or_link(lexical):
        return None
    if not lexical.is_file():
        return None
    if not os.path.isfile(lexical):
        return None
    try:
        actual = capture_portable_file_identity(lexical)
    except OSError:
        return None
    if not portable_identity_equal(actual.dev, actual.ino, dev, ino):
        return None
    return lexical


def collect_inference_speech(
    chunks: Iterable[dict[str, Any]],
    *,
    sample_rate: int,
    identity_check: Callable[[], bool] | None = None,
) -> Any:
    """消费 ``inference_instruct`` generator，累计有界并拼接张量。"""
    import numpy as np

    if sample_rate <= 0 or not math.isfinite(float(sample_rate)):
        raise RuntimeError("invalid_sample_rate")
    total_samples = 0
    max_samples = int(MAX_AUDIO_SECONDS * float(sample_rate))
    parts: list[Any] = []
    for item in chunks:
        if identity_check is not None and not identity_check():
            raise RuntimeError("output_identity_changed")
        speech = item.get("tts_speech")
        if speech is None:
            raise RuntimeError("missing_tts_speech")
        if hasattr(speech, "numel"):
            chunk_samples = int(speech.numel())
        elif hasattr(speech, "shape"):
            shape = getattr(speech, "shape", ())
            chunk_samples = 1
            for dim in shape:
                chunk_samples *= int(dim)
        else:
            try:
                chunk_samples = len(speech)  # type: ignore[arg-type]
            except TypeError:
                chunk_samples = 0
        if chunk_samples <= 0:
            raise RuntimeError("empty_chunk")
        total_samples += chunk_samples
        if total_samples > max_samples:
            raise RuntimeError("audio_too_long")
        est_bytes = total_samples * 2 + _WAV_PCM16_HEADER_RESERVE
        if est_bytes > MAX_AUDIO_BYTES:
            raise RuntimeError("audio_too_large")
        if hasattr(speech, "detach"):
            arr = speech.detach().cpu().numpy()
        else:
            arr = np.asarray(speech)
        arr = np.asarray(arr, dtype=np.float32).reshape(-1)
        if arr.size <= 0:
            raise RuntimeError("empty_chunk")
        if not np.isfinite(arr).all():
            raise RuntimeError("non_finite_audio")
        parts.append(arr)
    if not parts:
        raise RuntimeError("empty_inference")
    if len(parts) == 1:
        return parts[0]
    return np.concatenate(parts, axis=0)


def write_wav_to_owned_fd(
    path: Path,
    fd: int,
    speech: Any,
    sample_rate: int,
    *,
    identity_check: Callable[[], bool] | None = None,
) -> int:
    """写入已打开的输出句柄（同一 inode），不使用 replace 或按路径重开。"""
    import numpy as np
    import soundfile as sf

    if not _same_open_identity(path, fd):
        raise RuntimeError("output_identity_changed")
    if identity_check is not None and not identity_check():
        raise RuntimeError("output_identity_changed")
    if hasattr(speech, "detach"):
        arr = speech.detach().cpu().numpy()
    else:
        arr = np.asarray(speech)
    arr = np.asarray(arr, dtype=np.float32).reshape(-1)
    if arr.size <= 0 or not np.isfinite(arr).all():
        raise RuntimeError("empty_audio")
    est_bytes = arr.size * 2 + _WAV_PCM16_HEADER_RESERVE
    if est_bytes > MAX_AUDIO_BYTES:
        raise RuntimeError("audio_too_large")
    with os.fdopen(fd, "r+b", closefd=True) as f:
        if not _same_open_identity(path, f.fileno()):
            raise RuntimeError("output_identity_changed")
        f.seek(0)
        f.truncate(0)
        sf.write(f, arr, int(sample_rate), format="WAV", subtype="PCM_16")
        f.flush()
        size = f.tell()
    if size <= 0 or size > MAX_AUDIO_BYTES:
        raise RuntimeError("audio_too_large")
    st = path.stat()
    if int(st.st_size) != int(size):
        raise RuntimeError("audio_too_large")
    duration_sec = float(arr.size) / float(sample_rate) if sample_rate > 0 else 0.0
    if duration_sec <= 0.0 or duration_sec > MAX_AUDIO_SECONDS:
        raise RuntimeError("audio_too_long")
    return int(size)


def run_inference_instruct_to_wav(
    model: Any,
    *,
    text: str,
    speaker: str,
    instruct: str,
    out_path: Path,
    redirect_stdout: Any,
    expected_dev: int,
    expected_ino: int,
) -> int:
    sample_rate = int(getattr(model, "sample_rate", 0) or 0)
    fd = os.open(str(out_path), os.O_RDWR | os.O_BINARY)
    try:
        opened = capture_portable_file_identity_fd(fd)
    except OSError:
        os.close(fd)
        raise RuntimeError("output_identity_changed")
    if not portable_identity_equal(
        opened.dev, opened.ino, expected_dev, expected_ino
    ):
        os.close(fd)
        raise RuntimeError("output_identity_changed")

    def _identity_ok() -> bool:
        return _same_open_identity(out_path, fd)

    if not _identity_ok():
        os.close(fd)
        raise RuntimeError("output_identity_changed")
    try:
        with redirect_stdout():
            gen = model.inference_instruct(text, speaker, instruct, stream=False)
            speech = collect_inference_speech(
                gen,
                sample_rate=sample_rate,
                identity_check=_identity_ok,
            )
        if not _identity_ok():
            raise RuntimeError("output_identity_changed")
        return write_wav_to_owned_fd(
            out_path,
            fd,
            speech,
            sample_rate,
            identity_check=_identity_ok,
        )
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
