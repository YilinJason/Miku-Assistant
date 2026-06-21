"""
LLM 对话意图处理器模块。

作为意图链的兜底处理器：当前序系统/搜索处理器均未匹配时，
将用户输入交给本地 Ollama 模型生成对话回复。
"""
from typing import Optional

from src.core.handlers.base import IntentHandler
from src.core.safety import SafetyPolicy
from src.llm.ollama_client import OllamaClient


class LLMIntentHandler(IntentHandler):
    """
    大语言模型意图处理器。

    始终尝试处理（不返回 ``None``）；LLM 未初始化时返回错误提示字符串。
    """

    def __init__(
        self,
        llm_client: Optional[OllamaClient],
        safety_policy: Optional[SafetyPolicy] = None,
    ):
        """
        绑定 Ollama 客户端。

        Args:
            llm_client: Ollama API 客户端；为 ``None`` 时表示 LLM 模块未就绪。
            safety_policy: 安全策略（LLM 处理器本身不直接执行系统操作，保留接口一致性）。
        """
        super().__init__(safety_policy)
        self.llm_client = llm_client

    def handle(self, text: str) -> Optional[str]:
        """
        调用 Ollama 生成对话回复。

        Args:
            text: 用户输入文本。

        Returns:
            模型回复或 ``"LLM 模块未初始化"`` 提示。
        """
        if not self.llm_client:
            return "LLM 模块未初始化"
        return self.llm_client.generate(text)
