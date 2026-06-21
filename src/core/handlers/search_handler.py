"""
搜索意图处理器模块。

识别用户输入中的浏览器搜索或 B 站相关指令，
委托 ``ChromeSearcher`` 打开 Chrome 并执行搜索或导航。
"""
import logging
from typing import Optional

from src.core.handlers.base import IntentHandler
from src.core.safety import SafetyPolicy
from src.search.chrome_search import ChromeSearcher


class SearchIntentHandler(IntentHandler):
    """
    网页搜索意图处理器。

    从自然语言中提取搜索关键词；无法识别搜索意图时返回 ``None`` 继续路由。
    """

    def __init__(
        self,
        searcher: Optional[ChromeSearcher],
        safety_policy: Optional[SafetyPolicy] = None,
    ):
        """
        绑定 Chrome 搜索器与安全策略。

        Args:
            searcher: Chrome 搜索模块实例；为 ``None`` 时本处理器直接跳过。
            safety_policy: 安全策略，供搜索器打开浏览器时校验权限。
        """
        super().__init__(safety_policy)
        self.searcher = searcher
        self.logger = logging.getLogger(__name__)

    def handle(self, text: str) -> Optional[str]:
        """
        解析搜索意图并执行浏览器搜索。

        Args:
            text: 用户输入文本。

        Returns:
            搜索操作结果描述；非搜索意图或搜索器未初始化时返回 ``None``。
        """
        if not self.searcher:
            return None

        query = self.searcher.extract_search_query(text)
        if query is None:
            return None

        if query:
            self.logger.info("检测到搜索请求: %s", query)
        else:
            self.logger.info("检测到打开 Bilibili 首页的请求")
        return self.searcher.search(query)
