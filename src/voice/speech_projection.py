"""
朗读投影：显示原文不变，仅把送入 TTS 的文本做本地确定性清理。

无 I/O、无线程、无网络、无日志正文、无额外模型调用。
增量 ``SpeechProjector`` 按轮隔离；跨 chunk 以 token kind + 扫描进度
只处理新增字符，避免对 hold 从头重扫导致 O(n²)。
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

# 单次 feed / 完整投影的输入硬上限（Unicode 字符）
MAX_SPEECH_PROJECTION_INPUT_CHARS = 16_000
# 增量 hold 缓冲硬上限：与总输入上限对齐，避免截前缀后重解析敏感尾部
MAX_SPEECH_HOLD_BUFFER_CHARS = MAX_SPEECH_PROJECTION_INPUT_CHARS
# 单段 URL/目标扫描消费上限（继续吞掉后续 URL 字符但不回吐）
MAX_URL_SCAN_CHARS = 16_384

LINK_TIP = "链接已显示。"
IMAGE_TIP = "图片已显示。"
CODE_TIP = "代码内容已显示。"

# 兼容旧名
MAX_SPEECH_LINE_BUFFER_CHARS = MAX_SPEECH_HOLD_BUFFER_CHARS

# 裸 URL 终止：空白 + 常见中文句读/成对结束符（不作用于 Markdown 目标扫描）
_BARE_URL_TERMINATORS = frozenset(
    " \t\n\r<>\"'"
    "，。！？；：、）】》」』"
)
_QUARANTINE_TERMINATORS = _BARE_URL_TERMINATORS | frozenset("\v\f")
# 固定提示尾部 ``。`` 与源句读合并（仅提示边界）
_TIP_FOLLOW_PUNCT = frozenset("。.!！?？,，;；:：")
_TIP_PRIORITY_PUNCT = frozenset("!?！？")

_EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)
_EMOJI_SHORTCODE_RE = re.compile(r":[a-z0-9_+\-]{2,}:", re.IGNORECASE)
_KAOMOJI_FACE_RE = re.compile(
    r"[´`ω＾▽ﾟ゜・♪☆★≧≦｀∀ヽノ╮╯╰ДдＴツゝゞ]|[\U0001F300-\U0001FAFF]"
)
_KAOMOJI_PAREN_RE = re.compile(r"[（(][^（()）\n]{1,30}[）)]")
_FENCE_LINE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_HEADER_RE = re.compile(r"^#{1,6}\s+")
_BLOCKQUOTE_RE = re.compile(r"^>\s?")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,3}\.)\s+")
_TABLE_SEP_RE = re.compile(
    r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$"
)
_BOLD_RE = re.compile(r"\*\*([^*]{1,512})\*\*|__([^_]{1,512})__")
_ITALIC_RE = re.compile(
    r"(?<!\*)\*([^*\n]{1,512})\*(?!\*)|(?<!_)_([^_\n]{1,512})_(?!_)"
)
_PARTIAL_FENCE_RE = re.compile(r"^( {0,3})(`{1,2}|~{1,2})$")

_TRAILING_URL_PUNCT = set(".,;:!?）)」』\"'")

StepFn = Optional[Callable[[int], None]]


def _bump(on_step: StepFn, n: int = 1) -> None:
    if on_step is not None and n:
        on_step(n)


def _looks_like_kaomoji(segment: str) -> bool:
    inner = segment.strip("()（）")
    if not inner or len(inner) > 30:
        return False
    return _KAOMOJI_FACE_RE.search(inner) is not None


def _drop_kaomoji(match: re.Match[str]) -> str:
    return "" if _looks_like_kaomoji(match.group(0)) else match.group(0)


def _collapse_ws(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[ \t]{2,}", " ", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"([，。！？、；：])[ \t]+", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return cleaned.strip()


def _merge_fixed_tip(tip: str, following: str) -> Tuple[str, str]:
    """
    固定提示以 ``。`` 结尾时，若紧接源句读则只保留一个自然句读。
    问号/叹号优先；普通句号不重复。不改动提示边界以外的正文。
    """
    if not tip.endswith("。") or not following:
        return tip, following
    ch = following[0]
    if ch not in _TIP_FOLLOW_PUNCT:
        return tip, following
    rest = following[1:]
    if ch in ".。":
        return tip, rest
    if ch in _TIP_PRIORITY_PUNCT:
        return tip[:-1] + ch, rest
    return tip[:-1] + ch, rest


def _emit_fixed_tip(tip: str, text: str, end: int) -> Tuple[str, int]:
    """在 ``text[end:]`` 边界合并提示句读；返回 ``(emitted, new_end)``。"""
    if end >= len(text):
        return tip, end
    merged, rest = _merge_fixed_tip(tip, text[end:])
    consumed = len(text) - end - len(rest)
    return merged, end + consumed


def _is_partial_fence_prefix(buf: str) -> bool:
    return _PARTIAL_FENCE_RE.match(buf) is not None


def _parse_fence(line: str) -> Optional[Tuple[str, int]]:
    match = _FENCE_LINE_RE.match(line)
    if not match:
        return None
    ticks = match.group(2)
    return ticks[0], len(ticks)


def _is_fence_close(line: str, char: str, min_len: int) -> bool:
    match = _FENCE_LINE_RE.match(line)
    if not match:
        return False
    ticks = match.group(2)
    if ticks[0] != char or len(ticks) < min_len:
        return False
    return match.group(3).strip() == ""


def _startswith_scheme(text: str, i: int) -> Optional[str]:
    """大小写不敏感地匹配 ``https://`` / ``http://``；返回实际切片（保长度）。"""
    for scheme in ("https://", "http://"):
        end = i + len(scheme)
        if end <= len(text) and text[i:end].lower() == scheme:
            return text[i:end]
    return None


