"""
TTS 语言检测与朗读文本清理

在送入 edge-tts 前去除 emoji、颜文字等不适合朗读的内容，并按语言选择语音角色。
"""
import logging
import re

logger = logging.getLogger(__name__)

# Unicode emoji（勿使用跨 CJK 区段的大范围，否则会误删中文）
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

# Discord / 短代码式表情 :smile:
_EMOJI_SHORTCODE_RE = re.compile(r":[a-z0-9_+\-]{2,}:", re.IGNORECASE)

# 括号包裹的颜文字，如 (´･ω･`) （＾▽＾）
_KAOMOJI_PAREN_RE = re.compile(r"[（(][^（()）\n]{1,30}[）)]")

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _looks_like_kaomoji(segment: str) -> bool:
    """
    判断括号内片段是否更像颜文字而非正常语句。

    Args:
        segment: 含括号的完整匹配片段，如 ``(´･ω･`)``。

    Returns:
        若 CJK 占比过低、以符号为主则视为颜文字。
    """
    inner = segment.strip("()（）")
    if not inner or len(inner) > 30:
        return False
    cjk_count = len(_CJK_RE.findall(inner))
    # 几乎无正常文字、以符号/字母为主的括号内容视为颜文字
    return cjk_count / max(len(inner), 1) < 0.35


def sanitize_for_speech(text: str) -> str:
    """
    清理供 TTS 朗读的文本：去掉 emoji、颜文字、Markdown 星号等。

    屏幕显示仍用原始文本，不要对 display 结果调用此函数。

    Args:
        text: 原始待朗读文本。

    Returns:
        清理后的朗读文本；空输入原样返回。
    """
    if not text:
        return text

    cleaned = _EMOJI_RE.sub("", text)
    cleaned = _EMOJI_SHORTCODE_RE.sub("", cleaned)

    def _drop_kaomoji(match: re.Match) -> str:
        return "" if _looks_like_kaomoji(match.group(0)) else match.group(0)

    cleaned = _KAOMOJI_PAREN_RE.sub(_drop_kaomoji, cleaned)
    # Markdown 强调/列表符号，TTS 会读成「星号」
    cleaned = cleaned.replace("*", "").replace("＊", "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"([，。！？、；：])[ \t]+", r"\1", cleaned)
    return cleaned.strip()


# 分句：中英文句号、问号、叹号、分号、换行（lookbehind 必须定长，换行用 |\\n+ 直接切）
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;…])|\n+")


def split_for_speech(text: str, max_chars: int = 120) -> list[str]:
    """
    将长文本切成适合流水线朗读的短句。

    Args:
        text: 待分句文本。
        max_chars: 单句最大字符数，超长句会再按逗号切分。

    Returns:
        非空短句列表；空输入返回 ``[]``。
    """
    if not text:
        return []

    chunks: list[str] = []
    for part in _SENTENCE_SPLIT_RE.split(text):
        part = part.strip()
        if not part:
            continue
        if len(part) <= max_chars:
            chunks.append(part)
            continue
        # 过长句再按逗号切分
        for sub in re.split(r"(?<=[，,、])", part):
            sub = sub.strip()
            if sub:
                chunks.append(sub)

    return chunks if chunks else [text.strip()]


def detect_edge_voice(text: str, default_voice: str) -> str:
    """
    根据文本内容选择 edge-tts 语音角色。

    Args:
        text: 待朗读文本。
        default_voice: 默认语音 ID（如 ``zh-CN-XiaoxiaoNeural``）。

    Returns:
        匹配日/中/英内容的 Neural 语音 ID，无法判断时返回 ``default_voice``。
    """
    logger = logging.getLogger(__name__)

    if not text:
        return default_voice

    japanese_kana = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")
    chinese = re.compile(r"[\u4E00-\u9FFF]")
    english = re.compile(r"[a-zA-Z]")

    chinese_count = len(chinese.findall(text))
    japanese_count = len(japanese_kana.findall(text))
    english_count = len(english.findall(text))

    if japanese_kana.search(text) and japanese_count > chinese_count * 0.3:
        logger.debug("检测到日语文本")
        return "ja-JP-NanamiNeural"
    if chinese.search(text) and chinese_count > 0:
        logger.debug("检测到中文文本")
        return default_voice if "zh" in default_voice.lower() else "zh-CN-XiaoxiaoNeural"
    if english.search(text) and english_count > chinese_count:
        logger.debug("检测到英文文本")
        return default_voice if "en" in default_voice.lower() else "en-US-AriaNeural"

    return default_voice
