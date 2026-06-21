"""
Windows 系统控制模块

提供关机、重启、锁屏（须经 SafetyPolicy 授权 + 用户二次确认）。
"""
import logging
import platform
from typing import Optional

from src.core.safety import Operation, SafetyPolicy, safe_subprocess_run
from src.system.confirm_dialog import confirm_system_action


class WindowsController:
    """Windows 系统控制器，解析自然语言中的电源/锁屏意图并安全执行。"""

    _DRY_RUN_MESSAGES = {
        "shutdown": "[已拦截] 系统关机请求（安全策略未授权或未启用）。",
        "restart": "[已拦截] 系统重启请求（安全策略未授权或未启用）。",
        "lock": "[已拦截] 系统锁屏请求（安全策略未授权或未启用）。",
    }

    _REAL_MESSAGES = {
        "shutdown": "系统将在60秒后关机。如需取消，请在终端运行 shutdown /a。",
        "restart": "系统将在60秒后重启。如需取消，请在终端运行 shutdown /a。",
        "lock": "系统已锁定。",
    }

    _CANCELLED_MESSAGE = "已取消操作，未执行任何系统命令。"

    def __init__(
        self,
        safety_policy: Optional[SafetyPolicy] = None,
        dry_run: bool = False,
        require_confirmation: bool = True,
    ):
        """
        初始化 Windows 控制器。

        Args:
            safety_policy: 安全策略；无 ``system_power`` 权限时强制 dry_run。
            dry_run: 为 ``True`` 时只记录日志，不执行真实系统命令。
            require_confirmation: 真实执行前是否弹出二次确认对话框。
        """
        self.logger = logging.getLogger(__name__)
        self.safety_policy = safety_policy or SafetyPolicy()
        self.require_confirmation = require_confirmation
        self.dry_run = dry_run or not self.safety_policy.is_allowed(Operation.SYSTEM_POWER)

        if self.dry_run:
            self.logger.info("Windows 系统控制：未授权真实执行，将拦截电源/锁屏类命令")
        elif self.require_confirmation:
            self.logger.info("Windows 系统控制：已启用二次确认弹窗")

        if platform.system() != "Windows":
            self.logger.warning("当前系统不是 Windows，部分功能可能不可用")

        self.command_keywords = {
            "shutdown": ["关机", "关闭电脑", "关闭系统", "shutdown", "turn off"],
            "restart": ["重启", "重新启动", "restart", "reboot"],
            "lock": ["锁定", "锁屏", "lock", "lock screen"],
        }

    def check_and_execute(self, text: str) -> Optional[str]:
        """
        检测文本是否包含系统控制意图并执行对应命令。

        Args:
            text: 用户输入的自然语言文本。

        Returns:
            匹配到命令时返回执行结果说明字符串；无匹配返回 ``None``。
        """
        text_lower = text.lower()
        for command, keywords in self.command_keywords.items():
            for keyword in keywords:
                if keyword.lower() in text_lower:
                    return self._execute_command(command)
        return None

    def _execute_command(self, command: str) -> str:
        """
        执行单个系统命令（含 dry_run、确认与 safe_subprocess_run）。

        Args:
            command: ``shutdown`` / ``restart`` / ``lock`` 之一。

        Returns:
            面向用户的执行结果或拦截说明。
        """
        if self.dry_run:
            self.logger.info("[安全拦截] 系统命令: %s", command)
            return self._DRY_RUN_MESSAGES.get(command, f"[已拦截] 命令 {command}")

        if self.require_confirmation and not confirm_system_action(command):
            self.logger.info("用户取消系统命令: %s", command)
            return self._CANCELLED_MESSAGE

        try:
            ctx = f"windows_control:{command}"
            if command == "shutdown":
                safe_subprocess_run(
                    self.safety_policy, ["shutdown", "/s", "/t", "60"],
                    check=True, context=ctx,
                )
            elif command == "restart":
                safe_subprocess_run(
                    self.safety_policy, ["shutdown", "/r", "/t", "60"],
                    check=True, context=ctx,
                )
            elif command == "lock":
                safe_subprocess_run(
                    self.safety_policy,
                    ["rundll32.exe", "user32.dll,LockWorkStation"],
                    check=True, context=ctx,
                )
            else:
                return f"未知命令: {command}"
            return self._REAL_MESSAGES[command]
        except PermissionError as exc:
            return f"[已拦截] {exc}"
        except Exception as exc:
            self.logger.error("系统命令错误: %s", exc)
            return f"错误: {exc}"
