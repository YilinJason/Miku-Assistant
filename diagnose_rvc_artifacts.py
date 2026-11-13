# -*- coding: utf-8 -*-
"""
RVC 电流/金属伪影参数隔离评估。

默认 dry-run：零目录/文件创建、零子进程、零模型/Worker/设备/网络。
仅显式 ``--execute`` 才对固定 CosyVoice base 串行跑四组 RVC profile。
矩阵只变 ``index_rate`` / ``protect``；``filter_radius`` 固定为 1（RMVPE 不使用它）。
不改生产默认参数，不重跑基线 ``idx055_fr1``（其 protect=0.5），不自动播放。

用法：
    py diagnose_rvc_artifacts.py
    py diagnose_rvc_artifacts.py --execute
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
_scripts = project_root / "scripts"
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

from cosyvoice_eval_suite import EXPECTED_COUNT, SAMPLE_CASES  # noqa: E402
from src.core.config import Config  # noqa: E402
from src.voice.audio.temp_files import TempFileManager  # noqa: E402
from src.voice.naturalness_helpers import (  # noqa: E402
    build_execute_safety_policy,
    path_lexists,
    purge_sample_artifacts,
)
from src.voice.rvc.converter import RVCConverter  # noqa: E402
from src.voice.rvc.health_check import check_rvc_health  # noqa: E402
from src.voice.rvc.infer import (  # noqa: E402
    rvc_worker_running,
    stop_rvc_worker,
    warm_rvc_worker,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("diagnose_rvc_artifacts")

SCHEMA_VERSION = 1
INPUT_DIR_REL = "data/test/voice-naturalness/cosyvoice-instruct"
OUTPUT_DIR_REL = "data/test/voice-naturalness/rvc-artifact-eval"
LOCK_FILE_NAME = ".rvc_artifact_eval.lock"
REPORT_NAME = "report.json"

FIXED_PITCH = 0
FIXED_FILTER_RADIUS = 1
FIXED_RMS_MIX_RATE = 0.68
FIXED_F0_METHOD = "rmvpe"

ALLOWED_STAGES = frozenset(
    {
        "policy",
        "profile_params",
        "input_missing",
        "input_empty",
        "input_unreadable",
        "input_wav_invalid",
        "baseline_missing",
        "baseline_invalid",
        "lock_busy",
        "health",
        "staging",
        "adopt",
        "copy",
        "rvc",
        "shutdown",
        "worker_alive",
        "cleanup",
        "report",
        "unexpected",
    }
)


class ArtifactStageError(Exception):
    """固定阶段失败。消息只允许是阶段 token，不得携带路径或异常原文。"""

    def __init__(
        self,
        stage: str,
        *,
        label: Optional[str] = None,
        profile_id: Optional[str] = None,
    ) -> None:
        self.stage = stage if stage in ALLOWED_STAGES else "unexpected"
        self.label = label
        self.profile_id = profile_id
        super().__init__(self.stage)


@dataclass(frozen=True)
class RvcArtifactProfile:
    profile_id: str
    index_rate: float
    protect: float
    filter_radius: int


def _profile(profile_id: str, index_rate: float, protect: float) -> RvcArtifactProfile:
    if type(index_rate) is not float or type(protect) is not float:
        raise ArtifactStageError("profile_params")
    if type(FIXED_FILTER_RADIUS) is not int or FIXED_FILTER_RADIUS != 1:
        raise ArtifactStageError("profile_params")
    if not math.isfinite(index_rate) or not math.isfinite(protect):
        raise ArtifactStageError("profile_params")
    return RvcArtifactProfile(profile_id, index_rate, protect, FIXED_FILTER_RADIUS)


# 基线只引用上一轮 RVC，不进入运行矩阵。其 protect=0.5。
BASELINE_PROFILE = _profile("idx055_fr1", 0.55, 0.5)
RUN_PROFILES: Tuple[RvcArtifactProfile, ...] = (
    _profile("idx025_p050", 0.25, 0.5),
    _profile("idx055_p033", 0.55, 0.33),
    _profile("idx025_p033", 0.25, 0.33),
    _profile("idx000_p050", 0.0, 0.5),
)


def profile_pair_allowed(index_rate: object, protect: object) -> bool:
    """仅四组运行 profile 的精确 (index_rate, protect)。拒绝 bool / NaN / 越界。"""
    if type(index_rate) is not float or type(protect) is not float:
        return False
    if not math.isfinite(index_rate) or not math.isfinite(protect):
        return False
    for profile in RUN_PROFILES:
        if profile.index_rate == index_rate and profile.protect == protect:
            return True
    return False


def assert_profile_matrix() -> None:
    """启动前核对固定矩阵；失败即 profile_params，不碰设备。"""
    if type(FIXED_PITCH) is not int or FIXED_PITCH != 0:
        raise ArtifactStageError("profile_params")
    if type(FIXED_FILTER_RADIUS) is not int or FIXED_FILTER_RADIUS != 1:
        raise ArtifactStageError("profile_params")
    if type(FIXED_RMS_MIX_RATE) is not float or FIXED_RMS_MIX_RATE != 0.68:
        raise ArtifactStageError("profile_params")
    if FIXED_F0_METHOD != "rmvpe":
        raise ArtifactStageError("profile_params")
    expected = (
        ("idx025_p050", 0.25, 0.5, 1),
        ("idx055_p033", 0.55, 0.33, 1),
        ("idx025_p033", 0.25, 0.33, 1),
        ("idx000_p050", 0.0, 0.5, 1),
    )
    got = tuple(
        (profile.profile_id, profile.index_rate, profile.protect, profile.filter_radius)
        for profile in RUN_PROFILES
    )
    if got != expected:
        raise ArtifactStageError("profile_params")
    if (
        BASELINE_PROFILE.profile_id,
        BASELINE_PROFILE.index_rate,
        BASELINE_PROFILE.protect,
        BASELINE_PROFILE.filter_radius,
    ) != (
        "idx055_fr1",
        0.55,
        0.5,
        1,
    ):
        raise ArtifactStageError("profile_params")
    run_ids = {profile.profile_id for profile in RUN_PROFILES}
    if BASELINE_PROFILE.profile_id in run_ids or len(run_ids) != len(RUN_PROFILES):
        raise ArtifactStageError("profile_params")
    for profile in (BASELINE_PROFILE, *RUN_PROFILES):
        if profile.filter_radius != 1 or type(profile.protect) is not float:
            raise ArtifactStageError("profile_params")
    if not profile_pair_allowed(0.25, 0.5) or profile_pair_allowed(0.55, 0.5):
        raise ArtifactStageError("profile_params")
    if profile_pair_allowed(True, 0.5) or profile_pair_allowed(float("nan"), 0.33):
        raise ArtifactStageError("profile_params")
    if profile_pair_allowed(0.25, True) or profile_pair_allowed(0.0, 0.33):
        raise ArtifactStageError("profile_params")


def _require_run_profile(profile_id: str) -> RvcArtifactProfile:
    assert_profile_matrix()
    for profile in RUN_PROFILES:
        if profile.profile_id == profile_id:
            return profile
    raise ArtifactStageError("profile_params")


def build_profile_rvc_config(
    effective: Mapping[str, Any],
    profile_id: str,
) -> Dict[str, Any]:
    """
    只按固定 profile id 覆盖 index_rate / protect。

    filter_radius 固定为 1。其余推理参数锁死为任务给定值；后处理键沿用已封存配置。
    忽略调用方试图附带的任意路径或数值。失败回退保持关闭。
    """
    profile = _require_run_profile(profile_id)
    config = dict(effective)
    config["pitch"] = FIXED_PITCH
    config["protect"] = profile.protect
    config["rms_mix_rate"] = FIXED_RMS_MIX_RATE
    config["f0_method"] = FIXED_F0_METHOD
    config["index_rate"] = profile.index_rate
    config["filter_radius"] = profile.filter_radius
    config["worker_enabled"] = True
    config["fallback_on_worker_failure"] = False
    return config


def _repo_root() -> Path:
    return project_root


def _display_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(_repo_root().resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _is_memory_path(path: Path) -> bool:
    try:
        parts = [part.lower() for part in path.resolve().parts]
    except OSError:
        parts = [part.lower() for part in path.parts]
    return "data" in parts and "memory" in parts


def assert_eval_scope(path: Path) -> None:
    """评估读写不得落到 data/memory。"""
    if _is_memory_path(path):
        raise ArtifactStageError("unexpected")


def _known_label(label: Optional[str]) -> Optional[str]:
    if label is None:
        return None
    known = {case[0] for case in SAMPLE_CASES}
    if label not in known:
        return None
    return label


def _known_profile_id(profile_id: Optional[str]) -> Optional[str]:
    if profile_id is None:
        return None
    known = {profile.profile_id for profile in RUN_PROFILES}
    known.add(BASELINE_PROFILE.profile_id)
    if profile_id not in known:
        return None
    return profile_id


def sample_base_name(index: int, label: str) -> str:
    return f"{index:02d}_{label}_base.wav"


def staged_base_name(index: int, label: str, profile_id: str) -> str:
    """Staging 副本带 profile id，避免下一组覆盖上一组已登记文件。"""
    return f"{index:02d}_{label}_{profile_id}_base.wav"


def baseline_rvc_name(index: int, label: str) -> str:
    return f"{index:02d}_{label}_rvc.wav"


def sample_output_name(index: int, label: str, profile_id: str) -> str:
    return f"{index:02d}_{label}_{profile_id}.wav"


def staging_name_allowed(name: str) -> bool:
    """本次 staging 只允许固定 base 副本与固定 profile 输出名。"""
    for index, (label, _text, _instruct) in enumerate(SAMPLE_CASES, 1):
        for profile in RUN_PROFILES:
            if name == staged_base_name(index, label, profile.profile_id):
                return True
            if name == sample_output_name(index, label, profile.profile_id):
                return True
    return False


class EvalRunLock:
    """跨进程单实例锁；忙则立即 fail-closed。"""

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


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def read_wav_metrics(path: Path, *, role: str) -> Tuple[int, float, int]:
    """读取采样率、时长和字节数。失败映射为固定阶段，不向外抛原文。"""
    if role == "baseline":
        missing, empty, unread, invalid = (
            "baseline_missing",
            "baseline_invalid",
            "baseline_invalid",
            "baseline_invalid",
        )
    elif role == "output":
        missing = empty = unread = invalid = "copy"
    else:
        missing, empty, unread, invalid = (
            "input_missing",
            "input_empty",
            "input_unreadable",
            "input_wav_invalid",
        )
    assert_eval_scope(path)
    try:
        if path.is_symlink():
            raise ArtifactStageError(unread)
        if not path.is_file():
            raise ArtifactStageError(missing)
        size = path.stat().st_size
    except ArtifactStageError:
        raise
    except OSError:
        raise ArtifactStageError(unread) from None
    if type(size) is bool or size <= 0:
        raise ArtifactStageError(empty)
    try:
        import soundfile as sf

        info = sf.info(str(path))
        sample_rate = info.samplerate
        frames = info.frames
        duration = info.duration
    except ArtifactStageError:
        raise
    except Exception:
        raise ArtifactStageError(unread) from None
    if type(sample_rate) is bool or type(frames) is bool:
        raise ArtifactStageError(invalid)
    try:
        sample_rate_i = int(sample_rate)
        frames_i = int(frames)
        duration_f = float(duration)
    except (TypeError, ValueError):
        raise ArtifactStageError(invalid) from None
    if (
        sample_rate_i <= 0
        or frames_i <= 0
        or not math.isfinite(duration_f)
        or duration_f <= 0
    ):
        raise ArtifactStageError(invalid)
    return sample_rate_i, duration_f, int(size)


def validate_fixed_inputs(root: Path) -> None:
    """启动 RVC 前核对六段 base 与只读基线 RVC。"""
    input_dir = root / INPUT_DIR_REL
    assert_eval_scope(input_dir)
    if len(SAMPLE_CASES) != EXPECTED_COUNT or EXPECTED_COUNT != 6:
        raise ArtifactStageError("profile_params")
    for index, (label, _text, _instruct) in enumerate(SAMPLE_CASES, 1):
        try:
            read_wav_metrics(input_dir / sample_base_name(index, label), role="input")
            read_wav_metrics(
                input_dir / baseline_rvc_name(index, label),
                role="baseline",
            )
        except ArtifactStageError as exc:
            exc.label = label
            raise


def _refuse_protected(path: Path, input_dir: Path) -> None:
    assert_eval_scope(path)
    if _is_under(path, input_dir):
        raise ArtifactStageError("cleanup")


def purge_owned_files(
    temp_files: TempFileManager,
    input_dir: Path,
    *paths: Path,
) -> None:
    """只删除本次拥有、且不在输入目录内的文件。"""
    allowed: List[Path] = []
    for path in paths:
        if not path_lexists(path):
            continue
        _refuse_protected(path, input_dir)
        allowed.append(path)
    if allowed:
        purge_sample_artifacts(temp_files, *allowed)


def cleanup_owned_staging(
    temp_files: TempFileManager,
    staging: Path,
    input_dir: Path,
) -> None:
    """清理本次 staging。未知条目或输入目录内路径直接失败。"""
    if not path_lexists(staging):
        return
    _refuse_protected(staging, input_dir)
    if _is_under(staging, input_dir):
        raise ArtifactStageError("cleanup")
    if not staging.is_dir() or staging.is_symlink():
        raise ArtifactStageError("cleanup")
    for child in list(staging.iterdir()):
        if child.is_symlink() or child.is_dir() or not staging_name_allowed(child.name):
            raise ArtifactStageError("cleanup")
        _refuse_protected(child, input_dir)
    purge_owned_files(temp_files, input_dir, *list(staging.iterdir()))
    try:
        staging.rmdir()
    except OSError:
        raise ArtifactStageError("cleanup") from None
    if path_lexists(staging):
        raise ArtifactStageError("cleanup")


class RvcArtifactSession:
    """
    一个 profile 的 RVC Worker 生命周期。

    不构造 TTS/播放器，避免打开扬声器。``shutdown(stop_rvc=True)``
    停止全局 Worker，并在进程仍存活时失败。
    """

    def __init__(
        self,
        *,
        profile_id: str,
        rvc_config: Mapping[str, Any],
        safety_policy: Any,
        temp_files: TempFileManager,
    ) -> None:
        self.profile_id = profile_id
        self.rvc_config = dict(rvc_config)
        self.safety_policy = safety_policy
        self._temp_files = temp_files
        if rvc_worker_running():
            raise ArtifactStageError("worker_alive", profile_id=profile_id)
        self._converter = RVCConverter(
            self.rvc_config,
            safety_policy=safety_policy,
            temp_file_manager=temp_files,
        )
        if not warm_rvc_worker(
            self.rvc_config,
            safety_policy=safety_policy,
            temp_file_manager=temp_files,
        ):
            stop_rvc_worker()
            raise ArtifactStageError("rvc", profile_id=profile_id)

    def convert_owned(self, owned_src: str) -> str:
        result = self._converter.convert_file(owned_src)
        if not result:
            raise ArtifactStageError("rvc", profile_id=self.profile_id)
        return result

    def shutdown(self, *, stop_rvc: bool = True) -> None:
        if not stop_rvc:
            raise ArtifactStageError("shutdown", profile_id=self.profile_id)
        exited = stop_rvc_worker()
        if not exited or rvc_worker_running():
            raise ArtifactStageError("shutdown", profile_id=self.profile_id)


def _fixed_param_block(profile: RvcArtifactProfile) -> Dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "index_rate": profile.index_rate,
        "filter_radius": profile.filter_radius,
        "pitch": FIXED_PITCH,
        "protect": profile.protect,
        "rms_mix_rate": FIXED_RMS_MIX_RATE,
        "f0_method": FIXED_F0_METHOD,
        "postprocess": "existing_light",
    }


def _baseline_report_rows(root: Path) -> List[Dict[str, Any]]:
    input_dir = root / INPUT_DIR_REL
    rows: List[Dict[str, Any]] = []
    for index, (label, _text, _instruct) in enumerate(SAMPLE_CASES, 1):
        path = input_dir / baseline_rvc_name(index, label)
        sample_rate, duration, size = read_wav_metrics(path, role="baseline")
        rows.append(
            {
                "label": label,
                "file": f"{INPUT_DIR_REL}/{baseline_rvc_name(index, label)}",
                "bytes": size,
                "sample_rate": sample_rate,
                "duration_sec": round(duration, 6),
                "rerun": False,
            }
        )
    return rows


def print_dry_run_plan() -> int:
    try:
        assert_profile_matrix()
    except ArtifactStageError:
        print("错误: stage=profile_params")
        return 1
    print("=" * 60)
    print("RVC 伪影参数隔离评估（dry-run）")
    print("=" * 60)
    print()
    print("本模式不创建目录/文件、不启动子进程、不加载模型/RVC Worker、")
    print("不访问 GPU/网络/麦克风/扬声器，不读取 data/memory，不改生产配置。")
    print("真实矩阵请显式传入 --execute。不会自动播放。")
    print("四个新增 profile 严格串行：每组六段完成后 shutdown(stop_rvc=True)，")
    print("确认 Worker 退出后才构建下一组。基线 idx055_fr1 只读引用，不重跑。")
    print()
    print(f"schema_version: {SCHEMA_VERSION}")
    print(f"输入: {INPUT_DIR_REL}")
    print(f"输出: {OUTPUT_DIR_REL}")
    print(
        "固定参数: "
        f"pitch={FIXED_PITCH} filter_radius={FIXED_FILTER_RADIUS} "
        f"rms_mix_rate={FIXED_RMS_MIX_RATE} f0_method={FIXED_F0_METHOD} "
        "postprocess=existing_light"
    )
    print(
        "基线: "
        f"{BASELINE_PROFILE.profile_id} "
        f"index_rate={BASELINE_PROFILE.index_rate} "
        f"protect={BASELINE_PROFILE.protect} "
        f"filter_radius={BASELINE_PROFILE.filter_radius} rerun=false"
    )
    print("运行 profile：")
    for profile in RUN_PROFILES:
        print(
            f"  {profile.profile_id} "
            f"index_rate={profile.index_rate} "
            f"protect={profile.protect} "
            f"filter_radius={profile.filter_radius}"
        )
    print(f"样本数: {EXPECTED_COUNT}")
    print("固定 label：")
    for index, (label, _text, _instruct) in enumerate(SAMPLE_CASES, 1):
        print(f"  [{index:02d}_{label}]")
    print()
    print("提示: py diagnose_rvc_artifacts.py --execute")
    return 0


def _publish_failure(
    out_dir: Path,
    error: ArtifactStageError,
    *,
    profiles_completed: Sequence[str],
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "complete": False,
        "failure_stage": error.stage if error.stage in ALLOWED_STAGES else "unexpected",
        "failure_label": _known_label(error.label),
        "failure_profile_id": _known_profile_id(error.profile_id),
        "profiles_completed": [
            profile_id
            for profile_id in profiles_completed
            if _known_profile_id(profile_id) == profile_id
        ],
        "expected_new_profiles": len(RUN_PROFILES),
        "expected_samples_per_profile": EXPECTED_COUNT,
        "output_dir": OUTPUT_DIR_REL,
    }
    try:
        assert_eval_scope(out_dir)
        atomic_write_json(out_dir / REPORT_NAME, payload)
    except Exception:
        logger.error("stage=report")


def _print_stage(error: ArtifactStageError) -> None:
    label = _known_label(error.label) or "-"
    profile_id = _known_profile_id(error.profile_id) or "-"
    print(f"错误: stage={error.stage} profile={profile_id} label={label}")


def _convert_one(
    *,
    temp_files: TempFileManager,
    session: Any,
    staging: Path,
    input_dir: Path,
    persistent: Path,
    index: int,
    label: str,
    profile_id: str,
) -> Dict[str, Any]:
    staged_base = staging / staged_base_name(index, label, profile_id)
    try:
        if staged_base.exists() or _is_under(staged_base, input_dir):
            raise ArtifactStageError("staging", label=label, profile_id=profile_id)
        shutil.copy2(persistent, staged_base)
    except ArtifactStageError:
        raise
    except Exception:
        raise ArtifactStageError("copy", label=label, profile_id=profile_id) from None
    try:
        if staged_base.resolve() == persistent.resolve():
            raise ArtifactStageError("adopt", label=label, profile_id=profile_id)
        if not _is_under(staged_base, staging) or _is_under(staged_base, input_dir):
            raise ArtifactStageError("adopt", label=label, profile_id=profile_id)
        owned_src = temp_files.adopt_existing(staged_base)
        if temp_files.is_owned(persistent):
            raise ArtifactStageError("adopt", label=label, profile_id=profile_id)
        if Path(owned_src).resolve() == persistent.resolve():
            raise ArtifactStageError("adopt", label=label, profile_id=profile_id)
    except ArtifactStageError:
        raise
    except Exception:
        raise ArtifactStageError("adopt", label=label, profile_id=profile_id) from None

    started = time.perf_counter()
    try:
        rvc_out = session.convert_owned(owned_src)
        elapsed = time.perf_counter() - started
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ArtifactStageError("rvc", label=label, profile_id=profile_id)
        if not rvc_out or not Path(rvc_out).is_file() or Path(rvc_out).stat().st_size <= 0:
            raise ArtifactStageError("rvc", label=label, profile_id=profile_id)
        if not temp_files.is_owned(owned_src):
            raise ArtifactStageError("rvc", label=label, profile_id=profile_id)
    except ArtifactStageError:
        raise
    except Exception:
        raise ArtifactStageError("rvc", label=label, profile_id=profile_id) from None

    staged_out = staging / sample_output_name(index, label, profile_id)
    try:
        if _is_under(staged_out, input_dir) or not _is_under(staged_out, staging):
            raise ArtifactStageError("copy", label=label, profile_id=profile_id)
        shutil.copy2(rvc_out, staged_out)
        if staged_out.resolve() == persistent.resolve():
            raise ArtifactStageError("copy", label=label, profile_id=profile_id)
        temp_files.adopt_existing(staged_out)
        if Path(rvc_out).resolve() != staged_out.resolve():
            # 重写后的临时文件可能换了 inode；重新登记再删，避免泄漏。
            purge_owned_files(temp_files, input_dir, Path(rvc_out))
        if temp_files.is_owned(persistent):
            raise ArtifactStageError("cleanup", label=label, profile_id=profile_id)
    except ArtifactStageError:
        raise
    except Exception:
        raise ArtifactStageError("copy", label=label, profile_id=profile_id) from None
    return {
        "name": staged_out.name,
        "staged": staged_out,
        "label": label,
        "rvc_seconds": round(elapsed, 3),
    }


def _publish_profile_files(
    *,
    temp_files: TempFileManager,
    out_dir: Path,
    input_dir: Path,
    profile_id: str,
    staged_items: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    scratch: List[Path] = []
    replaced: List[Path] = []
    rows: List[Dict[str, Any]] = []
    try:
        planned: List[Tuple[Path, Path, Mapping[str, Any]]] = []
        for item in staged_items:
            final = out_dir / str(item["name"])
            publishing = out_dir / f".{item['name']}.publishing"
            _refuse_protected(final, input_dir)
            _refuse_protected(publishing, input_dir)
            scratch.append(publishing)
            shutil.copy2(item["staged"], publishing)
            planned.append((publishing, final, item))
        for publishing, final, item in planned:
            os.replace(str(publishing), str(final))
            if publishing in scratch:
                scratch.remove(publishing)
            replaced.append(final)
            if temp_files.is_owned(final):
                raise ArtifactStageError("copy", label=str(item["label"]), profile_id=profile_id)
            sample_rate, duration, size = read_wav_metrics(final, role="output")
            rows.append(
                {
                    "label": item["label"],
                    "file": final.name,
                    "bytes": size,
                    "sample_rate": sample_rate,
                    "duration_sec": round(duration, 6),
                    "rvc_seconds": item["rvc_seconds"],
                }
            )
        return rows
    except ArtifactStageError:
        _discard_publish_scratch(temp_files, input_dir, scratch, replaced)
        raise
    except Exception:
        _discard_publish_scratch(temp_files, input_dir, scratch, replaced)
        raise ArtifactStageError("copy", profile_id=profile_id) from None


def _discard_publish_scratch(
    temp_files: TempFileManager,
    input_dir: Path,
    scratch: Sequence[Path],
    replaced: Sequence[Path],
) -> None:
    """丢掉本次未完成发布的半成品，不碰输入目录和其它 profile。"""
    pending = [path for path in list(scratch) + list(replaced) if path_lexists(path)]
    if not pending:
        return
    purge_owned_files(temp_files, input_dir, *pending)


def run_execute(
    config: Config,
    *,
    session_cls: Callable[..., Any] = RvcArtifactSession,
    timeline: Optional[List[str]] = None,
    lock_cls: Callable[..., Any] = EvalRunLock,
    health_check: Callable[..., Any] = check_rvc_health,
    worker_running: Callable[[], bool] = rvc_worker_running,
) -> int:
    """串行跑四组 profile。失败返回非零，且不把后续 profile 送上 GPU。"""
    events = timeline if timeline is not None else []
    t_start = time.perf_counter()
    try:
        assert_profile_matrix()
    except ArtifactStageError as exc:
        _print_stage(exc)
        return 1

    rvc_config = config.get_rvc_config()
    if not rvc_config:
        print("错误: stage=policy profile=- label=-")
        return 1
    safety, effective, _seal_err = build_execute_safety_policy(config, rvc_config)
    if safety is None or effective is None:
        print("错误: stage=policy profile=- label=-")
        return 1

    root = _repo_root()
    input_dir = root / INPUT_DIR_REL
    out_dir = root / OUTPUT_DIR_REL
    try:
        assert_eval_scope(input_dir)
        assert_eval_scope(out_dir)
        input_error: Optional[ArtifactStageError] = None
        try:
            validate_fixed_inputs(root)
        except ArtifactStageError as exc:
            input_error = exc
    except ArtifactStageError as exc:
        _print_stage(exc)
        return 1

    run_lock = lock_cls(out_dir / LOCK_FILE_NAME)
    if not run_lock.acquire():
        print("错误: stage=lock_busy profile=- label=-")
        run_lock.release()
        return 1

    staging: Optional[Path] = None
    temp_files: Optional[TempFileManager] = None
    staging_cleaned = False
    profiles_completed: List[str] = []
    profile_rows: List[Dict[str, Any]] = []
    failure: Optional[ArtifactStageError] = None

    def _fail(error: ArtifactStageError) -> int:
        nonlocal failure
        failure = error
        _print_stage(error)
        _publish_failure(out_dir, error, profiles_completed=profiles_completed)
        return 1

    try:
        print("=" * 60)
        print("RVC 伪影参数隔离评估（--execute）")
        print("=" * 60)
        print()
        print(f"输出目录: {_display_rel(out_dir)}")
        print("阶段: 固定 base → 串行 RVC profile；基线不重跑")
        print()
        if input_error is not None:
            return _fail(input_error)

        try:
            health = health_check(effective, safety_policy=safety)
        except Exception:
            return _fail(ArtifactStageError("health"))
        if not getattr(health, "ok", False):
            return _fail(ArtifactStageError("health"))

        safety.register_file_delete_dir(out_dir)
        temp_files = TempFileManager(safety_policy=safety, default_dir=out_dir)
        staging = out_dir / f"_staging_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        if path_lexists(staging) or _is_under(staging, input_dir):
            return _fail(ArtifactStageError("staging"))
        try:
            staging.mkdir(parents=True, exist_ok=False)
        except OSError:
            return _fail(ArtifactStageError("staging"))
        safety.register_file_delete_dir(staging)

        for profile in RUN_PROFILES:
            if worker_running():
                return _fail(ArtifactStageError("worker_alive", profile_id=profile.profile_id))
            try:
                rvc_cfg = build_profile_rvc_config(effective, profile.profile_id)
            except ArtifactStageError as exc:
                return _fail(exc)
            events.append(f"init:{profile.profile_id}")
            session = None
            sample_error: Optional[ArtifactStageError] = None
            shutdown_error: Optional[ArtifactStageError] = None
            staged_items: List[Dict[str, Any]] = []
            try:
                session = session_cls(
                    profile_id=profile.profile_id,
                    rvc_config=rvc_cfg,
                    safety_policy=safety,
                    temp_files=temp_files,
                )
                for index, (label, _text, _instruct) in enumerate(SAMPLE_CASES, 1):
                    persistent = input_dir / sample_base_name(index, label)
                    try:
                        staged_items.append(
                            _convert_one(
                                temp_files=temp_files,
                                session=session,
                                staging=staging,
                                input_dir=input_dir,
                                persistent=persistent,
                                index=index,
                                label=label,
                                profile_id=profile.profile_id,
                            )
                        )
                    except ArtifactStageError as exc:
                        if exc.label is None:
                            exc.label = label
                        if exc.profile_id is None:
                            exc.profile_id = profile.profile_id
                        sample_error = exc
                        break
            except ArtifactStageError as exc:
                if exc.profile_id is None:
                    exc.profile_id = profile.profile_id
                sample_error = exc
            except Exception:
                sample_error = ArtifactStageError("rvc", profile_id=profile.profile_id)

            if session is not None:
                try:
                    session.shutdown(stop_rvc=True)
                    events.append(f"stop:{profile.profile_id}")
                except ArtifactStageError as exc:
                    if exc.profile_id is None:
                        exc.profile_id = profile.profile_id
                    shutdown_error = exc
                except Exception:
                    shutdown_error = ArtifactStageError(
                        "shutdown",
                        profile_id=profile.profile_id,
                    )
            else:
                try:
                    if not stop_rvc_worker() or worker_running():
                        shutdown_error = ArtifactStageError(
                            "shutdown",
                            profile_id=profile.profile_id,
                        )
                except Exception:
                    shutdown_error = ArtifactStageError(
                        "shutdown",
                        profile_id=profile.profile_id,
                    )

            if shutdown_error is not None:
                return _fail(shutdown_error)
            if sample_error is not None:
                return _fail(sample_error)
            if len(staged_items) != EXPECTED_COUNT:
                return _fail(
                    ArtifactStageError("rvc", profile_id=profile.profile_id)
                )
            try:
                rows = _publish_profile_files(
                    temp_files=temp_files,
                    out_dir=out_dir,
                    input_dir=input_dir,
                    profile_id=profile.profile_id,
                    staged_items=staged_items,
                )
            except ArtifactStageError as exc:
                if exc.profile_id is None:
                    exc.profile_id = profile.profile_id
                return _fail(exc)
            profile_rows.append({**_fixed_param_block(profile), "samples": rows})
            profiles_completed.append(profile.profile_id)
            print(f"[{profile.profile_id}] {EXPECTED_COUNT}/{EXPECTED_COUNT} ok")

        if staging is None or temp_files is None:
            return _fail(ArtifactStageError("staging"))
        try:
            cleanup_owned_staging(temp_files, staging, input_dir)
            staging_cleaned = True
            events.append("staging_cleaned")
        except ArtifactStageError as exc:
            return _fail(exc)
        except Exception:
            return _fail(ArtifactStageError("cleanup"))
        if worker_running():
            return _fail(ArtifactStageError("worker_alive"))

        try:
            baseline_rows = _baseline_report_rows(root)
            total = time.perf_counter() - t_start
            if not math.isfinite(total) or total < 0:
                raise ArtifactStageError("report")
            payload = {
                "schema_version": SCHEMA_VERSION,
                "complete": True,
                "output_dir": OUTPUT_DIR_REL,
                "input_dir": INPUT_DIR_REL,
                "total_seconds": round(total, 3),
                "worker_stopped": True,
                "fixed": {
                    "pitch": FIXED_PITCH,
                    "filter_radius": FIXED_FILTER_RADIUS,
                    "rms_mix_rate": FIXED_RMS_MIX_RATE,
                    "f0_method": FIXED_F0_METHOD,
                    "postprocess": "existing_light",
                },
                "baseline": {
                    **_fixed_param_block(BASELINE_PROFILE),
                    "rerun": False,
                    "samples": baseline_rows,
                },
                "profiles": profile_rows,
            }
            atomic_write_json(out_dir / REPORT_NAME, payload)
            events.append("report_published")
        except ArtifactStageError as exc:
            return _fail(exc)
        except Exception:
            return _fail(ArtifactStageError("report"))
        print()
        print(f"完成: {len(RUN_PROFILES)} profile；报告: {OUTPUT_DIR_REL}/{REPORT_NAME}")
        return 0
    except ArtifactStageError as exc:
        return _fail(exc)
    except Exception:
        logger.error("stage=unexpected")
        return _fail(ArtifactStageError("unexpected"))
    finally:
        if (
            staging is not None
            and temp_files is not None
            and not staging_cleaned
        ):
            try:
                cleanup_owned_staging(temp_files, staging, input_dir)
                staging_cleaned = True
            except Exception:
                logger.error("stage=cleanup")
        if temp_files is not None:
            try:
                temp_files.shutdown(timeout=0.5)
            except Exception:
                logger.error("stage=cleanup")
        try:
            if worker_running():
                stop_rvc_worker()
        except Exception:
            logger.error("stage=shutdown")
        run_lock.release()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="RVC 伪影参数隔离评估（默认 dry-run，仅接受 --execute）"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="对固定 CosyVoice base 串行运行四组 RVC profile",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.execute:
        return print_dry_run_plan()
    try:
        config = Config()
    except Exception:
        print("错误: stage=policy profile=- label=-")
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
