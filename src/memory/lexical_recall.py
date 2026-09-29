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
from src.memory.models import MemoryRecord
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

    # 单字/无 bigram 查询：允许 unigram 命中
    uni_hits = sum(1 for t in inter if t.startswith("c:") or t.startswith("u:"))
    return float(uni_hits)


def _sort_key(hit: ScoredMemory) -> tuple:
    record = hit.record
    # 相关度降序、importance 降序、更新时间降序、id 升序（稳定）
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

        try:
            # 多读 1 条仅用于判断是否还有未扫描候选；不参与打分与 candidate_count
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
            score = score_overlap(query_tokens, lexical_tokens(record.text))
            if score <= 0.0:
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
