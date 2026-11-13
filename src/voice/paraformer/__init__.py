# -*- coding: utf-8 -*-
"""本地 Paraformer-zh ASR worker 客户端与协议（主进程不 import torch）。"""
from __future__ import annotations

from src.voice.paraformer.client import ParaformerShutdownStatus, ParaformerWorkerClient
from src.voice.paraformer.settings import ParaformerSettings

__all__ = [
    "ParaformerSettings",
    "ParaformerShutdownStatus",
    "ParaformerWorkerClient",
]
