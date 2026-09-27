"""
意图分发结果：展示文本与副作用许可。

由路由器 / 助手消费；系统与搜索处理器经字符串适配保持原副作用语义，
LLM 路径由 ``GenerateResult`` 映射，不以「错误:」前缀或 ``allow_tts`` 猜测生成成败。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.llm.generate_result import GenerateResult
from src.core.speech_queue import PlaybackStatus


class ReplyStage(str, Enum):
    """
    展示/阶段语义（与 TTS 许可独立）。

    CLI 等调用方应读 ``stage``，不得用 ``allow_tts`` 推断生成是否失败。
    """

    OK = "ok"
    GENERATION_FAILED = "generation_failed"
    CANCELLED = "cancelled"
    PLAYBACK_FAILED = "playback_failed"


@dataclass(frozen=True)
class DispatchOutcome:
    """
    一次已处理意图的结果。

    - ``text``：可展示给用户的字符串（可为空）。
    - ``allow_tts`` / ``allow_character``：是否允许播报与角色转发。
    - ``stage``：生成/取消/播放阶段（展示用；默认 ``OK`` 保持旧构造兼容）。
    - ``playback_status``：可选播报等待结果；三参数构造仍兼容。
    - ``safe_to_resume_listen``：串行语音模式的播放静默证明。仅当本轮播报
      ``COMPLETED``，或可证明从未开始/无需播报时为 True。全双工模式持续监听并
      使用独立输入门控，不用此字段开关麦克风；不得据此声称硬打断底层音频。
    """

    text: str
    allow_tts: bool
    allow_character: bool
    stage: ReplyStage = ReplyStage.OK
    playback_status: Optional[PlaybackStatus] = None
    safe_to_resume_listen: bool = True

    @classmethod
    def from_legacy_handler_text(cls, text: str) -> "DispatchOutcome":
        """
        包装系统/搜索等仍返回 ``str`` 的处理器结果。

        本轮不重新分类其执行结果：一律允许 TTS 与角色转发（与改造前一致）。
        """
        return cls(
            text=text,
            allow_tts=True,
            allow_character=True,
            stage=ReplyStage.OK,
        )

    @classmethod
    def from_generate_result(cls, result: GenerateResult) -> "DispatchOutcome":
        """由结构化模型结果映射展示与副作用许可。"""
        if result.is_success:
            return cls(
                text=result.text or "",
                allow_tts=True,
                allow_character=True,
                stage=ReplyStage.OK,
            )
        if result.is_empty:
            return cls(
                text="",
                allow_tts=False,
                allow_character=False,
                stage=ReplyStage.GENERATION_FAILED,
            )
        return cls(
            text=result.to_legacy_string(),
            allow_tts=False,
            allow_character=False,
            stage=ReplyStage.GENERATION_FAILED,
        )

    @classmethod
    def module_unavailable(cls, message: str) -> "DispatchOutcome":
        """模块未就绪等本地提示：可展示，不播报、不转发角色。"""
        return cls(
            text=message,
            allow_tts=False,
            allow_character=False,
            stage=ReplyStage.GENERATION_FAILED,
        )

    @classmethod
    def cancelled(
        cls,
        message: str = "本轮已取消",
        *,
        safe_to_resume_listen: bool = True,
    ) -> "DispatchOutcome":
        """用户/轮次取消：不播报、不转发。未开始播报时可安全恢复监听。"""
        return cls(
            text=message,
            allow_tts=False,
            allow_character=False,
            stage=ReplyStage.CANCELLED,
            safe_to_resume_listen=safe_to_resume_listen,
        )

    @classmethod
    def playback_failed(
        cls,
        text: str,
        *,
        playback_status: Optional[PlaybackStatus] = PlaybackStatus.FAILED,
        safe_to_resume_listen: bool = False,
    ) -> "DispatchOutcome":
        """
        生成已成功（可能已提交历史），播放阶段失败/超时。

        ``text`` 为已生成全文；展示层不得再当「生成失败」重复打印全文。
        默认不可恢复监听：底层 ``speak`` 可能仍在运行。
        """
        return cls(
            text=text,
            allow_tts=False,
            allow_character=False,
            stage=ReplyStage.PLAYBACK_FAILED,
            playback_status=playback_status,
            safe_to_resume_listen=safe_to_resume_listen,
        )
