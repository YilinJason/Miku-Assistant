"""Host memory admission for local model cold start (fake WinAPI + wiring)."""
from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.host_memory import (
    COSY_REQUIRED_COMMIT_MB,
    COSY_REQUIRED_PHYSICAL_MB,
    ModelStartupMemoryError,
    ModelStartupMemoryDecision,
    PARAFORMER_REQUIRED_COMMIT_MB,
    PARAFORMER_REQUIRED_PHYSICAL_MB,
    REASON_HOST_MEMORY_COMMIT_INSUFFICIENT,
    REASON_HOST_MEMORY_PHYSICAL_INSUFFICIENT,
    REASON_HOST_MEMORY_UNOBSERVABLE,
    HostMemorySnapshot,
    RVC_REQUIRED_COMMIT_MB,
    RVC_REQUIRED_PHYSICAL_MB,
    check_model_startup_memory,
    performance_information_type,
    read_host_memory_snapshot,
    snapshot_from_performance_information,
)
from src.core.paths import PROJECT_ROOT
from src.core.rvc_capabilities import RvcProbeKind
from src.core.safety import SafetyPolicy, TrustedRvcProfile, safe_subprocess_run
from src.voice.rvc.infer import run_rvc_infer_subprocess
from src.voice.rvc.worker import RVCWorkerClient
from src.voice.tts.engine import TTSEngine

from tests.support.host_memory_fixtures import (
    LOW_COMMIT_HOST_MEMORY,
    LOW_PHYSICAL_HOST_MEMORY,
    SUFFICIENT_HOST_MEMORY,
    patch_sufficient_host_memory,
)
from tests.support.local_tts_runners import bind_fake_rvc_runners
from tests.support.win_performance_information import (
    assert_win64_performance_abi,
    make_get_performance_info_injector,
    snapshot_from_injected_pages,
)
from tests.test_rvc_health_probe_budget import _FakePopen

POLICY = SafetyPolicy({"permissions": {"subprocess": True}})


