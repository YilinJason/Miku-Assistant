"""
LLM 对话意图处理器模块。

作为意图链的兜底处理器：当前序系统/搜索处理器均未匹配时，
将用户输入交给本地 Ollama 模型生成对话回复。
"""
from typing import Any, Optional

from src.core.dispatch_outcome import DispatchOutcome
from src.core.handlers.base import IntentHandler
from src.core.safety import SafetyPolicy
from src.llm.ollama_client import OllamaClient


class LLMIntentHandler(IntentHandler):
    """
    大语言模型意图处理器。

    始终尝试处理（不返回 ``None``）；LLM 未初始化时返回错误提示。
    优先 ``generate_result``；仅有 ``generate`` 的替身客户端走字符串适配。
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

    def handle_outcome(self, text: str) -> Optional[DispatchOutcome]:
        """
        调用模型一次，映射为 ``DispatchOutcome``。

        成功（含以「错误:」开头的模型正文）允许 TTS/角色；失败与空回复不允许。
        """
        if not self.llm_client:
            return DispatchOutcome.module_unavailable("LLM 模块未初始化")
        return self._outcome_from_client(self.llm_client, text)

    def handle(self, text: str) -> Optional[str]:
        """
        兼容字符串入口：委托 ``handle_outcome``，保证同一次输入只调模型一次。
        """
        outcome = self.handle_outcome(text)
        return None if outcome is None else outcome.text

    @staticmethod
    def _outcome_from_client(client: Any, text: str) -> DispatchOutcome:
        generate_result = getattr(client, "generate_result", None)
        if callable(generate_result):
            return DispatchOutcome.from_generate_result(generate_result(text))

        # 仅有 generate() 的兼容客户端：无法区分调用失败与成功台词，
        # 非空视为可播报成功文本，空串视为空回复（不靠「错误:」前缀）。
        reply = client.generate(text)
        if reply:
            return DispatchOutcome(
                text=str(reply),
                allow_tts=True,
                allow_character=True,
            )
        return DispatchOutcome(text="", allow_tts=False, allow_character=False)
