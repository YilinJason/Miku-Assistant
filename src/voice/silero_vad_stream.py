# -*- coding: utf-8 -*-
"""Silero 流式 VAD 上下文（stdlib 模块面；首帧懒初始化 numpy state）。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Tuple

from src.voice.silero_vad import (
    BYTES_PER_SAMPLE,
    CONTEXT_SAMPLES,
    FRAME_SAMPLES,
)

if TYPE_CHECKING:
    from src.voice.silero_vad import SileroOnnxVAD

STREAM_FRAME_BYTES = FRAME_SAMPLES * BYTES_PER_SAMPLE


@dataclass(frozen=True)
class SileroStreamFrameScores:
    ok: bool
    reason: str
    probability: float = 0.0
    valid_samples: int = FRAME_SAMPLES
    elapsed_ms: float = 0.0


class SileroVadStreamContext:
    """拥有 ``[2,1,128]`` state 与 64 样本 context；不关闭 owner session。"""

    def __init__(self, owner: "SileroOnnxVAD") -> None:
        self._owner = owner
        self._closed = False
        self._invalidated = False
        self._closing = threading.Event()
        self._state: Any = None
        self._context: Any = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def invalidated(self) -> bool:
        return self._invalidated

    @property
    def closing(self) -> bool:
        return self._closing.is_set()

    def invalidate(self) -> None:
        self._invalidated = True
        self._state = None
        self._context = None

    def _finalize_close(self) -> None:
        self._closed = True
        self._invalidated = True
        self._state = None
        self._context = None

    def _owner_close_invalidate(self) -> None:
        self._closing.set()
        self._finalize_close()

    def _state_arrays(self, np: Any) -> Tuple[Optional[Any], Optional[Any]]:
        if self._invalidated or self._closed or self._closing.is_set():
            return None, None
        if self._state is None:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
            self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        return self._state, self._context

    def _commit_state(self, state: Any, context: Any) -> None:
        if self._invalidated or self._closed or self._closing.is_set():
            return
        self._state = state
        self._context = context

    def reset(self) -> bool:
        if self._closed:
            return False
        return self._owner._reset_stream_context(self)

    def process_frame(
        self,
        pcm: bytes,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> SileroStreamFrameScores:
        if self._closed:
            return SileroStreamFrameScores(ok=False, reason="closed")
        if self._invalidated:
            return SileroStreamFrameScores(ok=False, reason="stream_invalid")
        if self._closing.is_set():
            return SileroStreamFrameScores(ok=False, reason="closing")
        return self._owner._process_owned_stream_frame(
            self,
            pcm,
            cancel_event=cancel_event,
        )

    def close(self, *, timeout_sec: Optional[float] = None) -> bool:
        if self._closed:
            return True
        return self._owner._close_stream_context(self, timeout_sec=timeout_sec)
