# -*- coding: utf-8 -*-
"""
AutoModel 构造期间的窄边界 CPU checkpoint staging（仅 runtime worker 子进程）。

在 vendor 将 CUDA map_location 的 llm/flow/hift 权重先载入 GPU 再落回 CPU 的路径上，
临时将受支持的 ``torch.load`` 调用改为 ``map_location=cpu``；不修改 vendor、不缓存权重。
"""
from __future__ import annotations

import inspect
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Tuple

_STAGED_NAMES = frozenset({"llm.pt", "flow.pt", "hift.pt"})

_SCOPE_LOCK = threading.Lock()


class CpuCheckpointStagingError(RuntimeError):
    """作用域重入或并发构造时快速失败。"""


def _is_cuda_map_location(map_location: Any) -> bool:
    if map_location is None:
        return False
    if isinstance(map_location, str):
        lowered = map_location.lower()
        return lowered == "cuda" or lowered.startswith("cuda:")
    device_type = getattr(map_location, "type", None)
    if isinstance(device_type, str):
        return device_type == "cuda"
    return False


def _staged_checkpoint_path(model_dir: Path, file_arg: Any) -> Optional[Path]:
    if not isinstance(file_arg, (str, Path)):
        return None
    try:
        resolved = Path(file_arg).resolve()
        if resolved.parent != model_dir:
            return None
    except OSError:
        return None
    if resolved.name not in _STAGED_NAMES:
        return None
    return resolved


def _should_stage(
    model_dir: Path,
    file_arg: Any,
    map_location: Any,
    weights_only: Any,
) -> bool:
    if weights_only is not True:
        return False
    if not _is_cuda_map_location(map_location):
        return False
    return _staged_checkpoint_path(model_dir, file_arg) is not None


def _original_load_supports_mmap_kw(original: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(original)
    except Exception:
        return False
    if "mmap" not in sig.parameters:
        return False
    param = sig.parameters["mmap"]
    if param.kind in (
        inspect.Parameter.VAR_KEYWORD,
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.POSITIONAL_ONLY,
    ):
        return False
    return True


def _checkpoint_is_zip_pk0304(checkpoint_path: Path) -> bool:
    try:
        with open(checkpoint_path, "rb") as handle:
            header = handle.read(4)
    except OSError:
        return False
    return header == b"PK\x03\x04"


def _make_patched_load(
    original: Callable[..., Any],
    model_dir: Path,
    owner_thread_id: int,
) -> Tuple[Callable[..., Any], Callable[[], None]]:
    """每次作用域独立闭包：保留的 alias 永久委托其捕获的 original，不读全局 active。"""
    resolved_dir = model_dir.resolve()
    supports_mmap = _original_load_supports_mmap_kw(original)
    scope_active = True

    def patched_torch_load(*args: Any, **kwargs: Any) -> Any:
        if not scope_active or threading.get_ident() != owner_thread_id:
            return original(*args, **kwargs)

        if not args:
            return original(*args, **kwargs)

        file_arg = args[0]
        map_location_kw = "map_location" in kwargs
        map_location = kwargs.get("map_location")
        tail_args: Tuple[Any, ...] = args[1:]
        map_location_pos = False
        if not map_location_kw and tail_args:
            map_location = tail_args[0]
            map_location_pos = True
            tail_args = tail_args[1:]

        weights_only = kwargs.get("weights_only")
        if _should_stage(resolved_dir, file_arg, map_location, weights_only):
            checkpoint_path = _staged_checkpoint_path(resolved_dir, file_arg)
            assert checkpoint_path is not None
            staged_kwargs = dict(kwargs)
            if (
                "mmap" not in kwargs
                and supports_mmap
                and _checkpoint_is_zip_pk0304(checkpoint_path)
            ):
                staged_kwargs["mmap"] = True
            if map_location_pos:
                new_args = (file_arg, "cpu", *tail_args)
                return original(*new_args, **staged_kwargs)
            staged_kwargs["map_location"] = "cpu"
            return original(file_arg, *tail_args, **staged_kwargs)

        return original(*args, **kwargs)

    def deactivate() -> None:
        nonlocal scope_active
        scope_active = False

    return patched_torch_load, deactivate


@contextmanager
def cpu_checkpoint_staging_scope(model_dir: Path) -> Iterator[None]:
    """
    仅在当前线程、本次 AutoModel 构造期间包装 ``torch.load``。

    重入或并发第二作用域会 ``CpuCheckpointStagingError``；其它线程调用仍走原始 ``torch.load``。
    """
    if not _SCOPE_LOCK.acquire(blocking=False):
        raise CpuCheckpointStagingError(
            "cpu_checkpoint_staging_scope already active"
        )

    original_load: Optional[Callable[..., Any]] = None
    deactivate: Optional[Callable[[], None]] = None
    wrapper_installed = False
    try:
        import torch

        owner = threading.get_ident()
        original_load = torch.load
        patched_load, deactivate = _make_patched_load(
            original_load, Path(model_dir), owner
        )
        torch.load = patched_load
        wrapper_installed = True
        try:
            yield
        except (KeyboardInterrupt, SystemExit):
            raise
    finally:
        if wrapper_installed:
            if deactivate is not None:
                deactivate()
            if original_load is not None:
                import torch

                torch.load = original_load
        _SCOPE_LOCK.release()
