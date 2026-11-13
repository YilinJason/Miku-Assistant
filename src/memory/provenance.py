"""用户原文证据校验：候选事实不得由助手回答补全或极性翻转。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

# ---------------------------------------------------------------------------
# 子句 / 询问检测
# ---------------------------------------------------------------------------

_CLAUSE_SPLIT_RE = re.compile(r"[，,、；;。．.!！？?\n]+")
_CONJ_SPLIT_RE = re.compile(r"(?:并且|而且|以及)")
_UNKNOWN_SLOT_RE = re.compile(
    r"(什么|啥|谁|哪里|哪儿|哪天|几号|多少|哪个|哪些|是否|是不是|有没有|吗|呢|？|\?)"
)
# 不确定语气：即使子串看似肯定陈述，所在完整单元也不得作证据
_UNCERTAIN_TONE_RE = re.compile(r"(不确定|不清楚|可能|也许|大概|猜测)")
# 疑问语气助词 / 正反问（部分已在 unknown slot；此处作显式失败关闭）
_INTERROGATIVE_MOOD_RE = re.compile(r"(吗|呢|是否|是不是|有没有)")

# 纯记忆询问（向助手索取资料）
_MEMORY_QUERY_RE = re.compile(
    r"(你知道|你记得|你还记得|还记得我|你了解我|"
    r"do you (know|remember)|what('s| is) my)",
    re.IGNORECASE,
)

# 用户侧自我陈述标记（尚需排除疑问槽位）
_SELF_ASSERTION_MARK_RE = re.compile(
    r"("
    r"我叫|我的名字是|我是(?![否不])|"
    r"我不住在|我住在|我家在|"
    r"我不认识|我认识|"
    r"我不喜欢|我讨厌|我喜欢|我最喜欢|我平时.{0,8}喜欢|"
    r"我的?测试代号是|我的?代号是|"
    r"my name is|i(?:'m| am)\s|i (?:don't |do not )?live|i (?:don't |do not )?like|"
    r"i love|i prefer|i (?:don't |do not )?know"
    r")",
    re.IGNORECASE,
)

# 核心值若仅为字段标签 / 疑问占位则拒绝
_PLACEHOLDER_VALUES = frozenset(
    {
        "测试代号",
        "代号",
        "什么饮品",
        "饮品",
        "哪里",
        "哪儿",
        "名字",
        "什么名字",
        "什么",
        "啥",
        "谁",
        "哪个",
        "哪些",
        "多少",
        "哪天",
        "几号",
    }
)

# 可作为「喜欢的X是Y」槽位名的领域标签（不得作为 value）
_ALLOWED_DOMAIN_SLOTS = frozenset(
    {
        "饮品",
        "饮料",
        "食物",
        "口味",
        "味道",
        "类型",
        "种类",
        "冰激凌",
        "冰淇淋",
        "甜筒",
        "东西",
    }
)

_VALUE_LATIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{1,}")
# 允许单字中文事实值（茶/猫/狗）；占位与标签仍由 is_placeholder_core 拒绝
_VALUE_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+")

_LABEL_STOP = frozenset(
    {
        "用户",
        "喜欢",
        "最喜欢",
        "最爱",
        "偏好",
        "讨厌",
        "名字",
        "代号",
        "测试",
        "饮品",
        "住在",
        "家在",
        "认识",
        "朋友",
        "同事",
        "家人",
        "生日",
        "过敏",
        "知道",
        "记得",
        "什么",
        "哪个",
        "哪些",
        "不喜欢",
        "不住在",
        "不认识",
    }
)


def split_clauses(text: str) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return []
    return [part.strip() for part in _CLAUSE_SPLIT_RE.split(text) if part.strip()]


def split_fact_units(text: str) -> list[str]:
    """
    将候选拆成原子单元：标点子句 + ``并/而且/以及`` 连接。

    用于完整覆盖验证；任一单元未解析则整项拒绝。
    """
    units: list[str] = []
    for clause in split_clauses(text):
        for part in _CONJ_SPLIT_RE.split(clause):
            piece = part.strip()
            if piece:
                units.append(piece)
    if not units and isinstance(text, str) and text.strip():
        units.append(text.strip())
    return units


def _iter_delimited_spans(
    text: str, delimiter: re.Pattern[str]
) -> list[tuple[int, int, str]]:
    """按分隔符切分并保留每段在 ``text`` 中的 [start, end) 原样子串跨度。"""
    if not isinstance(text, str) or not text:
        return []
    spans: list[tuple[int, int, str]] = []
    last = 0
    for match in delimiter.finditer(text):
        raw = text[last : match.start()]
        stripped = raw.strip()
        if stripped:
            lead = len(raw) - len(raw.lstrip())
            start = last + lead
            spans.append((start, start + len(stripped), stripped))
        last = match.end()
    raw = text[last:]
    stripped = raw.strip()
    if stripped:
        lead = len(raw) - len(raw.lstrip())
        start = last + lead
        spans.append((start, start + len(stripped), stripped))
    return spans


def iter_clause_spans(text: str) -> list[tuple[int, int, str]]:
    """``user_text`` 子句跨度（不含分隔标点）。"""
    return _iter_delimited_spans(text, _CLAUSE_SPLIT_RE)


def enclosing_evidence_unit(user_text: str, quote_start: int, quote_end: int) -> Optional[str]:
    """
    定位 ``evidence_quote`` 某次出现所在的完整用户原子单元（子句内再按并列连词切）。

    若 quote 跨越子句/单元边界则返回 ``None``（不得拼接）。
    """
    if not isinstance(user_text, str) or quote_start < 0 or quote_end > len(user_text):
        return None
    if quote_start >= quote_end:
        return None
    for c_start, c_end, clause in iter_clause_spans(user_text):
        if not (c_start <= quote_start and quote_end <= c_end):
            continue
        # 子句内按并列连词切成原子单元
        rel = clause
        for u_start, u_end, unit in _iter_delimited_spans(rel, _CONJ_SPLIT_RE):
            abs_start = c_start + u_start
            abs_end = c_start + u_end
            if abs_start <= quote_start and quote_end <= abs_end:
                return unit
        # 无连词切分时整句即单元
        return clause
    return None


def iter_quote_enclosing_units(user_text: str, quote: str) -> list[str]:
    """``quote`` 在原文每次出现所对应的包围单元（去重保序）。"""
    if not isinstance(user_text, str) or not isinstance(quote, str) or not quote:
        return []
    units: list[str] = []
    seen: set[str] = set()
    start = 0
    while True:
        idx = user_text.find(quote, start)
        if idx < 0:
            break
        unit = enclosing_evidence_unit(user_text, idx, idx + len(quote))
        if unit is not None and unit not in seen:
            seen.add(unit)
            units.append(unit)
        start = idx + 1
    return units


def clause_has_unknown_slot(clause: str) -> bool:
    if not isinstance(clause, str) or not clause.strip():
        return False
    return bool(_UNKNOWN_SLOT_RE.search(clause))


def clause_has_uncertain_tone(clause: str) -> bool:
    if not isinstance(clause, str) or not clause.strip():
        return False
    return bool(_UNCERTAIN_TONE_RE.search(clause))


def clause_has_interrogative_mood(clause: str) -> bool:
    if not isinstance(clause, str) or not clause.strip():
        return False
    return bool(_INTERROGATIVE_MOOD_RE.search(clause))


def clause_has_meta_denial(clause: str) -> bool:
    """句尾否认 / 元陈述（是假的、不是真的等）。"""
    if not isinstance(clause, str) or not clause.strip():
        return False
    return bool(_META_DENIAL_TAIL_RE.search(clause.strip()))


def is_clear_assertive_evidence_unit(unit: str) -> bool:
    """
    完整用户单元是否可为事实证据：明确陈述、无可疑疑问/不确定语气。
    """
    if not isinstance(unit, str) or not unit.strip():
        return False
    if clause_has_unknown_slot(unit):
        return False
    if clause_has_interrogative_mood(unit):
        return False
    if clause_has_uncertain_tone(unit):
        return False
    return True


def is_interrogative_evidence(text: str) -> bool:
    """证据子串若含未知槽位或疑问语气，不得作为事实依据。"""
    return clause_has_unknown_slot(text) or clause_has_interrogative_mood(text)


def extract_assertive_clauses(text: str) -> list[str]:
    """可本地识别的安全自我陈述原文切片。"""
    from src.memory.fact_units import enumerate_safe_fact_units

    return [unit.text for unit in enumerate_safe_fact_units(text)]


def has_user_self_assertion(text: str) -> bool:
    """用户原文是否包含可提取的自我陈述。"""
    from src.memory.fact_units import enumerate_safe_fact_units

    return bool(enumerate_safe_fact_units(text))


def looks_like_pure_memory_query(text: str) -> bool:
    """
    纯记忆 / 自我属性询问：无一明确陈述子句。

    「我最喜欢桂花乌龙，你喜欢什么？」因前半句陈述而返回 False。
    「我的测试代号是什么？」因疑问槽位返回 True。
    """
    if not isinstance(text, str) or not text.strip():
        return False
    if extract_assertive_clauses(text):
        return False
    if _MEMORY_QUERY_RE.search(text):
        return True
    # 自我属性疑问（即使未命中「你知道」）
    if _SELF_ASSERTION_MARK_RE.search(text) and clause_has_unknown_slot(text):
        return True
    if re.search(
        r"(我(的)?(测试)?代号是(什么|啥)|我最喜欢(什么|啥)|"
        r"我叫什么|我住在哪|我是谁|我生日是哪天|我对什么过敏|"
        r"我(的)?生日是(什么|啥|哪天|几号))",
        text,
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# 谓词族 + 极性
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredicateClaim:
    family: str
    polarity: bool  # True=肯定，False=否定
    value: str
    render_key: str = ""
    slot: str = ""  # 槽位句式「喜欢的X是Y」中的 X；空表示无槽位


def _norm_value(value: str) -> str:
    """仅去首尾空白与明确句末/分隔标点；不得删「的」或中文限定。"""
    return value.strip(" \t\r\n。．，,、；;：:")


# 句尾否认 / 元陈述：值或整句命中则失败关闭
_META_DENIAL_TAIL_RE = re.compile(
    r"(是假的|是谣言|是假名|是错的|是骗的?|不是真的)$"
)

# 用户（第一人称）与事实（第三人称）谓词模式：(family, polarity, render_key, pattern)
# group(1) = value；槽位句式另有 group(2)；须能 fullmatch 整句
# 少量安全话语前缀纳入完整语法；未知前缀仍失败关闭
_USER_SAFE_PREFIX = r"(?:其实|嗯+)?"

# 明确「记住」口语前缀白名单（仅句首一次；按长度降序匹配）
_REMEMBER_PREFIXES: tuple[str, ...] = (
    "我跟你说一下",
    "我想告诉你",
    "你要记住",
    "请记住",
    "记住",
)
_REMEMBER_PREFIX_GAP_RE = re.compile(r"^[\s:：,，]+")

_USER_PREDICATE_PATTERNS: tuple[tuple[str, bool, str, re.Pattern[str]], ...] = (
    ("like", False, "like_neg", re.compile(_USER_SAFE_PREFIX + r"我不喜欢\s*(.+)")),
    ("like", False, "like_hate", re.compile(_USER_SAFE_PREFIX + r"我讨厌\s*(.+)")),
    ("like", False, "like_cant", re.compile(_USER_SAFE_PREFIX + r"我受不了\s*(.+)")),
    (
        "like",
        True,
        "like_most_drink",
        re.compile(_USER_SAFE_PREFIX + r"我最喜欢的饮品是\s*(.+)"),
    ),
    (
        "like",
        True,
        "like_most_of_is",
        re.compile(_USER_SAFE_PREFIX + r"我最喜欢的(.+?)是(.+)"),
    ),
    ("like", True, "like_most", re.compile(_USER_SAFE_PREFIX + r"我最喜欢(.+)")),
    (
        "like",
        True,
        "like_of_is",
        re.compile(_USER_SAFE_PREFIX + r"我喜欢的(.+?)是(.+)"),
    ),
    ("like", True, "like_pos", re.compile(_USER_SAFE_PREFIX + r"我喜欢(.+)")),
    (
        "like",
        True,
        "like_usual",
        re.compile(_USER_SAFE_PREFIX + r"我平时.{0,8}喜欢(.+)"),
    ),
    ("live", False, "live_neg", re.compile(_USER_SAFE_PREFIX + r"我不住在\s*(.+)")),
    ("live", True, "live_pos", re.compile(_USER_SAFE_PREFIX + r"我住在\s*(.+)")),
    ("live", True, "live_home", re.compile(_USER_SAFE_PREFIX + r"我家在\s*(.+)")),
    ("know", False, "know_neg", re.compile(_USER_SAFE_PREFIX + r"我不认识\s*(.+)")),
    ("know", True, "know_pos", re.compile(_USER_SAFE_PREFIX + r"我认识\s*(.+)")),
    ("identity", False, "id_neg_is", re.compile(_USER_SAFE_PREFIX + r"我不是\s*(.+)")),
    ("identity", False, "id_neg_name", re.compile(_USER_SAFE_PREFIX + r"我不叫\s*(.+)")),
    ("identity", True, "id_name", re.compile(_USER_SAFE_PREFIX + r"我叫\s*(.+)")),
    (
        "identity",
        True,
        "id_fullname",
        re.compile(_USER_SAFE_PREFIX + r"我的名字是\s*(.+)"),
    ),
    ("identity", True, "id_is", re.compile(_USER_SAFE_PREFIX + r"我是\s*(.+)")),
    (
        "code",
        True,
        "code_test",
        re.compile(_USER_SAFE_PREFIX + r"我的?测试代号是\s*(.+)"),
    ),
    ("code", True, "code", re.compile(_USER_SAFE_PREFIX + r"我的?代号是\s*(.+)")),
)

_FACT_PREDICATE_PATTERNS: tuple[tuple[str, bool, str, re.Pattern[str]], ...] = (
    ("like", False, "like_neg", re.compile(r"用户不喜欢\s*(.+)")),
    ("like", False, "like_hate", re.compile(r"用户讨厌\s*(.+)")),
    ("like", False, "like_cant", re.compile(r"用户受不了\s*(.+)")),
    ("like", True, "like_most_drink", re.compile(r"用户最喜欢的饮品是\s*(.+)")),
    ("like", True, "like_most_of_is", re.compile(r"用户最喜欢的(.+?)是(.+)")),
    ("like", True, "like_most", re.compile(r"用户最喜欢(.+)")),
    ("like", True, "like_of_is", re.compile(r"用户喜欢的(.+?)是(.+)")),
    ("like", True, "like_pos", re.compile(r"用户喜欢(.+)")),
    ("live", False, "live_neg", re.compile(r"用户不住在\s*(.+)")),
    ("live", True, "live_pos", re.compile(r"用户住在\s*(.+)")),
    ("live", True, "live_home", re.compile(r"用户家在\s*(.+)")),
    ("know", False, "know_neg", re.compile(r"用户不认识\s*(.+)")),
    ("know", True, "know_pos", re.compile(r"用户认识\s*(.+)")),
    ("identity", False, "id_neg_is", re.compile(r"用户不是\s*(.+)")),
    ("identity", False, "id_neg_name", re.compile(r"用户不叫\s*(.+)")),
    ("identity", True, "id_name", re.compile(r"用户叫\s*(.+)")),
    ("identity", True, "id_fullname", re.compile(r"用户的名字是\s*(.+)")),
    ("identity", True, "id_is", re.compile(r"用户是\s*(.+)")),
    ("code", True, "code_test", re.compile(r"用户的?测试代号是\s*(.+)")),
    ("code", True, "code", re.compile(r"用户的?代号是\s*(.+)")),
)

_SLOT_RENDER_KEYS = frozenset({"like_of_is", "like_most_of_is"})


def try_strip_remember_prefix(text: str) -> Optional[str]:
    """
    若句首匹配白名单「记住」前缀，返回去除**一个**前缀及可选空白/冒号/逗号后的剩余。
    未匹配返回 ``None``（不得在句中搜索）。
    """
    if not isinstance(text, str) or not text:
        return None
    for prefix in _REMEMBER_PREFIXES:
        if text.startswith(prefix):
            rest = text[len(prefix) :]
            rest = _REMEMBER_PREFIX_GAP_RE.sub("", rest, count=1)
            return rest if rest else None
    return None


def _parse_single_unit(
    unit: str, patterns: Sequence[tuple[str, bool, str, re.Pattern[str]]]
) -> Optional[PredicateClaim]:
    """解析单个原子单元；无法完整解析为一条谓词时返回 None。"""
    clause = unit.strip()
    if not clause or clause_has_unknown_slot(clause):
        return None
    if _META_DENIAL_TAIL_RE.search(clause):
        return None
    for family, polarity, render_key, pattern in patterns:
        match = pattern.fullmatch(clause)
        if not match:
            continue
        if render_key in _SLOT_RENDER_KEYS:
            slot = _norm_value(match.group(1))
            value = _norm_value(match.group(2))
            slot_ok = bool(slot) and (
                slot in _ALLOWED_DOMAIN_SLOTS
                or (
                    slot not in _PLACEHOLDER_VALUES
                    and not is_placeholder_core(slot)
                )
            )
            if (
                not slot_ok
                or not value
                or value in _PLACEHOLDER_VALUES
                or is_placeholder_core(value)
            ):
                return None
            if _META_DENIAL_TAIL_RE.search(slot) or _META_DENIAL_TAIL_RE.search(value):
                return None
            return PredicateClaim(
                family=family,
                polarity=polarity,
                value=value,
                render_key=render_key,
                slot=slot,
            )
        value = _norm_value(match.group(1))
        if not value or value in _PLACEHOLDER_VALUES or is_placeholder_core(value):
            return None
        if _META_DENIAL_TAIL_RE.search(value):
            return None
        return PredicateClaim(
            family=family, polarity=polarity, value=value, render_key=render_key
        )
    return None


def _parse_single_unit_user(unit: str) -> Optional[PredicateClaim]:
    """用户侧单单元严格解析（供本地候选枚举）；可剥一层白名单「记住」前缀。"""
    claim = _parse_single_unit(unit, _USER_PREDICATE_PATTERNS)
    if claim is not None:
        return claim
    stripped = try_strip_remember_prefix(unit.strip())
    if stripped is None:
        return None
    return _parse_single_unit(stripped, _USER_PREDICATE_PATTERNS)


def _parse_predicates_complete(
    text: str, patterns: Sequence[tuple[str, bool, str, re.Pattern[str]]]
) -> Optional[list[PredicateClaim]]:
    """
    完整覆盖解析：每个事实单元都必须成功解析。

    返回 ``None`` 表示存在未解析单元（失败关闭）。
    """
    units = split_fact_units(text)
    if not units:
        return None
    claims: list[PredicateClaim] = []
    for unit in units:
        claim = _parse_single_unit(unit, patterns)
        if claim is None:
            return None
        claims.append(claim)
    return claims


def _parse_predicates_soft(
    text: str, patterns: Sequence[tuple[str, bool, str, re.Pattern[str]]]
) -> list[PredicateClaim]:
    """证据侧：收集能解析的单元；用于与单条原子候选对齐。"""
    claims: list[PredicateClaim] = []
    for unit in split_fact_units(text):
        claim = _parse_single_unit(unit, patterns)
        if claim is not None:
            claims.append(claim)
    return claims


def parse_user_predicates(evidence: str) -> list[PredicateClaim]:
    """兼容旧调用：软收集；证据授权请用 ``parse_user_predicates_strict``。"""
    return _parse_predicates_soft(evidence, _USER_PREDICATE_PATTERNS)


def parse_user_predicates_strict(evidence: str) -> Optional[list[PredicateClaim]]:
    """用户单元严格完整覆盖解析；任一未解析片段则 ``None``。"""
    return _parse_predicates_complete(evidence, _USER_PREDICATE_PATTERNS)


def parse_fact_predicates(fact_text: str) -> list[PredicateClaim]:
    """兼容旧调用：完整覆盖失败时返回空列表。"""
    parsed = _parse_predicates_complete(fact_text, _FACT_PREDICATE_PATTERNS)
    return list(parsed or ())


def parse_fact_predicates_strict(fact_text: str) -> Optional[list[PredicateClaim]]:
    """候选侧严格解析；不完整则 ``None``。"""
    return _parse_predicates_complete(fact_text, _FACT_PREDICATE_PATTERNS)


def _claim_supports_fact(user_claim: PredicateClaim, fact_claim: PredicateClaim) -> bool:
    if user_claim.family != fact_claim.family:
        return False
    if user_claim.polarity != fact_claim.polarity:
        return False
    if is_placeholder_core(user_claim.value):
        return False
    if not _values_compatible(fact_claim.value, user_claim.value):
        return False
    # 槽位句式：两侧都有 slot 时须兼容；一侧无 slot 仍可按 value 对齐
    if user_claim.slot and fact_claim.slot:
        if is_placeholder_core(user_claim.slot):
            return False
        return _values_compatible(fact_claim.slot, user_claim.slot)
    return True


def unit_strictly_supports_fact(unit: str, fact_claim: PredicateClaim) -> bool:
    """
    完整用户包围单元必须严格解析为**恰好一条**谓词，并与候选对齐。

    任何未解析前缀/后缀/条件/引用/否认残留 → 失败关闭。
    """
    claims = parse_user_predicates_strict(unit)
    if claims is None or len(claims) != 1:
        return False
    return _claim_supports_fact(claims[0], fact_claim)


def is_placeholder_core(core: str) -> bool:
    if not core or not core.strip():
        return True
    stripped = core.strip()
    if stripped in _PLACEHOLDER_VALUES:
        return True
    if clause_has_unknown_slot(stripped):
        return True
    # 仅标签词堆叠
    tokens = _value_tokens(stripped)
    if not tokens:
        return True
    if tokens <= _PLACEHOLDER_VALUES:
        return True
    return False


def strip_fact_framing(fact_text: str) -> str:
    """去掉「用户…」框架，得到需由证据支撑的核心值（兼容旧调用）。"""
    if not isinstance(fact_text, str):
        return ""
    claims = parse_fact_predicates(fact_text)
    if claims:
        return claims[0].value
    text = fact_text.strip()
    text = re.sub(r"^用户的?", "", text, count=1)
    return text.strip(" \t\r\n。．，,、；;：:")


def _value_tokens(text: str) -> frozenset[str]:
    tokens: set[str] = set()
    for match in _VALUE_LATIN_RE.finditer(text):
        word = match.group(0).casefold()
        if word not in _LABEL_STOP and len(word) >= 2:
            tokens.add(word)
    for match in _VALUE_CJK_RE.finditer(text):
        run = match.group(0)
        if run in _LABEL_STOP or run in _PLACEHOLDER_VALUES:
            continue
        # 单字中文（茶/猫/狗）与多字 run 均可作为非空证据
        if len(run) >= 1 and run not in _LABEL_STOP:
            tokens.add(run)
        if len(run) > 4:
            for size in (2, 3, 4):
                for i in range(0, len(run) - size + 1):
                    piece = run[i : i + size]
                    if piece not in _LABEL_STOP and piece not in _PLACEHOLDER_VALUES:
                        tokens.add(piece)
    return frozenset(tokens)


def _values_compatible(fact_value: str, evidence_value: str) -> bool:
    """
    值兼容：相等，或候选值完整出现在证据值中（证据可更具体）。

    禁止证据值仅为候选子串（候选更具体、新增限定）时判 True。
    """
    fv = _norm_value(fact_value)
    ev = _norm_value(evidence_value)
    if not fv or not ev:
        return False
    if fv.casefold() == ev.casefold():
        return True
    # 候选更概括：其值整段落在证据值内
    if fv.casefold() in ev.casefold() and len(fv) < len(ev):
        return True
    return False


def fact_supported_by_evidence(fact_text: str, evidence_quote: str) -> bool:
    """
    候选必须是**单条原子事实**，且由用户证据在谓词/极性/值上支持。

    多子句、并列追加、省略主语残留、未解析内容或极性相反 → 拒绝。
    注意：写库授权应走 ``validate_candidate_user_evidence``（完整包围单元严格解析）。
    """
    if not isinstance(fact_text, str) or not isinstance(evidence_quote, str):
        return False
    if not evidence_quote.strip() or is_interrogative_evidence(evidence_quote):
        return False

    fact_claims = parse_fact_predicates_strict(fact_text)
    if fact_claims is None:
        return False
    # 原子事实契约：每个数组项恰好一条谓词
    if len(fact_claims) != 1:
        return False
    fc = fact_claims[0]
    if is_placeholder_core(fc.value):
        return False

    user_claims = parse_user_predicates(evidence_quote)
    if not user_claims:
        return False
    for uc in user_claims:
        if _claim_supports_fact(uc, fc):
            return True
    return False


def validate_candidate_user_evidence(
    *,
    user_text: str,
    fact_text: str,
    evidence_quote: Any,
    assistant_text: Optional[str] = None,
) -> bool:
    """
    兼容入口：仅当 evidence_quote 恰好等于某本地安全单元原文，
    且 fact_text 等于该单元 claim 的本地渲染结果时为 True。

    写库授权不得依赖本函数；正式路径见 ``enumerate_safe_fact_units``。
    """
    del assistant_text
    from src.memory.fact_units import enumerate_safe_fact_units, render_claim

    if not isinstance(user_text, str) or not user_text:
        return False
    if not isinstance(evidence_quote, str) or not evidence_quote.strip():
        return False
    if not isinstance(fact_text, str) or not fact_text.strip():
        return False
    if evidence_quote not in user_text:
        return False
    for unit in enumerate_safe_fact_units(user_text):
        if unit.text != evidence_quote:
            continue
        if render_claim(unit.claim) == fact_text:
            return True
    return False
