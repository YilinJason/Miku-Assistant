"""
用户意图路由模块。

将用户文本依次分发给已注册的 ``IntentHandler`` 链，
由第一个返回非 ``None`` 结果的处理器接管响应。
"""
import logging
from typing import List, Optional

from src.core.handlers.base import IntentHandler


class IntentRouter:
    """
    意图路由器。

    按注册顺序依次调用各处理器；支持运行时动态插入新处理器。
    """

    def __init__(self, handlers: List[IntentHandler]):
        """
        初始化路由器。

        Args:
            handlers: 处理器列表，顺序决定匹配优先级（靠前者优先）。
        """
        self.handlers = handlers
        self.logger = logging.getLogger(__name__)

    def dispatch(self, text: str) -> Optional[str]:
        """
        将用户输入分发给处理器链。

        Args:
            text: 用户输入文本。

        Returns:
            首个成功处理的回复文本；全部处理器返回 ``None`` 时为 ``None``。
        """
        for handler in self.handlers:
            result = handler.handle(text)
            if result is not None:
                self.logger.debug("由 %s 处理", handler.__class__.__name__)
                return result
        return None

    def register(self, handler: IntentHandler, index: Optional[int] = None) -> None:
        """
        运行时注册新处理器，便于扩展插件式意图。

        Args:
            handler: 待注册的 ``IntentHandler`` 实例。
            index: 插入位置；为 ``None`` 时追加到链尾。
        """
        if index is None:
            self.handlers.append(handler)
        else:
            self.handlers.insert(index, handler)
