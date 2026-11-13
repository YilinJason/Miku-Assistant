# -*- coding: utf-8 -*-
"""
CosyVoice-300M-Instruct → Nahida RVC 评估固定套件（stdlib only）。

实现已迁至 ``src.voice.cosyvoice.assets``；本模块保持评估/worker 导入兼容。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.voice.cosyvoice.assets import *  # noqa: F403, F401
