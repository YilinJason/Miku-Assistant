"""默认长期记忆数据库路径解析（不创建文件或目录）。"""
from __future__ import annotations

from pathlib import Path


def default_memory_db_path(base_dir: Path | str | None = None) -> Path:
    """
    解析建议的生产库路径：``{base}/data/memory/long_term.sqlite3``。

    仅做路径拼接，不创建目录或数据库。``base_dir`` 默认取仓库根（本文件上三级）。
    """
    if base_dir is None:
        root = Path(__file__).resolve().parents[2]
    else:
        root = Path(base_dir)
    return root / "data" / "memory" / "long_term.sqlite3"
