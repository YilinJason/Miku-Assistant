# -*- coding: utf-8 -*-
"""主进程数值线程 bootstrap（stdlib/fake；无 numpy 导入）。"""
from __future__ import annotations

import builtins
import os
import sys
import unittest
from types import ModuleType

from src.core.numeric_bootstrap import apply_main_numeric_thread_budget, thread_env_keys
from src.core.safety import Operation, SafetyPolicy

_BLOCKED_IMPORT_ROOTS = frozenset({"numpy", "onnxruntime"})


class NumericBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_restore = {
            key: os.environ.get(key)
            for key in thread_env_keys()
        }
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, prior in self._env_restore.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior

    def test_sets_exact_four_keys_to_one(self) -> None:
        apply_main_numeric_thread_budget()
        for key in thread_env_keys():
            self.assertEqual(os.environ.get(key), "1")

    def test_does_not_leak_env_modify_to_default_policy(self) -> None:
        apply_main_numeric_thread_budget()
        policy = SafetyPolicy()
        self.assertFalse(policy.is_allowed(Operation.ENV_MODIFY))

    def test_import_main_module_does_not_touch_env(self) -> None:
        env_before = dict(self._env_restore)
        main_snapshot = sys.modules.get("main")
        sys.modules.pop("main", None)
        try:
            import main as main_module  # noqa: F401
        finally:
            sys.modules.pop("main", None)
            if main_snapshot is not None:
                sys.modules["main"] = main_snapshot

        self.assertEqual(
            {k: os.environ.get(k) for k in thread_env_keys()},
            env_before,
        )

    def test_main_sets_thread_env_on_first_cli_app_import(self) -> None:
        main_snapshot = sys.modules.get("main")
        cli_snapshot = {
            key: value
            for key, value in sys.modules.items()
            if key == "src.cli.app" or key.startswith("src.cli.app.")
        }
        fake_cli_calls: list[str] = []
        original_import = builtins.__import__

        def fake_cli_main() -> None:
            fake_cli_calls.append("main")

        def guarded_import(
            name: str,
            globals=None,
            locals=None,
            fromlist=(),
            level: int = 0,
        ):
            root = name.split(".", 1)[0]
            if root in _BLOCKED_IMPORT_ROOTS or name in _BLOCKED_IMPORT_ROOTS:
                raise AssertionError(f"blocked import {name}")
            if name == "src.cli.app":
                for key in thread_env_keys():
                    if os.environ.get(key) != "1":
                        raise AssertionError(
                            f"{key} must be 1 before src.cli.app import"
                        )
                mod = ModuleType("src.cli.app")
                mod.main = fake_cli_main  # type: ignore[attr-defined]
                sys.modules["src.cli.app"] = mod
                return mod
            return original_import(name, globals, locals, fromlist, level)

        sys.modules.pop("main", None)
        for key in cli_snapshot:
            sys.modules.pop(key, None)
        try:
            import main as main_mod

            builtins.__import__ = guarded_import  # type: ignore[misc, assignment]
            try:
                main_mod.main()
            finally:
                builtins.__import__ = original_import  # type: ignore[misc, assignment]
        finally:
            sys.modules.pop("main", None)
            if main_snapshot is not None:
                sys.modules["main"] = main_snapshot
            else:
                sys.modules.pop("main", None)
            for key, value in cli_snapshot.items():
                sys.modules[key] = value
            for key in list(sys.modules):
                if key == "src.cli.app" or key.startswith("src.cli.app."):
                    if key not in cli_snapshot:
                        sys.modules.pop(key, None)

        self.assertEqual(fake_cli_calls, ["main"])


if __name__ == "__main__":
    unittest.main()
