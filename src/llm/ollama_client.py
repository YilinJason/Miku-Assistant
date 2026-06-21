"""
Ollama 客户端模块。

通过 HTTP API 调用本地 Ollama 服务的 ``/api/chat`` 与 ``/api/tags``，
维护多轮对话历史并支持流式与非流式生成。
"""
import requests
import logging
from typing import Optional, List, Dict, Any


class OllamaClient:
    """
    Ollama REST API 客户端。

    封装 chat 生成、连接/模型可用性探测及对话历史管理（默认保留最近 10 轮）。
    """

    def __init__(self, base_url: str = "http://localhost:11434",
                 model: str = "Miku_Assistant:latest",
                 timeout: int = 60):
        """
        初始化 Ollama 客户端。

        Args:
            base_url: Ollama 服务根 URL（不含尾部斜杠会自动去除）。
            model: 默认使用的模型名称（可含 ``:tag``）。
            timeout: HTTP 请求超时时间（秒）。
        """
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)
        self.conversation_history: List[Dict[str, str]] = []

    def generate(self, prompt: str, stream: bool = False,
                 system: Optional[str] = None) -> str:
        """
        调用 chat API 生成回复（自动附带最近对话历史）。

        Args:
            prompt: 当前轮用户输入。
            stream: 是否以 SSE 流式读取响应。
            system: 可选系统提示词，写入请求 payload。

        Returns:
            助手回复文本；网络或解析失败时返回以 ``错误:`` 开头的提示字符串。
        """
        url = f"{self.base_url}/api/chat"

        messages = []

        # 保留最近 10 轮（20 条消息）以控制上下文长度
        if self.conversation_history:
            messages.extend(self.conversation_history[-10:])

        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream
        }

        if system:
            payload["system"] = system

        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()

            if stream:
                result = ""
                for line in response.iter_lines():
                    if line:
                        import json
                        data = json.loads(line)
                        if 'message' in data and 'content' in data['message']:
                            result += data['message']['content']
                        if data.get('done', False):
                            break
                if result:
                    self.conversation_history.append({"role": "user", "content": prompt})
                    self.conversation_history.append({"role": "assistant", "content": result})
                return result
            else:
                data = response.json()
                message = data.get('message', {})
                result = message.get('content', '')

                if result:
                    self.conversation_history.append({"role": "user", "content": prompt})
                    self.conversation_history.append({"role": "assistant", "content": result})
                    self.logger.debug(f"对话历史已更新，当前有 {len(self.conversation_history)} 条消息")

                return result

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Ollama API请求失败: {e}")
            return f"错误: 无法连接到Ollama服务 ({e})"
        except Exception as e:
            self.logger.error(f"生成回复时出错: {e}")
            return f"错误: {str(e)}"

    def clear_history(self) -> None:
        """清空内存中的多轮对话历史。"""
        self.conversation_history = []
        self.logger.info("对话历史已清空")

    def check_connection(self) -> bool:
        """
        探测 Ollama 服务是否可达。

        Returns:
            ``GET /api/tags`` 成功时为 ``True``。
        """
        try:
            url = f"{self.base_url}/api/tags"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            return True
        except Exception:
            return False

    def check_model_available(self) -> bool:
        """
        检查当前配置的模型是否已在 Ollama 中安装。

        Returns:
            模型名完全匹配或基础名（``:`` 前）匹配时为 ``True``。
        """
        try:
            url = f"{self.base_url}/api/tags"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            models = response.json().get("models", [])
            names = {m.get("name", "") for m in models}
            if self.model in names:
                return True
            # 支持 name 带或不带 :latest 后缀的模糊匹配
            base = self.model.split(":")[0]
            return any(n.split(":")[0] == base for n in names)
        except Exception as exc:
            self.logger.warning("检查模型列表失败: %s", exc)
            return False

    def list_models(self) -> List[str]:
        """
        列出 Ollama 已安装的全部模型名称。

        Returns:
            模型名字符串列表；请求失败时返回空列表。
        """
        try:
            url = f"{self.base_url}/api/tags"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            return [m.get("name", "") for m in response.json().get("models", [])]
        except Exception:
            return []