def _is_url_body_char(ch: str) -> bool:
    if ch in _BARE_URL_TERMINATORS:
        return False
    o = ord(ch)
    if o < 0x20:
        return False
    return True


def _bare_url_break_at(text: str, i: int, *, finalize: bool = True) -> bool:
    """
    裸 URL 是否应在 ``text[i]`` 处终止（该字符不进入 URL）。
    ``,;`` 始终终止；``.`` / ``:`` / ``?`` / ``!`` 按后继区分 query/端口/跨 chunk。
    """
    ch = text[i]
    if ch in _BARE_URL_TERMINATORS:
        return True
    if ch in ",;":
        return True
    if ch == ".":
        if i + 1 >= len(text):
            return finalize
        nxt = text[i + 1]
        return not (nxt.isascii() and nxt.isalnum())
    if ch == ":":
        if i + 1 >= len(text):
            return finalize
        nxt = text[i + 1]
        if nxt.isdigit() or (nxt.isascii() and nxt.isalnum()):
            return False
        return True
    if ch == "?":
        if i + 1 >= len(text):
            return finalize
        nxt = text[i + 1]
        # query: ?q= / ?a&b ；句读: ?后文 / ?。
        if nxt.isascii() and (nxt.isalnum() or nxt in "=&_%-./~+"):
            return False
        return True
    if ch == "!":
        if i + 1 >= len(text):
            return finalize
        nxt = text[i + 1]
        if nxt.isascii() and nxt.isalnum():
            return False
        return True
    o = ord(ch)
    return o < 0x20


def _is_scheme_prefix(s: str) -> bool:
    if not s or len(s) > len("https://"):
        return False
    lower = s.lower()
    return "https://".startswith(lower) or "http://".startswith(lower)


def _scan_bare_url(
    text: str, start: int, *, finalize: bool, on_step: StepFn = None
) -> Tuple[Optional[str], int, bool]:
    """
    从 ``https?://`` 起扫描裸 URL。
    返回 ``(emitted_or_None, end_index, incomplete)``。
    """
    scheme = _startswith_scheme(text, start)
    if scheme is None:
        return None, start, False
    _bump(on_step, len(scheme))
    i = start + len(scheme)
    consumed = len(scheme)
    while i < len(text) and consumed < MAX_URL_SCAN_CHARS:
        _bump(on_step)
        if _bare_url_break_at(text, i, finalize=finalize):
            break
        i += 1
        consumed += 1
    while i < len(text) and not _bare_url_break_at(text, i, finalize=finalize):
        _bump(on_step)
        i += 1
    if i >= len(text) and not finalize:
        return None, start, True
    end = i
    while end > start + len(scheme) and text[end - 1] in _TRAILING_URL_PUNCT:
        end -= 1
    tip, end = _emit_fixed_tip(LINK_TIP, text, end)
    return tip, end, False

def _scan_optional_link_title(
    text: str, start: int, *, finalize: bool, on_step: StepFn = None
) -> Tuple[Optional[int], bool]:
    i = start
    while i < len(text) and text[i] in " \t":
        _bump(on_step)
        i += 1
    if i >= len(text):
        return (None, True) if not finalize else (start, False)
    opener = text[i]
    if opener == '"':
        closer = '"'
    elif opener == "'":
        closer = "'"
    elif opener == "(":
        closer = ")"
    else:
        return start, False
    _bump(on_step)
    i += 1
    scanned = 0
    while i < len(text) and scanned < MAX_URL_SCAN_CHARS:
        _bump(on_step)
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            i += 2
            scanned += 2
            _bump(on_step)
            continue
        if ch == closer:
            return i + 1, False
        if ch == "\n":
            return None, False
        i += 1
        scanned += 1
    if i >= len(text) and not finalize:
        return None, True
    return None, False


