"""语音识别跨模块结果与 Provider 契约。

本模块只定义数据和窄接口；导入时不得初始化模型、麦克风或后台线程。
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


class RecognitionStatus(str, Enum):
    """一次语音识别尝试的明确终态。"""

    SUCCESS = "success"
    SILENCE_TIMEOUT = "silence_timeout"
    UNRECOGNIZED = "unrecognized"
    SERVICE_ERROR = "service_error"
    DEVICE_ERROR = "device_error"
    NOT_READY = "not_ready"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


def _validate_capture_timing_fields(
    start: Optional[float],
    end: Optional[float],
    *,
    status: RecognitionStatus,
) -> None:
    if start is None and end is None:
        return
    if start is None or end is None:
        raise ValueError("capture timing requires both start and end")
    for name, value in (
        ("capture_start_mono", start),
        ("capture_end_mono", end),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite non-negative number")
        number = float(value)
        if not math.isfinite(number) or number < 0.0:
            raise ValueError(f"{name} must be a finite non-negative number")
    if float(end) < float(start):
        raise ValueError("capture_end_mono must be >= capture_start_mono")
    if status is not RecognitionStatus.SUCCESS:
        raise ValueError("non-success recognition must not carry capture timing")


@dataclass(frozen=True)
class RecognitionResult:
    """结构化识别结果；只有 ``SUCCESS`` 可以携带用户文本。"""

    status: RecognitionStatus
    text: str = ""
    error_detail: Optional[str] = None
    capture_start_mono: Optional[float] = None
    capture_end_mono: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, RecognitionStatus):
            raise TypeError("status must be RecognitionStatus")
        if not isinstance(self.text, str):
            raise TypeError("text must be str")
        if self.error_detail is not None and not isinstance(self.error_detail, str):
            raise TypeError("error_detail must be str or None")
        normalized = self.text.strip()
        object.__setattr__(self, "text", normalized)
        _validate_capture_timing_fields(
            self.capture_start_mono,
            self.capture_end_mono,
            status=self.status,
        )
        if self.status is RecognitionStatus.SUCCESS:
            if not normalized:
                raise ValueError("SUCCESS recognition requires non-empty text")
            if self.error_detail:
                raise ValueError("SUCCESS recognition must not carry error_detail")
        elif normalized:
            raise ValueError("non-success recognition must not carry text")

    @classmethod
    def success(
        cls,
        text: str,
        *,
        capture_start_mono: Optional[float] = None,
        capture_end_mono: Optional[float] = None,
    ) -> "RecognitionResult":
        return cls(
            status=RecognitionStatus.SUCCESS,
            text=text,
            capture_start_mono=capture_start_mono,
            capture_end_mono=capture_end_mono,
        )

    @classmethod
    def failure(
        cls,
        status: RecognitionStatus,
        detail: Optional[str] = None,
    ) -> "RecognitionResult":
        if status in (RecognitionStatus.SUCCESS, RecognitionStatus.CANCELLED):
            raise ValueError("failure status must be a non-success, non-cancelled status")
        return cls(status=status, error_detail=detail)

    @classmethod
    def cancelled(cls, detail: Optional[str] = None) -> "RecognitionResult":
        return cls(status=RecognitionStatus.CANCELLED, error_detail=detail)

    @classmethod
    def coerce(cls, value: Any) -> "RecognitionResult":
        """
        将任意返回值收口为合法 ``RecognitionResult``。

        非法类型、绕过 ``__post_init__`` 的破损对象、或违反不变量的结果
        一律变为 ``INTERNAL_ERROR``，不得当作用户输入。
        """
        if not isinstance(value, RecognitionResult):
            return cls.failure(
                RecognitionStatus.INTERNAL_ERROR,
                "STT provider 返回了无效结果类型",
            )
        try:
            # 重新经过唯一的数据类构造入口，完整复验字段类型与跨字段不变量。
            # 不能按状态挑字段重建，否则破损 SUCCESS 的 error_detail 或
            # 非成功结果中的非法 text 类型可能被静默丢弃并伪装成合法结果。
            return cls(
                status=value.status,
                text=value.text,
                error_detail=value.error_detail,
                capture_start_mono=value.capture_start_mono,
                capture_end_mono=value.capture_end_mono,
            )
        except Exception as exc:
            # 只收口普通实现/数据错误；KeyboardInterrupt/SystemExit 仍会向上抛出。
            return cls.failure(RecognitionStatus.INTERNAL_ERROR, str(exc))


@runtime_checkable
class RecognitionProvider(Protocol):
    """可替换的录音与识别 Provider。"""

    def recognize(
        self,
        *,
        timeout: Optional[float] = None,
        phrase_time_limit: Optional[float] = None,
        continuous: bool = False,
        cancel_event: Optional[threading.Event] = None,
    ) -> RecognitionResult:
        """执行一次识别并返回明确终态。"""

    def is_ready(self) -> bool:
        """返回 provider 是否已具备识别条件。"""

    def check_microphone(self) -> tuple[bool, str]:
        """执行显式设备预检；构造 provider 时不得因此打开设备。"""
