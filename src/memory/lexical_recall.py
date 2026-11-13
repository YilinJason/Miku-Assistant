"""确定性本地词法检索（非语义检索；无 embeddings / 分词库依赖）。"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional, Set

from src.memory.errors import (
    MemoryDataCorruptError,
    MemoryError,
    MemoryStoreUnavailableError,
    MemoryValidationError,
)
from src.memory.models import MemoryCategory, MemoryRecord
from src.memory.service_models import (
    MemoryRecallResult,
    ScoredMemory,
    empty_recall_result,
)
from src.memory.store import MemoryStore

# 拉丁词：casefold 后的字母数字序列
_LATIN_WORD_RE = re.compile(r"[a-z0-9]+", re.UNICODE)
# 常用汉字与兼容表意文字；不含标点
_CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")

DEFAULT_RECALL_LIMIT = 8
MAX_RECALL_LIMIT = 50
DEFAULT_CANDIDATE_LIMIT = 200
MAX_CANDIDATE_LIMIT = 500

# 记忆询问/事实框架词（最长优先）；派生内容特征时剔除
_FRAME_PHRASES: tuple[str, ...] = tuple(
    sorted(
        (
            "全新的会话",
            "全新会话",
            "这是一个",
            "你知道",
            "你记得",
            "还记得",
            "最喜欢",
            "是什么",
            "偏好",
            "喜欢",
            "我的",
            "用户",
            "什么",
            "哪个",
            "哪些",
            "会话",
            "这是",
            "我",
            "吗",
            "呢",
        ),
        key=len,
        reverse=True,
    )
)
_FRAME_RE = re.compile("|".join(re.escape(p) for p in _FRAME_PHRASES))
# 弱领域 / 上位类标签（单独不能证明对象相关；不作具体内容键）
_DOMAIN_SLOT_LABELS = frozenset(
    {
        "口味",
        "味道",
        "东西",
        "食物",
        "饮料",
        "饮品",
        "种类",
        "类型",
    }
)
_WEAK_CONTENT_BIGRAMS = frozenset(f"cb:{lab}" for lab in _DOMAIN_SLOT_LABELS if len(lab) >= 2)
# 剥离框架后残留的功能字：不作具体对象
_NON_OBJECT_UNIGRAMS = frozenset(
    {
        "c:喝",
        "c:吃",
        "c:的",
        "c:和",
        "c:是",
        "c:了",
        "c:在",
        "c:有",
        "c:想",
        "c:要",
    }
)


def lexical_tokens(text: str) -> frozenset[str]:
    """
    中英混合词法特征：

    - 英文：casefold 后的单词；长度 >= 3 时再加相邻字符 bigram
    - 中文：连续汉字的 unigram 与相邻 bigram
    - 其它非空白字符：单字符弱特征（确定性）
    """
    if not isinstance(text, str) or not text.strip():
        return frozenset()
    folded = text.casefold()
    tokens: Set[str] = set()

    for match in _LATIN_WORD_RE.finditer(folded):
        word = match.group(0)
        tokens.add(f"w:{word}")
        if len(word) >= 3:
            for i in range(len(word) - 1):
                tokens.add(f"wb:{word[i : i + 2]}")

    cjk_run: list[str] = []

    def flush_cjk() -> None:
        nonlocal cjk_run
        if not cjk_run:
            return
        for ch in cjk_run:
            tokens.add(f"c:{ch}")
        for i in range(len(cjk_run) - 1):
            tokens.add(f"cb:{cjk_run[i]}{cjk_run[i + 1]}")
        cjk_run = []

    for ch in folded:
        if _CJK_CHAR_RE.fullmatch(ch):
            cjk_run.append(ch)
            continue
        flush_cjk()
        if ch.isspace() or _LATIN_WORD_RE.fullmatch(ch):
            continue
        tokens.add(f"u:{ch}")
    flush_cjk()
    return frozenset(tokens)


def strip_memory_frame(text: str) -> str:
    """去掉固定记忆询问/事实框架词，保留具体对象内容。"""
    if not isinstance(text, str) or not text:
        return ""
    return _FRAME_RE.sub("", text)


def content_lexical_tokens(text: str) -> frozenset[str]:
    """内容词法特征：忽略询问/事实框架后的 lexical_tokens。"""
    return lexical_tokens(strip_memory_frame(text))


def query_domain_labels(query: str) -> frozenset[str]:
    """查询中的弱领域 / 上位类标签（框架剥离后）。"""
    focused = strip_memory_frame(query)
    if not focused:
        return frozenset()
    return frozenset(lab for lab in _DOMAIN_SLOT_LABELS if lab in focused)


def fact_domain_slots(fact_text: str) -> frozenset[str]:
    """
    从规范事实（或可严格解析的第一人称）正文读取领域 slot。

    ``like_most_drink`` 隐含 slot=饮品。不引入「拿铁=饮品」等脆弱词表。
    """
    from src.memory.provenance import (
        parse_fact_predicates_strict,
        parse_user_predicates_strict,
    )

    if not isinstance(fact_text, str) or not fact_text.strip():
        return frozenset()
    text = fact_text.strip().rstrip("。．.!！？?\n")
    claims = parse_fact_predicates_strict(text)
    if not claims:
        claims = parse_user_predicates_strict(text)
    if not claims:
        return frozenset()
    slots: Set[str] = set()
    for claim in claims:
        if claim.slot:
            slots.add(claim.slot.strip())
        if claim.render_key == "like_most_drink":
            slots.add("饮品")
    return frozenset(s for s in slots if s)


def concrete_content_keys(content_tokens: frozenset[str]) -> frozenset[str]:
    """
    具体对象内容键（基于已有内容 token）：非领域 bigram 与拉丁词。

    单字请用 ``concrete_content_keys_from_text``，避免「茶」误命中「抹茶」。
    """
    keys: Set[str] = set()
    for t in content_tokens:
        if t in _WEAK_CONTENT_BIGRAMS:
            continue
        if t.startswith("cb:"):
            lab = t[3:]
            if lab in _DOMAIN_SLOT_LABELS:
                continue
            keys.add(t)
        elif t.startswith("w:") or t.startswith("wb:"):
            keys.add(t)
    return frozenset(keys)


def concrete_content_keys_from_text(text: str) -> frozenset[str]:
    """
    从正文派生具体对象键：

    - 可严格解析时：用谓词 value 的 token（单字值如茶/猫；多字不拆出内嵌单字）
    - 不可解析时：回退到内容词法（含内嵌单字），以兼容旧自由文本记录
    - 多字 run 的 bigram / 拉丁词始终纳入
    """
    if not isinstance(text, str) or not text.strip():
        return frozenset()

    from src.memory.provenance import (
        _value_tokens,
        parse_fact_predicates_strict,
        parse_user_predicates_strict,
    )

    focused = strip_memory_frame(text)
    keys: Set[str] = set()

    for match in _LATIN_WORD_RE.finditer(focused.casefold()):
        word = match.group(0)
        keys.add(f"w:{word}")
        if len(word) >= 3:
            for i in range(len(word) - 1):
                keys.add(f"wb:{word[i : i + 2]}")

    cjk_run: list[str] = []

    def flush_cjk_bigrams_only() -> None:
        nonlocal cjk_run
        if len(cjk_run) >= 2:
            for i in range(len(cjk_run) - 1):
                bg = f"{cjk_run[i]}{cjk_run[i + 1]}"
                token = f"cb:{bg}"
                if bg in _DOMAIN_SLOT_LABELS or token in _WEAK_CONTENT_BIGRAMS:
                    continue
                keys.add(token)
        cjk_run = []

    for ch in focused.casefold():
        if _CJK_CHAR_RE.fullmatch(ch):
            cjk_run.append(ch)
            continue
        flush_cjk_bigrams_only()
    flush_cjk_bigrams_only()

    cleaned = text.strip().rstrip("。．.!！？?\n")
    claims = parse_fact_predicates_strict(cleaned)
    if not claims:
        claims = parse_user_predicates_strict(cleaned)

    if claims:
        for claim in claims:
            for tok in _value_tokens(claim.value):
                if not tok:
                    continue
                if len(tok) == 1 and _CJK_CHAR_RE.fullmatch(tok):
                    token = f"c:{tok}"
                    if token not in _NON_OBJECT_UNIGRAMS:
                        keys.add(token)
                elif len(tok) >= 2 and all(_CJK_CHAR_RE.fullmatch(ch) for ch in tok):
                    for i in range(len(tok) - 1):
                        bg = tok[i : i + 2]
                        token = f"cb:{bg}"
                        if bg in _DOMAIN_SLOT_LABELS or token in _WEAK_CONTENT_BIGRAMS:
                            continue
                        keys.add(token)
                else:
                    folded = tok.casefold()
                    if len(folded) >= 2 and folded.isascii():
                        keys.add(f"w:{folded}")
    else:
        # 自由文本：保留内嵌单字，避免「猫」无法命中「养了一只猫」
        for t in content_lexical_tokens(text):
            if t.startswith("c:") and t not in _NON_OBJECT_UNIGRAMS:
                keys.add(t)
            elif t.startswith("cb:"):
                lab = t[3:]
                if lab in _DOMAIN_SLOT_LABELS or t in _WEAK_CONTENT_BIGRAMS:
                    continue
                keys.add(t)
            elif t.startswith("w:") or t.startswith("wb:"):
                keys.add(t)

    # 整句剥离框架后若仅为孤立单字，确保查询「猫/茶」有键
    only = focused.strip()
    if len(only) == 1 and _CJK_CHAR_RE.fullmatch(only):
        token = f"c:{only}"
        if token not in _NON_OBJECT_UNIGRAMS:
            keys.add(token)

    return frozenset(keys)


def is_generic_preference_query(query: str) -> bool:
    """
    真正无对象的广义偏好询问（如「我喜欢什么」）。

    含单字对象、多字对象或弱领域标签时不得命中。
    """
    if not isinstance(query, str) or not query.strip():
        return False
    if query_domain_labels(query):
        return False
    if concrete_content_keys_from_text(query):
        return False
    return any(p in query for p in ("喜欢", "偏好", "最喜欢"))


def _record_allowed_for_query(
    query: str,
    *,
    query_tokens: frozenset[str],
    query_content: frozenset[str],
    record_text: str,
    record_category: MemoryCategory,
    doc_tokens: frozenset[str],
    doc_content: frozenset[str],
) -> bool:
    """
    失败关闭的对象相关性：

    - generic：仅偏好类
    - concrete：须有同一具体单字或更强内容交集
    - domain：须规范事实 slot 与领域标签相交；无 slot 且无具体交集 → 拒绝
    - 不得用「喜欢」等框架词证明对象相关
    """
    del query_content, doc_content  # 改用正文派生，保证单字孤立语义
    domains = query_domain_labels(query)
    q_concrete = concrete_content_keys_from_text(query)
    d_concrete = concrete_content_keys_from_text(record_text)
    generic = is_generic_preference_query(query)

    if generic:
        return record_category == MemoryCategory.PREFERENCE

    matched = False
    if q_concrete:
        inter = q_concrete & d_concrete
        if inter:
            q_bigrams = frozenset(t for t in q_concrete if t.startswith("cb:"))
            if len(q_bigrams) >= 2:
                hit_bigrams = frozenset(t for t in inter if t.startswith("cb:"))
                if hit_bigrams or any(
                    t.startswith("c:") or t.startswith("w:") for t in inter
                ):
                    matched = True
            else:
                matched = True

    if domains and not matched:
        slots = fact_domain_slots(record_text)
        if slots & domains:
            matched = True

    if matched:
        return True

    if q_concrete or domains:
        return False

    return score_overlap(query_tokens, doc_tokens) > 0.0


def score_overlap(query_tokens: frozenset[str], doc_tokens: frozenset[str]) -> float:
    """
    相关度：拉丁词与中文 bigram 为主信号；单字 unigram 仅在查询无 bigram 时生效。

    避免「今天」与「天气」因共享「天」而产生假阳性。
    """
    if not query_tokens or not doc_tokens:
        return 0.0
    inter = query_tokens & doc_tokens
    if not inter:
        return 0.0

    score = 0.0
    for token in inter:
        if token.startswith("w:"):
            score += 3.0
        elif token.startswith("cb:"):
            score += 2.0
        elif token.startswith("wb:"):
            score += 1.0

    if score > 0.0:
        return score

    query_has_cjk_bigram = any(t.startswith("cb:") for t in query_tokens)
    query_has_latin_word = any(t.startswith("w:") for t in query_tokens)
    if query_has_cjk_bigram or query_has_latin_word:
        return 0.0

    uni_hits = sum(1 for t in inter if t.startswith("c:") or t.startswith("u:"))
    return float(uni_hits)


def _sort_key(hit: ScoredMemory) -> tuple:
    record = hit.record
    return (
        -hit.score,
        -record.importance,
        -record.updated_at.timestamp(),
        record.id,
    )


class LexicalMemoryRecall:
    """
    扫描存储返回的最近有限候选集，做确定性词法打分。

    无正相关信号时返回空；不把本实现描述为语义检索。
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._store = store
        self._logger = logger or logging.getLogger(__name__)

    def recall(
        self,
        query: str,
        *,
        limit: int = DEFAULT_RECALL_LIMIT,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> MemoryRecallResult:
        if not isinstance(query, str):
            raise MemoryValidationError("query must be str")
        limit = self._require_positive_capped(
            "limit", limit, hard_max=MAX_RECALL_LIMIT
        )
        candidate_limit = self._require_positive_capped(
            "candidate_limit", candidate_limit, hard_max=MAX_CANDIDATE_LIMIT
        )

        query_tokens = lexical_tokens(query)
        if not query_tokens:
            return empty_recall_result()

        query_content = content_lexical_tokens(query)
        generic_pref = is_generic_preference_query(query)

        try:
            fetched = self._store.list_memories(
                limit=candidate_limit + 1,
                newest_first=True,
            )
        except (MemoryStoreUnavailableError, MemoryDataCorruptError) as exc:
            self._logger.error(
                "memory recall store failure err=%s", type(exc).__name__
            )
            return empty_recall_result(error_type=type(exc).__name__)
        except MemoryError as exc:
            self._logger.error(
                "memory recall memory error err=%s", type(exc).__name__
            )
            return empty_recall_result(error_type=type(exc).__name__)

        truncated = len(fetched) > candidate_limit
        candidates = fetched[:candidate_limit]

        hits: list[ScoredMemory] = []
        seen_ids: set[str] = set()
        for record in candidates:
            if not isinstance(record, MemoryRecord):
                continue
            if record.id in seen_ids:
                continue
            seen_ids.add(record.id)
            doc_tokens = lexical_tokens(record.text)
            doc_content = content_lexical_tokens(record.text)

            if not _record_allowed_for_query(
                query,
                query_tokens=query_tokens,
                query_content=query_content,
                record_text=record.text,
                record_category=record.category,
                doc_tokens=doc_tokens,
                doc_content=doc_content,
            ):
                continue

            score = score_overlap(query_tokens, doc_tokens)
            if score <= 0.0:
                if generic_pref and record.category == MemoryCategory.PREFERENCE:
                    score = 0.5
                else:
                    continue
            hits.append(ScoredMemory(record=record, score=score))

        hits.sort(key=_sort_key)
        selected = tuple(hits[:limit])
        return MemoryRecallResult(
            hits=selected,
            candidate_count=len(candidates),
            truncated_candidates=truncated,
        )

    @staticmethod
    def _require_positive_capped(name: str, value: int, *, hard_max: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise MemoryValidationError(f"{name} must be a positive int")
        if value > hard_max:
            raise MemoryValidationError(f"{name} must be <= {hard_max}")
        return value


def tokens_for_tests(texts: Iterable[str]) -> frozenset[str]:
    """测试辅助：合并多段文本的词法特征。"""
    merged: Set[str] = set()
    for text in texts:
        merged |= set(lexical_tokens(text))
    return frozenset(merged)
