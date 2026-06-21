"""RVC vc_single 推理参数（从 config 统一读取）"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional


def resolve_index_path(rvc_config: Dict[str, Any]) -> str:
    """返回可用的 index 路径，不存在则空字符串。"""
    index_path = str(rvc_config.get("index_path") or "").strip()
    if index_path and os.path.isfile(index_path):
        return index_path
    return ""


def build_vc_kwargs(
    rvc_config: Dict[str, Any],
    *,
    pitch: Optional[int] = None,
    index_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """
    构建 ``VC.vc_single`` 关键字参数。

    默认值针对 Nahida 日常对话做了偏「干净、少电流感」的调优：
    - index_rate 略降，减轻检索带来的颗粒感
    - protect 提高，保护清辅音/气声，减少伪影
    - rms_mix_rate < 1，混合源音量包络，听感更自然
    """
    index_path = resolve_index_path(rvc_config)
    effective_index_rate = float(
        index_rate if index_rate is not None else rvc_config.get("index_rate", 0.55)
    )
    if not index_path:
        effective_index_rate = 0.0

    return {
        "f0_up_key": int(pitch if pitch is not None else rvc_config.get("pitch", 0)),
        "f0_method": str(rvc_config.get("f0_method", "rmvpe")),
        "file_index": index_path,
        "index_rate": effective_index_rate,
        "filter_radius": int(rvc_config.get("filter_radius", 3)),
        "resample_sr": int(rvc_config.get("resample_sr", 0)),
        "rms_mix_rate": float(rvc_config.get("rms_mix_rate", 0.75)),
        "protect": float(rvc_config.get("protect", 0.5)),
        "f0_file": "",
        "file_index2": "",
    }