def _scan_balanced_dest(
    text: str, start: int, *, finalize: bool, on_step: StepFn = None
) -> Tuple[Optional[int], bool]:
    if start >= len(text):
        return (None, not finalize)
    i = start
    if text[i] == "<":
        _bump(on_step)
        j = i + 1
        scanned = 0
        while j < len(text) and scanned < MAX_URL_SCAN_CHARS:
            _bump(on_step)
            if text[j] == "\\" and j + 1 < len(text):
                j += 2
                scanned += 2
                _bump(on_step)
                continue
            if text[j] == ">":
                break
            if text[j] in "\n\r":
                return None, False
            j += 1
            scanned += 1
        if j >= len(text) or text[j] != ">":
            return (None, not finalize) if not finalize else (None, False)
        _bump(on_step)
        i = j + 1
        title_end, incomplete = _scan_optional_link_title(
            text, i, finalize=finalize, on_step=on_step
        )
        if incomplete:
            return None, True
        if title_end is None:
            return None, False
        i = title_end
        while i < len(text) and text[i] in " \t":
            _bump(on_step)
            i += 1
        if i >= len(text):
            return (None, not finalize)
        if text[i] != ")":
            return None, False
        _bump(on_step)
        return i + 1, False

    depth = 1
    scanned = 0
    in_double = False
    in_single = False
    while i < len(text) and depth > 0 and scanned < MAX_URL_SCAN_CHARS:
        _bump(on_step)
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            i += 2
            scanned += 2
            _bump(on_step)
            continue
        if in_double:
            if ch == '"':
                in_double = False
            i += 1
            scanned += 1
            continue
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            scanned += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            scanned += 1
            continue
        if ch == "'":
            in_single = True
            i += 1
            scanned += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                i += 1
                scanned += 1
                break
        i += 1
        scanned += 1
    while i < len(text) and depth > 0:
        _bump(on_step)
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            i += 2
            _bump(on_step)
            continue
        if in_double:
            if ch == '"':
                in_double = False
            i += 1
            continue
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == '"':
            in_double = True
        elif ch == "'":
            in_single = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    if depth != 0:
        return (None, not finalize) if not finalize else (None, False)
    return i, False


def _find_md_label(
    text: str, open_idx: int, *, on_step: StepFn = None
) -> Optional[Tuple[str, int]]:
    if open_idx >= len(text) or text[open_idx] != "[":
        return None
    j = open_idx + 1
    while j < len(text) and j - open_idx < 512:
        _bump(on_step)
        if text[j] == "]" and (j == open_idx + 1 or text[j - 1] != "\\"):
            return text[open_idx + 1 : j], j + 1
        if text[j] == "\n":
            return None
        j += 1
    return None


def _try_markdown_link(
    text: str, i: int, *, finalize: bool, image: bool, on_step: StepFn = None
) -> Tuple[Optional[str], int, bool]:
    if image:
        if not text.startswith("![", i):
            return None, i, False
        open_bracket = i + 1
    else:
        if text.startswith("![", i):
            return None, i, False
        if text[i] != "[":
            return None, i, False
        open_bracket = i

    found = _find_md_label(text, open_bracket, on_step=on_step)
    if found is None:
        if finalize:
            return None, i, False
        return None, i, True
    label, after_label = found
    if after_label >= len(text):
        if finalize:
            return None, i, False
        return None, i, True
    if text[after_label] != "(":
        return None, i, False
    _bump(on_step)
    dest_end, incomplete = _scan_balanced_dest(
        text, after_label + 1, finalize=finalize, on_step=on_step
    )
    if incomplete:
        return None, i, True
    if dest_end is None:
        if finalize:
            if image:
                tip, after_label = _emit_fixed_tip(IMAGE_TIP, text, after_label)
                return tip, after_label, False
            return label, after_label, False
        return None, i, True
    if image:
        tip, dest_end = _emit_fixed_tip(IMAGE_TIP, text, dest_end)
        return tip, dest_end, False
    return label, dest_end, False


