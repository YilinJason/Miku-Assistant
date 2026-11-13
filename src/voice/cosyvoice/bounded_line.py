# -*- coding: utf-8 -*-
"""有界文本行读取（避免无 size 的 readline 在坏协议下无限分配）。"""
from __future__ import annotations

from typing import IO, Optional


class BoundedLineTooLongError(Exception):
    """单行超过上限。"""


def read_bounded_line(stream: IO[str], max_chars: int) -> Optional[str]:
    """
    读取一行（含末尾 ``\\n`` 若存在），不含换行时最多 ``max_chars`` 个字符。

    超过 ``max_chars`` 且仍未见换行则抛出 ``BoundedLineTooLongError``。
    遇 EOF 且无内容返回 ``None``；有内容无换行则返回已读片段（不含 ``\\n``）。
    """
    if max_chars <= 0:
        raise ValueError("max_chars")
    buf: list[str] = []
    while len(buf) <= max_chars:
        ch = stream.read(1)
        if not ch:
            if not buf:
                return None
            return "".join(buf)
        if ch == "\n":
            return "".join(buf) + "\n"
        buf.append(ch)
    raise BoundedLineTooLongError()
