# -*- coding: utf-8 -*-
"""Runtime worker 固定资产路径：先词法校验再 resolve。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.voice.cosyvoice.assets import (
    MATCHA_DIR_REL,
    MODEL_DIR_REL,
    RUNTIME_MODEL_LOCK_REL,
    SUITE_CACHE_REL,
    VENDOR_COSYVOICE_REL,
    WETEXT_DIR_REL,
)
from src.voice.cosyvoice.path_trust import assert_trusted_repo_path


def validate_worker_lexical_layout(repo_root: Path) -> Optional[str]:
    """
    在 resolve / 模型加载前校验固定布局词法路径。

    成功返回 ``None``；失败返回固定 ``ERR_*`` 标签（由调用方发协议错误）。
    """
    from src.voice.cosyvoice.protocol import ERR_ASSET_PIN, ERR_OUTPUT_REJECTED

    repo = Path(repo_root)
    rels = (
        VENDOR_COSYVOICE_REL,
        MATCHA_DIR_REL,
        MODEL_DIR_REL,
        WETEXT_DIR_REL,
        SUITE_CACHE_REL,
        RUNTIME_MODEL_LOCK_REL,
    )
    for rel in rels:
        target = repo / rel
        try:
            if rel == RUNTIME_MODEL_LOCK_REL:
                assert_trusted_repo_path(repo, target.parent, kind="dir")
                assert_trusted_repo_path(repo, target)
            else:
                assert_trusted_repo_path(repo, target, kind="dir")
        except PermissionError:
            if rel == RUNTIME_MODEL_LOCK_REL:
                return ERR_OUTPUT_REJECTED
            return ERR_ASSET_PIN
    return None


def resolve_trusted_worker_paths(repo_root: Path) -> dict[str, Path]:
    """在 ``validate_worker_lexical_layout`` 成功后 resolve 固定路径。"""
    repo = Path(repo_root)
    layout_err = validate_worker_lexical_layout(repo)
    if layout_err is not None:
        raise PermissionError(layout_err)
    cosy = (repo / VENDOR_COSYVOICE_REL).resolve()
    matcha = (repo / MATCHA_DIR_REL).resolve()
    model = (repo / MODEL_DIR_REL).resolve()
    wetext = (repo / WETEXT_DIR_REL).resolve()
    cache = (repo / SUITE_CACHE_REL).resolve()
    lock = (repo / RUNTIME_MODEL_LOCK_REL).resolve()
    for p in (cosy, matcha, model, wetext, cache, lock.parent):
        assert_trusted_repo_path(repo, p, kind="dir")
    assert_trusted_repo_path(repo, lock)
    return {
        "cosy_root": cosy,
        "matcha_root": matcha,
        "model_dir": model,
        "wetext_dir": wetext,
        "cache_root": cache,
        "lock_path": lock,
    }
