# -*- coding: utf-8 -*-
"""Paraformer 固定资产布局与官方 pin 校验（stdlib only）。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Tuple

from src.voice.cosyvoice.path_trust import assert_trusted_repo_path, is_reparse_or_link

MODEL_REVISION = "d7811ee3ac581fbcfdeb37c98c6ba674028433dc"
HF_REPO_ID = "funasr/paraformer-zh"

ASR_PYTHON_REL = ".venv-asr/Scripts/python.exe"
MODEL_DIR_REL = "vendor/paraformer-zh"
RUNTIME_WORKER_SCRIPT_REL = "scripts/paraformer_runtime_worker.py"

MODEL_REQUIRED_FILES: Tuple[str, ...] = (
    "model.pt",
    "config.yaml",
    "configuration.json",
    "am.mvn",
    "tokens.json",
    "seg_dict",
)

OFFICIAL_MODEL_PT_SHA256 = (
    "5bba782a5e9196166233b9ab12ba04cadff9ef9212b4ff6153ed9290ff679025"
)

# Hugging Face revision d7811ee… 官方 Git blob ID（非 LFS 小文件）
OFFICIAL_GIT_BLOB_SHA1: Dict[str, str] = {
    "am.mvn": "681910cd1ab6458b61474cdbf0d1ac5e810f7b0d",
    "config.yaml": "c0a9f403c8e62017afcd765e13006617f3e43471",
    "configuration.json": "6f77310bef84268f6c82ca0adf36a43d695a50ea",
    "seg_dict": "fb19e07f2870b09b08226a42d7d5d6d26a3f3a16",
    "tokens.json": "f8bc9c31ab1ba458aee5423fa24164958153f923",
}

# Hugging Face revision d7811ee… 官方字节大小（setup/runtime 均 fail-closed）
OFFICIAL_FILE_BYTES: Dict[str, int] = {
    "model.pt": 880_502_012,
    "config.yaml": 2509,
    "configuration.json": 472,
    "am.mvn": 11203,
    "tokens.json": 93676,
    "seg_dict": 8_287_834,
}

OFFICIAL_README_BYTES = 3537
OFFICIAL_EXAMPLE_BYTES = 417_742

SETUP_USER_HINT = (
    "本地 Paraformer 尚未就绪：请使用项目文档中的 ASR 环境准备步骤，"
    "并确认 vendor/paraformer-zh 官方 pin 资产与 .venv-asr 已安装。"
)


def fixed_layout_paths(repo_root: Path) -> Dict[str, Path]:
    root = repo_root.resolve()
    return {
        "repo_root": root,
        "asr_python": root / ASR_PYTHON_REL,
        "model_dir": root / MODEL_DIR_REL,
        "runtime_worker_script": root / RUNTIME_WORKER_SCRIPT_REL,
    }


def _assert_ordinary_trusted_file(repo_root: Path, path: Path) -> None:
    assert_trusted_repo_path(repo_root, path, kind="file")
    resolved = path.resolve()
    if is_reparse_or_link(resolved):
        raise PermissionError("reparse_escape")
    if not resolved.is_file() or resolved.is_symlink():
        raise PermissionError("not_regular_file")


def _assert_ordinary_trusted_dir(repo_root: Path, path: Path) -> None:
    assert_trusted_repo_path(repo_root, path, kind="dir")
    resolved = path.resolve()
    if is_reparse_or_link(resolved):
        raise PermissionError("reparse_escape")
    if not resolved.is_dir() or resolved.is_symlink():
        raise PermissionError("not_regular_dir")


def revalidate_fixed_layout_paths(repo_root: Path) -> Dict[str, Path]:
    layout = fixed_layout_paths(repo_root)
    root = layout["repo_root"]
    for key in ("asr_python", "runtime_worker_script"):
        _assert_ordinary_trusted_file(root, layout[key])
    _assert_ordinary_trusted_dir(root, layout["model_dir"])
    model_dir = layout["model_dir"]
    for rel in MODEL_REQUIRED_FILES:
        candidate = model_dir / rel
        _assert_ordinary_trusted_file(root, candidate)
    return layout


def _sha256_file(path: Path, *, expected_size: int | None = None) -> str:
    if expected_size is not None:
        size = path.stat().st_size
        if size != expected_size:
            raise RuntimeError("size_mismatch")
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _git_blob_sha1(path: Path, *, expected_size: int) -> str:
    if path.stat().st_size != expected_size:
        raise RuntimeError("blob_size_mismatch")
    digest = hashlib.sha1()
    digest.update(f"blob {expected_size}\0".encode("ascii"))
    with path.open("rb") as fh:
        remaining = expected_size
        while remaining > 0:
            chunk = fh.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    if remaining != 0:
        raise RuntimeError("blob_size_mismatch")
    return digest.hexdigest()


def verify_pinned_assets(*, model_dir: Path, repo_root: Path | None = None) -> None:
    from src.voice.paraformer.config_trust import assert_model_configs_safe

    root = (repo_root or model_dir.parent.parent).resolve()
    _assert_ordinary_trusted_dir(root, model_dir.resolve())
    for rel in MODEL_REQUIRED_FILES:
        target = (model_dir / rel).resolve()
        _assert_ordinary_trusted_file(root, target)
        expected_size = OFFICIAL_FILE_BYTES.get(rel)
        if expected_size is None:
            raise RuntimeError("size_pin_missing")
        if rel == "model.pt":
            digest = _sha256_file(target, expected_size=expected_size)
            if digest.lower() != OFFICIAL_MODEL_PT_SHA256.lower():
                raise RuntimeError("model_pt_mismatch")
        else:
            expected = OFFICIAL_GIT_BLOB_SHA1.get(rel)
            if not expected:
                raise RuntimeError("pin_missing")
            if _git_blob_sha1(target, expected_size=expected_size) != expected:
                raise RuntimeError("blob_mismatch")
    assert_model_configs_safe(model_dir)
