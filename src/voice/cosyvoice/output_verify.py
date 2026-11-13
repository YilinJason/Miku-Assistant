# -*- coding: utf-8 -*-
"""父进程侧 CosyVoice 输出 WAV 复验（单句柄、有限样本、有界时长/字节）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.core.paths import PROJECT_ROOT, TMP_DIR
from src.voice.cosyvoice.cross_file_identity import capture_portable_file_identity
from src.voice.cosyvoice.protocol import MAX_AUDIO_BYTES, MAX_AUDIO_SECONDS
from src.voice.cosyvoice.worker_output import validate_worker_output_path

_VERIFY_CHUNK_SAMPLES = 8192


def _identity_matches(identity: Any, st: os.stat_result) -> bool:
    return int(st.st_dev) == int(identity.st_dev) and int(st.st_ino) == int(
        identity.st_ino
    )


def verify_synth_output(
    key: str,
    identity: Any,
    nbytes: int,
    *,
    repo_root: Path | None = None,
    tmp_root: Path | None = None,
) -> bool:
    """在已登记 dev/ino 上通过单一只读句柄复验 WAV（禁止按路径多次重开）。"""
    repo = repo_root or PROJECT_ROOT
    tmp = tmp_root or TMP_DIR
    if (
        nbytes <= 0
        or nbytes > MAX_AUDIO_BYTES
        or identity is None
    ):
        return False
    fd = -1
    try:
        try:
            portable = capture_portable_file_identity(key)
        except OSError:
            return False
        if (
            validate_worker_output_path(
                key,
                portable.dev,
                portable.ino,
                repo_root=repo,
                tmp_root=tmp,
            )
            is None
        ):
            return False
        fd = os.open(key, os.O_RDONLY | os.O_BINARY)
    except OSError:
        return False
    try:
        st_fd = os.fstat(fd)
        if not _identity_matches(identity, st_fd):
            return False
        if int(st_fd.st_size) != int(nbytes) or int(st_fd.st_size) <= 0:
            return False
        import numpy as np
        import soundfile as sf

        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1
            if not _identity_matches(identity, os.fstat(handle.fileno())):
                return False
            with sf.SoundFile(handle) as snd:
                rate = int(snd.samplerate)
                frames = int(snd.frames)
                channels = int(snd.channels)
                if (
                    rate <= 0
                    or frames <= 0
                    or channels != 1
                    or float(frames) / float(rate) > MAX_AUDIO_SECONDS
                ):
                    return False
                if float(frames) / float(rate) <= 0.0:
                    return False
                remaining = frames
                while remaining > 0:
                    block = min(_VERIFY_CHUNK_SAMPLES, remaining)
                    data = snd.read(block, dtype="float32", always_2d=False)
                    arr = np.asarray(data, dtype=np.float32).reshape(-1)
                    if arr.size <= 0 or not np.isfinite(arr).all():
                        return False
                    remaining -= int(arr.size)
        try:
            st_path = os.stat(key, follow_symlinks=False)
        except OSError:
            return False
        if not _identity_matches(identity, st_path):
            return False
        if int(st_path.st_size) != int(nbytes):
            return False
        return True
    except Exception:
        return False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
