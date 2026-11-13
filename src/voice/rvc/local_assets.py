"""Stdlib-only preflight for fixed ``rvc_python`` base_model assets (no import)."""
from __future__ import annotations

import importlib.util
import os
import stat
from dataclasses import dataclass

RVC_PYTHON_NOT_INSTALLED = "rvc_python_not_installed"
RVC_PYTHON_ORIGIN_UNAVAILABLE = "rvc_python_origin_unavailable"
RVC_PYTHON_LIB_DIR_INVALID = "rvc_python_lib_dir_invalid"
RVC_BASE_MODEL_ASSETS_MISSING = "rvc_base_model_assets_missing"

_REQUIRED_FILES = (
    ("base_model", "hubert_base.pt"),
    ("base_model", "rmvpe.pt"),
)

_PACKAGE_INIT_NAMES = frozenset({"__init__.py", "__init__.pyc"})


class RvcLocalAssetsError(Exception):
    """Stable, path-free local asset preflight failure."""


@dataclass(frozen=True)
class RvcLocalAssetsResult:
    lib_dir: str


def _lib_dir_from_package_origin(origin: str) -> str:
    base = os.path.basename(origin)
    if base in _PACKAGE_INIT_NAMES:
        return os.path.dirname(os.path.abspath(origin))
    raise RvcLocalAssetsError(RVC_PYTHON_ORIGIN_UNAVAILABLE)


def _resolve_lib_dir_from_spec() -> str:
    try:
        spec = importlib.util.find_spec("rvc_python")
    except (ImportError, ValueError, OSError) as exc:
        raise RvcLocalAssetsError(RVC_PYTHON_ORIGIN_UNAVAILABLE) from exc

    if spec is None:
        raise RvcLocalAssetsError(RVC_PYTHON_NOT_INSTALLED)

    origin = spec.origin
    if not isinstance(origin, str) or not origin or origin == "namespace":
        raise RvcLocalAssetsError(RVC_PYTHON_ORIGIN_UNAVAILABLE)

    if spec.submodule_search_locations is None:
        base = os.path.basename(origin)
        if base not in _PACKAGE_INIT_NAMES:
            raise RvcLocalAssetsError(RVC_PYTHON_ORIGIN_UNAVAILABLE)

    lib_dir = _lib_dir_from_package_origin(origin)
    if not os.path.isdir(lib_dir):
        raise RvcLocalAssetsError(RVC_PYTHON_LIB_DIR_INVALID)
    return lib_dir


def _is_nonempty_regular_file(path: str) -> bool:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    return st.st_size > 0


def ensure_rvc_python_local_assets() -> RvcLocalAssetsResult:
    """
    Verify fixed HuBERT/RMVPE weights exist under the package tree.

    Existence-only check; does not validate weight contents. Does not import
    ``rvc_python`` or perform network I/O.
    """
    lib_dir = _resolve_lib_dir_from_spec()
    for parts in _REQUIRED_FILES:
        candidate = os.path.join(lib_dir, *parts)
        if not _is_nonempty_regular_file(candidate):
            raise RvcLocalAssetsError(RVC_BASE_MODEL_ASSETS_MISSING)
    return RvcLocalAssetsResult(lib_dir=lib_dir)
