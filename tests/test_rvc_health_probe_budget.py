"""RVC health/probe 调用预算与 SafetyPolicy 30s cap 对齐（纯 fake owned run）。"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import src.core.owned_rvc_run as owned_rvc_run_module
from src.core.rvc_capabilities import RvcProbeKind
from src.core.safety import SafetyPolicy, TrustedRvcProfile
from src.voice.rvc.health_check import check_rvc_health
from src.voice.rvc.infer import ensure_policy_rvc_python

from tests.support.local_tts_runners import bind_fake_rvc_runners


def _policy_with_assets(tmp: Path) -> tuple[SafetyPolicy, Path, Path]:
    py = tmp / "python.exe"
    model = tmp / "model.pth"
    py.write_bytes(b"")
    model.write_bytes(b"m")
    policy = SafetyPolicy(
        {
            "permissions": {
                "rvc_worker": True,
                "rvc_infer": True,
                "rvc_probe": True,
            }
        },
        trusted_rvc_profile=TrustedRvcProfile(python_path=py, model_path=model),
    )
    return policy, py, model


class _FakePopen:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdin = MagicMock()
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self._code = 0

    def poll(self):
        return self._code

    def terminate(self):
        self._code = 1

    def kill(self):
        self._code = 1

    def wait(self, timeout=None):
        return self._code


@unittest.skipUnless(sys.platform == "win32", "owned RVC probe path")
class RvcHealthProbeBudgetTests(unittest.TestCase):
    def test_health_import_uses_real_policy_probe_within_30s_cap(self) -> None:
        timeouts: list[float] = []
        real_execute = owned_rvc_run_module.execute_owned_rvc_run

        def spy_execute(policy, argv, **kwargs):
            run_kwargs = kwargs
            timeouts.append(float(run_kwargs["timeout"]))
            return real_execute(policy, argv, **run_kwargs)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, py, model = _policy_with_assets(root)
            bind_fake_rvc_runners(policy, popen_fn=_FakePopen)
            cfg = {"model_path": str(model), "python_path": str(py)}
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
                report = check_rvc_health(cfg, safety_policy=policy)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(len(timeouts), 1)
            self.assertLessEqual(timeouts[0], 30.0)

    def test_run_rvc_probe_timeout_over_cap_rejected_zero_spawn(self) -> None:
        popen_calls: list[int] = []

        def boom(*_a, **_k):
            popen_calls.append(1)
            return _FakePopen()

        with TemporaryDirectory() as tmp:
            policy, py, model = _policy_with_assets(Path(tmp))
            bind_fake_rvc_runners(policy, popen_fn=boom)
            cfg = {"model_path": str(model), "python_path": str(py)}
            ensure_policy_rvc_python(policy, cfg)
            with self.assertRaises(PermissionError):
                policy.run_rvc_probe(
                    RvcProbeKind.IMPORT_RVC,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    encoding="utf-8",
                    errors="ignore",
                )
            self.assertEqual(popen_calls, [])


if __name__ == "__main__":
    unittest.main()
