# -*- coding: utf-8 -*-
"""Paraformer ASR 环境准备与固定音频诊断 helper（可离线单测）。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.request import Request, urlopen

from src.core.host_memory import check_model_startup_memory
from src.core.owned_worker_job import (
    JOB_ENV_NAME,
    take_owned_worker_from_exception,
    worker_entry_join_owned_job,
)
from src.core.safety import (
    Operation,
    SafetyPolicy,
    TrustedLocalAsrProfile,
    safe_subprocess_popen,
)
from src.voice.cosyvoice.path_trust import assert_trusted_repo_path, is_reparse_or_link
from src.voice.paraformer.assets import (
    HF_REPO_ID,
    MODEL_REVISION,
    OFFICIAL_EXAMPLE_BYTES,
    OFFICIAL_FILE_BYTES,
    OFFICIAL_GIT_BLOB_SHA1,
    OFFICIAL_MODEL_PT_SHA256,
    OFFICIAL_README_BYTES,
    SETUP_USER_HINT,
    fixed_layout_paths,
    verify_pinned_assets,
)
from src.voice.paraformer.settings import INFERENCE_DEADLINE_SEC, STARTUP_DEADLINE_SEC

REQUIREMENTS_ASR_REL = "requirements-asr.txt"
SETUP_WORKER_SCRIPT_REL = "scripts/paraformer_asr_setup_worker.py"
SETUP_SPAWN_CLEANUP_BUDGET_SEC = 5.0
MAX_SETUP_CAPTURE_BYTES = 256 * 1024
PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

README_REL = "README.md"
README_GIT_BLOB = "09724db9b2a03c1db73149e85e4bd03a6a1f279e"
EXAMPLE_REL = "example/asr_example.wav"
EXAMPLE_SHA256 = "732f28a4445eb2b656675b946695e59752b7df824d475c4bc531c441f43b30f9"
EXAMPLE_BYTES = OFFICIAL_EXAMPLE_BYTES

MODEL_PT_BYTES = OFFICIAL_FILE_BYTES["model.pt"]

ENV_IDENTITY_REL = "tmp/paraformer_asr_env_identity.json"
WARM_DIAGNOSTIC_REPEATS = 3

HF_RESOLVE_BASE = (
    f"https://huggingface.co/{HF_REPO_ID}/resolve/{MODEL_REVISION}"
)

DOWNLOAD_FILES: Tuple[Tuple[str, str, int, Optional[str]], ...] = (
    *((rel, "git_blob", OFFICIAL_FILE_BYTES[rel], OFFICIAL_GIT_BLOB_SHA1[rel]) for rel in OFFICIAL_FILE_BYTES if rel != "model.pt"),
    ("model.pt", "lfs_sha256", MODEL_PT_BYTES, OFFICIAL_MODEL_PT_SHA256),
    (README_REL, "git_blob", OFFICIAL_README_BYTES, README_GIT_BLOB),
    (EXAMPLE_REL, "lfs_sha256", EXAMPLE_BYTES, EXAMPLE_SHA256),
)

PIP_INSTALL_TOTAL_DEADLINE_SEC = 900.0
PIP_CMD_TIMEOUT_SEC = 120
PIP_CMD_RETRIES = 2
VENV_CREATE_TIMEOUT_SEC = 180.0
VERIFY_ENV_TIMEOUT_SEC = 120.0
DOWNLOAD_TOTAL_DEADLINE_SEC = 3600.0
DOWNLOAD_PER_READ_TIMEOUT_SEC = 120.0
DOWNLOAD_CHUNK_BYTES = 1024 * 1024

MIN_FREE_DISK_BYTES = 3 * 1024 * 1024 * 1024

REQUIRED_PYTHON_MAJOR = 3
REQUIRED_PYTHON_MINOR = 12

NAHIDA_REFERENCE_REL = (
    "data/test/voice-naturalness/cosyvoice-instruct/01_warm_companion_rvc.wav"
)
NAHIDA_REFERENCE_SHA256 = (
    "7c4aee8b69428e4bbe3bb773cfc47efae1618efb46b7b80ebaf1465739fe19ba"
)
NAHIDA_REFERENCE_BYTES = 382_444

# 只读固定诊断片段（最多 12 路）；缺失文件在 execute 前跳过，不创建。
FIXED_DIAGNOSTIC_CLIP_RELS: Tuple[str, ...] = (
    f"vendor/paraformer-zh/{EXAMPLE_REL}",
    "data/test/voice-naturalness/cosyvoice-instruct/01_warm_companion_base.wav",
    "data/test/voice-naturalness/cosyvoice-instruct/02_caring_question_base.wav",
    "data/test/voice-naturalness/cosyvoice-instruct/03_pleasant_surprise_base.wav",
    "data/test/voice-naturalness/cosyvoice-instruct/04_encouragement_base.wav",
    "data/test/voice-naturalness/cosyvoice-instruct/05_soothing_base.wav",
    "data/test/voice-naturalness/cosyvoice-instruct/06_serious_calm_base.wav",
    "data/test/voice-naturalness/01_neutral.wav",
    "data/test/voice-naturalness/02_question.wav",
    "data/test/voice-naturalness/03_exclamation.wav",
    "data/test/voice-naturalness/04_ellipsis.wav",
    NAHIDA_REFERENCE_REL,
)

MAX_DIAGNOSTIC_CLIPS = 12
MAX_UTTERANCE_SEC = 15.0
DIAGNOSTIC_HARD_DEADLINE_SEC = 180.0
DIAGNOSTIC_PID_SETTLE_SEC = 2.0

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ComponentStatus:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class CheckReport:
    ok: bool
    components: Tuple[ComponentStatus, ...]
    hint: str = SETUP_USER_HINT


def requirements_path(repo_root: Path) -> Path:
    return (repo_root / REQUIREMENTS_ASR_REL).resolve()


def maybe_join_owned_job(*, environ: Mapping[str, str] | None = None) -> None:
    source = environ if environ is not None else os.environ
    if source.get(JOB_ENV_NAME):
        worker_entry_join_owned_job(environ=source)


def build_setup_safety_policy(repo_root: Path) -> SafetyPolicy:
    layout = fixed_layout_paths(repo_root)
    venv_root = layout["asr_python"].parent.parent
    tmp_dir = repo_root / "tmp"
    policy = SafetyPolicy(
        {
            "permissions": {
                Operation.SUBPROCESS.value: True,
                Operation.FILE_DELETE.value: True,
            },
            "file_delete_allowed_dirs": [
                str(layout["model_dir"]),
                str(venv_root),
                str(tmp_dir),
            ],
        }
    )

    def _setup_owned_popen(args, **kwargs):
        argv = list(args)
        if sys.platform == "win32":
            from src.core.owned_worker_job import spawn_owned_local_tts_worker

            return spawn_owned_local_tts_worker(
                argv,
                kwargs,
                popen_fn=subprocess.Popen,
                parent_pid=os.getpid(),
                job_backend=policy._owned_worker_job_backend,
            )
        return subprocess.Popen(argv, **kwargs)

    policy.bind_process_runners(popen_fn=_setup_owned_popen)
    return policy


def build_diagnostic_safety_policy(repo_root: Path) -> Tuple[Optional[SafetyPolicy], str]:
    try:
        fixed_layout_paths(repo_root)
        from src.voice.paraformer.assets import revalidate_fixed_layout_paths

        revalidate_fixed_layout_paths(repo_root)
    except PermissionError:
        return None, "layout_invalid"
    profile = TrustedLocalAsrProfile(repo_root=repo_root.resolve())
    policy = SafetyPolicy(
        {
            "permissions": {
                Operation.LOCAL_ASR_WORKER.value: True,
                Operation.FILE_DELETE.value: True,
            },
        },
        trusted_local_asr_profile=profile,
    )
    report_root = repo_root / "tmp"
    policy.register_file_delete_dir(report_root)
    return policy, ""


def _requirements_sha256(repo_root: Path) -> str:
    return hashlib.sha256(requirements_path(repo_root).read_bytes()).hexdigest()


def _sha256_file_bounded(path: Path, *, expected_size: int) -> str:
    if path.stat().st_size != expected_size:
        raise RuntimeError("size_mismatch")
    h = hashlib.sha256()
    with path.open("rb") as fh:
        remaining = expected_size
        while remaining > 0:
            chunk = fh.read(min(DOWNLOAD_CHUNK_BYTES, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    if remaining != 0:
        raise RuntimeError("size_mismatch")
    return h.hexdigest()


def _git_blob_sha1_bounded(path: Path, *, expected_size: int) -> str:
    if path.stat().st_size != expected_size:
        raise RuntimeError("blob_size_mismatch")
    digest = hashlib.sha1()
    digest.update(f"blob {expected_size}\0".encode("ascii"))
    with path.open("rb") as fh:
        remaining = expected_size
        while remaining > 0:
            chunk = fh.read(min(DOWNLOAD_CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    if remaining != 0:
        raise RuntimeError("blob_size_mismatch")
    return digest.hexdigest()


def _verify_existing_file(
    path: Path,
    *,
    kind: str,
    expected_bytes: int,
    expected_digest: Optional[str],
) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    if path.stat().st_size != expected_bytes:
        return False
    if kind == "lfs_sha256":
        if not expected_digest:
            return False
        return (
            _sha256_file_bounded(path, expected_size=expected_bytes).lower()
            == expected_digest.lower()
        )
    if kind == "git_blob":
        if not expected_digest:
            return False
        return _git_blob_sha1_bounded(path, expected_size=expected_bytes) == expected_digest
    return False


def check_disk_space(repo_root: Path) -> ComponentStatus:
    try:
        usage = shutil.disk_usage(repo_root)
    except OSError as exc:
        return ComponentStatus("disk", False, type(exc).__name__)
    if usage.free < MIN_FREE_DISK_BYTES:
        return ComponentStatus(
            "disk",
            False,
            f"free_lt_{MIN_FREE_DISK_BYTES}",
        )
    return ComponentStatus("disk", True, str(usage.free))


def check_host_memory_for_setup() -> ComponentStatus:
    decision = check_model_startup_memory("paraformer")
    if not decision.allowed:
        return ComponentStatus("host_memory", False, decision.reason)
    return ComponentStatus("host_memory", True)


def check_main_python() -> ComponentStatus:
    if sys.version_info[:2] != (REQUIRED_PYTHON_MAJOR, REQUIRED_PYTHON_MINOR):
        return ComponentStatus(
            "main_python",
            False,
            f"need_{REQUIRED_PYTHON_MAJOR}.{REQUIRED_PYTHON_MINOR}",
        )
    return ComponentStatus("main_python", True, sys.version.split()[0])


def check_requirements_file(repo_root: Path) -> ComponentStatus:
    path = requirements_path(repo_root)
    if not path.is_file():
        return ComponentStatus("requirements_asr", False, "missing")
    text = path.read_text(encoding="utf-8")
    for needle in (
        "torch==2.6.0+cpu",
        "torchaudio==2.6.0+cpu",
        "funasr==1.4.22",
        "numpy==1.26.4",
        "transformers==4.49.0",
    ):
        if needle not in text:
            return ComponentStatus("requirements_asr", False, "pin_mismatch")
    return ComponentStatus("requirements_asr", True)


def _validate_venv_interpreter(layout: Mapping[str, Path]) -> str:
    py = layout["asr_python"]
    if not py.is_file():
        return "missing"
    resolved = py.resolve()
    if is_reparse_or_link(resolved):
        return "venv_python_reparse"
    venv_root = py.parent.parent
    cfg = venv_root / "pyvenv.cfg"
    if not cfg.is_file():
        return "pyvenv_cfg_missing"
    text = cfg.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"^version\s*=\s*3\.12\.", text, re.MULTILINE):
        return "pyvenv_version"
    home_match = re.search(r"^home\s*=\s*(.+)$", text, re.MULTILINE)
    if home_match:
        home = Path(home_match.group(1).strip())
        if home.is_symlink() or (home.exists() and is_reparse_or_link(home.resolve())):
            return "pyvenv_home_reparse"
    return ""


def check_venv_identity(repo_root: Path, layout: Mapping[str, Path]) -> ComponentStatus:
    err = _validate_venv_interpreter(layout)
    if err == "missing":
        return ComponentStatus("venv_asr", False, "missing")
    if err:
        return ComponentStatus("venv_asr", False, err)
    identity_path = (repo_root / ENV_IDENTITY_REL).resolve()
    if not identity_path.is_file():
        return ComponentStatus("venv_asr", False, "identity_missing")
    try:
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ComponentStatus("venv_asr", False, "identity_invalid")
    if payload.get("asr_python") != str(layout["asr_python"].resolve()):
        return ComponentStatus("venv_asr", False, "identity_path_mismatch")
    if payload.get("requirements_sha256") != _requirements_sha256(repo_root):
        return ComponentStatus("venv_asr", False, "requirements_changed")
    return ComponentStatus("venv_asr", True)


def check_model_assets(layout: Mapping[str, Path]) -> ComponentStatus:
    model_dir = layout["model_dir"]
    missing: List[str] = []
    for rel, kind, size, digest in DOWNLOAD_FILES:
        dest = model_dir / rel
        if not _verify_existing_file(
            dest, kind=kind, expected_bytes=size, expected_digest=digest
        ):
            missing.append(rel)
    if missing:
        return ComponentStatus("model_assets", False, ",".join(missing[:6]))
    try:
        verify_pinned_assets(model_dir=model_dir, repo_root=layout["repo_root"])
    except RuntimeError as exc:
        return ComponentStatus("model_assets", False, str(exc))
    return ComponentStatus("model_assets", True)


def run_check(repo_root: Path) -> CheckReport:
    layout = fixed_layout_paths(repo_root)
    components = (
        check_main_python(),
        check_requirements_file(repo_root),
        check_host_memory_for_setup(),
        check_disk_space(repo_root),
        check_venv_identity(repo_root, layout),
        check_model_assets(layout),
    )
    ok = all(c.ok for c in components)
    return CheckReport(ok=ok, components=components)


def _setup_child_env() -> Dict[str, str]:
    keys = (
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "PROGRAMDATA",
        "SystemDrive",
        "NUMBER_OF_PROCESSORS",
    )
    env = {k: os.environ[k] for k in keys if k in os.environ}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    for banned in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "PYTHONPATH", "PYTHONHOME"):
        env.pop(banned, None)
    return env


def collect_asr_env_identity_from_interpreter() -> Dict[str, Any]:
    """在 ASR venv 解释器内收集 pin 元数据（setup worker env_verify mode）。"""
    import importlib.metadata as md

    import torch
    import torchaudio

    def pkg(name: str) -> str:
        try:
            return md.version(name)
        except md.PackageNotFoundError:
            return ""

    return {
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "torchaudio": torchaudio.__version__,
        "torch_cuda_is_none": torch.version.cuda is None,
        "funasr": pkg("funasr"),
        "numpy": pkg("numpy"),
        "transformers": pkg("transformers"),
    }


def _setup_worker_script(repo_root: Path) -> Path:
    script = (repo_root / SETUP_WORKER_SCRIPT_REL).resolve()
    if not script.is_file() or script.is_symlink():
        raise RuntimeError("setup_worker_missing")
    assert_trusted_repo_path(repo_root, script, kind="file")
    return script


def build_setup_worker_argv(
    mode: str,
    *,
    repo_root: Path,
    layout: Mapping[str, Path],
) -> List[str]:
    if mode not in {"venv_create", "pip_install", "env_verify", "pip_freeze"}:
        raise RuntimeError("setup_mode_invalid")
    script = _setup_worker_script(repo_root)
    if mode == "venv_create":
        interpreter = sys.executable
    else:
        interpreter = str(layout["asr_python"].resolve())
    return [interpreter, str(script), "--mode", mode]


def _spawn_setup_popen(
    policy: SafetyPolicy,
    argv: Sequence[str],
    *,
    context: str,
    cwd: Path,
    stdout: Any,
    stderr: Any,
) -> subprocess.Popen:
    return safe_subprocess_popen(
        policy,
        list(argv),
        context=context,
        cwd=str(cwd.resolve()),
        env=_setup_child_env(),
        shell=False,
        stdout=stdout,
        stderr=stderr,
    )


def _setup_log_paths(
    repo_root: Path,
    *,
    context: str,
) -> Tuple[Path, Path]:
    tmp_dir = repo_root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stamp = uuid.uuid4().hex[:12]
    stdout_path = tmp_dir / f"asr_setup_{context}_{stamp}.stdout.log"
    stderr_path = tmp_dir / f"asr_setup_{context}_{stamp}.stderr.log"
    return stdout_path, stderr_path


def _assert_setup_logs_bounded(
    stdout_path: Path,
    stderr_path: Path,
    *,
    limit: int = MAX_SETUP_CAPTURE_BYTES,
) -> None:
    for path in (stdout_path, stderr_path):
        if path.is_file() and path.stat().st_size > limit:
            raise RuntimeError("setup_log_too_large")


def _read_bounded_log_text(path: Path, *, limit: int = MAX_SETUP_CAPTURE_BYTES) -> str:
    if not path.is_file():
        return ""
    size = path.stat().st_size
    if size > limit:
        raise RuntimeError("setup_log_too_large")
    return path.read_bytes().decode("utf-8", errors="replace")


def _setup_process_quiescent(
    proc: Optional[subprocess.Popen],
    *,
    budget: float = SETUP_SPAWN_CLEANUP_BUDGET_SEC,
) -> bool:
    if proc is None:
        return True
    try:
        proc.kill()
    except (OSError, ProcessLookupError, AttributeError):
        pass
    deadline = time.monotonic() + float(budget)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return proc.poll() is not None


def _run_setup_command(
    policy: SafetyPolicy,
    mode: str,
    repo_root: Path,
    layout: Mapping[str, Path],
    *,
    timeout: float,
    context: str,
    capture_stdout: bool = False,
) -> str:
    argv = build_setup_worker_argv(mode, repo_root=repo_root, layout=layout)
    policy.register_file_delete_dir(repo_root / "tmp")
    stdout_path, stderr_path = _setup_log_paths(repo_root, context=context)
    stdout_fh: Any = None
    stderr_fh: Any = None
    proc: Optional[subprocess.Popen] = None
    rc: Optional[int] = None
    deadline = time.monotonic() + float(timeout)
    try:
        stdout_fh = stdout_path.open("xb")
        stderr_fh = stderr_path.open("xb")
        try:
            proc = _spawn_setup_popen(
                policy,
                argv,
                context=context,
                cwd=repo_root,
                stdout=stdout_fh,
                stderr=stderr_fh,
            )
        except BaseException as exc:
            owner = take_owned_worker_from_exception(exc)
            if owner is not None and not _setup_process_quiescent(owner):
                raise RuntimeError(f"{context}_cleanup_incomplete") from exc
            raise
        stdout_fh.close()
        stdout_fh = None
        stderr_fh.close()
        stderr_fh = None
        while True:
            try:
                _assert_setup_logs_bounded(stdout_path, stderr_path)
            except RuntimeError as exc:
                if str(exc) == "setup_log_too_large":
                    if proc is not None and not _setup_process_quiescent(proc):
                        raise RuntimeError(f"{context}_cleanup_incomplete") from exc
                    raise RuntimeError(f"{context}_log_too_large") from exc
                raise
            rc = proc.poll()
            if rc is not None:
                break
            if time.monotonic() >= deadline:
                if not _setup_process_quiescent(proc):
                    raise RuntimeError(f"{context}_cleanup_incomplete")
                raise TimeoutError(f"{context}_timeout")
            time.sleep(0.05)
    except (KeyboardInterrupt, SystemExit):
        if proc is not None and not _setup_process_quiescent(proc):
            raise RuntimeError(f"{context}_cleanup_incomplete")
        raise
    finally:
        if stdout_fh is not None:
            try:
                stdout_fh.close()
            except OSError:
                pass
        if stderr_fh is not None:
            try:
                stderr_fh.close()
            except OSError:
                pass
    assert rc is not None
    if rc != 0:
        raise RuntimeError(f"{context}_failed:{rc}")
    if not capture_stdout:
        return ""
    return _read_bounded_log_text(stdout_path)


def ensure_venv(
    policy: SafetyPolicy,
    repo_root: Path,
    layout: Mapping[str, Path],
) -> None:
    py = layout["asr_python"]
    if py.is_file():
        err = _validate_venv_interpreter(layout)
        if err:
            raise RuntimeError(err)
        return
    venv_root = py.parent.parent
    assert_trusted_repo_path(repo_root, venv_root, kind="dir")
    venv_root.mkdir(parents=True, exist_ok=True)
    _run_setup_command(
        policy,
        "venv_create",
        repo_root,
        layout,
        timeout=VENV_CREATE_TIMEOUT_SEC,
        context="venv_create",
    )
    if not py.is_file():
        raise RuntimeError("venv_create_incomplete")
    err = _validate_venv_interpreter(layout)
    if err:
        raise RuntimeError(err)


def pip_install_asr_env(
    policy: SafetyPolicy,
    repo_root: Path,
    layout: Mapping[str, Path],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    req = requirements_path(repo_root)
    if not req.is_file():
        raise RuntimeError("requirements_missing")
    deadline = clock() + PIP_INSTALL_TOTAL_DEADLINE_SEC
    last_error: Optional[BaseException] = None
    for attempt in range(2):
        if clock() >= deadline:
            raise TimeoutError("pip_install_deadline")
        try:
            remaining = max(1.0, deadline - clock())
            _run_setup_command(
                policy,
                "pip_install",
                repo_root,
                layout,
                timeout=min(PIP_INSTALL_TOTAL_DEADLINE_SEC, remaining),
                context="pip_install",
            )
            return
        except (KeyboardInterrupt, SystemExit):
            raise
        except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
            last_error = exc
            if attempt == 0 and clock() < deadline:
                time.sleep(min(2.0, deadline - clock()))
    raise RuntimeError("pip_install_failed") from last_error


def verify_asr_env_identity(
    policy: SafetyPolicy,
    repo_root: Path,
    layout: Mapping[str, Path],
) -> Dict[str, Any]:
    stdout = _run_setup_command(
        policy,
        "env_verify",
        repo_root,
        layout,
        timeout=VERIFY_ENV_TIMEOUT_SEC,
        context="asr_env_verify",
        capture_stdout=True,
    )
    payload = json.loads((stdout or "").strip() or "{}")
    if not str(payload.get("python", "")).startswith("3.12."):
        raise RuntimeError("asr_python_version")
    if payload.get("torch_cuda_is_none") is not True:
        raise RuntimeError("asr_torch_not_cpu")
    for key, expected in (
        ("funasr", "1.4.22"),
        ("numpy", "1.26.4"),
        ("transformers", "4.49.0"),
        ("torch", "2.6.0+cpu"),
        ("torchaudio", "2.6.0+cpu"),
    ):
        if payload.get(key) != expected:
            raise RuntimeError(f"asr_pin_{key}")
    return payload


def _download_url(rel: str) -> str:
    return f"{HF_RESOLVE_BASE}/{rel}"


def _stream_download(
    url: str,
    dest_partial: Path,
    *,
    expected_bytes: int,
    deadline: float,
) -> None:
    req = Request(url, headers={"User-Agent": "MikuAssistant-ASR-Setup/1.0"})
    with urlopen(req, timeout=DOWNLOAD_PER_READ_TIMEOUT_SEC) as resp:
        total = 0
        with dest_partial.open("wb") as out:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("download_deadline")
                chunk = resp.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_bytes:
                    raise RuntimeError("download_oversize")
                out.write(chunk)
    if dest_partial.stat().st_size != expected_bytes:
        raise RuntimeError("download_size_mismatch")


def _cleanup_partial(policy: SafetyPolicy, path: Path) -> None:
    if not path.exists():
        return
    if not policy.is_path_deletable(path):
        raise RuntimeError("partial_not_deletable")
    path.unlink()


def download_pinned_assets(
    policy: SafetyPolicy,
    layout: Mapping[str, Path],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> List[str]:
    repo_root = layout["repo_root"]
    model_dir = layout["model_dir"]
    assert_trusted_repo_path(repo_root, model_dir, kind="dir")
    model_dir.mkdir(parents=True, exist_ok=True)
    policy.register_file_delete_dir(model_dir)
    deadline = clock() + DOWNLOAD_TOTAL_DEADLINE_SEC
    reused: List[str] = []
    for rel, kind, size, digest in DOWNLOAD_FILES:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise RuntimeError("dest_escape")
        dest_lex = model_dir / rel
        assert_trusted_repo_path(repo_root, model_dir, kind="dir")
        assert_trusted_repo_path(repo_root, dest_lex.parent, kind="dir")
        if dest_lex.exists():
            assert_trusted_repo_path(repo_root, dest_lex, kind="file")
        if dest_lex.exists() and not dest_lex.is_file():
            raise RuntimeError(f"dest_not_file:{rel}")
        if dest_lex.is_file() and _verify_existing_file(
            dest_lex, kind=kind, expected_bytes=size, expected_digest=digest
        ):
            reused.append(rel)
            continue
        if dest_lex.is_file():
            raise RuntimeError(f"existing_untrusted:{rel}")
        dest_lex.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        partial = dest_lex.with_name(dest_lex.name + f".{token}.partial")
        try:
            partial.open("xb").close()
        except FileExistsError as exc:
            raise RuntimeError("partial_exists") from exc
        policy.register_file_delete_dir(partial.parent)
        try:
            _stream_download(
                _download_url(rel),
                partial,
                expected_bytes=size,
                deadline=deadline,
            )
            if kind == "lfs_sha256":
                if (
                    _sha256_file_bounded(partial, expected_size=size).lower()
                    != (digest or "").lower()
                ):
                    raise RuntimeError("digest_mismatch")
            elif kind == "git_blob":
                if _git_blob_sha1_bounded(partial, expected_size=size) != digest:
                    raise RuntimeError("blob_mismatch")
            _atomic_publish_new_file(partial, dest_lex)
        except BaseException:
            try:
                _cleanup_partial(policy, partial)
            except Exception:
                pass
            raise
    verify_pinned_assets(model_dir=model_dir, repo_root=repo_root)
    return reused


def capture_pip_inventory(
    policy: SafetyPolicy,
    repo_root: Path,
    layout: Mapping[str, Path],
) -> List[str]:
    stdout = _run_setup_command(
        policy,
        "pip_freeze",
        repo_root,
        layout,
        timeout=120.0,
        context="pip_freeze",
        capture_stdout=True,
    )
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    return lines


def write_env_identity(
    repo_root: Path,
    layout: Mapping[str, Path],
    inventory: Sequence[str],
) -> Path:
    tmp_dir = repo_root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / "paraformer_asr_env_identity.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "asr_python": str(layout["asr_python"].resolve()),
        "requirements_sha256": _requirements_sha256(repo_root),
        "inventory_lines": len(inventory),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_setup_report(repo_root: Path, payload: Mapping[str, Any]) -> Path:
    tmp_dir = repo_root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    name = f"paraformer_asr_setup_{uuid.uuid4().hex[:12]}.json"
    path = tmp_dir / name
    with path.open("x", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def run_execute_setup(
    repo_root: Path,
    *,
    policy: Optional[SafetyPolicy] = None,
    clock: Callable[[], float] = time.monotonic,
) -> Tuple[int, Optional[Path]]:
    maybe_join_owned_job()
    layout = fixed_layout_paths(repo_root)
    pol = policy or build_setup_safety_policy(repo_root)
    started = clock()
    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "revision": MODEL_REVISION,
        "repo_id": HF_REPO_ID,
    }
    try:
        mem = check_host_memory_for_setup()
        if not mem.ok:
            raise RuntimeError(mem.detail)
        disk = check_disk_space(repo_root)
        if not disk.ok:
            raise RuntimeError(disk.detail)
        py_check = check_main_python()
        if not py_check.ok:
            raise RuntimeError(py_check.detail)
        ensure_venv(pol, repo_root, layout)
        pip_install_asr_env(pol, repo_root, layout, clock=clock)
        env_identity = verify_asr_env_identity(pol, repo_root, layout)
        reused = download_pinned_assets(pol, layout, clock=clock)
        inventory = capture_pip_inventory(pol, repo_root, layout)
        identity_path = write_env_identity(repo_root, layout, inventory)
        report.update(
            ok=True,
            elapsed_sec=round(clock() - started, 3),
            env_identity=env_identity,
            reused_files=reused,
            inventory=inventory,
            env_identity_path=identity_path.relative_to(repo_root).as_posix(),
            storage_note=(
                "模型与 .venv-asr 占用本地磁盘；无按分钟云端 ASR 计费。"
            ),
        )
        path = write_setup_report(repo_root, report)
        return 0, path
    except Exception as exc:  # noqa: BLE001
        report.update(
            ok=False,
            elapsed_sec=round(clock() - started, 3),
            error=type(exc).__name__,
            detail=str(exc),
        )
        try:
            path = write_setup_report(repo_root, report)
        except Exception:
            path = None
        return 1, path


def snapshot_shared_miku_wavs(tmp_dir: Path) -> List[Dict[str, Any]]:
    if not tmp_dir.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(tmp_dir.glob("miku_*.wav")):
        try:
            st = path.stat()
            rows.append(
                {
                    "name": path.name,
                    "dev": st.st_dev,
                    "ino": st.st_ino,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                }
            )
        except OSError:
            continue
    return rows


def verify_nahida_reference(repo_root: Path) -> ComponentStatus:
    path = (repo_root / NAHIDA_REFERENCE_REL).resolve()
    if not path.is_file():
        return ComponentStatus("nahida_reference", False, "missing")
    if path.stat().st_size != NAHIDA_REFERENCE_BYTES:
        return ComponentStatus("nahida_reference", False, "size_mismatch")
    digest = _sha256_file_bounded(path, expected_size=NAHIDA_REFERENCE_BYTES)
    if digest.lower() != NAHIDA_REFERENCE_SHA256.lower():
        return ComponentStatus("nahida_reference", False, "sha256_mismatch")
    return ComponentStatus("nahida_reference", True, str(NAHIDA_REFERENCE_BYTES))


def resolve_diagnostic_clips(repo_root: Path) -> List[Path]:
    found: List[Path] = []
    for rel in FIXED_DIAGNOSTIC_CLIP_RELS:
        if len(found) >= MAX_DIAGNOSTIC_CLIPS:
            break
        path = (repo_root / rel).resolve()
        if path.is_file() and not path.is_symlink():
            found.append(path)
    return found


def load_pcm_16k_mono(
    path: Path,
    *,
    max_seconds: float = MAX_UTTERANCE_SEC,
) -> Tuple[bytes, float]:
    import numpy as np
    import soundfile as sf
    from scipy import signal

    info = sf.info(str(path))
    if info.duration > max_seconds + 1e-3:
        raise ValueError("wav_too_long")
    if info.samplerate <= 0 or info.frames <= 0:
        raise ValueError("wav_invalid")
    max_frames = min(info.frames, int(max_seconds * info.samplerate))
    data, sr = sf.read(
        str(path),
        dtype="float32",
        start=0,
        stop=max_frames,
        always_2d=False,
    )
    if data.ndim > 1:
        data = np.mean(data, axis=1)
    if sr <= 0 or len(data) == 0:
        raise ValueError("wav_invalid")
    if sr != 16000:
        target_len = int(round(len(data) * 16000 / sr))
        data = signal.resample(data, target_len).astype("float32")
        sr = 16000
    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    duration = len(data) / float(sr)
    return pcm, duration


def try_tree_sample(pid: int, repo_root: Path) -> Optional[Dict[str, Any]]:
    sampler_path = repo_root / "tmp" / "codex_windows_resource_sampler.py"
    if not sampler_path.is_file():
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "codex_windows_resource_sampler", sampler_path
    )
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "tree_sample", None)
    if not callable(fn):
        return None
    try:
        sample = fn(pid, include_gpu=False)
    except Exception:
        return None
    if isinstance(sample, dict):
        return sample
    return {"raw": sample}


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * (len(ordered) - 1)))
    idx = max(0, min(idx, len(ordered) - 1))
    return ordered[idx]


def min_transcribe_budget_sec(*, cold: bool) -> float:
    if cold:
        return STARTUP_DEADLINE_SEC + INFERENCE_DEADLINE_SEC + 0.5
    return INFERENCE_DEADLINE_SEC + 0.5


def transcribe_outcome_success(outcome: Any) -> bool:
    text = getattr(outcome, "text", "")
    if not getattr(outcome, "ok", False):
        return False
    if not isinstance(text, str) or not text.strip():
        return False
    if getattr(outcome, "empty", False):
        return False
    label = getattr(outcome, "error_label", "") or ""
    return label == ""


def stt_recognition_success(result: Any) -> bool:
    from src.voice.recognition import RecognitionStatus

    status = getattr(result, "status", None)
    text = getattr(result, "text", "")
    return (
        status is RecognitionStatus.SUCCESS
        and isinstance(text, str)
        and bool(text.strip())
    )


def extract_pids_from_tree_sample(sample: Mapping[str, Any]) -> List[int]:
    found: List[int] = []
    for key in ("pids", "pid_list", "process_pids"):
        val = sample.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, int) and item > 0:
                    found.append(item)
    pid = sample.get("pid")
    if isinstance(pid, int) and pid > 0:
        found.append(pid)
    processes = sample.get("processes")
    if isinstance(processes, list):
        for row in processes:
            if isinstance(row, dict) and isinstance(row.get("pid"), int):
                found.append(row["pid"])
    return sorted(set(found))


def finalize_diagnostic_verdict(
    payload: Mapping[str, Any],
    *,
    require_report_written: bool = True,
) -> Tuple[bool, int]:
    required_true = (
        "shutdown_complete",
        "stt_engine_shutdown_complete",
        "shared_wav_metadata_unchanged",
        "owned_pids_gone",
        "inference_all_ok",
        "wrapper_ok",
    )
    if require_report_written:
        required_true = ("report_written",) + required_true
    for key in required_true:
        if payload.get(key) is not True:
            return False, 1
    if payload.get("failure"):
        return False, 1
    if payload.get("hard_deadline_hit"):
        return False, 1
    nahida = payload.get("nahida_reference") or {}
    if not nahida.get("ok"):
        return False, 1
    return True, 0


def process_alive(pid: int) -> bool:
    """Delegate to reviewed parent_process probe (conservative on access denied)."""
    if pid <= 0:
        return False
    from src.core.parent_process import parent_process_is_alive

    return parent_process_is_alive(pid)


def verify_pids_gone(pids: Sequence[int]) -> bool:
    return all(not process_alive(pid) for pid in pids if pid > 0)


def verify_pids_gone_with_settle(
    pids: Sequence[int],
    *,
    clock: Callable[[], float] = time.monotonic,
    settle_sec: float = DIAGNOSTIC_PID_SETTLE_SEC,
    poll_sleep_sec: float = 0.05,
) -> bool:
    """有界轮询 ``process_alive``；仍存活或不可观测（含 access denied）则最终 False。"""
    unique = sorted({int(p) for p in pids if int(p) > 0})
    if not unique:
        return True
    deadline = clock() + float(settle_sec)
    while True:
        if all(not process_alive(pid) for pid in unique):
            return True
        if clock() >= deadline:
            return False
        remaining = deadline - clock()
        time.sleep(min(poll_sleep_sec, max(0.0, remaining)))


def collect_tree_pids(
    tree_samples: Sequence[Mapping[str, Any]],
    *,
    seed: Sequence[int] = (),
) -> List[int]:
    merged = set(int(p) for p in seed if isinstance(p, int) and p > 0)
    for sample in tree_samples:
        merged.update(extract_pids_from_tree_sample(sample))
    return sorted(merged)


def _atomic_publish_new_file(partial: Path, dest_lex: Path) -> None:
    if dest_lex.exists():
        raise RuntimeError("dest_raced")
    try:
        os.rename(partial, dest_lex)
    except OSError as exc:
        raise RuntimeError("dest_raced") from exc
