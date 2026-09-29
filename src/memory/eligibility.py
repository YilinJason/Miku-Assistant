"""记忆提取资格：窄、可解释的本地边界（非完整注入防护）。"""
from __future__ import annotations

import re
from typing import Sequence

from src.memory.provenance import has_user_self_assertion, looks_like_pure_memory_query
from src.memory.service_models import ExtractedMemory, MemoryTurnEvent

# 稳定跨会话信号（窄正向）；缺省不得仅靠黑名单判定
# 注意：准入不得因 assistant_text 含这些词而放行；纯记忆询问另见 provenance
_STABLE_SIGNAL_RE = re.compile(
    r"(喜欢|最爱|偏好|讨厌|受不了|我叫|我的名字|住在|家在|"
    r"认识|朋友|同事|家人|生日|过敏|"
    r"prefer|favorite|my name|i am|i'm|live in|friend)",
    re.IGNORECASE,
)

# 临时算术 / 纯寒暄（无稳定信号时跳过提取）
_SIMPLE_ARITH_RE = re.compile(
    r"(\d+\s*[\+\-\×\*xX÷/]\s*\d+|\d+\s*等于\s*几|"
    r"what\s+is\s+\d+\s*[\+\-\*\/]\s*\d+)",
    re.IGNORECASE,
)
_GREETING_RE = re.compile(
    r"^(嗨|你好|您好|哈喽|hello|hi|hey)[\s\,，。!！?？]*$",
    re.IGNORECASE,
)

# 明显要求模型忽略规则 / 执行伪指令（窄）；不得冒充完整注入防护
_INJECTION_MARKER_RE = re.compile(
    r"(忽略.{0,12}规则|忽略以上|ignore\s+(all|previous|above|prior)|"
    r"disregard\s+(all|previous|above)|"
    r"输出.{0,8}密码|执行关机|system\s*prompt)",
    re.IGNORECASE,
)

# 提取结果本地过滤：纯算术答案或回声式伪指令
_PURE_MATH_RESULT_RE = re.compile(
    r"^[\d\s\+\-\*=等于约是为\.]+$",
)
_INJECTION_ECHO_RE = re.compile(
    r"(忽略.*规则|系统密码|执行关机|ignore\s+(all|previous))",
    re.IGNORECASE,
)


def has_stable_memory_signal(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(_STABLE_SIGNAL_RE.search(text))


def looks_like_prompt_injection(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(_INJECTION_MARKER_RE.search(text))


def looks_like_ephemeral_chitchat(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    stripped = text.strip()
    if _GREETING_RE.match(stripped):
        return True
    if _SIMPLE_ARITH_RE.search(stripped) and not has_stable_memory_signal(stripped):
        return True
    return False


def should_skip_extraction(turn: MemoryTurnEvent) -> bool:
    """
    整轮不值得调用模型时返回 True。

    准入只看 ``user_text``：不得因助手回答含稳定词而提取。
    纯记忆查询（无用户自我陈述）跳过；含「我最喜欢…，你呢？」等自我陈述仍可提取。
    """
    if not isinstance(turn, MemoryTurnEvent) or not turn.is_extractable:
        return True
    user = turn.user_text
    # 纯记忆询问优先：避免「最喜欢的饮品」等问句标签误触发稳定信号
    if looks_like_pure_memory_query(user):
        return True
    if has_user_self_assertion(user) or has_stable_memory_signal(user):
        return False
    if looks_like_prompt_injection(user):
        return True
    if looks_like_ephemeral_chitchat(user):
        return True
    # 用户侧很短且无稳定信号：寒暄/算术
    if len(user.strip()) <= 24:
        if _SIMPLE_ARITH_RE.search(user) or _GREETING_RE.match(user.strip()):
            return True
    return False


def filter_extracted_memories(
    items: Sequence[ExtractedMemory],
    *,
    turn: MemoryTurnEvent,
) -> tuple[ExtractedMemory, ...]:
    """对模型输出做窄本地过滤；不得一刀切删除全部 other。"""
    out: list[ExtractedMemory] = []
    for item in items:
        text = item.text.strip()
        if _PURE_MATH_RESULT_RE.match(text) and not has_stable_memory_signal(text):
            continue
        if _INJECTION_ECHO_RE.search(text) and not has_stable_memory_signal(text):
            continue
        # 注入轮次且用户侧无自我陈述：丢弃候选（不读 assistant_text）
        if looks_like_prompt_injection(turn.user_text) and not has_user_self_assertion(
            turn.user_text
        ):
            continue
        out.append(item)
    return tuple(out)