def _try_inline_code(
    text: str, i: int, *, finalize: bool, on_step: StepFn = None
) -> Tuple[Optional[str], int, bool]:
    if text[i] != "`":
        return None, i, False
    j = i + 1
    while j < len(text) and j - i <= 256:
        _bump(on_step)
        if text[j] == "`":
            return text[i + 1 : j], j + 1, False
        if text[j] == "\n":
            break
        j += 1
    if finalize:
        _bump(on_step, max(0, len(text) - i))
        return "", len(text), False
    return None, i, True


def _decorate_plain(segment: str) -> str:
    if not segment:
        return ""
    cleaned = _BOLD_RE.sub(
        lambda m: m.group(1) if m.group(1) is not None else m.group(2), segment
    )
    cleaned = _ITALIC_RE.sub(
        lambda m: m.group(1) if m.group(1) is not None else m.group(2), cleaned
    )
    cleaned = cleaned.replace("*", "").replace("＊", "")
    cleaned = _EMOJI_RE.sub("", cleaned)
    cleaned = _EMOJI_SHORTCODE_RE.sub("", cleaned)
    cleaned = _KAOMOJI_PAREN_RE.sub(_drop_kaomoji, cleaned)
    return cleaned


def _strip_line_markup(line: str) -> str:
    if _TABLE_SEP_RE.match(line):
        return ""
    body = line
    body = _HEADER_RE.sub("", body)
    body = _BLOCKQUOTE_RE.sub("", body)
    body = _LIST_RE.sub(r"\1", body)
    if "|" in body and body.count("|") >= 2:
        cells = [c.strip() for c in body.strip().strip("|").split("|")]
        body = "，".join(c for c in cells if c)
    return body


def _project_inline(
    text: str, *, finalize: bool, on_step: StepFn = None
) -> Tuple[str, str, Optional[str], int]:
    """
    线性扫描投影。返回 ``(spoken, hold_suffix, token_kind, progress)``。
    ``finalize=True`` 时 hold 为空；未完成时 ``token_kind`` 标明增量续扫种类。
    """
    if not text:
        return "", "", None, 0
    out: List[str] = []
    i = 0
    plain_start = 0

    def flush_plain(upto: int) -> None:
        nonlocal plain_start
        if upto > plain_start:
            out.append(_decorate_plain(text[plain_start:upto]))
        plain_start = upto

    n = len(text)
    while i < n:
        _bump(on_step)
        if text[i] in "(（":
            km = _KAOMOJI_PAREN_RE.match(text, i)
            if km is not None and _looks_like_kaomoji(km.group(0)):
                flush_plain(i)
                i = km.end()
                plain_start = i
                continue
        if not finalize and text[i] == "!":
            rest = text[i:]
            if rest == "!" or rest == "![" or (
                rest.startswith("![") and "]" not in rest[2:]
            ):
                flush_plain(i)
                return (
                    _collapse_ws("".join(out)),
                    text[i:],
                    "bang",
                    len(text) - i,
                )
        if text.startswith("![", i):
            emitted, end, incomplete = _try_markdown_link(
                text, i, finalize=finalize, image=True, on_step=on_step
            )
            if incomplete:
                flush_plain(i)
                return (
                    _collapse_ws("".join(out)),
                    text[i:],
                    "md_image",
                    0,
                )
            if emitted is not None:
                flush_plain(i)
                out.append(emitted)
                i = end
                plain_start = i
                continue
        if text[i] == "[" and not (i > 0 and text[i - 1] == "!"):
            emitted, end, incomplete = _try_markdown_link(
                text, i, finalize=finalize, image=False, on_step=on_step
            )
            if incomplete:
                flush_plain(i)
                return (
                    _collapse_ws("".join(out)),
                    text[i:],
                    "md_link",
                    0,
                )
            if emitted is not None:
                flush_plain(i)
                out.append(emitted)
                i = end
                plain_start = i
                continue
        scheme = _startswith_scheme(text, i)
        if scheme is not None:
            emitted, end, incomplete = _scan_bare_url(
                text, i, finalize=finalize, on_step=on_step
            )
            if incomplete:
                flush_plain(i)
                return (
                    _collapse_ws("".join(out)),
                    text[i:],
                    "bare_url",
                    len(text) - i,
                )
            if emitted is not None:
                flush_plain(i)
                out.append(emitted)
                i = end
                plain_start = i
                continue
        if not finalize and _is_scheme_prefix(text[i:]):
            if i == 0 or text[i - 1] in " \t\n\r([（\"'":
                flush_plain(i)
                return (
                    _collapse_ws("".join(out)),
                    text[i:],
                    "scheme_prefix",
                    len(text) - i,
                )
        if text[i] == "`":
            emitted, end, incomplete = _try_inline_code(
                text, i, finalize=finalize, on_step=on_step
            )
            if incomplete:
                flush_plain(i)
                return (
                    _collapse_ws("".join(out)),
                    text[i:],
                    "inline_code",
                    len(text) - i,
                )
            if emitted is not None:
                flush_plain(i)
                out.append(emitted)
                i = end
                plain_start = i
                continue
        i += 1

    flush_plain(n)
    return _collapse_ws("".join(out)), "", None, 0


