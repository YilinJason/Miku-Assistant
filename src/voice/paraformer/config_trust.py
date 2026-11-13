# -*- coding: utf-8 -*-
"""拒绝 Paraformer 配置中的远程/逃逸引用（stdlib only）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

_UNSAFE_YAML = re.compile(
    r"(\$\{|\$\(|https?://|modelscope://|hf://|http://|file://|\.\./)",
    re.IGNORECASE,
)


def assert_model_configs_safe(model_dir: Path) -> None:
    for name in ("config.yaml", "configuration.json"):
        path = model_dir / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("config_unreadable") from exc
        if _UNSAFE_YAML.search(text):
            raise RuntimeError("config_unsafe")
        if name.endswith(".json"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError("config_invalid") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("config_invalid")
            for key in ("model", "init_param", "model_path", "remote_code"):
                val = payload.get(key)
                if isinstance(val, str) and _UNSAFE_YAML.search(val):
                    raise RuntimeError("config_unsafe")
