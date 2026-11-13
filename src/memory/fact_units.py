"""本地可信事实单元：切分、严格解析、规范渲染；模型不得创造事实正文。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from src.memory.models import MemoryCategory
from src.memory.provenance import (
    PredicateClaim,
    _parse_single_unit_user,
    clause_has_interrogative_mood,
    clause_has_meta_denial,
    clause_has_uncertain_tone,
    clause_has_unknown_slot,
)

# 子句边界；终止符保留语义（尤其 ？/? 不得当陈述）
_BOUNDARY_RE = re.compile(r"[，,、；;。．.!！？?\n]")
_QUESTION_TERMINATORS = frozenset({"？", "?"})
# 仅完整并列连接词；禁止裸「并」（避免拆开「并且/并不是/并蒂莲」）
_SUPPORTED_CONJ_RE = re.compile(r"(?:并且|而且|以及)")


@dataclass(frozen=True)
class UserFactUnit:
    """本地识别的不可变用户事实单元；text 必须等于 user_text[start:end]。"""

    unit_id: int
    start: int
    end: int
    text: str
    terminator: str
    claim: PredicateClaim

    def __post_init__(self) -> None:
        if self.unit_id < 0:
            raise ValueError("unit_id must be >= 0")
        if self.start < 0 or self.end < self.start:
            raise ValueError("invalid span")
        if not self.text:
            raise ValueError("text must be non-empty")


_RENDERERS: dict[str, Callable[[str], str]] = {
    "like_neg": lambda v: f"用户不喜欢{v}",
    "like_hate": lambda v: f"用户讨厌{v}",
    "like_cant": lambda v: f"用户受不了{v}",
    "like_most_drink": lambda v: f"用户最喜欢的饮品是{v}",
    "like_most": lambda v: f"用户最喜欢{v}",
    "like_pos": lambda v: f"用户喜欢{v}",
    "like_usual": lambda v: f"用户喜欢{v}",
    "live_neg": lambda v: f"用户不住在{v}",
    "live_pos": lambda v: f"用户住在{v}",
    "live_home": lambda v: f"用户家在{v}",
    "know_neg": lambda v: f"用户不认识{v}",
    "know_pos": lambda v: f"用户认识{v}",
    "id_neg_is": lambda v: f"用户不是{v}",
    "id_neg_name": lambda v: f"用户不叫{v}",
    "id_name": lambda v: f"用户叫{v}",
    "id_fullname": lambda v: f"用户的名字是{v}",
    "id_is": lambda v: f"用户是{v}",
    "code_test": lambda v: f"用户的测试代号是{v}",
    "code": lambda v: f"用户的代号是{v}",
}


def render_claim(claim: PredicateClaim) -> str:
    """由本地 claim 生成唯一规范事实正文；不得自由摘要。"""
    if claim.render_key == "like_of_is":
        if not claim.slot:
            raise ValueError("like_of_is requires slot")
        return f"用户喜欢的{claim.slot}是{claim.value}"
    if claim.render_key == "like_most_of_is":
        if not claim.slot:
            raise ValueError("like_most_of_is requires slot")
        return f"用户最喜欢的{claim.slot}是{claim.value}"
    renderer = _RENDERERS.get(claim.render_key)
    if renderer is None:
        raise ValueError(f"unknown render_key={claim.render_key!r}")
    return renderer(claim.value)


def category_for_claim(claim: PredicateClaim) -> MemoryCategory:
    if claim.family == "like":
        return MemoryCategory.PREFERENCE
    if claim.family == "know":
        return MemoryCategory.RELATIONSHIP
    if claim.family in ("identity", "code", "live"):
        return MemoryCategory.USER_PROFILE
    return MemoryCategory.OTHER


def _remember_prefix_gap_end(user_text: str) -> int:
    """
    若原文以白名单「记住」前缀开头，返回前缀及可选空白/冒号/逗号之后的下标；
    这些字符内的边界标点不得切开事实单元。
    """
    from src.memory.provenance import _REMEMBER_PREFIXES

    if not user_text:
        return 0
    for prefix in _REMEMBER_PREFIXES:
        if user_text.startswith(prefix):
            i = len(prefix)
            while i < len(user_text) and user_text[i] in " \t:：,，":
                i += 1
            return i
    return 0


def _iter_boundary_segments(user_text: str) -> list[tuple[int, int, str, str]]:
    """返回 (start, end, text, terminator)；text == user_text[start:end]。"""
    if not isinstance(user_text, str) or not user_text:
        return []
    segments: list[tuple[int, int, str, str]] = []
    last = 0
    protect_until = _remember_prefix_gap_end(user_text)
    for match in _BOUNDARY_RE.finditer(user_text):
        if match.start() < protect_until:
            continue
        start = last
        end = match.start()
        raw = user_text[start:end]
        lead = len(raw) - len(raw.lstrip(" \t\r"))
        trail = len(raw) - len(raw.rstrip(" \t\r"))
        piece_start = start + lead
        piece_end = end - trail if trail else end
        if piece_start < piece_end:
            segments.append(
                (
                    piece_start,
                    piece_end,
                    user_text[piece_start:piece_end],
                    match.group(0),
                )
            )
        last = match.end()
    raw = user_text[last:]
    lead = len(raw) - len(raw.lstrip(" \t\r"))
    trail = len(raw) - len(raw.rstrip(" \t\r"))
    piece_start = last + lead
    piece_end = len(user_text) - trail if trail else len(user_text)
    if piece_start < piece_end:
        segments.append(
            (piece_start, piece_end, user_text[piece_start:piece_end], "")
        )
    return segments


def _segment_fails_context(seg_text: str, terminator: str) -> bool:
    """
    标点 segment 级失败关闭：终止符与段内任意位置的疑问/不确定/否认。

    必须在并列拆分之前执行，避免问号只附着到最后一个 child。
    """
    if terminator in _QUESTION_TERMINATORS:
        return True
    if not seg_text or not seg_text.strip():
        return True
    if clause_has_unknown_slot(seg_text):
        return True
    if clause_has_interrogative_mood(seg_text):
        return True
    if clause_has_uncertain_tone(seg_text):
        return True
    if clause_has_meta_denial(seg_text):
        return True
    return False


def _split_supported_conj_pieces(
    text: str, base_start: int
) -> Optional[list[tuple[int, int, str]]]:
    """
    按完整连接词 ``并且|而且|以及`` 切分。

    找不到连接词、任一侧为空、或切出少于 2 段时返回 ``None``（表示不可用并列路径）。
    """
    if not _SUPPORTED_CONJ_RE.search(text):
        return None
    pieces: list[tuple[int, int, str]] = []
    last = 0
    for match in _SUPPORTED_CONJ_RE.finditer(text):
        chunk = text[last : match.start()]
        if not chunk.strip():
            return None
        lead = len(chunk) - len(chunk.lstrip())
        trail = len(chunk) - len(chunk.rstrip())
        ps = last + lead
        pe = match.start() - trail if trail else match.start()
        if ps >= pe:
            return None
        pieces.append((base_start + ps, base_start + pe, text[ps:pe]))
        last = match.end()
    chunk = text[last:]
    if not chunk.strip():
        return None
    lead = len(chunk) - len(chunk.lstrip())
    trail = len(chunk) - len(chunk.rstrip())
    ps = last + lead
    pe = len(text) - trail if trail else len(text)
    if ps >= pe:
        return None
    pieces.append((base_start + ps, base_start + pe, text[ps:pe]))
    if len(pieces) < 2:
        return None
    return pieces


def try_parse_segment_units(
    *,
    user_text: str,
    seg_start: int,
    seg_text: str,
    terminator: str,
) -> Optional[list[tuple[int, int, str, PredicateClaim]]]:
    """
    对单个标点 segment 做完整解析。

    成功返回全部 child ``(start, end, text, claim)``；失败返回 ``None``（整段拒绝）。
    已发现并列连接词但结构非法时不得回退单事实解析。
    """
    if user_text[seg_start : seg_start + len(seg_text)] != seg_text:
        return None
    if _segment_fails_context(seg_text, terminator):
        return None

    # 区分「无连接词」与「有连接词但结构非法」：后者立即整段拒绝，禁止回退
    if _SUPPORTED_CONJ_RE.search(seg_text):
        pieces = _split_supported_conj_pieces(seg_text, seg_start)
        if pieces is None:
            return None
        out: list[tuple[int, int, str, PredicateClaim]] = []
        for start, end, text in pieces:
            if user_text[start:end] != text:
                return None
            claim = _parse_single_unit_user(text)
            if claim is None:
                return None
            out.append((start, end, text, claim))
        return out

    # 无并列连接词：整段恰好一条事实
    whole = _parse_single_unit_user(seg_text)
    if whole is None:
        return None
    return [
        (
            seg_start,
            seg_start + len(seg_text),
            seg_text,
            whole,
        )
    ]


def enumerate_safe_fact_units(user_text: str) -> tuple[UserFactUnit, ...]:
    """
    从用户原文枚举本地安全候选单元。

    标点 segment 先做语境验收；同段并列 all-or-nothing；跨逗号兄弟互不影响。
    """
    if not isinstance(user_text, str) or not user_text.strip():
        return ()
    units: list[UserFactUnit] = []
    next_id = 0
    for seg_start, _seg_end, seg_text, terminator in _iter_boundary_segments(user_text):
        parsed = try_parse_segment_units(
            user_text=user_text,
            seg_start=seg_start,
            seg_text=seg_text,
            terminator=terminator,
        )
        if parsed is None:
            continue
        for u_start, u_end, u_text, claim in parsed:
            units.append(
                UserFactUnit(
                    unit_id=next_id,
                    start=u_start,
                    end=u_end,
                    text=u_text,
                    terminator=terminator,
                    claim=claim,
                )
            )
            next_id += 1
    return tuple(units)


def public_candidate_list(units: Sequence[UserFactUnit]) -> list[dict[str, object]]:
    """发给模型的只读候选列表（仅 unit_id + 原文切片）。"""
    return [{"unit_id": u.unit_id, "text": u.text} for u in units]
