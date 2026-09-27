"""
Ollama 生成调用的结构化结果。

用单一 ``status`` 区分成功、空回复与各类失败，避免多个可随意组合的布尔字段。
失败判定不依赖回复文本是否以「错误:」开头。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GenerateStatus(Enum):
    """一次 generate 调用的结局分类。"""

    SUCCESS = "success"
    EMPTY = "empty"
    CONNECTION_ERROR = "connection_error"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    RESPONSE_ERROR = "response_error"  # 解析失败、结构错误、非字符串正文等


@dataclass(frozen=True)
class GenerateResult:
    """
    生成结果。

    - ``SUCCESS``：``text`` 为非空 str；``error_detail`` 为 None。
    - ``EMPTY``：模型侧得到空字符串；``text`` / ``error_detail`` 均为 None。
    - 失败类：``error_detail`` 为说明字符串；``text`` 为 None。
    """

    status: GenerateStatus
    text: Optional[str] = None
    error_detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status is GenerateStatus.SUCCESS:
            if not isinstance(self.text, str) or not self.text:
                raise ValueError("SUCCESS requires non-empty str text")
            if self.error_detail is not None:
                raise ValueError("SUCCESS must not set error_detail")
        elif self.status is GenerateStatus.EMPTY:
            if self.text is not None or self.error_detail is not None:
                raise ValueError("EMPTY must not set text or error_detail")
        else:
            if self.text is not None:
                raise ValueError("failure statuses must not set text")
            if not isinstance(self.error_detail, str) or not self.error_detail:
                raise ValueError("failure statuses require non-empty error_detail")

    @classmethod
    def success(cls, text: str) -> "GenerateResult":
        return cls(status=GenerateStatus.SUCCESS, text=text)

    @classmethod
    def empty(cls) -> "GenerateResult":
        return cls(status=GenerateStatus.EMPTY)

    @classmethod
    def failure(
        cls, status: GenerateStatus, error_detail: str
    ) -> "GenerateResult":
        if status in (GenerateStatus.SUCCESS, GenerateStatus.EMPTY):
            raise ValueError(f"failure() cannot use status={status}")
        # 异常 str() 可能为空（如 Timeout()）；必须给出稳定非空兜底，避免 __post_init__ 再抛错
        detail = (error_detail or "").strip() or f"{status.value}"
        return cls(status=status, error_detail=detail)

    @property
    def is_success(self) -> bool:
        return self.status is GenerateStatus.SUCCESS

    @property
    def is_empty(self) -> bool:
        return self.status is GenerateStatus.EMPTY

    @property
    def is_failure(self) -> bool:
        return self.status not in (
            GenerateStatus.SUCCESS,
            GenerateStatus.EMPTY,
        )

    def to_legacy_string(self) -> str:
        """
        兼容 ``generate() -> str`` 的既有约定。

        - 成功：原文（可含「错误:」前缀的模型文本）
        - 空回复：``\"\"``
        - 连接/超时/HTTP：``错误: 无法连接到Ollama服务 ({detail})``
        - 响应解析/结构等：``错误: {detail}``
        """
        if self.status is GenerateStatus.SUCCESS:
            return self.text or ""
        if self.status is GenerateStatus.EMPTY:
            return ""
        detail = self.error_detail or ""
        if self.status in (
            GenerateStatus.CONNECTION_ERROR,
            GenerateStatus.TIMEOUT,
            GenerateStatus.HTTP_ERROR,
        ):
            return f"错误: 无法连接到Ollama服务 ({detail})"
        return f"错误: {detail}"
