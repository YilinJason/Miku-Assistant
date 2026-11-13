"""RVC startup preflight: skip duplicate IMPORT_RVC when Worker will load (fake only)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import src.core.owned_rvc_run as owned_rvc_run_module
from src.core.safety import SafetyPolicy
from src.voice.rvc.health_check import RVCHealthReport, check_rvc_health
from src.voice.tts.engine import TTSEngine

from tests.test_rvc_health_probe_budget import _FakePopen, _policy_with_assets
from tests.support.local_tts_runners import bind_fake_rvc_runners
from tests.support.host_memory_fixtures import (
    install_module_sufficient_host_memory,
    patch_sufficient_host_memory,
)


def setUpModule() -> None:
    install_module_sufficient_host_memory()


def _bare_engine(*, worker_enabled: bool = True) -> TTSEngine:
    engine = TTSEngine.__new__(TTSEngine)
    engine.logger = MagicMock()
    engine._rvc = SimpleNamespace(enabled=True)
    engine._rvc_config = {
        "prewarm_audio_processing": False,
        "worker_enabled": worker_enabled,
    }
    engine._rvc_prepare_done = False
    engine._rvc_ready = False
    engine.safety_policy = SafetyPolicy({"permissions": {"rvc_worker": True}})
    engine._temp_files = None
    return engine


@unittest.skipUnless(sys.platform == "win32", "owned RVC probe path")
class RvcStartupPreflightTests(unittest.TestCase):
    def test_probe_import_must_be_strict_bool(self) -> None:
        invalid = (0, 1, None, "", "false", "0")
        for bad in invalid:
            with self.subTest(probe_import=repr(bad)):
                with self.assertRaises(ValueError) as ctx:
                    check_rvc_health({"model_path": "x"}, probe_import=bad)  # type: ignore[arg-type]
                self.assertIn("probe_import must be bool", str(ctx.exception))

    def test_probe_import_false_skips_owned_run(self) -> None:
        execute_calls: list[int] = []

        def boom(*_a, **_k):
            execute_calls.append(1)
            raise AssertionError("unexpected probe spawn")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, py, model = _policy_with_assets(root)
            bind_fake_rvc_runners(policy, popen_fn=_FakePopen)
            cfg = {"model_path": str(model), "python_path": str(py)}
            script = MagicMock()
            script.is_file.return_value = True
            with patch.object(
                owned_rvc_run_module, "execute_owned_rvc_run", side_effect=boom
            ), patch(
                "src.voice.rvc.health_check.is_rvc_runtime_ready",
                return_value=True,
            ), patch(
                "src.voice.rvc.health_check.RVC_INFER_SCRIPT",
                script,
            ):
                report = check_rvc_health(
                    cfg, safety_policy=policy, probe_import=False
                )
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(execute_calls, [])
            self.assertTrue(
                any("跳过 IMPORT_RVC" in w for w in report.warnings),
                report.warnings,
            )

    def test_prepare_worker_path_zero_import_probe_one_warm(self) -> None:
        execute_calls: list[int] = []
        warm_calls: list[int] = []

        def spy_execute(*_a, **_k):
            execute_calls.append(1)
            return MagicMock(returncode=0)

        def fake_warm(*_a, **_k):
            warm_calls.append(1)
            return True

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, py, model = _policy_with_assets(root)
            engine = _bare_engine(worker_enabled=True)
            engine.safety_policy = policy
            engine._rvc_config["model_path"] = str(model)
            engine._rvc_config["python_path"] = str(py)
            bind_fake_rvc_runners(policy, popen_fn=_FakePopen)
            script = MagicMock()
            script.is_file.return_value = True
            with patch.object(
                owned_rvc_run_module,
                "execute_owned_rvc_run",
                side_effect=spy_execute,
            ), patch(
                "src.voice.rvc.health_check.is_rvc_runtime_ready",
                return_value=True,
            ), patch(
                "src.voice.rvc.health_check.RVC_INFER_SCRIPT",
                script,
            ), patch(
                "src.voice.rvc.infer.warm_rvc_worker",
                side_effect=fake_warm,
            ):
                self.assertTrue(engine.prepare_rvc())
            self.assertEqual(execute_calls, [])
            self.assertEqual(warm_calls, [1])
            self.assertTrue(engine._rvc_ready)  # noqa: SLF001

    def test_prepare_worker_warm_fail_not_ready_despite_local_preflight(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, py, model = _policy_with_assets(root)
            engine = _bare_engine(worker_enabled=True)
            engine.safety_policy = policy
            engine._rvc_config["model_path"] = str(model)
            engine._rvc_config["python_path"] = str(py)
            script = MagicMock()
            script.is_file.return_value = True
            with patch(
                "src.voice.rvc.health_check.is_rvc_runtime_ready",
                return_value=True,
            ), patch(
                "src.voice.rvc.health_check.RVC_INFER_SCRIPT",
                script,
            ), patch(
                "src.voice.rvc.infer.warm_rvc_worker",
                return_value=False,
            ):
                self.assertFalse(engine.prepare_rvc())
            self.assertFalse(engine._rvc_ready)  # noqa: SLF001

    def test_prepare_non_worker_still_runs_import_probe(self) -> None:
        execute_calls: list[int] = []

        def spy_execute(*_a, **_k):
            execute_calls.append(1)
            return MagicMock(returncode=0)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, py, model = _policy_with_assets(root)
            engine = _bare_engine(worker_enabled=False)
            engine.safety_policy = policy
            engine._rvc_config["model_path"] = str(model)
            engine._rvc_config["python_path"] = str(py)
            bind_fake_rvc_runners(policy, popen_fn=_FakePopen)
            from src.voice.rvc.infer import ensure_policy_rvc_python

            cfg = dict(engine._rvc_config)
            ensure_policy_rvc_python(policy, cfg)
            script = MagicMock()
            script.is_file.return_value = True
            with patch.object(
                owned_rvc_run_module,
                "execute_owned_rvc_run",
                side_effect=spy_execute,
            ), patch(
                "src.voice.rvc.health_check.is_rvc_runtime_ready",
                return_value=True,
            ), patch(
                "src.voice.rvc.health_check.RVC_INFER_SCRIPT",
                script,
            ):
                self.assertTrue(engine.prepare_rvc())
            self.assertEqual(len(execute_calls), 1)
            self.assertTrue(engine._rvc_ready)  # noqa: SLF001

    def test_prepare_passes_probe_import_false_for_worker(self) -> None:
        seen: list[bool] = []

        def fake_check(*_a, **kwargs):
            seen.append(kwargs.get("probe_import", True))
            return RVCHealthReport(ok=False, errors=["stop"])

        engine = _bare_engine(worker_enabled=True)
        with patch(
            "src.voice.rvc.health_check.check_rvc_health",
            side_effect=fake_check,
        ):
            engine.prepare_rvc()
        self.assertEqual(seen, [False])

    def test_keyboard_interrupt_from_health_not_swallowed(self) -> None:
        engine = _bare_engine(worker_enabled=True)
        with patch(
            "src.voice.rvc.health_check.check_rvc_health",
            side_effect=KeyboardInterrupt("kbi"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                engine.prepare_rvc()
        self.assertFalse(engine._rvc_ready)  # noqa: SLF001

    def test_rvc_ready_false_while_warm_callback_runs(self) -> None:
        observed: list[bool] = []

        def slow_warm(*_a, **_k):
            observed.append(engine._rvc_ready)  # noqa: SLF001
            return True

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, py, model = _policy_with_assets(root)
            engine = _bare_engine(worker_enabled=True)
            engine.safety_policy = policy
            engine._rvc_config["model_path"] = str(model)
            engine._rvc_config["python_path"] = str(py)
            script = MagicMock()
            script.is_file.return_value = True
            with patch(
                "src.voice.rvc.health_check.is_rvc_runtime_ready",
                return_value=True,
            ), patch(
                "src.voice.rvc.health_check.RVC_INFER_SCRIPT",
                script,
            ), patch(
                "src.voice.rvc.infer.warm_rvc_worker",
                side_effect=slow_warm,
            ):
                self.assertTrue(engine.prepare_rvc())
            self.assertEqual(observed, [False])

    def test_warm_exception_sticky_no_second_warm(self) -> None:
        warm_calls: list[int] = []
        err = RuntimeError("warm failed")

        def boom(*_a, **_k):
            warm_calls.append(1)
            raise err

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, py, model = _policy_with_assets(root)
            engine = _bare_engine(worker_enabled=True)
            engine.safety_policy = policy
            engine._rvc_config["model_path"] = str(model)
            engine._rvc_config["python_path"] = str(py)
            script = MagicMock()
            script.is_file.return_value = True
            with patch(
                "src.voice.rvc.health_check.is_rvc_runtime_ready",
                return_value=True,
            ), patch(
                "src.voice.rvc.health_check.RVC_INFER_SCRIPT",
                script,
            ), patch(
                "src.voice.rvc.infer.warm_rvc_worker",
                side_effect=boom,
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    engine.prepare_rvc()
                self.assertIs(ctx.exception, err)
                self.assertFalse(engine._rvc_ready)  # noqa: SLF001
                self.assertFalse(engine.prepare_rvc())
            self.assertEqual(warm_calls, [1])

    def test_warm_kbi_sticky_not_ready(self) -> None:
        warm_calls: list[int] = []
        err = KeyboardInterrupt("kbi")

        def kbi(*_a, **_k):
            warm_calls.append(1)
            raise err

        engine = _bare_engine(worker_enabled=True)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, py, model = _policy_with_assets(root)
            engine.safety_policy = policy
            engine._rvc_config["model_path"] = str(model)
            engine._rvc_config["python_path"] = str(py)
            script = MagicMock()
            script.is_file.return_value = True
            with patch(
                "src.voice.rvc.health_check.is_rvc_runtime_ready",
                return_value=True,
            ), patch(
                "src.voice.rvc.health_check.RVC_INFER_SCRIPT",
                script,
            ), patch(
                "src.voice.rvc.infer.warm_rvc_worker",
                side_effect=kbi,
            ):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    engine.prepare_rvc()
                self.assertIs(ctx.exception, err)
                self.assertFalse(engine._rvc_ready)  # noqa: SLF001
                self.assertFalse(engine.prepare_rvc())
            self.assertEqual(warm_calls, [1])


if __name__ == "__main__":
    unittest.main()