def _project_line_outside_fence(
    line: str, *, finalize: bool, on_step: StepFn = None
) -> Tuple[str, str, Optional[str], int]:
    body = _strip_line_markup(line)
    if not body and line and _TABLE_SEP_RE.match(line):
        return "", "", None, 0
    spoken, hold, kind, progress = _project_inline(
        body, finalize=finalize, on_step=on_step
    )
    if spoken and not hold:
        spoken = spoken + " "
    return spoken, hold, kind, progress


def project_for_speech(text: str) -> str:
    """完整文本朗读投影（幂等）。"""
    if not text:
        return ""
    projector = SpeechProjector()
    spoken = projector.feed(text)
    spoken += projector.finish()
    return _collapse_ws(spoken)


class SpeechProjector:
    """
    每生成轮独占的增量投影器。

    跨 chunk 保持：围栏状态、token kind、已扫描进度；续扫只处理新增字符。
    hold 溢出时进入 quarantine，整段丢弃直至可信终止符，绝不截前缀重解析。
    """

    def __init__(
        self,
        *,
        max_input_chars: int = MAX_SPEECH_PROJECTION_INPUT_CHARS,
        max_line_buffer: int = MAX_SPEECH_HOLD_BUFFER_CHARS,
    ) -> None:
        if max_input_chars < 1:
            raise ValueError("max_input_chars must be >= 1")
        if max_line_buffer < 1:
            raise ValueError("max_line_buffer must be >= 1")
        self._max_input = max_input_chars
        self._max_hold = max_line_buffer
        self._seen = 0
        self._hold = ""
        self._in_fence = False
        self._fence_char = ""
        self._fence_len = 0
        self._tip_emitted = False
        self._quarantine = False
        self._tok_kind: Optional[str] = None
        self._tok_progress = 0
        # Markdown dest 续扫状态
        self._md_phase = ""  # "", "label", "dest", "angle", "title"
        self._md_label = ""
        self._md_depth = 0
        self._md_in_double = False
        self._md_in_single = False
        self._md_title_closer = ""
        self._md_after_label = 0
        self.scan_steps = 0
        self._tip_tail = False

    def _on_step(self, n: int = 1) -> None:
        self.scan_steps += n

    def _clear_token(self) -> None:
        self._tok_kind = None
        self._tok_progress = 0
        self._md_phase = ""
        self._md_label = ""
        self._md_depth = 0
        self._md_in_double = False
        self._md_in_single = False
        self._md_title_closer = ""
        self._md_after_label = 0

    def _drain_quarantine(self, chunk: str) -> Tuple[str, bool]:
        for idx, ch in enumerate(chunk):
            self._on_step()
            if ch in _QUARANTINE_TERMINATORS:
                self._quarantine = False
                return chunk[idx:], True
        return "", False

    def _enter_quarantine(self) -> None:
        self._hold = ""
        self._quarantine = True
        self._clear_token()

    def _continue_bare_url(self) -> str:
        """已确认裸 URL：只扫描 hold[progress:] 的新增字符。"""
        hold = self._hold
        progress = self._tok_progress
        if progress < 0:
            progress = 0
        if progress > len(hold):
            progress = len(hold)
        i = progress
        while i < len(hold):
            self._on_step()
            if _bare_url_break_at(hold, i, finalize=False):
                end = i
                scheme = _startswith_scheme(hold, 0)
                scheme_len = len(scheme) if scheme else 0
                while end > scheme_len and hold[end - 1] in _TRAILING_URL_PUNCT:
                    end -= 1
                tip, end = _emit_fixed_tip(LINK_TIP, hold, end)
                self._clear_token()
                self._hold = hold[end:]
                more = self._soft_project_hold()
                return _collapse_ws(tip + more)
            i += 1
        self._tok_progress = len(hold)
        return ""
    def _continue_scheme_prefix(self) -> str:
        hold = self._hold
        self._on_step(max(0, len(hold) - self._tok_progress))
        if _startswith_scheme(hold, 0) is not None:
            scheme = _startswith_scheme(hold, 0) or ""
            self._tok_kind = "bare_url"
            self._tok_progress = len(scheme)
            return self._continue_bare_url()
        if _is_scheme_prefix(hold):
            self._tok_progress = len(hold)
            return ""
        self._clear_token()
        return self._soft_project_hold()

    def _continue_bang(self) -> str:
        hold = self._hold
        self._on_step(max(0, len(hold) - self._tok_progress))
        if hold == "!" or hold == "![" or (
            hold.startswith("![") and "]" not in hold[2:]
        ):
            self._tok_progress = len(hold)
            return ""
        if hold.startswith("!["):
            self._tok_kind = "md_image"
            self._md_phase = "label"
            self._tok_progress = 2
            self._md_label = ""
            return self._continue_md()
        self._clear_token()
        return self._soft_project_hold()

    def _continue_inline_code(self) -> str:
        hold = self._hold
        progress = min(self._tok_progress, len(hold))
        if progress < 1:
            progress = 1
        i = progress
        while i < len(hold) and i <= 256:
            self._on_step()
            if hold[i] == "`":
                inner = hold[1:i]
                self._clear_token()
                self._hold = hold[i + 1 :]
                more = self._soft_project_hold()
                return _collapse_ws(inner + more)
            if hold[i] == "\n":
                break
            i += 1
        if len(hold) > 256:
            self._clear_token()
            self._hold = ""
            return ""
        self._tok_progress = len(hold)
        return ""

    def _continue_md(self) -> str:
        """Markdown 增量：label / dest / angle / title 只推进新增字符。"""
        hold = self._hold
        image = self._tok_kind == "md_image"
        i = self._tok_progress

        if self._md_phase in ("", "label"):
            if self._md_phase == "":
                # 刚进入：定位 '['
                if image:
                    if not hold.startswith("!["):
                        self._clear_token()
                        return self._soft_project_hold()
                    i = 2
                else:
                    if not hold or hold[0] != "[":
                        self._clear_token()
                        return self._soft_project_hold()
                    i = 1
                self._md_phase = "label"
                self._md_label = ""
            # 扫 label
            while i < len(hold) and i < 512:
                self._on_step()
                ch = hold[i]
                if ch == "]" and (i == 0 or hold[i - 1] != "\\"):
                    self._md_label = hold[
                        (2 if image else 1) : i
                    ]
                    i += 1
                    self._md_after_label = i
                    self._md_phase = "await_paren"
                    break
                if ch == "\n":
                    self._clear_token()
                    self._hold = hold
                    # 失败：当普通文本
                    spoken, h, k, p = _project_inline(
                        hold, finalize=False, on_step=self._on_step
                    )
                    self._hold = h
                    self._tok_kind = k
                    self._tok_progress = p
                    return spoken
                i += 1
            else:
                self._tok_progress = i
                return ""

        if self._md_phase == "await_paren":
            if i >= len(hold):
                self._tok_progress = i
                return ""
            self._on_step()
            if hold[i] != "(":
                # 非链接
                self._clear_token()
                # 输出已读 '[' label ']' 作为正文继续
                spoken, h, k, p = _project_inline(
                    hold, finalize=False, on_step=self._on_step
                )
                self._hold = h
                self._tok_kind = k
                self._tok_progress = p
                return spoken
            i += 1
            self._md_phase = "dest"
            self._md_depth = 1
            self._md_in_double = False
            self._md_in_single = False

        if self._md_phase == "dest":
            if i < len(hold) and hold[i] == "<" and self._md_depth == 1 and not (
                self._md_in_double or self._md_in_single
            ):
                # 转入 angle
                self._on_step()
                i += 1
                self._md_phase = "angle"
            else:
                while i < len(hold) and self._md_depth > 0:
                    self._on_step()
                    ch = hold[i]
                    if ch == "\\" and i + 1 < len(hold):
                        i += 2
                        self._on_step()
                        continue
                    if self._md_in_double:
                        if ch == '"':
                            self._md_in_double = False
                        i += 1
                        continue
                    if self._md_in_single:
                        if ch == "'":
                            self._md_in_single = False
                        i += 1
                        continue
                    if ch == '"':
                        self._md_in_double = True
                        i += 1
                        continue
                    if ch == "'":
                        self._md_in_single = True
                        i += 1
                        continue
                    if ch == "(":
                        self._md_depth += 1
                    elif ch == ")":
                        self._md_depth -= 1
                        if self._md_depth == 0:
                            i += 1
                            tip = IMAGE_TIP if image else self._md_label
                            if image:
                                tip, i = _emit_fixed_tip(IMAGE_TIP, hold, i)
                            self._clear_token()
                            self._hold = hold[i:]
                            more = self._soft_project_hold()
                            return _collapse_ws(tip + more)
                    i += 1
                self._tok_progress = i
                return ""

        if self._md_phase == "angle":
            while i < len(hold):
                self._on_step()
                ch = hold[i]
                if ch == "\\" and i + 1 < len(hold):
                    i += 2
                    self._on_step()
                    continue
                if ch == ">":
                    i += 1
                    self._md_phase = "angle_after"
                    break
                if ch in "\n\r":
                    self._clear_token()
                    return ""
                i += 1
            else:
                self._tok_progress = i
                return ""

        if self._md_phase == "angle_after":
            # optional title + ')'
            while i < len(hold) and hold[i] in " \t":
                self._on_step()
                i += 1
            if i >= len(hold):
                self._tok_progress = i
                return ""
            ch = hold[i]
            if ch in "\"'(":
                self._md_title_closer = {"\"": "\"", "'": "'", "(": ")"}[ch]
                self._on_step()
                i += 1
                self._md_phase = "title"
            elif ch == ")":
                self._on_step()
                i += 1
                tip = IMAGE_TIP if image else self._md_label
                if image:
                    tip, i = _emit_fixed_tip(IMAGE_TIP, hold, i)
                self._clear_token()
                self._hold = hold[i:]
                more = self._soft_project_hold()
                return _collapse_ws(tip + more)
            else:
                # malformed
                self._clear_token()
                tip = IMAGE_TIP if image else self._md_label
                self._hold = hold[self._md_after_label :]
                more = self._soft_project_hold()
                return _collapse_ws(tip + more)

        if self._md_phase == "title":
            closer = self._md_title_closer
            while i < len(hold):
                self._on_step()
                ch = hold[i]
                if ch == "\\" and i + 1 < len(hold):
                    i += 2
                    self._on_step()
                    continue
                if ch == closer:
                    i += 1
                    self._md_phase = "close_paren"
                    break
                if ch == "\n":
                    self._clear_token()
                    return ""
                i += 1
            else:
                self._tok_progress = i
                return ""

        if self._md_phase == "close_paren":
            while i < len(hold) and hold[i] in " \t":
                self._on_step()
                i += 1
            if i >= len(hold):
                self._tok_progress = i
                return ""
            self._on_step()
            if hold[i] != ")":
                self._clear_token()
                tip = IMAGE_TIP if image else self._md_label
                self._hold = hold[self._md_after_label :]
                more = self._soft_project_hold()
                return _collapse_ws(tip + more)
            i += 1
            tip = IMAGE_TIP if image else self._md_label
            if image:
                tip, i = _emit_fixed_tip(IMAGE_TIP, hold, i)
            self._clear_token()
            self._hold = hold[i:]
            more = self._soft_project_hold()
            return _collapse_ws(tip + more)

        self._tok_progress = i
        return ""

    def _soft_project_hold(self) -> str:
        if not self._hold:
            self._clear_token()
            return ""
        if self._tok_kind == "bare_url":
            return self._continue_bare_url()
        if self._tok_kind == "scheme_prefix":
            return self._continue_scheme_prefix()
        if self._tok_kind == "bang":
            return self._continue_bang()
        if self._tok_kind == "inline_code":
            return self._continue_inline_code()
        if self._tok_kind in ("md_link", "md_image"):
            return self._continue_md()

        spoken, hold, kind, progress = _project_line_outside_fence(
            self._hold, finalize=False, on_step=self._on_step
        )
        self._hold = hold
        self._tok_kind = kind
        self._tok_progress = progress
        if kind in ("md_link", "md_image"):
            # 转入增量 md 状态机；progress=0 表示从未扫
            self._md_phase = ""
            self._tok_progress = 0
            if hold:
                return spoken + self._continue_md()
        return spoken

    def _push(self, pieces: List[str], text: str, *, is_tip: bool = False) -> None:
        if not text:
            return
        if self._tip_tail and pieces and text[0] in _TIP_FOLLOW_PUNCT:
            merged, rest = _merge_fixed_tip(pieces[-1], text)
            pieces[-1] = merged
            text = rest
            self._tip_tail = False
        if not text:
            return
        pieces.append(text)
        self._tip_tail = is_tip

    def feed(self, chunk: str) -> str:

        if not chunk:
            return ""
        remaining = self._max_input - self._seen
        if remaining <= 0:
            return ""
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        self._seen += len(chunk)

        pieces: List[str] = []

        if self._quarantine:
            chunk, tip = self._drain_quarantine(chunk)
            if tip:
                tip_text, chunk = _merge_fixed_tip(LINK_TIP, chunk)
                self._push(pieces, tip_text, is_tip=not chunk)
            if not chunk:
                return _collapse_ws("".join(pieces)) if pieces else ""

        self._hold += chunk
        while True:
            nl = self._hold.find("\n")
            if nl < 0:
                break
            line = self._hold[:nl]
            self._hold = self._hold[nl + 1 :]
            self._clear_token()
            piece = self._handle_line(line, finalize=True)
            if piece:
                self._push(
                    pieces, piece, is_tip=(piece in (LINK_TIP, IMAGE_TIP, CODE_TIP))
                )

        if self._hold and not self._in_fence:
            if self._tok_kind is None and _is_partial_fence_prefix(self._hold):
                pass
            elif self._tok_kind is None and _parse_fence(self._hold) is not None:
                piece = self._handle_line(self._hold, finalize=True)
                self._hold = ""
                self._clear_token()
                if piece:
                    self._push(
                        pieces,
                        piece,
                        is_tip=(piece in (LINK_TIP, IMAGE_TIP, CODE_TIP)),
                    )
            else:
                spoken = self._soft_project_hold()
                if spoken:
                    self._push(pieces, spoken, is_tip=False)
        elif self._hold and self._in_fence:
            pass

        if len(self._hold) > self._max_hold:
            self._enter_quarantine()

        return _collapse_ws("".join(pieces)) if pieces else ""

    def finish(self) -> str:
        pieces: List[str] = []
        if self._quarantine:
            self._quarantine = False
            self._push(pieces, LINK_TIP, is_tip=True)
            self._hold = ""
            self._clear_token()
        elif self._hold:
            if self._in_fence:
                self._hold = ""
                self._clear_token()
            else:
                kind = self._tok_kind
                if kind == "bare_url":
                    self._push(pieces, LINK_TIP, is_tip=True)
                    self._hold = ""
                    self._clear_token()
                elif kind == "inline_code":
                    self._hold = ""
                    self._clear_token()
                elif kind in ("md_link", "md_image"):
                    hold = self._hold
                    image = kind == "md_image"
                    emitted, end, _incomplete = _try_markdown_link(
                        hold, 0, finalize=True, image=image, on_step=self._on_step
                    )
                    self._clear_token()
                    self._hold = ""
                    if emitted is not None:
                        self._push(
                            pieces,
                            emitted,
                            is_tip=(emitted in (LINK_TIP, IMAGE_TIP, CODE_TIP)),
                        )
                        if end < len(hold):
                            spoken, _, _, _ = _project_inline(
                                hold[end:], finalize=True, on_step=self._on_step
                            )
                            if spoken:
                                self._push(pieces, spoken, is_tip=False)
                    else:
                        spoken, _, _, _ = _project_inline(
                            hold, finalize=True, on_step=self._on_step
                        )
                        if spoken:
                            self._push(pieces, spoken, is_tip=False)
                elif kind in ("scheme_prefix", "bang"):
                    spoken, _, _, _ = _project_inline(
                        self._hold, finalize=True, on_step=self._on_step
                    )
                    self._hold = ""
                    self._clear_token()
                    if spoken:
                        self._push(pieces, spoken, is_tip=False)
                else:
                    piece = self._handle_line(self._hold, finalize=True)
                    self._hold = ""
                    self._clear_token()
                    if piece:
                        self._push(
                            pieces,
                            piece,
                            is_tip=(piece in (LINK_TIP, IMAGE_TIP, CODE_TIP)),
                        )
        self._in_fence = False
        self._fence_char = ""
        self._fence_len = 0
        self._tip_emitted = False
        self._tip_tail = False
        self._clear_token()
        return _collapse_ws("".join(pieces)) if pieces else ""

    def _handle_line(self, line: str, *, finalize: bool) -> str:
        if self._in_fence:
            if _is_fence_close(line, self._fence_char, self._fence_len):
                self._in_fence = False
                self._fence_char = ""
                self._fence_len = 0
                self._tip_emitted = False
                return ""
            return ""

        fence = _parse_fence(line)
        if fence is not None:
            char, length = fence
            self._in_fence = True
            self._fence_char = char
            self._fence_len = length
            if not self._tip_emitted:
                self._tip_emitted = True
                return CODE_TIP
            return ""

        spoken, hold, kind, progress = _project_line_outside_fence(
            line, finalize=finalize, on_step=self._on_step
        )
        if hold and not finalize:
            self._hold = hold + self._hold
            self._tok_kind = kind
            self._tok_progress = progress
            if kind in ("md_link", "md_image"):
                self._md_phase = ""
                self._tok_progress = 0
            return spoken
        return spoken
