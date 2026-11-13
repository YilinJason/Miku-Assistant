# -*- coding: utf-8 -*-
"""Bounded modelscope.snapshot_download stub for Cosy runtime worker imports only."""
from __future__ import annotations

import importlib.machinery
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterator, Mapping, MutableMapping, Optional

from src.core.paths import PROJECT_ROOT

KNOWN_WETEXT_REPO_ID = "pengzhendong/wetext"
_SCOPE_LOCK = threading.RLock()
_SCOPE_DEPTH = 0
_MISSING = object()


class _ThreadBoundSnapshotModule(ModuleType):
    """Temporary modelscope stub visible only to the scope owner thread."""

    def __init__(self, name: str, *, owner_tid: int, snapshot_fn: Callable[..., str]) -> None:
        super().__init__(name)
        self._local_owner_tid = owner_tid
        self._local_snapshot_fn = snapshot_fn

    def __getattr__(self, name: str):
        if name == "snapshot_download":
            if threading.get_ident() != self._local_owner_tid:
                raise RuntimeError("scoped modelscope API is owner-thread only")
            return self._local_snapshot_fn
        raise AttributeError(name)


def resolve_trusted_wetext_root(
    wetext_root: Path,
    *,
    repo_root: Path | None = None,
) -> Path:
    from src.voice.cosyvoice.assets import WETEXT_DIR_REL
    from src.voice.cosyvoice.path_trust import assert_trusted_repo_path

    repo = Path(repo_root or PROJECT_ROOT)
    expected = (repo / WETEXT_DIR_REL).resolve()
    candidate = Path(wetext_root)
    assert_trusted_repo_path(repo, candidate, kind="dir")
    resolved = candidate.resolve()
    if resolved != expected:
        raise PermissionError("wetext root is not the pinned asset directory")
    return resolved


def _validate_repo_id(repo_id: object) -> str:
    if type(repo_id) is not str:
        raise TypeError("modelscope repo id must be str")
    if repo_id != repo_id.strip() or not repo_id:
        raise ValueError("invalid modelscope repo id")
    if repo_id.casefold() != KNOWN_WETEXT_REPO_ID.casefold():
        raise ImportError("unsupported modelscope repo")
    return repo_id


def make_local_snapshot_download(
    wetext_root: Path,
    *,
    repo_root: Path | None = None,
) -> Callable[..., str]:
    pinned = Path(wetext_root)
    repo = Path(repo_root or PROJECT_ROOT)
    resolve_trusted_wetext_root(pinned, repo_root=repo)

    def local_snapshot_download(repo_id: object, *args: object, **kwargs: object) -> str:
        if args:
            raise ImportError("unsupported modelscope snapshot_download positional args")
        if kwargs:
            raise ImportError("unsupported modelscope snapshot_download kwargs")
        _validate_repo_id(repo_id)
        root = resolve_trusted_wetext_root(pinned, repo_root=repo)
        return str(root)

    return local_snapshot_download


def _install_modelscope_stub(
    modules: MutableMapping[str, object],
    *,
    snapshot_fn: Callable[..., str],
    owner_tid: int,
) -> _ThreadBoundSnapshotModule:
    name = "modelscope"
    stub = _ThreadBoundSnapshotModule(
        name,
        owner_tid=owner_tid,
        snapshot_fn=snapshot_fn,
    )
    stub.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    modules[name] = stub
    return stub


def _restore_modules(
    modules: MutableMapping[str, object],
    saved: Mapping[str, object],
    installed: Mapping[str, ModuleType],
) -> None:
    for name, stub in installed.items():
        current = modules.get(name)
        if current is not stub:
            continue
        previous = saved.get(name, _MISSING)
        if previous is _MISSING:
            modules.pop(name, None)
        else:
            modules[name] = previous  # type: ignore[assignment]


@contextmanager
def modelscope_local_snapshot_import_scope(
    *,
    wetext_root: Path,
    repo_root: Path | None = None,
) -> Iterator[Callable[..., str]]:
    """Expose a strict local snapshot_download while importing wetext/Cosy modules."""
    global _SCOPE_DEPTH
    trusted = resolve_trusted_wetext_root(wetext_root, repo_root=repo_root)
    snapshot_fn = make_local_snapshot_download(trusted, repo_root=repo_root)
    owner_tid = threading.get_ident()
    saved: dict[str, object] = {}
    installed: dict[str, ModuleType] = {}
    with _SCOPE_LOCK:
        if _SCOPE_DEPTH:
            raise RuntimeError("nested modelscope import scope not allowed")
        _SCOPE_DEPTH += 1
    body_exc: BaseException | None = None
    try:
        saved["modelscope"] = sys.modules.get("modelscope", _MISSING)
        stub = _install_modelscope_stub(
            sys.modules,
            snapshot_fn=snapshot_fn,
            owner_tid=owner_tid,
        )
        installed["modelscope"] = stub
        try:
            yield snapshot_fn
        except BaseException as exc:
            body_exc = exc
            raise
        finally:
            current = sys.modules.get("modelscope")
            if current is not stub:
                if body_exc is None:
                    raise RuntimeError(
                        "foreign modelscope module replaced import scope stub"
                    )
            else:
                _restore_modules(sys.modules, saved, installed)
    finally:
        with _SCOPE_LOCK:
            _SCOPE_DEPTH -= 1


def import_wetext_with_local_snapshot(
    wetext_root: Path,
    *,
    repo_root: Path | None = None,
):
    """Import wetext.wetext under the stub scope; keep a strict local alias after."""
    trusted = resolve_trusted_wetext_root(wetext_root, repo_root=repo_root)
    with modelscope_local_snapshot_import_scope(
        wetext_root=trusted,
        repo_root=repo_root,
    ):
        import wetext.wetext as wetext_mod  # noqa: WPS433 — worker-only import path

    wetext_mod.snapshot_download = make_local_snapshot_download(
        trusted,
        repo_root=repo_root,
    )  # type: ignore[method-assign]
    return wetext_mod


def assert_snapshot_download_still_local(
    wetext_mod: object,
    wetext_root: Path,
    *,
    repo_root: Path | None = None,
) -> None:
    fn = getattr(wetext_mod, "snapshot_download", None)
    if not callable(fn):
        raise AssertionError("wetext snapshot_download missing after scope")
    trusted = resolve_trusted_wetext_root(wetext_root, repo_root=repo_root)
    path = fn(KNOWN_WETEXT_REPO_ID)
    if path != str(trusted):
        raise AssertionError("wetext snapshot_download no longer resolves locally")
