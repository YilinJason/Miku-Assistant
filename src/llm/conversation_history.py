"""
进程内内存会话历史。

负责消息存储与请求上下文窗口选择，不负责 HTTP、持久化或长期记忆。
请求窗口（默认 10 条消息）与存储容量是两套概念：本模块请求侧截取窗口，
存储侧保留现有「只增不减、仅 clear 清空」行为。
"""
from __future__ import annotations

from typing import Dict, List

# 请求 payload 附带的历史消息条数上限（message 条数，不是「轮」）
DEFAULT_REQUEST_WINDOW = 10


class ConversationHistory:
    """
    单会话内存历史。

    默认各自新建的实例相互隔离。若多个 ``OllamaClient`` 显式注入同一
    ``ConversationHistory``，则共享同一存储，``clear()`` 会影响全部共享者。
    ``snapshot()`` / ``build_messages()`` 返回可修改的副本；修改副本不影响内部记录。
    """

    def __init__(self, request_window: int = DEFAULT_REQUEST_WINDOW) -> None:
        if request_window < 0:
            raise ValueError("request_window must be >= 0")
        self._request_window = request_window
        self._messages: List[Dict[str, str]] = []

    @property
    def request_window(self) -> int:
        """请求上下文选用的历史消息条数上限。"""
        return self._request_window

    def __len__(self) -> int:
        return len(self._messages)

    def build_messages(self, user_prompt: str) -> List[Dict[str, str]]:
        """
        构造一次请求的 messages：最近至多 ``request_window`` 条历史 + 当前用户输入。

        返回值均为浅拷贝字典；不会把当前输入写入内部存储。
        """
        if self._messages and self._request_window > 0:
            selected = self._messages[-self._request_window :]
        else:
            selected = []
        out = [dict(m) for m in selected]
        out.append({"role": "user", "content": user_prompt})
        return out

    def commit_turn(self, user_content: str, assistant_content: str) -> None:
        """
        在成功得到非空助手回复后追加完整一轮（user + assistant）。

        不截断存储容量（有界存储后续单独处理）。
        """
        self._messages.append({"role": "user", "content": user_content})
        self._messages.append({"role": "assistant", "content": assistant_content})

    def clear(self) -> None:
        """清空本实例的全部历史消息。"""
        self._messages.clear()

    def snapshot(self) -> List[Dict[str, str]]:
        """返回当前历史的可修改副本（列表与字典均为拷贝）；改副本不影响内部。"""
        return [dict(m) for m in self._messages]
