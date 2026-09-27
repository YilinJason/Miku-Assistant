"""意图路由装配（仅组装已有依赖，不创建客户端或启动副作用）。"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from src.core.handlers import (
    LLMIntentHandler,
    SearchIntentHandler,
    SystemIntentHandler,
)
from src.core.router import IntentRouter

if TYPE_CHECKING:
    from src.core.safety import SafetyPolicy
    from src.llm.ollama_client import OllamaClient
    from src.search.chrome_search import ChromeSearcher
    from src.system.windows_control import WindowsController


def build_intent_router(
    windows_controller: Optional["WindowsController"] = None,
    chrome_searcher: Optional["ChromeSearcher"] = None,
    llm_client: Optional["OllamaClient"] = None,
    safety_policy: Optional["SafetyPolicy"] = None,
) -> IntentRouter:
    """
    用调用方已持有的依赖组装意图路由链。

    优先级固定为：系统 → 搜索 → LLM。
    本函数不创建真实客户端、不启动音频、不加载模型、不开启清理任务。

    Args:
        windows_controller: 可选 Windows 控制器；``None`` 时系统意图跳过。
        chrome_searcher: 可选搜索器；``None`` 时搜索意图跳过。
        llm_client: 可选 Ollama 客户端；``None`` 时 LLM 返回未初始化提示。
        safety_policy: 注入各 handler 的安全策略（可 ``None``）。

    Returns:
        已注册默认三处理器的 ``IntentRouter``。
    """
    return IntentRouter(
        [
            SystemIntentHandler(windows_controller, safety_policy),
            SearchIntentHandler(chrome_searcher, safety_policy),
            LLMIntentHandler(llm_client, safety_policy),
        ]
    )
