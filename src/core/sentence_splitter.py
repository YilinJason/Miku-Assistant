"""
跨 chunk 增量分句。

处理中英文句末标点（含英文句号）与换行；成功完成时刷出尾句；
失败/取消时丢弃缓冲。无标点超长输入连续按 max_buffer 切分，残留有上界。
"""
from __future__ import annotations

import re
from typing import List

# 句末：中文标点无需尾随空白；英文句号需空白或结束；叹号问号；换行。
_END_RE = re.compile(
    r"[。！？…；]+|"
    r"[!?]+(?:\s+|$)|"
    r"\.(?:\s+|$)|"
    r";(?:\s+|$)|"
    r"\n+"
)


class IncrementalSentenceSplitter:
    """有界缓冲的增量分句器。"""

    def __init__(self, max_buffer: int = 500) -> None:
        if max_buffer < 1:
            raise ValueError("max_buffer must be >= 1")
        self.max_buffer = max_buffer
        self._buf = ""

    @property
    def buffered_len(self) -> int:
        return len(self._buf)

    def push(self, delta: str) -> List[str]:
        """追加增量，返回本批完整句子（可能为空列表）。"""
        if not delta:
            return []
        self._buf += delta
        return self._extract(force_overflow=True)

    def finish(self) -> List[str]:
        """成功完成时刷出尾部；超长尾句亦按 max_buffer 连续切分。"""
        out = self._extract(force_overflow=True)
        while self._buf.strip():
            if len(self._buf) <= self.max_buffer:
                tail = self._buf.strip()
                self._buf = ""
                if tail:
                    out.append(tail)
                break
            chunk = self._buf[: self.max_buffer]
            self._buf = self._buf[self.max_buffer :]
            piece = chunk.strip()
            if piece:
                out.append(piece)
        return out

    def discard(self) -> None:
        """失败或取消：丢弃未提交尾句。"""
        self._buf = ""

    def _extract(self, *, force_overflow: bool) -> List[str]:
        out: List[str] = []
        while True:
            match = _END_RE.search(self._buf)
            if not match:
                break
            end = match.end()
            sentence = self._buf[:end].strip()
            self._buf = self._buf[end:]
            if sentence:
                out.append(sentence)
        if force_overflow:
            while len(self._buf) >= self.max_buffer:
                chunk = self._buf[: self.max_buffer]
                self._buf = self._buf[self.max_buffer :]
                piece = chunk.strip()
                if piece:
                    out.append(piece)
        return out
