"""
Intent 处理器抽象基类模块。

定义 ``handle`` / ``handle_outcome`` 接口及 ``SafetyPolicy`` 注入约定；
新增处理器须通过 ``safe_*`` 执行系统操作，禁止直接调用底层 API。
"""
from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

from src.core.dispatch_outcome import DispatchOutcome

if TYPE_CHECKING:
    from src.core.safety import SafetyPolicy


class IntentHandler(ABC):
    """
    用户意图处理器抽象基类。

    各具体处理器实现 ``handle`` 方法：能处理则返回回复字符串，
    无法识别则返回 ``None`` 交由下一处理器继续。

    结构化路径使用 ``handle_outcome``：默认将非 ``None`` 的 ``handle`` 字符串
    包装为原副作用语义（允许 TTS/角色）。LLM 等覆盖 ``handle_outcome`` 的子类
    应令 ``handle`` 委托 ``handle_outcome``，避免重复调用模型。

    新增处理器时：
    - 必须通过 ``SafetyPolicy`` 执行任何系统级操作
    - 禁止直接使用 winreg / subprocess / os.system
    - 详见 ``src/core/safety.py``
    """

    def __init__(self, safety_policy: Optional["SafetyPolicy"] = None):
        """
        注入安全策略供子类在系统操作前校验权限。

        Args:
            safety_policy: 安全策略实例；可为 ``None``（仅做纯文本/LLM 处理时）。
        """
        self.safety_policy = safety_policy

    @abstractmethod
    def handle(self, text: str) -> Optional[str]:
        """
        尝试处理用户输入。

        Args:
            text: 用户原始输入文本。

        Returns:
            助手回复字符串；无法处理时返回 ``None``。
        """

    def handle_outcome(self, text: str) -> Optional[DispatchOutcome]:
        """
        结构化处理入口；默认委托 ``handle`` 并保持原字符串副作用语义。

        覆盖本方法的子类（如 LLM）应保证 ``handle`` 不重复调用模型。
        """
        result = self.handle(text)
        if result is None:
            return None
        return DispatchOutcome.from_legacy_handler_text(result)
