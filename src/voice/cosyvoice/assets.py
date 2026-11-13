# -*- coding: utf-8 -*-
"""
CosyVoice 固定资产布局与 pin 校验（stdlib only）。

评估套件与生产专用能力共用；不 import torch、不读主程序配置。
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

# (label, text, instruct) — 顺序固定
SAMPLE_CASES: Tuple[Tuple[str, str, str], ...] = (
    (
        "warm_companion",
        "早上好，今天也一起慢慢来。",
        "用温柔、亲近、自然的语气说这句话，语速稍慢，并有轻微的情绪起伏。",
    ),
    (
        "caring_question",
        "你听起来有点累，需要先休息一下吗？",
        "用关心、体贴、轻柔的语气询问，结尾自然上扬。",
    ),
    (
        "pleasant_surprise",
        "你真的做到了？！太棒了！",
        "用真诚惊喜、开心而不尖锐的语气表达。",
    ),
    (
        "encouragement",
        "没关系，我们再试一次就好。",
        "用鼓励、可靠、有陪伴感的语气表达，保持自然停顿。",
    ),
    (
        "soothing",
        "不用着急……我会一直陪着你。",
        "用安慰、平静、柔和的语气表达，语速较慢。",
    ),
    (
        "serious_calm",
        "这一步需要谨慎处理，我们先确认清楚。",
        "用认真、冷静、清晰的语气表达，不要夸张。",
    ),
)

EXPECTED_COUNT = len(SAMPLE_CASES)
SPEAKER = "中文女"
SCHEMA_VERSION = 1

# 已由 Codex 真实验证的固定上游版本（不得由 CLI 覆盖）
COSYVOICE_COMMIT = "074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
MATCHA_COMMIT = "dd9105b34bf2be2230f4aa1e4769fb586a3c824e"
MODEL_REVISION = "706bee1915e9fd1f1214929e2a0509c874cff433"
MODEL_BYTES_EXPECTED = 2295746007
WETEXT_COMMIT = "030bb1febce0bd0168549a03e181cd9c1d70c799"

# 计入 MODEL_BYTES_EXPECTED 的顶层固定文件（不含 asset/、.cache/）
MODEL_REQUIRED_FILES: Tuple[str, ...] = (
    ".gitattributes",
    "campplus.onnx",
    "config.json",
    "configuration.json",
    "cosyvoice.yaml",
    "flow.pt",
    "hift.pt",
    "llm.pt",
    "README.md",
    "speech_tokenizer_v1.onnx",
    "spk2info.pt",
)

# 相对仓库根的固定布局
VENDOR_COSYVOICE_REL = "vendor/CosyVoice"
MATCHA_DIR_REL = "vendor/CosyVoice/third_party/Matcha-TTS"
COSYVOICE_PYTHON_REL = "vendor/CosyVoice/.venv/Scripts/python.exe"
MODEL_DIR_REL = "vendor/CosyVoice/pretrained_models/CosyVoice-300M-Instruct"
WETEXT_DIR_REL = "vendor/CosyVoice/pretrained_models/wetext"
EVAL_WORKER_SCRIPT_REL = "scripts/cosyvoice_eval_worker.py"
RUNTIME_WORKER_SCRIPT_REL = "scripts/cosyvoice_runtime_worker.py"
OUTPUT_DIR_REL = "data/test/voice-naturalness/cosyvoice-instruct"
SUITE_CACHE_REL = "vendor/CosyVoice/runtime/eval_cache"
RUNTIME_MODEL_LOCK_REL = "vendor/CosyVoice/runtime/cosyvoice_model.lock"
LOCK_FILE_NAME = ".expressive_tts_eval.lock"

# 评估套件兼容别名
WORKER_SCRIPT_REL = EVAL_WORKER_SCRIPT_REL

WORKER_TIMEOUT_SEC = 900
TERMINATE_GRACE_SEC = 10
KILL_WAIT_SEC = 10
GIT_META_TIMEOUT_SEC = 5


def resolve_git_dir(repo: Path) -> Path:
    """解析 ``repo/.git``（目录或 gitdir 文件）为真实 git 目录。"""
    git_path = repo / ".git"
    if git_path.is_file():
        text = git_path.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            raise RuntimeError("gitdir_malformed")
        target = text.split(":", 1)[1].strip()
        if not target:
            raise RuntimeError("gitdir_empty")
        return (repo / target).resolve()
    if git_path.is_dir():
        return git_path.resolve()
    raise RuntimeError("git_missing")


def read_git_head_commit(repo: Path) -> str:
    """只读解析 HEAD 指向的 40 位 commit；失败抛错（不得当作匹配）。"""
    git_dir = resolve_git_dir(repo)
    head_file = git_dir / "HEAD"
    if not head_file.is_file():
        raise RuntimeError("head_missing")
    head = head_file.read_text(encoding="utf-8").strip()
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        if not ref or ".." in ref or ref.startswith(("/", "\\")):
            raise RuntimeError("ref_invalid")
        ref_path = (git_dir / ref).resolve()
        try:
            ref_path.relative_to(git_dir)
        except ValueError as exc:
            raise RuntimeError("ref_escape") from exc
        if not ref_path.is_file():
            raise RuntimeError("ref_missing")
        commit = ref_path.read_text(encoding="utf-8").strip()
    else:
        commit = head
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit.lower()):
        raise RuntimeError("commit_malformed")
    return commit.lower()


def read_model_revision_actual(model_dir: Path) -> str:
    """从 HuggingFace 下载元数据读取实际 revision（不联网）。"""
    meta = (
        model_dir
        / ".cache"
        / "huggingface"
        / "download"
        / "llm.pt.metadata"
    )
    if not meta.is_file():
        raise RuntimeError("model_revision_meta_missing")
    first = meta.read_text(encoding="utf-8").splitlines()[0].strip().lower()
    if len(first) != 40 or any(c not in "0123456789abcdef" for c in first):
        raise RuntimeError("model_revision_meta_malformed")
    return first


def measure_model_bytes(model_dir: Path) -> int:
    """汇总固定文件清单字节；任一缺失则抛错。"""
    total = 0
    for rel in MODEL_REQUIRED_FILES:
        path = model_dir / rel
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"model_file_missing:{rel}")
        total += path.stat().st_size
    return total


def verify_pinned_assets(
    *,
    cosyvoice_root: Path,
    matcha_root: Path,
    wetext_root: Path,
    model_dir: Path,
) -> dict:
    """
    模型加载前 fail-closed 校验 git HEAD / 模型 revision / 文件清单字节。

    Returns:
        含 ``*_expected`` 与 ``*_actual`` 的字典。
    """
    cosy_actual = read_git_head_commit(cosyvoice_root)
    if cosy_actual != COSYVOICE_COMMIT:
        raise RuntimeError("cosyvoice_commit_mismatch")
    matcha_actual = read_git_head_commit(matcha_root)
    if matcha_actual != MATCHA_COMMIT:
        raise RuntimeError("matcha_commit_mismatch")
    wetext_actual = read_git_head_commit(wetext_root)
    if wetext_actual != WETEXT_COMMIT:
        raise RuntimeError("wetext_commit_mismatch")
    rev_actual = read_model_revision_actual(model_dir)
    if rev_actual != MODEL_REVISION:
        raise RuntimeError("model_revision_mismatch")
    bytes_actual = measure_model_bytes(model_dir)
    if bytes_actual != MODEL_BYTES_EXPECTED:
        raise RuntimeError("model_bytes_mismatch")
    return {
        "cosyvoice_commit_expected": COSYVOICE_COMMIT,
        "cosyvoice_commit_actual": cosy_actual,
        "matcha_commit_expected": MATCHA_COMMIT,
        "matcha_commit_actual": matcha_actual,
        "wetext_commit_expected": WETEXT_COMMIT,
        "wetext_commit_actual": wetext_actual,
        "model_revision_expected": MODEL_REVISION,
        "model_revision_actual": rev_actual,
        "model_bytes_expected": MODEL_BYTES_EXPECTED,
        "model_bytes_actual": bytes_actual,
    }


def staging_entry_allowed(name: str) -> bool:
    """本次 staging 目录允许的子项名。"""
    if name == "worker_report.json":
        return True
    if name == "worker_report.json.tmp":
        return True
    if name.endswith(".wav.tmp"):
        return True
    if name.endswith("_base.wav") and len(name) >= 12 and name[:2].isdigit():
        return True
    return False


def fixed_layout_paths(repo_root: Path) -> dict[str, Path]:
    """仓库内固定资产词法绝对路径（不 resolve 末段，不校验存在性）。"""
    from src.voice.cosyvoice.path_trust import seal_trusted_repo_root

    root = seal_trusted_repo_root(repo_root)
    return {
        "repo_root": root,
        "cosyvoice_python": root / COSYVOICE_PYTHON_REL,
        "eval_worker_script": root / EVAL_WORKER_SCRIPT_REL,
        "runtime_worker_script": root / RUNTIME_WORKER_SCRIPT_REL,
        "model_dir": root / MODEL_DIR_REL,
        "wetext_dir": root / WETEXT_DIR_REL,
        "matcha_dir": root / MATCHA_DIR_REL,
        "vendor_root": root / VENDOR_COSYVOICE_REL,
        "cache_root": root / SUITE_CACHE_REL,
        "output_dir": root / OUTPUT_DIR_REL,
    }


def revalidate_fixed_layout_paths(repo_root: Path) -> dict[str, Path]:
    """spawn 前复验：固定布局每项路径仍在受信仓库内且逐级非 reparse/link。"""
    from src.voice.cosyvoice.path_trust import assert_trusted_repo_path

    layout = fixed_layout_paths(repo_root)
    root = layout["repo_root"]
    assert_trusted_repo_path(root, layout["vendor_root"], kind="dir")
    assert_trusted_repo_path(root, layout["matcha_dir"], kind="dir")
    assert_trusted_repo_path(root, layout["wetext_dir"], kind="dir")
    assert_trusted_repo_path(root, layout["model_dir"], kind="dir")
    assert_trusted_repo_path(root, layout["cache_root"], kind="dir")
    assert_trusted_repo_path(root, layout["cosyvoice_python"], kind="file")
    assert_trusted_repo_path(root, layout["runtime_worker_script"], kind="file")
    assert_trusted_repo_path(root, layout["eval_worker_script"], kind="file")
    return layout


# 评估套件 ``from assets import *`` 兼容：显式列表，不导出内部子模块
__all__ = [
    "COSYVOICE_COMMIT",
    "COSYVOICE_PYTHON_REL",
    "EVAL_WORKER_SCRIPT_REL",
    "EXPECTED_COUNT",
    "GIT_META_TIMEOUT_SEC",
    "KILL_WAIT_SEC",
    "LOCK_FILE_NAME",
    "MATCHA_COMMIT",
    "MATCHA_DIR_REL",
    "MODEL_BYTES_EXPECTED",
    "MODEL_DIR_REL",
    "MODEL_REQUIRED_FILES",
    "MODEL_REVISION",
    "OUTPUT_DIR_REL",
    "RUNTIME_WORKER_SCRIPT_REL",
    "SAMPLE_CASES",
    "SCHEMA_VERSION",
    "SPEAKER",
    "SUITE_CACHE_REL",
    "TERMINATE_GRACE_SEC",
    "VENDOR_COSYVOICE_REL",
    "WETEXT_COMMIT",
    "WETEXT_DIR_REL",
    "WORKER_SCRIPT_REL",
    "WORKER_TIMEOUT_SEC",
    "fixed_layout_paths",
    "measure_model_bytes",
    "read_git_head_commit",
    "read_model_revision_actual",
    "revalidate_fixed_layout_paths",
    "resolve_git_dir",
    "staging_entry_allowed",
    "verify_pinned_assets",
]
