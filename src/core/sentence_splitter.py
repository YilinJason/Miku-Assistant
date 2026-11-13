"""
跨 chunk 增量分句。

处理中英文句末标点（含英文句号）与换行；成功完成时刷出尾句；
失败/取消时丢弃缓冲。无标点超长输入连续按 max_buffer 切分，残留有上界。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

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

    def __init__(
        self,
        max_buffer: int = 500,
        *,
        first_clause_min_chars: int | None = None,
    ) -> None:
        if max_buffer < 1:
            raise ValueError("max_buffer must be >= 1")
        if first_clause_min_chars is not None:
            if isinstance(first_clause_min_chars, bool) or not isinstance(
                first_clause_min_chars, int
            ):
                raise TypeError("first_clause_min_chars must be int or None")
            if not 1 <= first_clause_min_chars <= max_buffer:
                raise ValueError(
                    "first_clause_min_chars must be in 1..max_buffer inclusive"
                )
        self.max_buffer = max_buffer
        self.first_clause_min_chars = first_clause_min_chars
        self._buf = ""
        self._first_clause_eligible = first_clause_min_chars is not None

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
        self._first_clause_eligible = self.first_clause_min_chars is not None

    def _earliest_soft_comma(self, buf: str) -> Optional[Tuple[int, str]]:
        """最早合格软逗号：(余缓冲起点, 句子)。"""
        min_c = self.first_clause_min_chars
        assert min_c is not None
        for i, ch in enumerate(buf):
            if ch == "，":
                prefix = buf[: i + 1].strip()
                if len(prefix) >= min_c:
                    return (i + 1, prefix)
            elif ch == "," and i + 1 < len(buf) and buf[i + 1].isspace():
                prefix = buf[: i + 1].strip()
                if len(prefix) >= min_c:
                    rem = i + 1
                    while rem < len(buf) and buf[rem].isspace():
                        rem += 1
                    return (rem, prefix)
        return None

    def _take_earliest_first_output(self) -> Optional[str]:
        """首段资格内：硬句末、软逗号、overflow 边界取最早者。"""
        buf = self._buf
        best_end: Optional[int] = None
        best_sentence: Optional[str] = None

        match = _END_RE.search(buf)
        if match:
            end = match.end()
            sentence = buf[:end].strip()
            if sentence:
                best_end = end
                best_sentence = sentence

        soft = self._earliest_soft_comma(buf)
        if soft is not None:
            rem, sentence = soft
            if best_end is None or rem < best_end:
                best_end = rem
                best_sentence = sentence

        if len(buf) >= self.max_buffer:
            piece = buf[: self.max_buffer].strip()
            if piece and (best_end is None or self.max_buffer < best_end):
                best_end = self.max_buffer
                best_sentence = piece

        if best_sentence is None or best_end is None:
            return None

        self._buf = buf[best_end:]
        return best_sentence

    def _extract(self, *, force_overflow: bool) -> List[str]:
        out: List[str] = []
        if (
            force_overflow
            and self._first_clause_eligible
            and self.first_clause_min_chars is not None
        ):
            first = self._take_earliest_first_output()
            if first:
                out.append(first)
                self._first_clause_eligible = False
        while True:
            match = _END_RE.search(self._buf)
            if not match:
                break
            end = match.end()
            sentence = self._buf[:end].strip()
            self._buf = self._buf[end:]
            if sentence:
                out.append(sentence)
                self._first_clause_eligible = False
        if force_overflow:
            while len(self._buf) >= self.max_buffer:
                chunk = self._buf[: self.max_buffer]
                self._buf = self._buf[self.max_buffer :]
                piece = chunk.strip()
                if piece:
                    out.append(piece)
                    self._first_clause_eligible = False
        return out
