"""
进程内内存会话历史。

负责消息存储与请求上下文窗口选择，不负责 HTTP、持久化或长期记忆。
请求窗口（默认 10 条消息）与存储容量（完整轮上限）是两套概念。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 请求 payload 附带的历史消息条数上限（message 条数，不是「轮」）
DEFAULT_REQUEST_WINDOW = 10
# 完整 user/assistant 轮的存储上限（默认有限，防止长会话只增不减）
DEFAULT_MAX_STORED_TURNS = 500
MAX_MAX_STORED_TURNS = 50_000


def require_max_stored_turns(name: str, value: Any) -> int:
    """严格校验完整轮存储上限；拒绝 bool/str/float/非正/超硬上限。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int")
    if value > MAX_MAX_STORED_TURNS:
        raise ValueError(f"{name} must be <= {MAX_MAX_STORED_TURNS}")
    return value


class ConversationHistory:
    """
    单会话内存历史。

    默认各自新建的实例相互隔离。若多个 ``OllamaClient`` 显式注入同一
    ``ConversationHistory``，则共享同一存储，``clear()`` 会影响全部共享者。
    ``snapshot()`` / ``build_messages()`` 返回可修改的副本；修改副本不影响内部记录。
    """

    def __init__(
        self,
        request_window: int = DEFAULT_REQUEST_WINDOW,
        max_stored_turns: int = DEFAULT_MAX_STORED_TURNS,
    ) -> None:
        if request_window < 0:
            raise ValueError("request_window must be >= 0")
        self._request_window = request_window
        self._max_stored_turns = require_max_stored_turns(
            "max_stored_turns", max_stored_turns
        )
        self._messages: List[Dict[str, str]] = []

    @property
    def request_window(self) -> int:
        """请求上下文选用的历史消息条数上限。"""
        return self._request_window

    @property
    def max_stored_turns(self) -> int:
        """可保留的完整 user/assistant 轮数上限。"""
        return self._max_stored_turns

    def __len__(self) -> int:
        return len(self._messages)

    @property
    def turn_count(self) -> int:
        """完整轮数（成对消息按 2 计）。"""
        return len(self._messages) // 2

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
        在成功得到非空助手回复后追加完整一轮（user + assistant），再按轮裁剪。

        裁剪只删除完整轮，不会留下半轮。
        """
        self._messages.append({"role": "user", "content": user_content})
        self._messages.append({"role": "assistant", "content": assistant_content})
        self._trim_complete_turns()

    def _trim_complete_turns(self) -> None:
        max_messages = self._max_stored_turns * 2
        if len(self._messages) <= max_messages:
            return
        # 始终成对裁剪：丢弃最旧的完整轮
        overflow = len(self._messages) - max_messages
        drop = overflow + (overflow % 2)
        if drop > 0:
            del self._messages[:drop]

    def clear(self) -> None:
        """清空本实例的全部历史消息。"""
        self._messages.clear()

    def snapshot(self) -> List[Dict[str, str]]:
        """返回当前历史的可修改副本（列表与字典均为拷贝）；改副本不影响内部。"""
        return [dict(m) for m in self._messages]
