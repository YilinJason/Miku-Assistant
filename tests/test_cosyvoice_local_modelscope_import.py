"""Fake tests for bounded Cosy worker modelscope import adapter."""
from __future__ import annotations

import importlib.abc
import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType

from src.core.paths import PROJECT_ROOT
from src.voice.cosyvoice.assets import WETEXT_DIR_REL
from src.voice.cosyvoice.local_modelscope_import import (
    KNOWN_WETEXT_REPO_ID,
    assert_snapshot_download_still_local,
    import_wetext_with_local_snapshot,
    make_local_snapshot_download,
    modelscope_local_snapshot_import_scope,
    resolve_trusted_wetext_root,
)

PINNED_WETEXT = PROJECT_ROOT / WETEXT_DIR_REL


def _require_pinned_wetext() -> Path:
    if not PINNED_WETEXT.is_dir():
        raise unittest.SkipTest("pinned wetext asset directory missing")
    return resolve_trusted_wetext_root(PINNED_WETEXT, repo_root=PROJECT_ROOT)


class _ConsumerLoader(importlib.abc.Loader):
    def __init__(self, fullname: str, *, wetext_root: Path) -> None:
        self.fullname = fullname
        self.wetext_root = wetext_root

    def create_module(self, spec):  # noqa: ANN001
        return None

    def exec_module(self, module: ModuleType) -> None:
        with modelscope_local_snapshot_import_scope(
            wetext_root=self.wetext_root,
            repo_root=PROJECT_ROOT,
        ):
            from modelscope import snapshot_download

            module.snapshot_download = snapshot_download  # type: ignore[attr-defined]
            module.resolved = snapshot_download(KNOWN_WETEXT_REPO_ID)  # type: ignore[attr-defined]


class _WetextPackageLoader(importlib.abc.Loader):
    def create_module(self, spec):  # noqa: ANN001
        return None

    def exec_module(self, module: ModuleType) -> None:
        module.__path__ = []  # type: ignore[attr-defined]


class _ConsumerFinder(importlib.abc.MetaPathFinder):
    def __init__(self, *, wetext_root: Path) -> None:
        self.wetext_root = wetext_root

    def find_spec(self, fullname, path, target=None):  # noqa: ANN001
        if fullname == "wetext":
            return importlib.util.spec_from_loader(
                fullname,
                _WetextPackageLoader(),
                is_package=True,
            )
        if fullname != "wetext.wetext":
            return None
        loader = _ConsumerLoader(fullname, wetext_root=self.wetext_root)
        return importlib.util.spec_from_loader(fullname, loader)


class LocalModelscopeImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_modules = {
            key: sys.modules[key]
            for key in list(sys.modules)
            if key == "modelscope" or key.startswith("modelscope.")
            or key in ("wetext", "wetext.wetext")
        }

    def tearDown(self) -> None:
        for key in list(sys.modules):
            if key == "modelscope" or key.startswith("modelscope.") or key in (
                "wetext",
                "wetext.wetext",
            ):
                sys.modules.pop(key, None)
        sys.modules.update(self._saved_modules)

    def test_scope_resolves_known_repo_only(self) -> None:
        root = _require_pinned_wetext()
        with modelscope_local_snapshot_import_scope(
            wetext_root=root,
            repo_root=PROJECT_ROOT,
        ) as fn:
            self.assertEqual(fn(KNOWN_WETEXT_REPO_ID), str(root))
            from modelscope import snapshot_download

            self.assertEqual(snapshot_download(KNOWN_WETEXT_REPO_ID), str(root))
        with self.assertRaises(ImportError):
            fn("pengzhendong/wetext-other")

    def test_non_string_repo_rejected(self) -> None:
        root = _require_pinned_wetext()
        fn = make_local_snapshot_download(root, repo_root=PROJECT_ROOT)

        class PretendRepo:
            def __str__(self) -> str:
                return KNOWN_WETEXT_REPO_ID

        with self.assertRaises(TypeError):
            fn(PretendRepo())

    def test_arbitrary_directory_rejected(self) -> None:
        with TemporaryDirectory(dir=PROJECT_ROOT / "tmp", prefix="sdk-untrusted-") as folder:
            with self.assertRaises((ImportError, PermissionError, ValueError)):
                with modelscope_local_snapshot_import_scope(
                    wetext_root=Path(folder),
                    repo_root=PROJECT_ROOT,
                ):
                    pass

    def test_scope_restores_absent_modelscope(self) -> None:
        root = _require_pinned_wetext()
        sys.modules.pop("modelscope", None)
        with modelscope_local_snapshot_import_scope(
            wetext_root=root,
            repo_root=PROJECT_ROOT,
        ):
            self.assertIn("modelscope", sys.modules)
        self.assertNotIn("modelscope", sys.modules)

    def test_scope_restores_preexisting_module_object(self) -> None:
        root = _require_pinned_wetext()
        original = ModuleType("modelscope")
        original.marker = "keep-me"  # type: ignore[attr-defined]
        sys.modules["modelscope"] = original
        with modelscope_local_snapshot_import_scope(
            wetext_root=root,
            repo_root=PROJECT_ROOT,
        ):
            self.assertIsNot(sys.modules["modelscope"], original)
        self.assertIs(sys.modules["modelscope"], original)

    def test_foreign_module_not_clobbered_on_exit(self) -> None:
        root = _require_pinned_wetext()
        original = ModuleType("modelscope")
        foreign = ModuleType("foreign_replacement")
        sys.modules["modelscope"] = original
        with self.assertRaises(RuntimeError):
            with modelscope_local_snapshot_import_scope(
                wetext_root=root,
                repo_root=PROJECT_ROOT,
            ):
                sys.modules["modelscope"] = foreign
        self.assertIs(sys.modules["modelscope"], foreign)

    def test_nested_scope_rejected(self) -> None:
        root = _require_pinned_wetext()
        with modelscope_local_snapshot_import_scope(
            wetext_root=root,
            repo_root=PROJECT_ROOT,
        ):
            with self.assertRaises(RuntimeError):
                with modelscope_local_snapshot_import_scope(
                    wetext_root=root,
                    repo_root=PROJECT_ROOT,
                ):
                    pass

    def test_keyboard_interrupt_restores_modules(self) -> None:
        root = _require_pinned_wetext()
        sys.modules.pop("modelscope", None)
        with self.assertRaises(KeyboardInterrupt):
            with modelscope_local_snapshot_import_scope(
                wetext_root=root,
                repo_root=PROJECT_ROOT,
            ):
                raise KeyboardInterrupt()
        self.assertNotIn("modelscope", sys.modules)

    def test_concurrent_second_scope_rejected(self) -> None:
        root = _require_pinned_wetext()
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                with modelscope_local_snapshot_import_scope(
                    wetext_root=root,
                    repo_root=PROJECT_ROOT,
                ):
                    entered.set()
                    release.wait(timeout=2.0)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(timeout=2.0))
        try:
            with self.assertRaises(RuntimeError):
                with modelscope_local_snapshot_import_scope(
                    wetext_root=root,
                    repo_root=PROJECT_ROOT,
                ):
                    pass
        finally:
            release.set()
            thread.join(timeout=3.0)
            self.assertFalse(thread.is_alive())
            if errors:
                raise BaseExceptionGroup("concurrent scope worker errors", errors)

    def test_other_thread_cannot_use_scoped_api(self) -> None:
        root = _require_pinned_wetext()
        captured: list[object] = []

        def foreign_import() -> None:
            try:
                mod = sys.modules["modelscope"]
                captured.append(getattr(mod, "snapshot_download"))
            except (ImportError, RuntimeError, AttributeError):
                pass

        thread = threading.Thread(target=foreign_import)
        with modelscope_local_snapshot_import_scope(
            wetext_root=root,
            repo_root=PROJECT_ROOT,
        ):
            thread.start()
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            self.assertFalse(captured)

    def test_fake_loader_consumer_import_chain(self) -> None:
        root = _require_pinned_wetext()
        finder = _ConsumerFinder(wetext_root=root)
        sys.meta_path.insert(0, finder)
        try:
            sys.modules.pop("wetext.wetext", None)
            mod = importlib.import_module("wetext.wetext")
            self.assertEqual(mod.resolved, str(root))  # type: ignore[attr-defined]
            assert_snapshot_download_still_local(mod, root, repo_root=PROJECT_ROOT)
        finally:
            sys.meta_path.remove(finder)

    def test_import_wetext_integration_when_installed(self) -> None:
        try:
            spec = importlib.util.find_spec("wetext.wetext")
        except ModuleNotFoundError:
            spec = None
        if spec is None:
            self.skipTest("wetext.wetext not installed in this interpreter")
        root = _require_pinned_wetext()
        mod = import_wetext_with_local_snapshot(root, repo_root=PROJECT_ROOT)
        assert_snapshot_download_still_local(mod, root, repo_root=PROJECT_ROOT)

    def test_untrusted_wetext_root_rejected(self) -> None:
        with TemporaryDirectory(dir=PROJECT_ROOT / "tmp", prefix="sdk-missing-") as folder:
            missing = Path(folder) / "nope"
            with self.assertRaises((PermissionError, ValueError)):
                make_local_snapshot_download(missing, repo_root=PROJECT_ROOT)


if __name__ == "__main__":
    unittest.main()
