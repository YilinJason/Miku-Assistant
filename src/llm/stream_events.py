"""
Ollama 增量生成事件契约。

与 ``GenerateResult``（整段聚合）分离：本模块描述流式过程中的增量与终结事件。
不表示端到端实时对话或语音打断已接通。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.llm.generate_result import GenerateStatus


class StreamEventKind(Enum):
    """流式事件种类：增量文本 / 成功完成 / 失败。"""

    TEXT_DELTA = "text_delta"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class StreamEvent:
    """
    单次流式事件。

    - ``TEXT_DELTA``：``delta`` 为非空增量；非终结。
    - ``COMPLETED``：成功终结；``text`` 为本轮聚合全文（可为空）；``status`` 为
      ``SUCCESS`` 或 ``EMPTY``。
    - ``FAILED``：失败终结；``error_detail`` 非空；``status`` 为失败类
      ``GenerateStatus``。

    正常消费至结束时恰有一个终结事件（``COMPLETED`` 或 ``FAILED``）；
    终结后不得再产出事件。
    """

    kind: StreamEventKind
    delta: Optional[str] = None
    text: Optional[str] = None
    error_detail: Optional[str] = None
    status: Optional[GenerateStatus] = None

    def __post_init__(self) -> None:
        if self.kind is StreamEventKind.TEXT_DELTA:
            if not isinstance(self.delta, str) or not self.delta:
                raise ValueError("TEXT_DELTA requires non-empty delta")
            if self.text is not None or self.error_detail is not None:
                raise ValueError("TEXT_DELTA must not set text/error_detail")
            if self.status is not None:
                raise ValueError("TEXT_DELTA must not set status")
        elif self.kind is StreamEventKind.COMPLETED:
            if self.delta is not None or self.error_detail is not None:
                raise ValueError("COMPLETED must not set delta/error_detail")
            if not isinstance(self.text, str):
                raise ValueError("COMPLETED requires str text (may be empty)")
            if self.status not in (
                GenerateStatus.SUCCESS,
                GenerateStatus.EMPTY,
            ):
                raise ValueError("COMPLETED status must be SUCCESS or EMPTY")
            if self.status is GenerateStatus.SUCCESS and not self.text:
                raise ValueError("COMPLETED SUCCESS requires non-empty text")
            if self.status is GenerateStatus.EMPTY and self.text:
                raise ValueError("COMPLETED EMPTY requires empty text")
        elif self.kind is StreamEventKind.FAILED:
            if self.delta is not None or self.text is not None:
                raise ValueError("FAILED must not set delta/text")
            if not isinstance(self.error_detail, str) or not self.error_detail:
                raise ValueError("FAILED requires non-empty error_detail")
            if self.status is None or self.status in (
                GenerateStatus.SUCCESS,
                GenerateStatus.EMPTY,
            ):
                raise ValueError("FAILED requires a failure GenerateStatus")
        else:
            raise ValueError(f"unknown kind: {self.kind}")

    @property
    def is_terminal(self) -> bool:
        return self.kind in (StreamEventKind.COMPLETED, StreamEventKind.FAILED)

    @classmethod
    def text_delta(cls, delta: str) -> "StreamEvent":
        return cls(kind=StreamEventKind.TEXT_DELTA, delta=delta)

    @classmethod
    def completed(cls, text: str) -> "StreamEvent":
        if text:
            return cls(
                kind=StreamEventKind.COMPLETED,
                text=text,
                status=GenerateStatus.SUCCESS,
            )
        return cls(
            kind=StreamEventKind.COMPLETED,
            text="",
            status=GenerateStatus.EMPTY,
        )

    @classmethod
    def failed(
        cls, status: GenerateStatus, error_detail: str
    ) -> "StreamEvent":
        detail = (error_detail or "").strip() or status.value
        return cls(
            kind=StreamEventKind.FAILED,
            error_detail=detail,
            status=status,
        )
