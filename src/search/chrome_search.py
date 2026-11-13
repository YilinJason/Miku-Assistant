"""
Chrome搜索模块
通过关键字触发Chrome浏览器进行网页搜索
"""
import logging
import re
import urllib.parse
import os
from pathlib import Path
from typing import Optional

from src.core.safety import (
    Operation,
    SafetyPolicy,
    safe_registry_read,
    safe_subprocess_popen,
    safe_webbrowser_open,
)


class ChromeSearcher:
    """Chrome 搜索器：解析搜索意图并通过 Chrome 或系统默认浏览器打开结果页。"""
    
    def __init__(self, chrome_path: Optional[str] = None,
                 search_engine: str = "google",
                 auto_open: bool = True,
                 safety_policy: Optional[SafetyPolicy] = None):
        """
        初始化Chrome搜索器
        
        Args:
            chrome_path: Chrome 可执行文件路径，``None`` 表示自动检测或使用系统默认。
            search_engine: 默认搜索引擎（``google``、``bing``、``baidu`` 等）。
            auto_open: 是否自动打开浏览器；``False`` 时仅返回 URL 字符串。
            safety_policy: 安全策略；浏览器启动、子进程与注册表读取时使用。
        """
        # 先初始化logger，因为_find_chrome_path需要使用
        self.logger = logging.getLogger(__name__)
        self.safety_policy = safety_policy or SafetyPolicy()
        # 如果没有指定路径，尝试自动检测 Chrome（不使用注册表，除非显式授权）
        if chrome_path is None:
            chrome_path = self._find_chrome_path()
        
        self.chrome_path = chrome_path
        self.search_engine = search_engine
        self.auto_open = auto_open
        
        if self.chrome_path:
            self.logger.info("Chrome 路径已配置 has_custom_path=True")
        else:
            self.logger.warning("未找到Chrome，将使用系统默认浏览器")
        
        # 搜索引擎URL模板
        self.search_urls = {
            'google': 'https://www.google.com/search?q={}',
            'baidu': 'https://www.baidu.com/s?wd={}',
            'bilibili': 'https://search.bilibili.com/all?keyword={}'
        }
        
        # Bilibili关键词（用于自动识别）
        self.bilibili_keywords = ['b站', 'bilibili', '哔哩哔哩', 'bilibili搜索', 'b站搜索']
        
        # 临时存储检测到的搜索引擎（用于Bilibili自动识别）
        self._detected_engine = None
        
        # 打开/访问动词（用于打开网站首页）
        self.open_verbs = ['打开', '访问', '进入', '去', 'open', 'visit', 'go to']
        
        # 搜索关键词模式
        self.search_patterns = [
            r'搜索\s+(.+)',
            r'查找\s+(.+)',
            r'查一下\s+(.+)',
            r'帮我搜\s+(.+)',
            r'search\s+(.+)',
            r'find\s+(.+)',
            r'look\s+up\s+(.+)',
        ]
        
        # Bilibili专用搜索模式（优先级更高）
        self.bilibili_patterns = [
            r'(?:b站|bilibili|哔哩哔哩)\s*搜索\s+(.+)',
            r'(?:b站|bilibili|哔哩哔哩)\s*查找\s+(.+)',
            r'在\s*(?:b站|bilibili|哔哩哔哩)\s*搜索\s+(.+)',
            r'在\s*(?:b站|bilibili|哔哩哔哩)\s*查找\s+(.+)',
        ]
        
        # Bilibili打开首页模式（用于直接打开首页）
        self.bilibili_open_patterns = [
            r'(?:打开|访问|进入|去)\s*(?:b站|bilibili|哔哩哔哩)',
            r'(?:b站|bilibili|哔哩哔哩)\s*(?:打开|访问|进入)',
            r'(?:open|visit|go\s+to)\s*(?:b站|bilibili|bilibili)',
        ]
    
    def _find_chrome_path(self) -> Optional[str]:
        """
        自动检测 Chrome 浏览器的安装路径。

        依次尝试常见安装目录；若授权 ``registry_read`` 则查询注册表 App Paths。

        Returns:
            找到则返回 ``chrome.exe`` 绝对路径，否则 ``None``。
        """
        possible_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
            r"C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe",
        ]
        
        for path in possible_paths:
            expanded_path = os.path.expandvars(path)
            if Path(expanded_path).exists():
                self.logger.info("自动检测到Chrome has_path=True")
                return expanded_path
        
        if self.safety_policy.is_allowed(Operation.REGISTRY_READ):
            try:
                import winreg
                reg_paths = [
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
                    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
                ]
                for hkey, reg_path in reg_paths:
                    chrome_path = safe_registry_read(
                        self.safety_policy, hkey, reg_path, context="detect_chrome"
                    )
                    if chrome_path and Path(chrome_path).exists():
                        self.logger.info("从注册表检测到 Chrome has_path=True")
                        return chrome_path
            except ImportError:
                pass
        else:
            self.logger.debug("注册表读取未授权，跳过 Chrome 注册表检测")
        
        self.logger.warning("未找到Chrome浏览器，将使用系统默认浏览器")
        return None
    
    def extract_search_query(self, text: str) -> Optional[str]:
        """
        从自然语言文本中提取搜索关键词。

        支持通用搜索句式、B 站专用模式及「打开 B 站首页」（返回空字符串）。

        Args:
            text: 用户输入文本。

        Returns:
            提取到的查询词；打开 B 站首页意图返回 ``""``；无法识别返回 ``None``。
            副作用：可能设置 ``self._detected_engine`` 为 ``bilibili``。
        """
        self._detected_engine = None
        
        text_lower = text.lower()
        is_bilibili_search = any(keyword in text_lower for keyword in self.bilibili_keywords)
        
        if is_bilibili_search:
            self._detected_engine = 'bilibili'
        
        # 优先检查是否是打开Bilibili首页的请求
        for pattern in self.bilibili_open_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                self.logger.info("检测到打开Bilibili首页的请求")
                return ""
        
        # 优先匹配Bilibili专用搜索模式
        for pattern in self.bilibili_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                query = match.group(1).strip()
                if query:
                    for keyword in self.bilibili_keywords:
                        query = re.sub(keyword, '', query, flags=re.IGNORECASE).strip()
                    if query:
                        return query
        
        # 匹配通用搜索模式
        for pattern in self.search_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                query = match.group(1).strip()
                if query:
                    for keyword in self.bilibili_keywords:
                        query = re.sub(keyword, '', query, flags=re.IGNORECASE).strip()
                    if query:
                        return query
        
        # 如果没有匹配到模式，检查是否包含明显的搜索意图
        search_indicators = ['搜索', '查找', '查一下', 'search', 'find']
        if any(indicator in text_lower for indicator in search_indicators):
            quoted = re.findall(r'["\'](.+?)["\']', text)
            if quoted:
                query = quoted[0]
                for keyword in self.bilibili_keywords:
                    query = re.sub(keyword, '', query, flags=re.IGNORECASE).strip()
                if query:
                    return query
            for indicator in search_indicators:
                if indicator in text_lower:
                    parts = text_lower.split(indicator, 1)
                    if len(parts) > 1:
                        query = parts[1].strip()
                        for keyword in self.bilibili_keywords:
                            query = re.sub(keyword, '', query, flags=re.IGNORECASE).strip()
                        if query:
                            return query
        
        # 如果检测到Bilibili关键词但没有搜索内容，返回空字符串以触发打开Bilibili
        if is_bilibili_search:
            for keyword in self.bilibili_keywords:
                if keyword in text_lower:
                    parts = text_lower.split(keyword, 1)
                    if len(parts) > 1:
                        potential_query = parts[1].strip()
                        for k in self.bilibili_keywords:
                            potential_query = re.sub(k, '', potential_query, flags=re.IGNORECASE).strip()
                        if potential_query:
                            return potential_query
            return ""
        
        return None
    
    def search(self, query: str, force_engine: Optional[str] = None) -> str:
        """
        构造搜索 URL 并在浏览器中打开（或仅返回 URL）。

        Args:
            query: 搜索关键词；空字符串且引擎为 bilibili 时打开首页。
            force_engine: 强制使用的搜索引擎，覆盖 ``_detected_engine`` 与默认配置。

        Returns:
            面向用户的操作结果说明；未授权浏览器时返回 ``搜索URL: ...``。
        """
        engine_to_use = force_engine or self._detected_engine or self.search_engine
        
        if not query:
            if engine_to_use == 'bilibili':
                search_url = 'https://www.bilibili.com'
                self.logger.info("查询为空，打开Bilibili首页")
            else:
                return "搜索查询为空"
        else:
            if engine_to_use == 'bilibili':
                for keyword in self.bilibili_keywords:
                    query = re.sub(keyword, '', query, flags=re.IGNORECASE).strip()
            
            if not query.strip() and engine_to_use == 'bilibili':
                search_url = 'https://www.bilibili.com'
                self.logger.info("清理后查询为空，打开Bilibili首页")
            else:
                search_url_template = self.search_urls.get(
                    engine_to_use, 
                    self.search_urls['google']
                )
                encoded_query = urllib.parse.quote(query, safe='')
                search_url = search_url_template.format(encoded_query)
        
        self._detected_engine = None
        
        query_chars = len(query or "")
        self.logger.info(
            "准备搜索 chars=%d engine=%s has_url=%s",
            query_chars,
            engine_to_use,
            bool(search_url),
        )
        
        try:
            if self.auto_open:
                if not self.safety_policy.is_allowed(Operation.BROWSER_LAUNCH):
                    self.logger.warning("浏览器启动未授权，仅返回 URL")
                    return f"搜索URL: {search_url}"

                if self.chrome_path and Path(self.chrome_path).exists():
                    try:
                        self.logger.info("使用 Chrome 路径打开 has_custom_path=True")
                        import subprocess
                        safe_subprocess_popen(
                            self.safety_policy,
                            [self.chrome_path, search_url],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            context="chrome_search",
                        )
                        engine_name = 'B站' if engine_to_use == 'bilibili' else engine_to_use
                        self.logger.info(
                            "已通过Chrome打开搜索 chars=%d engine=%s",
                            query_chars,
                            engine_name,
                        )
                        return f"已在{engine_name}搜索: {query}"
                    except PermissionError as exc:
                        from src.core.log_privacy import exception_log_label

                        self.logger.warning(
                            "Chrome 启动被安全策略拦截 err=%s",
                            exception_log_label(exc),
                        )
                        return f"[已拦截] 无法打开浏览器: {exc}"
                    except Exception as e:
                        from src.core.log_privacy import exception_log_label

                        self.logger.warning(
                            "使用Chrome路径失败，尝试系统默认浏览器 err=%s",
                            exception_log_label(e),
                        )
                        safe_webbrowser_open(
                            self.safety_policy, search_url, context="chrome_search_fallback"
                        )
                        engine_name = 'B站' if engine_to_use == 'bilibili' else engine_to_use
                        self.logger.info(
                            "已通过系统默认浏览器打开搜索 chars=%d engine=%s",
                            query_chars,
                            engine_name,
                        )
                        return f"已在{engine_name}搜索: {query}"
                else:
                    self.logger.info("使用系统默认浏览器打开")
                    safe_webbrowser_open(
                        self.safety_policy, search_url, context="chrome_search_default"
                    )
                    engine_name = 'B站' if engine_to_use == 'bilibili' else engine_to_use
                    self.logger.info(
                        "已通过系统默认浏览器打开搜索 chars=%d engine=%s",
                        query_chars,
                        engine_name,
                    )
                    return f"已在{engine_name}搜索: {query}"
            else:
                self.logger.info("auto_open=False，仅返回URL has_url=%s", bool(search_url))
                return f"搜索URL: {search_url}"
                
        except Exception as e:
            from src.core.log_privacy import exception_log_label

            self.logger.error(
                "打开搜索失败 err=%s", exception_log_label(e), exc_info=True
            )
            return f"搜索失败: {str(e)}"
    
    def set_search_engine(self, engine: str):
        """
        切换默认搜索引擎。

        Args:
            engine: 引擎标识，须在 ``search_urls`` 中已定义。
        """
        if engine in self.search_urls:
            self.search_engine = engine
            self.logger.info(f"搜索引擎已切换为: {engine}")
        else:
            self.logger.warning(f"不支持的搜索引擎: {engine}")

