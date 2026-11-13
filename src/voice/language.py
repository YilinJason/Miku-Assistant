"""
TTS 语言检测与朗读文本清理

在送入 edge-tts 前对文本做朗读投影（去 emoji、Markdown 噪声、URL 等）。
屏幕显示仍用原始文本，不要对 display 结果调用投影。
"""
import logging
import re

from src.voice.speech_projection import project_for_speech

logger = logging.getLogger(__name__)


def sanitize_for_speech(text: str) -> str:
    """
    清理供 TTS 朗读的文本（委托 ``project_for_speech``，幂等）。

    屏幕显示仍用原始文本，不要对 display 结果调用此函数。
    """
    return project_for_speech(text)


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
