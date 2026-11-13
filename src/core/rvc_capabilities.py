"""
RVC 专用子进程能力：固定解释器、固定脚本与固定 flag 契约。

调用方只能通过具名请求/探测类型启动进程，不能自行声明任意
executable/script 即获得许可。通用 ``Operation.SUBPROCESS`` 不是回退旁路。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.core.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

RVC_WORKER_SCRIPT = PROJECT_ROOT / "scripts" / "rvc_worker.py"
RVC_INFER_SCRIPT = PROJECT_ROOT / "scripts" / "rvc_infer.py"

# Worker：解释器固定附加 ``-u``；脚本 flag 集合如下（含可选 --index）
_WORKER_REQUIRED_FLAGS = (
    "--model",
    "--device",
    "--pitch",
    "--index-rate",
    "--protect",
    "--filter-radius",
    "--rms-mix-rate",
    "--f0-method",
    "--parent-pid",
)
_WORKER_OPTIONAL_FLAGS = ("--index",)
_WORKER_ALLOWED_FLAGS = frozenset(_WORKER_REQUIRED_FLAGS + _WORKER_OPTIONAL_FLAGS)

_INFER_REQUIRED_FLAGS = (
    "--input",
    "--output",
    "--model",
    "--device",
    "--pitch",
    "--index-rate",
    "--protect",
    "--filter-radius",
    "--rms-mix-rate",
    "--f0-method",
)
_INFER_OPTIONAL_FLAGS = ("--index",)
_INFER_ALLOWED_FLAGS = frozenset(_INFER_REQUIRED_FLAGS + _INFER_OPTIONAL_FLAGS)


class RvcProbeKind(str, Enum):
    """允许的 RVC 环境探测（固定 ``-c`` 片段，不可由调用方拼接）。"""

    IMPORT_RVC = "import_rvc"
    CUDA_AVAILABLE = "cuda_available"


def _probe_bootstrap_code() -> str:
    root = PROJECT_ROOT
    return (
        "import os, sys\n"
        f"ROOT = {str(root)!r}\n"
        "if ROOT not in sys.path:\n"
        "    sys.path.insert(0, ROOT)\n"
        "from src.core.owned_worker_job import worker_entry_join_owned_job\n"
        "worker_entry_join_owned_job()\n"
        "from src.voice.rvc.cpu_budget import apply_rvc_torch_thread_budget\n"
        "apply_rvc_torch_thread_budget()\n"
    )


_PROBE_CODE: Mapping[RvcProbeKind, str] = {
    RvcProbeKind.IMPORT_RVC: (
        _probe_bootstrap_code() + "from rvc_python.infer import RVCInference"
    ),
    RvcProbeKind.CUDA_AVAILABLE: (
        _probe_bootstrap_code()
        + "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)"
    ),
}


@dataclass(frozen=True)
class RvcWorkerLaunch:
    """常驻 Worker 启动参数（动态值均为独立字段，由本模块组装 argv）。"""

    model_path: str
    device: str
    pitch: int
    index_rate: float
    protect: float
    filter_radius: int
    rms_mix_rate: float
    f0_method: str
    parent_pid: int
    index_path: Optional[str] = None


@dataclass(frozen=True)
class RvcInferLaunch:
    """一次性推理启动参数。"""

    input_path: str
    output_path: str
    model_path: str
    device: str
    pitch: int
    index_rate: float
    protect: float
    filter_radius: int
    rms_mix_rate: float
    f0_method: str
    index_path: Optional[str] = None


def _deny(reason: str) -> PermissionError:
    """拒绝时不附带完整命令、用户目录或正文。"""
    logger.warning("RVC 子进程能力拒绝: %s", reason)
    return PermissionError(f"RVC 子进程能力拒绝: {reason}")


def _resolve_regular_file(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise _deny(f"{label} 路径无法解析") from exc
    if not resolved.is_file():
        raise _deny(f"{label} 不是普通文件")
    return resolved


def _validate_no_shell(kwargs: Mapping[str, Any]) -> None:
    if kwargs.get("shell"):
        raise _deny("永久拒绝 shell=True")


def _validate_flag_pairs(
    pairs: Sequence[Tuple[str, str]],
    *,
    allowed: frozenset,
    required: Sequence[str],
) -> List[str]:
    seen: set[str] = set()
    argv: List[str] = []
    for flag, value in pairs:
        if flag not in allowed:
            raise _deny(f"未知 flag: {flag}")
        if flag in seen:
            raise _deny(f"重复 flag: {flag}")
        if value is None:
            raise _deny(f"缺值 flag: {flag}")
        if not isinstance(value, str):
            raise _deny(f"flag 值必须为独立字符串: {flag}")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise _deny(f"flag 值含非法字符: {flag}")
        seen.add(flag)
        argv.extend([flag, value])
    missing = [f for f in required if f not in seen]
    if missing:
        raise _deny(f"缺少必需 flag: {missing[0]}")
    return argv


def expected_worker_script() -> Path:
    return _resolve_regular_file(RVC_WORKER_SCRIPT, label="worker script")


def expected_infer_script() -> Path:
    return _resolve_regular_file(RVC_INFER_SCRIPT, label="infer script")


def build_worker_argv(python: Path, launch: RvcWorkerLaunch) -> List[str]:
    """组装 Worker argv；在调用方注入假 runner 前完成全部校验。"""
    script = expected_worker_script()
    pairs: List[Tuple[str, str]] = [
        ("--model", str(launch.model_path)),
        ("--device", str(launch.device)),
        ("--pitch", str(int(launch.pitch))),
        ("--index-rate", str(float(launch.index_rate))),
        ("--protect", str(float(launch.protect))),
        ("--filter-radius", str(int(launch.filter_radius))),
        ("--rms-mix-rate", str(float(launch.rms_mix_rate))),
        ("--f0-method", str(launch.f0_method)),
        ("--parent-pid", str(int(launch.parent_pid))),
    ]
    if launch.index_path:
        pairs.append(("--index", str(launch.index_path)))
    flag_argv = _validate_flag_pairs(
        pairs, allowed=_WORKER_ALLOWED_FLAGS, required=_WORKER_REQUIRED_FLAGS
    )
    return [str(python), "-u", str(script), *flag_argv]


def build_infer_argv(python: Path, launch: RvcInferLaunch) -> List[str]:
    script = expected_infer_script()
    pairs: List[Tuple[str, str]] = [
        ("--input", str(launch.input_path)),
        ("--output", str(launch.output_path)),
        ("--model", str(launch.model_path)),
        ("--device", str(launch.device)),
        ("--pitch", str(int(launch.pitch))),
        ("--index-rate", str(float(launch.index_rate))),
        ("--protect", str(float(launch.protect))),
        ("--filter-radius", str(int(launch.filter_radius))),
        ("--rms-mix-rate", str(float(launch.rms_mix_rate))),
        ("--f0-method", str(launch.f0_method)),
    ]
    if launch.index_path:
        pairs.append(("--index", str(launch.index_path)))
    flag_argv = _validate_flag_pairs(
        pairs, allowed=_INFER_ALLOWED_FLAGS, required=_INFER_REQUIRED_FLAGS
    )
    return [str(python), str(script), *flag_argv]


def build_probe_argv(python: Path, kind: RvcProbeKind) -> List[str]:
    if not isinstance(kind, RvcProbeKind):
        raise _deny("未知探测类型")
    code = _PROBE_CODE.get(kind)
    if code is None:
        raise _deny("未知探测类型")
    return [str(python), "-c", code]


def assert_executable_matches(registered: Path, candidate: Path) -> Path:
    """要求可执行文件与本次注册/推导的 RVC Python 完全一致。"""
    expected = _resolve_regular_file(registered, label="registered RVC python")
    actual = _resolve_regular_file(candidate, label="RVC python")
    if actual != expected:
        raise _deny("意外解释器")
    return actual


def reject_string_command(args: Any) -> None:
    if isinstance(args, (str, bytes)):
        raise _deny("永久拒绝字符串命令")


def sanitize_popen_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    _validate_no_shell(kwargs)
    for forbidden in ("executable", "preexec_fn", "args"):
        if forbidden in kwargs:
            raise _deny(f"禁止覆盖 {forbidden}")
    out = dict(kwargs)
    out.pop("shell", None)
    return out


_RUN_ALLOWED_KEYS = frozenset(
    {
        "capture_output",
        "text",
        "timeout",
        "encoding",
        "errors",
        "check",
    }
)


def sanitize_run_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    _validate_no_shell(kwargs)
    for forbidden in ("executable", "preexec_fn", "args", "env", "cwd"):
        if forbidden in kwargs:
            raise _deny(f"禁止覆盖 {forbidden}")
    unknown = sorted(k for k in kwargs if k not in _RUN_ALLOWED_KEYS and k != "shell")
    if unknown:
        raise _deny(f"未知 run 参数 {unknown[0]}")
    out = dict(kwargs)
    out.pop("shell", None)
    return out
