"""
系统命令意图处理器模块。

将关机、重启、锁屏等 Windows 系统指令委托给 ``WindowsController`` 识别与执行，
须通过 ``SafetyPolicy`` 校验 ``system_power`` 等权限。
"""
from typing import Optional

from src.core.handlers.base import IntentHandler
from src.core.safety import SafetyPolicy
from src.system.windows_control import WindowsController


class SystemIntentHandler(IntentHandler):
    """
    Windows 系统控制意图处理器。

    在意图链中优先级最高；无法识别系统指令时返回 ``None``。
    """

    def __init__(
        self,
        controller: Optional[WindowsController],
        safety_policy: Optional[SafetyPolicy] = None,
    ):
        """
        绑定 Windows 控制器与安全策略。

        Args:
            controller: Windows 控制模块；为 ``None`` 时跳过系统指令处理。
            safety_policy: 安全策略，控制器内部用于电源/Shell 等操作授权。
        """
        super().__init__(safety_policy)
        self.controller = controller

    def handle(self, text: str) -> Optional[str]:
        """
        检测并执行系统级命令。

        Args:
            text: 用户输入文本。

        Returns:
            执行结果或确认提示；非系统指令时返回 ``None``。
        """
        if self.controller:
            return self.controller.check_and_execute(text)
        return None
