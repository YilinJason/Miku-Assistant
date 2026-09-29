"""
系统安全策略模块。

所有可能影响本机的操作必须经 ``SafetyPolicy`` 授权，并通过 ``safe_*`` 工具函数执行。

原则：
- 注册表写入：永久禁止（代码层硬编码）
- 注册表读取：默认禁止，需在 config 中显式开启
- 其他高风险操作：默认禁止或需 config 显式开启
- 未来新功能必须通过 SafetyPolicy / safe_* 工具函数，禁止直接调用 winreg、subprocess 等
"""
from __future__ import annotations

import logging
import os
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union


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


# 注册表写入永远不允许，即使用户在配置中尝试开启
_IMMUTABLE_DENY = frozenset({Operation.REGISTRY_WRITE})


class SafetyPolicy:
    """
    集中式安全策略。

    从 ``config/security`` 加载各操作权限，管理文件删除白名单目录，
    并在 ``MIKU_DRY_RUN=1`` 时自动禁用高风险操作。
    """

    _DEFAULT_PERMISSIONS: Dict[str, bool] = {
        Operation.REGISTRY_READ: False,
        Operation.REGISTRY_WRITE: False,
        Operation.SYSTEM_POWER: False,
        Operation.SUBPROCESS: True,
        Operation.OS_SHELL: False,
        Operation.BROWSER_LAUNCH: False,
        Operation.FILE_DELETE: True,
        Operation.ENV_MODIFY: False,
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        从 security 配置节构建策略实例。

        Args:
            config: 安全配置字典，通常含 ``permissions`` 与
                ``file_delete_allowed_dirs``；为 ``None`` 时使用全部默认值。
        """
        self.logger = logging.getLogger(__name__)
        config = config or {}
        permissions = dict(config.get("permissions") or {})

        # 兼容旧配置 system.execute_commands
        if "system_power" not in permissions and "execute_commands" in config:
            permissions["system_power"] = bool(config.get("execute_commands"))

        self._permissions: Dict[str, bool] = {}
        for op, default in self._DEFAULT_PERMISSIONS.items():
            key = op.value if isinstance(op, Operation) else op
            self._permissions[key] = bool(permissions.get(key, default))

        self._file_delete_dirs: List[Path] = [
            Path(p).resolve()
            for p in (config.get("file_delete_allowed_dirs") or [])
        ]

        env_dry = os.environ.get("MIKU_DRY_RUN", "").lower() in ("1", "true", "yes")
        if env_dry:
            self._permissions[Operation.SYSTEM_POWER.value] = False
            self._permissions[Operation.SUBPROCESS.value] = False
            self._permissions[Operation.OS_SHELL.value] = False
            self._permissions[Operation.BROWSER_LAUNCH.value] = False
            self.logger.warning("MIKU_DRY_RUN=1：已禁用系统电源、子进程、Shell 与浏览器启动")

    @classmethod
    def from_app_config(cls, app_config) -> "SafetyPolicy":
        """
        从 ``Config`` 应用配置对象构建 ``SafetyPolicy``。

        Args:
            app_config: 具备 ``get("security")`` 的配置实例。

        Returns:
            已合并旧版 ``system.execute_commands`` 兼容逻辑的策略实例。
        """
        security = app_config.get("security", {}) or {}
        legacy_execute = app_config.get("system.execute_commands")
        if legacy_execute is not None and "permissions" not in security:
            security = dict(security)
            perms = dict(security.get("permissions") or {})
            if "system_power" not in perms:
                perms["system_power"] = bool(legacy_execute)
            security["permissions"] = perms
        return cls(security)

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
            context: 附加说明，便于日志定位（如命令行或 URL）。

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

        Args:
            file_path: 待删除文件路径。

        Returns:
            可安全删除时为 ``True``。
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
            操作键到是否允许的映射字典。
        """
        return {k: self.is_allowed(k) for k in self._permissions}


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
    policy.require(Operation.REGISTRY_READ, context or sub_key)
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
    在策略允许下调用 ``subprocess.run``。

    Args:
        policy: 安全策略实例。
        args: 命令行参数序列。
        context: 拒绝时的日志上下文说明。
        **kwargs: 透传给 ``subprocess.run`` 的额外关键字参数。

    Returns:
        ``subprocess.CompletedProcess`` 结果对象。

    Raises:
        PermissionError: ``subprocess`` 权限未开启时。
    """
    policy.require(Operation.SUBPROCESS, context or " ".join(args))
    return subprocess.run(list(args), **kwargs)


def safe_subprocess_popen(
    policy: SafetyPolicy,
    args: Sequence[str],
    *,
    context: str = "",
    **kwargs,
) -> subprocess.Popen:
    """
    在策略允许下调用 ``subprocess.Popen``。

    Args:
        policy: 安全策略实例。
        args: 命令行参数序列。
        context: 拒绝时的日志上下文说明。
        **kwargs: 透传给 ``subprocess.Popen`` 的额外关键字参数。

    Returns:
        已启动的 ``subprocess.Popen`` 进程对象。

    Raises:
        PermissionError: ``subprocess`` 权限未开启时。
    """
    policy.require(Operation.SUBPROCESS, context or " ".join(args))
    return subprocess.Popen(list(args), **kwargs)


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
    policy.require(Operation.OS_SHELL, context or command)
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
    policy.require(Operation.BROWSER_LAUNCH, context or url)
    import webbrowser
    webbrowser.open(url)