class HostMemoryUnitTests(unittest.TestCase):
    def test_kind_must_be_rvc_or_cosyvoice(self) -> None:
        with self.assertRaises(ValueError):
            check_model_startup_memory("ollama")

    def test_non_windows_allowed_without_snapshot(self) -> None:
        if sys.platform == "win32":
            self.skipTest("non-Windows branch")
        decision = check_model_startup_memory("rvc")
        self.assertTrue(decision.allowed)

    def test_windows_fail_closed_when_unobservable(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only")
        with patch(
            "src.core.host_memory.read_host_memory_snapshot", return_value=None
        ):
            decision = check_model_startup_memory("rvc")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_HOST_MEMORY_UNOBSERVABLE)

    def test_physical_insufficient(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only")
        snap = HostMemorySnapshot(
            physical_available_mb=RVC_REQUIRED_PHYSICAL_MB - 1,
            commit_available_mb=RVC_REQUIRED_COMMIT_MB,
        )
        with patch("src.core.host_memory.read_host_memory_snapshot", return_value=snap):
            decision = check_model_startup_memory("rvc")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_HOST_MEMORY_PHYSICAL_INSUFFICIENT)

    def test_commit_insufficient(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only")
        snap = HostMemorySnapshot(
            physical_available_mb=COSY_REQUIRED_PHYSICAL_MB,
            commit_available_mb=COSY_REQUIRED_COMMIT_MB - 1,
        )
        with patch("src.core.host_memory.read_host_memory_snapshot", return_value=snap):
            decision = check_model_startup_memory("cosyvoice")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_HOST_MEMORY_COMMIT_INSUFFICIENT)

    def test_sufficient_allows(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only")
        with patch(
            "src.core.host_memory.read_host_memory_snapshot",
            return_value=SUFFICIENT_HOST_MEMORY,
        ):
            self.assertTrue(check_model_startup_memory("rvc").allowed)
            self.assertTrue(check_model_startup_memory("cosyvoice").allowed)


class ParaformerHostMemoryAdmissionTests(unittest.TestCase):
    def test_paraformer_commit_denied_below_6144_mib(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only")
        snap = HostMemorySnapshot(
            physical_available_mb=PARAFORMER_REQUIRED_PHYSICAL_MB,
            commit_available_mb=6143,
        )
        with patch("src.core.host_memory.read_host_memory_snapshot", return_value=snap):
            decision = check_model_startup_memory("paraformer")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_HOST_MEMORY_COMMIT_INSUFFICIENT)
        self.assertEqual(decision.required_commit_mb, PARAFORMER_REQUIRED_COMMIT_MB)
        self.assertEqual(decision.required_physical_mb, PARAFORMER_REQUIRED_PHYSICAL_MB)

    def test_paraformer_admitted_at_6144_commit_with_sufficient_physical(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only")
        snap = HostMemorySnapshot(
            physical_available_mb=PARAFORMER_REQUIRED_PHYSICAL_MB,
            commit_available_mb=PARAFORMER_REQUIRED_COMMIT_MB,
        )
        with patch("src.core.host_memory.read_host_memory_snapshot", return_value=snap):
            decision = check_model_startup_memory("paraformer")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.required_commit_mb, 6144)

    def test_paraformer_physical_floor_4096_unchanged(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only")
        snap = HostMemorySnapshot(
            physical_available_mb=PARAFORMER_REQUIRED_PHYSICAL_MB - 1,
            commit_available_mb=PARAFORMER_REQUIRED_COMMIT_MB + 1024,
        )
        with patch("src.core.host_memory.read_host_memory_snapshot", return_value=snap):
            decision = check_model_startup_memory("paraformer")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_HOST_MEMORY_PHYSICAL_INSUFFICIENT)

    def test_paraformer_unobservable_snapshot_denied(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only")
        with patch("src.core.host_memory.read_host_memory_snapshot", return_value=None):
            decision = check_model_startup_memory("paraformer")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_HOST_MEMORY_UNOBSERVABLE)

    def test_rvc_and_cosy_thresholds_unchanged(self) -> None:
        self.assertEqual(RVC_REQUIRED_PHYSICAL_MB, 4096)
        self.assertEqual(RVC_REQUIRED_COMMIT_MB, 14336)
        self.assertEqual(COSY_REQUIRED_PHYSICAL_MB, 8192)
        self.assertEqual(COSY_REQUIRED_COMMIT_MB, 10240)


@unittest.skipUnless(sys.platform == "win32", "Windows GetPerformanceInfo ABI")
class HostMemoryWinApiTests(unittest.TestCase):
    def test_performance_information_abi_layout(self) -> None:
        assert_win64_performance_abi()
        self.assertIs(
            performance_information_type(),
            performance_information_type(),
        )

    def test_injected_pages_snapshot_math(self) -> None:
        snap = snapshot_from_injected_pages(
            page_size=4096,
            physical_available_pages=2560,
            physical_total_pages=100000,
            commit_limit_pages=5000,
            commit_total_pages=2000,
        )
        assert snap is not None
        self.assertEqual(snap.physical_available_mb, 10)
        self.assertEqual(snap.commit_available_mb, (3000 * 4096) // (1024 * 1024))

    def test_read_host_memory_via_injected_get_performance_info(self) -> None:
        injector = make_get_performance_info_injector(
            page_size=4096,
            physical_available_pages=2560,
            physical_total_pages=100000,
            commit_limit_pages=5000,
            commit_total_pages=2000,
        )
        with patch.object(
            ctypes.windll.psapi, "GetPerformanceInfo", side_effect=injector
        ):
            snap = read_host_memory_snapshot()
        assert snap is not None
        self.assertEqual(snap.physical_available_mb, 10)

    def test_parse_rejects_bool_and_negative(self) -> None:
        class _Info:
            PageSize = True
            CommitLimit = 100
            CommitTotal = 10
            PhysicalAvailable = 50
            PhysicalTotal = 100

        self.assertIsNone(snapshot_from_performance_information(_Info()))
        pi = performance_information_type()
        info = pi()
        info.PageSize = 4096
        info.PhysicalAvailable = 100
        info.PhysicalTotal = 1000
        info.CommitLimit = 10
        info.CommitTotal = 20
        self.assertIsNone(snapshot_from_performance_information(info))

    def test_live_read_observable_on_this_host(self) -> None:
        snap = read_host_memory_snapshot()
        self.assertIsNotNone(snap)
        self.assertGreater(snap.physical_available_mb, 0)
        self.assertGreater(snap.commit_available_mb, 0)


class WiringTests(unittest.TestCase):
    def _policy(self, tmp: Path) -> tuple[SafetyPolicy, Path, Path]:
        py = tmp / "python.exe"
        model = tmp / "m.pth"
        py.write_bytes(b"")
        model.write_bytes(b"x")
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

    def test_rvc_client_construct_raises_without_cuda_probe(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows admission")
        cfg = {"device": "auto", "model_path": "m.pth"}
        with patch(
            "src.core.host_memory.read_host_memory_snapshot",
            return_value=LOW_PHYSICAL_HOST_MEMORY,
        ), patch("src.voice.rvc.worker.resolve_rvc_device") as resolve:
            with self.assertRaises(ModelStartupMemoryError):
                RVCWorkerClient(cfg, safety_policy=_allow_policy())
            resolve.assert_not_called()

    def test_prepare_rvc_catches_construct_memory_error(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows admission")
        engine = TTSEngine.__new__(TTSEngine)
        engine.logger = MagicMock()
        engine._rvc = SimpleNamespace(enabled=True)
        engine._rvc_config = {
            "prewarm_audio_processing": False,
            "worker_enabled": True,
            "model_path": "m.pth",
        }
        engine._rvc_prepare_done = False
        engine._rvc_ready = False
        engine.safety_policy = _allow_policy()
        engine._temp_files = None
        decision = ModelStartupMemoryDecision(
            allowed=False,
            reason=REASON_HOST_MEMORY_PHYSICAL_INSUFFICIENT,
            physical_available_mb=1024,
            commit_available_mb=1024,
            required_physical_mb=RVC_REQUIRED_PHYSICAL_MB,
            required_commit_mb=RVC_REQUIRED_COMMIT_MB,
        )
        err = ModelStartupMemoryError(decision.reason, decision)
        with patch(
            "src.voice.rvc.health_check.check_rvc_health",
            return_value=MagicMock(ok=True),
        ), patch(
            "src.voice.rvc.infer.warm_rvc_worker",
            side_effect=err,
        ):
            self.assertFalse(engine.prepare_rvc())
        self.assertFalse(engine._rvc_ready)  # noqa: SLF001
        self.assertIn("Nahida 未启用", engine._rvc_last_error)

    def test_rvc_ready_reuse_skips_memory_recheck(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows admission")
        with TemporaryDirectory() as tmp:
            policy, _py, model = self._policy(Path(tmp))
            cfg = {"device": "cpu", "model_path": str(model)}
            with patch_sufficient_host_memory():
                client = RVCWorkerClient(cfg, safety_policy=policy)
            proc = MagicMock()
            proc.poll.return_value = None
            client._ready = True  # noqa: SLF001
            client._proc = proc  # noqa: SLF001
            with patch(
                "src.core.host_memory.check_model_startup_memory",
                side_effect=AssertionError("must not recheck when ready"),
            ):
                self.assertTrue(client.start())

    def test_entry_points_zero_spawn_and_zero_cuda_probe(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows admission")
        cases = (
            ("low_physical", LOW_PHYSICAL_HOST_MEMORY),
            ("low_commit", LOW_COMMIT_HOST_MEMORY),
            ("unobservable", None),
        )
        for label, snapshot in cases:
            with self.subTest(case=label):
                spawn_calls: list[int] = []
                probe_calls: list[int] = []

                def spawn_boom(*_a, **_k):
                    spawn_calls.append(1)
                    raise AssertionError("spawn")

                def probe_boom(*_a, **_k):
                    probe_calls.append(1)
                    raise AssertionError("cuda probe")

                with TemporaryDirectory() as tmp:
                    policy, py, model = self._policy(Path(tmp))
                    cfg = {
                        "device": "cpu",
                        "model_path": str(model),
                        "python_path": str(py),
                    }
                    bind_fake_rvc_runners(policy, popen_fn=_FakePopen())
                    with patch_sufficient_host_memory():
                        client = RVCWorkerClient(cfg, safety_policy=policy)
                    policy.spawn_rvc_worker = spawn_boom  # type: ignore[method-assign]

                    with patch(
                        "src.core.host_memory.read_host_memory_snapshot",
                        return_value=snapshot,
                    ):
                        self.assertFalse(client.start())

                        from src.voice.rvc.infer import ensure_policy_rvc_python

                        ensure_policy_rvc_python(policy, cfg)
                        policy.run_rvc_probe = probe_boom  # type: ignore[method-assign]
                        inp = Path(tmp) / "in.wav"
                        out = Path(tmp) / "out.wav"
                        inp.write_bytes(b"x")
                        self.assertFalse(
                            run_rvc_infer_subprocess(
                                str(inp),
                                str(out),
                                cfg,
                                safety_policy=policy,
                                temp_file_manager=object(),
                            )
                        )

                        from src.voice.cosyvoice import client as cosy_mod
                        from src.voice.cosyvoice.client import CosyVoiceWorkerClient
                        from src.voice.cosyvoice.settings import CosyVoiceSettings

                        settings = CosyVoiceSettings(
                            startup_timeout_sec=5,
                            synth_timeout_sec=10,
                            idle_timeout_sec=60,
                            max_text_chars=240,
                            cpu_threads=4,
                            min_free_ram_mb=8192,
                            min_free_vram_mb=4096,
                            cuda_memory_limit_mb=4096,
                        )
                        cosy_policy = SafetyPolicy(
                            {"permissions": {"local_tts_worker": True}}
                        )
                        cosy_policy.spawn_local_tts_worker = spawn_boom  # type: ignore[method-assign]
                        with patch.object(cosy_mod, "_ACTIVE_CLIENT", None):
                            cosy = CosyVoiceWorkerClient(
                                settings, cosy_policy, MagicMock()
                            )
                            self.assertFalse(
                                cosy._start_worker_poll(0, None)  # noqa: SLF001
                            )

                self.assertEqual(spawn_calls, [], label)
                self.assertEqual(probe_calls, [], label)


def _allow_policy() -> SafetyPolicy:
    return SafetyPolicy({"permissions": {"rvc_worker": True}})


class ImportSentinelTests(unittest.TestCase):
    def test_host_memory_import_without_heavy_deps(self) -> None:
        code = f"""
import importlib.abc, sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
blocked = []
BLOCK = ("src.voice", "numpy", "scipy", "torch", "soundfile")
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in BLOCK or any(
            fullname.startswith(p + ".") for p in BLOCK if p != "src.voice"
        ) or fullname == "src.voice" or fullname.startswith("src.voice."):
            blocked.append(fullname)
            raise ImportError("blocked:" + fullname)
        return None
sys.meta_path.insert(0, Block())
import src.core.host_memory as hm
assert blocked == []
assert hm.read_host_memory_snapshot() is None or hm.RVC_REQUIRED_PHYSICAL_MB == 4096
print("ok")
"""
        result = safe_subprocess_run(
            POLICY,
            [sys.executable, "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
