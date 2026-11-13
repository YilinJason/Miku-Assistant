"""
系统安全策略模块。

所有可能影响本机的操作必须经 ``SafetyPolicy`` 授权，并通过 ``safe_*`` 工具函数执行。

原则：
- 注册表写入：永久禁止（代码层硬编码）
- 注册表读取：默认禁止，需在 config 中显式开启
- 通用 ``subprocess``：默认禁止；RVC 使用具名专用能力，不得回退到通用执行
- 其他高风险操作：默认禁止或需 config 显式开启
- 未来新功能必须通过 SafetyPolicy / safe_* 工具函数，禁止直接调用 winreg、subprocess 等
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from src.core.rvc_capabilities import (
    RvcInferLaunch,
    RvcProbeKind,
    RvcWorkerLaunch,
    assert_executable_matches,
    build_infer_argv,
    build_probe_argv,
    build_worker_argv,
    reject_string_command,
    sanitize_popen_kwargs,
    sanitize_run_kwargs,
)
from src.voice.rvc.cpu_budget import child_env_with_rvc_cpu_budget



@dataclass(frozen=True)
class FileIdentity:
    """普通文件身份：path + 平台稳定元数据；用于检测同名替换。"""

    path: str
    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int


def capture_file_identity(path: Union[str, Path]) -> FileIdentity:
    """捕获普通文件身份；symlink/目录/缺失即失败。"""
    p = Path(path)
    try:
        if p.is_symlink():
            raise PermissionError("拒绝 symlink/junction")
        resolved = p.resolve()
        st = resolved.stat()
    except OSError as exc:
        raise PermissionError("文件身份无法读取") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise PermissionError("不是普通文件")
    return FileIdentity(
        path=str(resolved),
        st_dev=int(st.st_dev),
        st_ino=int(getattr(st, "st_ino", 0) or 0),
        st_size=int(st.st_size),
        st_mtime_ns=int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
    )


def identities_match(
    expected: FileIdentity,
    actual: FileIdentity,
    *,
    strict_meta: bool = False,
) -> bool:
    """
    同一普通文件：path + st_dev/st_ino。

    ``strict_meta=True`` 时额外比对 size/mtime，用于封存的解释器/模型/索引，
    检测原地改写；临时文件内容写入不得用 strict_meta。
    """
    if expected.path != actual.path:
        return False
    if not (expected.st_ino and actual.st_ino):
        # 平台无法提供 ino 时 fail closed（不假装同一文件）
        return False
    if expected.st_dev != actual.st_dev or expected.st_ino != actual.st_ino:
        return False
    if strict_meta:
        return (
            expected.st_size == actual.st_size
            and expected.st_mtime_ns == actual.st_mtime_ns
        )
    return True


@dataclass(frozen=True)
class TrustedRvcProfile:
    """构造期一次性传入的受信 RVC profile；发布后不可再变更。"""

    python_path: Union[str, Path]
    model_path: Optional[Union[str, Path]] = None
    index_path: Optional[Union[str, Path]] = None


@dataclass(frozen=True)
class TrustedLocalTtsProfile:
    """构造期一次性传入的受信本地 TTS（CosyVoice runtime）profile。"""

    repo_root: Union[str, Path]


@dataclass(frozen=True)
class TrustedLocalAsrProfile:
    """构造期一次性传入的受信本地 ASR（Paraformer runtime）profile。"""

    repo_root: Union[str, Path]


@dataclass(frozen=True)
class TempFileShutdownStatus:
    """TempFileManager 关闭可观察契约。"""

    complete: bool
    owned: int
    pending: int
    inflight: int
    worker_alive: bool
    active_io_leases: int = 0
    pending_cleanup: int = 0
    worker_start_unknown: bool = False
    worker_creator_pending: bool = False


class Operation(str, Enum):
    """受安全策略管控的操作类型枚举。"""

    REGISTRY_READ = "registry_read"
    REGISTRY_WRITE = "registry_write"
    SYSTEM_POWER = "system_power"
    SUBPROCESS = "subprocess"
    OS_SHELL = "os_shell"
    BROWSER_LAUNCH = "browser_launch"
    FILE_DELETE = "file_delete"
    ENV_MODIFY = "env_modify"
    RVC_WORKER = "rvc_worker"
    RVC_INFER = "rvc_infer"
    RVC_PROBE = "rvc_probe"
    REMOTE_MODEL_ENDPOINT = "remote_model_endpoint"
    INSECURE_REMOTE_MODEL_HTTP = "insecure_remote_model_http"
    CLOUD_STT = "cloud_stt"
    CLOUD_TTS = "cloud_tts"
    LOCAL_TTS_WORKER = "local_tts_worker"
    LOCAL_ASR_WORKER = "local_asr_worker"


# 注册表写入永远不允许，即使用户在配置中尝试开启
_IMMUTABLE_DENY = frozenset({Operation.REGISTRY_WRITE})

_SYSTEM_POWER_ARGV = {
    "shutdown": ("shutdown", "/s", "/t", "60"),
    "restart": ("shutdown", "/r", "/t", "60"),
    "lock": ("rundll32.exe", "user32.dll,LockWorkStation"),
}

_PERMISSION_KEYS = tuple(op.value for op in Operation)


def _require_bool(path: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be bool, got {type(value).__name__}")
    return value


def parse_security_permissions(
    raw: Optional[Dict[str, Any]],
    *,
    defaults: Dict[str, bool],
) -> Dict[str, bool]:
    """
    严格解析 ``security.permissions``；错误在任何设备/子进程副作用前失败。

    未知键拒绝；已知键若出现则必须是 bool。
    """
    permissions = dict(raw or {})
    unknown = sorted(k for k in permissions if k not in defaults)
    if unknown:
        raise ValueError(f"security.permissions 含未知键: {unknown[0]}")
    resolved: Dict[str, bool] = {}
    for key, default in defaults.items():
        if key not in permissions:
            resolved[key] = default
            continue
        resolved[key] = _require_bool(f"security.permissions.{key}", permissions[key])
    return resolved


class SafetyPolicy:
    """
    集中式安全策略。

    从 ``config/security`` 加载各操作权限，管理文件删除白名单目录，
    并在 ``MIKU_DRY_RUN=1`` 时自动禁用高风险操作与全部子进程能力。
    """

    _DEFAULT_PERMISSIONS: Dict[str, bool] = {
        Operation.REGISTRY_READ.value: False,
        Operation.REGISTRY_WRITE.value: False,
        Operation.SYSTEM_POWER.value: False,
        Operation.SUBPROCESS.value: False,
        Operation.OS_SHELL.value: False,
        Operation.BROWSER_LAUNCH.value: False,
        Operation.FILE_DELETE.value: True,
        Operation.ENV_MODIFY.value: False,
        # 裸 SafetyPolicy() 永久拒绝 RVC 专用能力；仅 from_app_config / 显式配置可开启
        Operation.RVC_WORKER.value: False,
        Operation.RVC_INFER.value: False,
        Operation.RVC_PROBE.value: False,
        Operation.REMOTE_MODEL_ENDPOINT.value: False,
        Operation.INSECURE_REMOTE_MODEL_HTTP.value: False,
        Operation.CLOUD_STT.value: False,
        Operation.CLOUD_TTS.value: False,
        Operation.LOCAL_TTS_WORKER.value: False,
        Operation.LOCAL_ASR_WORKER.value: False,
    }

    # 正式应用配置缺省（与 config.yaml / Config 默认一致）；不得用于裸构造旁路
    _APP_DEFAULT_RVC_PERMISSIONS: Dict[str, bool] = {
        Operation.RVC_WORKER.value: True,
        Operation.RVC_INFER.value: True,
        Operation.RVC_PROBE.value: True,
    }
    # 正式应用默认本地 Paraformer + edge-tts；远端模型默认仍拒绝
    _APP_DEFAULT_NETWORK_PERMISSIONS: Dict[str, bool] = {
        Operation.CLOUD_STT.value: False,
        Operation.CLOUD_TTS.value: True,
    }
    _APP_DEFAULT_LOCAL_ASR_PERMISSIONS: Dict[str, bool] = {
        Operation.LOCAL_ASR_WORKER.value: True,
    }

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        trusted_rvc_profile: Optional[TrustedRvcProfile] = None,
        trusted_local_tts_profile: Optional[TrustedLocalTtsProfile] = None,
        trusted_local_asr_profile: Optional[TrustedLocalAsrProfile] = None,
    ):
        """
        从 security 配置节构建策略实例。

        Args:
            config: 安全配置字典，通常含 ``permissions`` 与
                ``file_delete_allowed_dirs``；为 ``None`` 时使用全部默认值。
            trusted_rvc_profile: 装配边界一次性受信 RVC profile；仅构造期生效。
            trusted_local_tts_profile: 构造期封存 CosyVoice runtime 固定资产。
            trusted_local_asr_profile: 构造期封存 Paraformer runtime 固定资产。
        """
        self.logger = logging.getLogger(__name__)
        config = config or {}
        permissions = dict(config.get("permissions") or {})

        # 兼容旧配置 system.execute_commands
        if "system_power" not in permissions and "execute_commands" in config:
            permissions["system_power"] = bool(config.get("execute_commands"))

        self._permissions = parse_security_permissions(
            permissions, defaults=dict(self._DEFAULT_PERMISSIONS)
        )

        self._file_delete_dirs: List[Path] = [
            Path(p).resolve()
            for p in (config.get("file_delete_allowed_dirs") or [])
        ]

        self._rvc_python: Optional[Path] = None
        self._rvc_model: Optional[Path] = None
        self._rvc_index: Optional[Path] = None
        self._rvc_python_id: Optional[FileIdentity] = None
        self._rvc_model_id: Optional[FileIdentity] = None
        self._rvc_index_id: Optional[FileIdentity] = None
        self._rvc_profile_sealed: bool = False
        self._local_tts_python: Optional[Path] = None
        self._local_tts_worker_script: Optional[Path] = None
        self._local_tts_cache_root: Optional[Path] = None
        self._local_tts_cosyvoice_root: Optional[Path] = None
        self._local_tts_matcha_root: Optional[Path] = None
        self._local_tts_wetext_root: Optional[Path] = None
        self._local_tts_model_dir: Optional[Path] = None
        self._local_tts_python_id: Optional[FileIdentity] = None
        self._local_tts_worker_script_id: Optional[FileIdentity] = None
        self._local_tts_model_file_ids: Optional[Dict[str, FileIdentity]] = None
        self._local_tts_repo_root: Optional[Path] = None
        self._local_tts_profile_sealed: bool = False
        self._local_asr_python: Optional[Path] = None
        self._local_asr_worker_script: Optional[Path] = None
        self._local_asr_model_dir: Optional[Path] = None
        self._local_asr_python_id: Optional[FileIdentity] = None
        self._local_asr_worker_script_id: Optional[FileIdentity] = None
        self._local_asr_model_file_ids: Optional[Dict[str, FileIdentity]] = None
        self._local_asr_repo_root: Optional[Path] = None
        self._local_asr_profile_sealed: bool = False
        self._run_fn: Callable[..., subprocess.CompletedProcess] = subprocess.run
        self._popen_fn: Callable[..., subprocess.Popen] = subprocess.Popen
        self._owned_worker_job_backend: Any = None
        self._owned_rvc_run_factory_lock = threading.Lock()

        env_dry = os.environ.get("MIKU_DRY_RUN", "").lower() in ("1", "true", "yes")
        if env_dry:
            self._permissions[Operation.SYSTEM_POWER.value] = False
            self._permissions[Operation.SUBPROCESS.value] = False
            self._permissions[Operation.OS_SHELL.value] = False
            self._permissions[Operation.BROWSER_LAUNCH.value] = False
            self._permissions[Operation.RVC_WORKER.value] = False
            self._permissions[Operation.RVC_INFER.value] = False
            self._permissions[Operation.RVC_PROBE.value] = False
            self._permissions[Operation.REMOTE_MODEL_ENDPOINT.value] = False
            self._permissions[Operation.INSECURE_REMOTE_MODEL_HTTP.value] = False
            self._permissions[Operation.CLOUD_STT.value] = False
            self._permissions[Operation.CLOUD_TTS.value] = False
            self._permissions[Operation.LOCAL_TTS_WORKER.value] = False
            self._permissions[Operation.LOCAL_ASR_WORKER.value] = False
            self.logger.warning(
                "MIKU_DRY_RUN=1：已禁用系统电源、通用/专用子进程、Shell、浏览器启动、"
                "远端模型端点、云 STT/TTS 与本地 TTS/ASR worker"
            )

        if trusted_rvc_profile is not None:
            self._seal_trusted_rvc_profile(trusted_rvc_profile)
        if trusted_local_tts_profile is not None:
            self._seal_trusted_local_tts_profile(trusted_local_tts_profile)
        if trusted_local_asr_profile is not None:
            self._seal_trusted_local_asr_profile(trusted_local_asr_profile)

    @classmethod
    def from_app_config(
        cls,
        app_config,
        *,
        trusted_rvc_profile: Optional[TrustedRvcProfile] = None,
        trusted_local_tts_profile: Optional[TrustedLocalTtsProfile] = None,
        trusted_local_asr_profile: Optional[TrustedLocalAsrProfile] = None,
    ) -> "SafetyPolicy":
        """
        从 ``Config`` 应用配置对象构建 ``SafetyPolicy``。

        正式应用缺省会合并 ``_APP_DEFAULT_RVC_PERMISSIONS``（与 config.yaml 一致）；
        裸 ``SafetyPolicy()`` 仍拒绝全部 RVC 专用能力。
        受信 RVC profile 仅能经 ``trusted_rvc_profile`` 在构造期封存。
        """
        security = dict(app_config.get("security", {}) or {})
        legacy_execute = app_config.get("system.execute_commands")
        perms = dict(security.get("permissions") or {})
        if legacy_execute is not None and "system_power" not in perms:
            perms["system_power"] = bool(legacy_execute)
        for key, value in cls._APP_DEFAULT_RVC_PERMISSIONS.items():
            perms.setdefault(key, value)
        for key, value in cls._APP_DEFAULT_NETWORK_PERMISSIONS.items():
            perms.setdefault(key, value)
        for key, value in cls._APP_DEFAULT_LOCAL_ASR_PERMISSIONS.items():
            perms.setdefault(key, value)
        security["permissions"] = perms
        return cls(
            security,
            trusted_rvc_profile=trusted_rvc_profile,
            trusted_local_tts_profile=trusted_local_tts_profile,
            trusted_local_asr_profile=trusted_local_asr_profile,
        )

    def bind_process_runners(
        self,
        *,
        run_fn: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        popen_fn: Optional[Callable[..., subprocess.Popen]] = None,
        owned_worker_job_backend: Any = None,
    ) -> None:
        """注入假 runner/popen，供行为测试在不创建真实子进程时验证。"""
        if run_fn is not None:
            self._run_fn = run_fn
        if popen_fn is not None:
            self._popen_fn = popen_fn
        if owned_worker_job_backend is not None:
            self._owned_worker_job_backend = owned_worker_job_backend

    def register_rvc_python(self, python_path: Union[str, Path]) -> Path:
        """已废弃；已发布策略不得登记/封存 RVC profile。"""
        self.logger.warning("RVC 子进程能力拒绝: 下层不得登记解释器")
        raise PermissionError("RVC 子进程能力拒绝: 下层不得登记解释器")

    def register_rvc_profile(
        self,
        *,
        python_path: Union[str, Path],
        model_path: Union[str, Path],
        index_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """已废弃；已发布策略不得登记/封存 RVC profile。"""
        self.logger.warning("RVC 子进程能力拒绝: 下层不得登记 profile")
        raise PermissionError("RVC 子进程能力拒绝: 下层不得登记 profile")

    def seal_rvc_profile(
        self,
        *,
        python_path: Union[str, Path],
        model_path: Optional[Union[str, Path]] = None,
        index_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """公开封存入口已关闭；请在构造时传入 ``trusted_rvc_profile``。"""
        self.logger.warning("RVC 子进程能力拒绝: 已发布策略不得封存 profile")
        raise PermissionError("RVC 子进程能力拒绝: 已发布策略不得封存 profile")

    def _seal_trusted_rvc_profile(self, profile: TrustedRvcProfile) -> None:
        """构造期内部封存；含严格元数据身份。"""
        if self._rvc_profile_sealed:
            raise PermissionError("RVC 子进程能力拒绝: profile 已封存")
        python_id = capture_file_identity(profile.python_path)
        model_id = (
            capture_file_identity(profile.model_path) if profile.model_path else None
        )
        index_id = (
            capture_file_identity(profile.index_path) if profile.index_path else None
        )
        self._rvc_python = Path(python_id.path)
        self._rvc_python_id = python_id
        self._rvc_model = Path(model_id.path) if model_id else None
        self._rvc_model_id = model_id
        self._rvc_index = Path(index_id.path) if index_id else None
        self._rvc_index_id = index_id
        self._rvc_profile_sealed = True

    def _seal_trusted_local_tts_profile(self, profile: TrustedLocalTtsProfile) -> None:
        from src.voice.cosyvoice.assets import (
            MODEL_REQUIRED_FILES,
            revalidate_fixed_layout_paths,
        )
        from src.voice.cosyvoice.path_trust import seal_trusted_repo_root

        if self._local_tts_profile_sealed:
            raise PermissionError("本地 TTS worker 能力拒绝: profile 已封存")
        try:
            repo = seal_trusted_repo_root(Path(profile.repo_root))
        except PermissionError as exc:
            raise PermissionError(f"本地 TTS worker 能力拒绝: {exc}") from exc
        try:
            layout = revalidate_fixed_layout_paths(repo)
        except PermissionError as exc:
            raise PermissionError(f"本地 TTS worker 能力拒绝: {exc}") from exc
        self._local_tts_repo_root = layout["repo_root"]
        python_id = capture_file_identity(layout["cosyvoice_python"])
        worker_id = capture_file_identity(layout["runtime_worker_script"])
        model_ids: Dict[str, FileIdentity] = {}
        model_dir = layout["model_dir"]
        for rel in MODEL_REQUIRED_FILES:
            model_ids[rel] = capture_file_identity(model_dir / rel)
        self._local_tts_python = Path(python_id.path)
        self._local_tts_worker_script = Path(worker_id.path)
        self._local_tts_cache_root = layout["cache_root"]
        self._local_tts_cosyvoice_root = layout["vendor_root"]
        self._local_tts_matcha_root = layout["matcha_dir"]
        self._local_tts_wetext_root = layout["wetext_dir"]
        self._local_tts_model_dir = model_dir
        self._local_tts_python_id = python_id
        self._local_tts_worker_script_id = worker_id
        self._local_tts_model_file_ids = model_ids
        self._local_tts_profile_sealed = True

    def _seal_trusted_local_asr_profile(self, profile: TrustedLocalAsrProfile) -> None:
        from src.voice.cosyvoice.path_trust import seal_trusted_repo_root
        from src.voice.paraformer.assets import (
            MODEL_REQUIRED_FILES,
            revalidate_fixed_layout_paths,
        )

        if self._local_asr_profile_sealed:
            raise PermissionError("本地 ASR worker 能力拒绝: profile 已封存")
        try:
            repo = seal_trusted_repo_root(Path(profile.repo_root))
        except PermissionError as exc:
            raise PermissionError(f"本地 ASR worker 能力拒绝: {exc}") from exc
        try:
            layout = revalidate_fixed_layout_paths(repo)
        except PermissionError as exc:
            raise PermissionError(f"本地 ASR worker 能力拒绝: {exc}") from exc
        self._local_asr_repo_root = layout["repo_root"]
        python_id = capture_file_identity(layout["asr_python"])
        worker_id = capture_file_identity(layout["runtime_worker_script"])
        model_ids: Dict[str, FileIdentity] = {}
        model_dir = layout["model_dir"]
        for rel in MODEL_REQUIRED_FILES:
            model_ids[rel] = capture_file_identity(model_dir / rel)
        self._local_asr_python = Path(python_id.path)
        self._local_asr_worker_script = Path(worker_id.path)
        self._local_asr_model_dir = model_dir
        self._local_asr_python_id = python_id
        self._local_asr_worker_script_id = worker_id
        self._local_asr_model_file_ids = model_ids
        self._local_asr_profile_sealed = True

    def local_asr_profile_sealed(self) -> bool:
        return self._local_asr_profile_sealed

    def local_tts_profile_sealed(self) -> bool:
        return self._local_tts_profile_sealed

    def rvc_python(self) -> Optional[Path]:
        """当前封存的 RVC 解释器；未封存时为 ``None``。"""
        return self._rvc_python

    def rvc_model(self) -> Optional[Path]:
        return self._rvc_model

    def rvc_index(self) -> Optional[Path]:
        return self._rvc_index

    def rvc_profile_sealed(self) -> bool:
        return self._rvc_profile_sealed

    def assert_sealed_rvc_python(self) -> Path:
        """启动/探测前复核已封存解释器的文件身份（含严格元数据）。"""
        self._assert_identity(self._rvc_python_id, label="解释器", strict_meta=True)
        if self._rvc_python is None:
            raise PermissionError("RVC 子进程能力拒绝: 未封存解释器")
        return self._rvc_python

    def _assert_identity(
        self,
        expected: Optional[FileIdentity],
        *,
        label: str,
        strict_meta: bool = True,
    ) -> FileIdentity:
        if expected is None:
            self.logger.warning("RVC 子进程能力拒绝: 未封存%s", label)
            raise PermissionError(f"RVC 子进程能力拒绝: 未封存{label}")
        try:
            actual = capture_file_identity(expected.path)
        except PermissionError:
            self.logger.warning("RVC 子进程能力拒绝: %s 身份无效或已替换", label)
            raise PermissionError(f"RVC 子进程能力拒绝: {label} 身份无效或已替换")
        if not identities_match(expected, actual, strict_meta=strict_meta):
            self.logger.warning("RVC 子进程能力拒绝: %s 文件身份已变化", label)
            raise PermissionError(f"RVC 子进程能力拒绝: {label} 文件身份已变化")
        return actual

    def assert_rvc_launch_assets(
        self,
        *,
        model_path: Union[str, Path],
        index_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """创建进程前校验模型/索引路径与封存身份（含严格元数据）。"""
        if not self._rvc_profile_sealed:
            self.logger.warning("RVC 子进程能力拒绝: profile 未封存")
            raise PermissionError("RVC 子进程能力拒绝: profile 未封存")
        self._assert_identity(self._rvc_python_id, label="解释器", strict_meta=True)
        if self._rvc_model_id is None:
            self.logger.warning("RVC 子进程能力拒绝: 未封存模型")
            raise PermissionError("RVC 子进程能力拒绝: 未封存模型")
        try:
            actual_model = Path(model_path).resolve()
        except OSError as exc:
            raise PermissionError("RVC 子进程能力拒绝: 模型路径无法解析") from exc
        if str(actual_model) != self._rvc_model_id.path:
            self.logger.warning("RVC 子进程能力拒绝: 意外模型")
            raise PermissionError("RVC 子进程能力拒绝: 意外模型")
        self._assert_identity(self._rvc_model_id, label="模型", strict_meta=True)
        if index_path:
            if self._rvc_index_id is None:
                self.logger.warning("RVC 子进程能力拒绝: 意外索引")
                raise PermissionError("RVC 子进程能力拒绝: 意外索引")
            try:
                actual_index = Path(index_path).resolve()
            except OSError as exc:
                raise PermissionError("RVC 子进程能力拒绝: 索引路径无法解析") from exc
            if str(actual_index) != self._rvc_index_id.path:
                self.logger.warning("RVC 子进程能力拒绝: 意外索引")
                raise PermissionError("RVC 子进程能力拒绝: 意外索引")
            self._assert_identity(self._rvc_index_id, label="索引", strict_meta=True)
        elif self._rvc_index_id is not None:
            # 封存了索引则启动也必须带同一索引
            self.logger.warning("RVC 子进程能力拒绝: 缺少已封存索引")
            raise PermissionError("RVC 子进程能力拒绝: 缺少已封存索引")

    def is_allowed(self, operation: Union[Operation, str]) -> bool:
        """
        查询指定操作是否被策略允许。

        Args:
            operation: ``Operation`` 枚举或对应字符串键。

        Returns:
            允许为 ``True``；注册表写入等不可变拒绝项恒为 ``False``。
        """
        key = operation.value if isinstance(operation, Operation) else operation
        if key in {op.value for op in _IMMUTABLE_DENY}:
            return False
        return self._permissions.get(key, False)

    def require(self, operation: Union[Operation, str], context: str = "") -> None:
        """
        断言操作被允许，否则抛出 ``PermissionError``。

        Args:
            operation: 待检查的操作类型。
            context: 附加说明；不得传入完整命令或用户正文。

        Raises:
            PermissionError: 操作未被策略允许时。
        """
        if not self.is_allowed(operation):
            key = operation.value if isinstance(operation, Operation) else operation
            msg = f"安全策略拒绝操作 [{key}]"
            if context:
                msg += f": {context}"
            self.logger.warning(msg)
            raise PermissionError(msg)

    def register_file_delete_dir(self, directory: Union[str, Path]) -> None:
        """
        将目录加入文件删除白名单（仅允许删除该目录及其子路径下的文件）。

        Args:
            directory: 允许删除的目录路径。
        """
        self._file_delete_dirs.append(Path(directory).resolve())

    def is_path_deletable(self, file_path: Union[str, Path]) -> bool:
        """
        判断给定文件路径是否在删除白名单内且 ``FILE_DELETE`` 权限已开启。

        注意：临时文件删除还需通过 ``TempFileManager`` 的所有权登记；
        本方法只判断目录白名单与权限。
        """
        if not self.is_allowed(Operation.FILE_DELETE):
            return False
        if not self._file_delete_dirs:
            return False
        try:
            resolved = Path(file_path).resolve()
        except OSError:
            return False
        return any(
            resolved == d or d in resolved.parents
            for d in self._file_delete_dirs
        )

    def summary(self) -> Dict[str, bool]:
        """
        返回各操作权限的快照，便于启动日志输出。

        Returns:
            操作键到是否允许的映射；不含路径、argv 或认证信息。
        """
        return {k: self.is_allowed(k) for k in self._permissions}

    def _require_registered_rvc_python(self) -> Path:
        if not self._rvc_profile_sealed or self._rvc_python is None:
            self.logger.warning("RVC 子进程能力拒绝: 未封存解释器")
            raise PermissionError("RVC 子进程能力拒绝: 未封存解释器")
        self._assert_identity(self._rvc_python_id, label="解释器", strict_meta=True)
        try:
            return assert_executable_matches(self._rvc_python, self._rvc_python)
        except PermissionError:
            raise
        except Exception as exc:
            self.logger.warning("RVC 子进程能力拒绝: 解释器无效")
            raise PermissionError("RVC 子进程能力拒绝: 解释器无效") from exc

    def spawn_rvc_worker(
        self,
        launch: RvcWorkerLaunch,
        **popen_kwargs: Any,
    ) -> subprocess.Popen:
        """经 ``rvc_worker`` 能力启动常驻 Worker；拒绝时不创建进程。"""
        self.require(Operation.RVC_WORKER, "rvc_worker")
        reject_string_command(popen_kwargs.get("args"))
        python = self._require_registered_rvc_python()
        self.assert_rvc_launch_assets(
            model_path=launch.model_path, index_path=launch.index_path
        )
        argv = build_worker_argv(python, launch)
        clean = sanitize_popen_kwargs(popen_kwargs)
        if os.name == "nt":
            import sys

            if sys.platform == "win32":
                from src.core.owned_worker_job import spawn_owned_voice_worker

                if launch.parent_pid != os.getpid():
                    raise PermissionError(
                        "RVC worker 能力拒绝: parent_pid 与启动进程不一致"
                    )
                merged = dict(clean)
                if merged.get("env") is None:
                    base_env = os.environ
                else:
                    base_env = merged["env"]
                merged["env"] = child_env_with_rvc_cpu_budget(base_env)
                return spawn_owned_voice_worker(
                    argv,
                    merged,
                    popen_fn=self._popen_fn,
                    parent_pid=launch.parent_pid,
                    job_backend=self._owned_worker_job_backend,
                )
        merged = dict(clean)
        if merged.get("env") is None:
            base_env = os.environ
        else:
            base_env = merged["env"]
        merged["env"] = child_env_with_rvc_cpu_budget(base_env)
        return self._popen_fn(argv, **merged)

    def run_rvc_infer(
        self,
        launch: RvcInferLaunch,
        *,
        temp_file_manager: Any = None,
        **run_kwargs: Any,
    ) -> subprocess.CompletedProcess:
        """经 ``rvc_infer`` 能力执行一次性推理；不回退通用 subprocess。"""
        self.require(Operation.RVC_INFER, "rvc_infer")
        reject_string_command(run_kwargs.get("args"))
        python = self._require_registered_rvc_python()
        self.assert_rvc_launch_assets(
            model_path=launch.model_path, index_path=launch.index_path
        )
        if temp_file_manager is None:
            self.logger.warning("RVC 子进程能力拒绝: 缺少临时文件所有权管理器")
            raise PermissionError("RVC 子进程能力拒绝: 缺少临时文件所有权管理器")
        temp_file_manager.assert_rvc_io_paths(
            launch.input_path, launch.output_path
        )
        argv = build_infer_argv(python, launch)
        clean = sanitize_run_kwargs(run_kwargs)
        if os.name == "nt" and sys.platform == "win32":
            from src.core.owned_rvc_run import execute_owned_rvc_run

            return execute_owned_rvc_run(
                self,
                argv,
                max_timeout_budget=90.0,
                io_paths=(str(launch.input_path), str(launch.output_path)),
                temp_file_manager=temp_file_manager,
                **clean,
            )
        merged = dict(clean)
        merged["env"] = child_env_with_rvc_cpu_budget(os.environ)
        return self._run_fn(argv, **merged)

    def run_rvc_probe(
        self,
        kind: RvcProbeKind,
        **run_kwargs: Any,
    ) -> subprocess.CompletedProcess:
        """经 ``rvc_probe`` 能力执行固定环境探测。"""
        self.require(Operation.RVC_PROBE, "rvc_probe")
        reject_string_command(run_kwargs.get("args"))
        python = self._require_registered_rvc_python()
        argv = build_probe_argv(python, kind)
        clean = sanitize_run_kwargs(run_kwargs)
        if os.name == "nt" and sys.platform == "win32":
            from src.core.owned_rvc_run import execute_owned_rvc_run

            return execute_owned_rvc_run(
                self,
                argv,
                max_timeout_budget=30.0,
                **clean,
            )
        merged = dict(clean)
        merged["env"] = child_env_with_rvc_cpu_budget(os.environ)
        return self._run_fn(argv, **merged)

    def retire_rvc_runs(self, timeout: float = 2.0) -> bool:
        """停止本 policy 当前单次 infer/probe run；可重试，严格 bool。"""
        from src.core.owned_rvc_run import retire_policy_rvc_runs

        return retire_policy_rvc_runs(self, timeout=timeout) is True

    def rvc_runs_quiescent(self) -> bool:
        """本 policy 单次 run 快照；不启动资源。"""
        from src.core.owned_rvc_run import policy_rvc_runs_quiescent

        return policy_rvc_runs_quiescent(self) is True

    def _require_sealed_local_tts_python(self) -> Path:
        if not self._local_tts_profile_sealed or self._local_tts_python is None:
            self.logger.warning("本地 TTS worker 能力拒绝: 未封存 profile")
            raise PermissionError("本地 TTS worker 能力拒绝: 未封存 profile")
        self._assert_identity(
            self._local_tts_python_id, label="CosyVoice 解释器", strict_meta=True
        )
        try:
            return assert_executable_matches(
                self._local_tts_python, self._local_tts_python
            )
        except PermissionError:
            raise
        except Exception as exc:
            self.logger.warning("本地 TTS worker 能力拒绝: 解释器无效")
            raise PermissionError("本地 TTS worker 能力拒绝: 解释器无效") from exc

    def assert_local_tts_launch_assets(self) -> None:
        """创建进程前复核封存身份与 pin 元数据（不整包 hash 模型）。"""
        from src.voice.cosyvoice.assets import (
            revalidate_fixed_layout_paths,
            verify_pinned_assets,
        )

        if not self._local_tts_profile_sealed:
            self.logger.warning("本地 TTS worker 能力拒绝: profile 未封存")
            raise PermissionError("本地 TTS worker 能力拒绝: profile 未封存")
        if self._local_tts_repo_root is None:
            raise PermissionError("本地 TTS worker 能力拒绝: 未封存仓库根")
        try:
            revalidate_fixed_layout_paths(self._local_tts_repo_root)
        except PermissionError as exc:
            raise PermissionError(f"本地 TTS worker 能力拒绝: {exc}") from exc
        self._assert_identity(
            self._local_tts_worker_script_id,
            label="runtime worker 脚本",
            strict_meta=True,
        )
        if self._local_tts_model_file_ids:
            for rel, expected_id in self._local_tts_model_file_ids.items():
                self._assert_identity(expected_id, label=f"模型文件 {rel}", strict_meta=True)
        if (
            self._local_tts_cosyvoice_root is None
            or self._local_tts_matcha_root is None
            or self._local_tts_wetext_root is None
            or self._local_tts_model_dir is None
        ):
            raise PermissionError("本地 TTS worker 能力拒绝: 固定资产不完整")
        try:
            verify_pinned_assets(
                cosyvoice_root=self._local_tts_cosyvoice_root,
                matcha_root=self._local_tts_matcha_root,
                wetext_root=self._local_tts_wetext_root,
                model_dir=self._local_tts_model_dir,
            )
        except RuntimeError as exc:
            self.logger.warning("本地 TTS worker 能力拒绝: pin 校验失败")
            raise PermissionError("本地 TTS worker 能力拒绝: pin 校验失败") from exc

    def spawn_local_tts_worker(
        self,
        launch: Any,
        **popen_kwargs: Any,
    ) -> subprocess.Popen:
        """经 ``local_tts_worker`` 能力启动 CosyVoice runtime worker。"""
        from src.voice.cosyvoice.capabilities import (
            build_runtime_popen_options,
            build_runtime_worker_argv,
            merge_local_tts_popen_kwargs,
        )

        self.require(Operation.LOCAL_TTS_WORKER, "local_tts_worker")
        reject_string_command(popen_kwargs.get("args"))
        python = self._require_sealed_local_tts_python()
        self.assert_local_tts_launch_assets()
        if self._local_tts_cache_root is None or self._local_tts_cosyvoice_root is None:
            raise PermissionError("本地 TTS worker 能力拒绝: 缺少固定资产根")
        if self._local_tts_worker_script is None:
            raise PermissionError("本地 TTS worker 能力拒绝: 未封存 worker 脚本")
        argv = build_runtime_worker_argv(
            python,
            self._local_tts_worker_script,
            launch,
            repo_root=self._local_tts_repo_root,
        )
        fixed = build_runtime_popen_options(
            cache_root=self._local_tts_cache_root,
            vendor_root=self._local_tts_cosyvoice_root,
            cpu_threads=launch.settings.cpu_threads,
        )
        merged = merge_local_tts_popen_kwargs(fixed, popen_kwargs)
        if os.name == "nt":
            import sys

            if sys.platform == "win32":
                from src.core.owned_worker_job import spawn_owned_local_tts_worker

                if launch.parent_pid != os.getpid():
                    raise PermissionError(
                        "本地 TTS worker 能力拒绝: parent_pid 与启动进程不一致"
                    )
                return spawn_owned_local_tts_worker(
                    argv,
                    merged,
                    popen_fn=self._popen_fn,
                    parent_pid=launch.parent_pid,
                    job_backend=self._owned_worker_job_backend,
                )
        return self._popen_fn(argv, **merged)

    def _require_sealed_local_asr_python(self) -> Path:
        if not self._local_asr_profile_sealed or self._local_asr_python is None:
            self.logger.warning("本地 ASR worker 能力拒绝: 未封存 profile")
            raise PermissionError("本地 ASR worker 能力拒绝: 未封存 profile")
        self._assert_identity(
            self._local_asr_python_id, label="Paraformer 解释器", strict_meta=True
        )
        try:
            return assert_executable_matches(
                self._local_asr_python, self._local_asr_python
            )
        except PermissionError:
            raise
        except Exception as exc:
            self.logger.warning("本地 ASR worker 能力拒绝: 解释器无效")
            raise PermissionError("本地 ASR worker 能力拒绝: 解释器无效") from exc

    def assert_local_asr_launch_assets(self) -> None:
        from src.voice.paraformer.assets import (
            revalidate_fixed_layout_paths,
            verify_pinned_assets,
        )

        if not self._local_asr_profile_sealed:
            self.logger.warning("本地 ASR worker 能力拒绝: profile 未封存")
            raise PermissionError("本地 ASR worker 能力拒绝: profile 未封存")
        if self._local_asr_repo_root is None:
            raise PermissionError("本地 ASR worker 能力拒绝: 未封存仓库根")
        try:
            revalidate_fixed_layout_paths(self._local_asr_repo_root)
        except PermissionError as exc:
            raise PermissionError(f"本地 ASR worker 能力拒绝: {exc}") from exc
        self._assert_identity(
            self._local_asr_worker_script_id,
            label="ASR runtime worker 脚本",
            strict_meta=True,
        )
        if self._local_asr_model_file_ids:
            for rel, expected_id in self._local_asr_model_file_ids.items():
                self._assert_identity(
                    expected_id, label=f"ASR 模型文件 {rel}", strict_meta=True
                )
        if (
            self._local_asr_model_dir is None
        ):
            raise PermissionError("本地 ASR worker 能力拒绝: 固定资产不完整")
        try:
            verify_pinned_assets(
                model_dir=self._local_asr_model_dir,
                repo_root=self._local_asr_repo_root,
            )
        except RuntimeError as exc:
            self.logger.warning("本地 ASR worker 能力拒绝: pin 校验失败")
            raise PermissionError("本地 ASR worker 能力拒绝: pin 校验失败") from exc

    def spawn_local_asr_worker(
        self,
        launch: Any,
        **popen_kwargs: Any,
    ) -> subprocess.Popen:
        """经 ``local_asr_worker`` 能力启动 Paraformer runtime worker。"""
        from src.voice.paraformer.capabilities import (
            LocalAsrWorkerLaunch,
            build_runtime_popen_options,
            build_runtime_worker_argv,
            merge_local_asr_popen_kwargs,
        )

        from src.voice.paraformer.settings import ParaformerSettings

        self.require(Operation.LOCAL_ASR_WORKER, "local_asr_worker")
        reject_string_command(popen_kwargs.get("args"))
        if not isinstance(launch, LocalAsrWorkerLaunch):
            raise PermissionError("本地 ASR worker 能力拒绝: launch 类型无效")
        if not isinstance(launch.settings, ParaformerSettings):
            raise PermissionError("本地 ASR worker 能力拒绝: settings 类型无效")
        if launch.parent_pid != os.getpid():
            raise PermissionError("本地 ASR worker 能力拒绝: parent_pid 不一致")
        python = self._require_sealed_local_asr_python()
        self.assert_local_asr_launch_assets()
        if self._local_asr_repo_root is None or self._local_asr_worker_script is None:
            raise PermissionError("本地 ASR worker 能力拒绝: 缺少固定资产")
        argv = build_runtime_worker_argv(
            python,
            self._local_asr_worker_script,
            launch,
            repo_root=self._local_asr_repo_root,
        )
        fixed = build_runtime_popen_options(
            repo_root=self._local_asr_repo_root,
            cpu_threads=launch.settings.cpu_threads,
        )
        merged = merge_local_asr_popen_kwargs(fixed, popen_kwargs)
        if os.name == "nt":
            import sys

            if sys.platform == "win32":
                from src.core.owned_worker_job import spawn_owned_local_tts_worker

                if launch.parent_pid != os.getpid():
                    raise PermissionError(
                        "本地 ASR worker 能力拒绝: parent_pid 与启动进程不一致"
                    )
                return spawn_owned_local_tts_worker(
                    argv,
                    merged,
                    popen_fn=self._popen_fn,
                    parent_pid=launch.parent_pid,
                    job_backend=self._owned_worker_job_backend,
                )
        return self._popen_fn(argv, **merged)


def safe_registry_read(
    policy: SafetyPolicy,
    hkey: Any,
    sub_key: str,
    context: str = "",
) -> Optional[str]:
    """
    在策略允许下只读查询注册表项默认值。

    Args:
        policy: 安全策略实例。
        hkey: ``winreg`` 根键常量（如 ``HKEY_LOCAL_MACHINE``）。
        sub_key: 注册表子路径。
        context: 拒绝时的日志上下文说明。

    Returns:
        查询到的字符串值；键不存在或读取失败时返回 ``None``。

    Raises:
        PermissionError: ``registry_read`` 权限未开启时。
    """
    policy.require(Operation.REGISTRY_READ, context or "registry_read")
    import winreg
    try:
        key = winreg.OpenKey(hkey, sub_key)
        value = winreg.QueryValue(key, None)
        winreg.CloseKey(key)
        return value
    except (FileNotFoundError, OSError):
        return None


def safe_subprocess_run(
    policy: SafetyPolicy,
    args: Sequence[str],
    *,
    context: str = "",
    **kwargs,
) -> subprocess.CompletedProcess:
    """
    在通用 ``subprocess`` 权限下调用 runner（兼容入口）。

    RVC 不得使用本函数作为回退旁路；请使用 ``SafetyPolicy.run_rvc_*``。
    """
    reject_string_command(args)
    policy.require(Operation.SUBPROCESS, context or "subprocess")
    clean = sanitize_run_kwargs(kwargs)
    return policy._run_fn(list(args), **clean)


def safe_subprocess_popen(
    policy: SafetyPolicy,
    args: Sequence[str],
    *,
    context: str = "",
    **kwargs,
) -> subprocess.Popen:
    """
    在通用 ``subprocess`` 权限下调用 popen（兼容入口）。

    RVC 不得使用本函数作为回退旁路。
    """
    reject_string_command(args)
    policy.require(Operation.SUBPROCESS, context or "subprocess")
    clean = sanitize_popen_kwargs(kwargs)
    return policy._popen_fn(list(args), **clean)


def safe_system_power_run(
    policy: SafetyPolicy,
    command: str,
    *,
    context: str = "",
    **kwargs,
) -> subprocess.CompletedProcess:
    """
    在 ``system_power`` 权限下执行固定电源/锁屏 argv。

    不依赖通用 ``subprocess`` 权限；仍须由调用方完成用户确认双门。
    """
    if command not in _SYSTEM_POWER_ARGV:
        policy.logger.warning("安全策略拒绝未知电源命令")
        raise PermissionError("安全策略拒绝未知电源命令")
    policy.require(Operation.SYSTEM_POWER, context or command)
    clean = sanitize_run_kwargs(kwargs)
    return policy._run_fn(list(_SYSTEM_POWER_ARGV[command]), **clean)


def safe_os_system(policy: SafetyPolicy, command: str, *, context: str = "") -> int:
    """
    在策略允许下调用 ``os.system`` 执行 Shell 命令。

    Args:
        policy: 安全策略实例。
        command: 完整 Shell 命令字符串。
        context: 拒绝时的日志上下文说明。

    Returns:
        ``os.system`` 返回的退出码。

    Raises:
        PermissionError: ``os_shell`` 权限未开启时。
    """
    policy.require(Operation.OS_SHELL, context or "os_shell")
    return os.system(command)


def safe_setenv(policy: SafetyPolicy, key: str, value: str, *, context: str = "") -> None:
    """
    在策略允许下设置进程环境变量。

    Args:
        policy: 安全策略实例。
        key: 环境变量名。
        value: 环境变量值。
        context: 拒绝时的日志上下文说明。

    Raises:
        PermissionError: ``env_modify`` 权限未开启时。
    """
    policy.require(Operation.ENV_MODIFY, context or key)
    os.environ[key] = value


def safe_unsetenv(policy: SafetyPolicy, key: str, *, context: str = "") -> None:
    """
    在策略允许下删除进程环境变量。

    Args:
        policy: 安全策略实例。
        key: 待删除的环境变量名。
        context: 拒绝时的日志上下文说明。

    Raises:
        PermissionError: ``env_modify`` 权限未开启时。
    """
    policy.require(Operation.ENV_MODIFY, context or key)
    if key in os.environ:
        del os.environ[key]


def safe_webbrowser_open(policy: SafetyPolicy, url: str, *, context: str = "") -> None:
    """
    在策略允许下通过系统默认浏览器打开 URL。

    Args:
        policy: 安全策略实例。
        url: 目标 URL。
        context: 拒绝时的日志上下文说明。

    Raises:
        PermissionError: ``browser_launch`` 权限未开启时。
    """
    policy.require(Operation.BROWSER_LAUNCH, context or "browser")
    import webbrowser
    webbrowser.open(url)
