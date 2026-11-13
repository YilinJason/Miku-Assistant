"""RVC 子进程 CPU/BLAS/Torch 线程预算——纯 offline fake 行为测试。"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.rvc_capabilities import (
    RvcInferLaunch,
    RvcProbeKind,
    RvcWorkerLaunch,
    build_probe_argv,
)
from src.core.safety import Operation, SafetyPolicy, TrustedRvcProfile
from src.voice.audio.temp_files import TempFileManager
from src.voice.rvc.cpu_budget import (
    apply_rvc_torch_thread_budget,
    child_env_with_rvc_cpu_budget,
    compute_rvc_cpu_thread_budget,
)

from tests.support.fake_owned_worker_job_backend import FakeOwnedWorkerJobBackend
from tests.support.local_tts_runners import bind_fake_rvc_runners


def _assert_tfm_quiet(mgr: TempFileManager, timeout: float = 2.0) -> None:
    status = mgr.shutdown(timeout=timeout)
    if not getattr(status, "complete", False):
        raise AssertionError("TempFileManager shutdown incomplete")
    if mgr.delayed_worker_alive():
        raise AssertionError("TFM delayed worker still alive")
    if mgr.owned_count() != 0:
        raise AssertionError("TFM still owns paths after shutdown")


def _assert_fake_job_quiet(backend: FakeOwnedWorkerJobBackend) -> None:
    for handle in list(backend._jobs.keys()):
        active = backend.query_active_process_count(handle)
        if active not in (0, None):
            raise AssertionError(f"fake owned job still active: handle={handle} count={active}")
        redirector = backend._redirector_for_job.get(handle)
        if redirector is not None and redirector.poll() is None:
            raise AssertionError(f"fake owned redirector still running: handle={handle}")


def _retire_fake_owned(
    policy: SafetyPolicy,
    backend: FakeOwnedWorkerJobBackend,
    procs: list,
    *,
    timeout: float = 2.0,
) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=timeout)
        if proc.poll() is None:
            raise AssertionError("fake owned process did not exit after retire")
    procs.clear()
    if not policy.retire_rvc_runs(timeout=timeout):
        raise AssertionError("policy retire_rvc_runs incomplete")
    if not policy.rvc_runs_quiescent():
        raise AssertionError("policy rvc_runs not quiescent")
    _assert_fake_job_quiet(backend)


def _cleanup_spawn_fixture(
    policy: SafetyPolicy,
    backend: FakeOwnedWorkerJobBackend,
    procs: list,
    mgr: TempFileManager | None,
    *,
    timeout: float = 2.0,
) -> None:
    errors: list[BaseException] = []
    try:
        _retire_fake_owned(policy, backend, procs, timeout=timeout)
    except BaseException as exc:
        errors.append(exc)
    if mgr is not None:
        try:
            _assert_tfm_quiet(mgr, timeout=timeout)
        except BaseException as exc:
            errors.append(exc)
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise AssertionError(f"spawn fixture cleanup failed: {errors!r}") from errors[0]


def _make_policy(tmp: Path) -> tuple[SafetyPolicy, Path, Path]:
    python = tmp / "python.exe"
    python.write_bytes(b"")
    model = tmp / "model.pth"
    model.write_bytes(b"m")
    policy = SafetyPolicy(
        {
            "permissions": {
                "subprocess": False,
                "file_delete": True,
                "rvc_worker": True,
                "rvc_infer": True,
                "rvc_probe": True,
            }
        },
        trusted_rvc_profile=TrustedRvcProfile(python_path=python, model_path=model),
    )
    return policy, python, model


class RvcCpuBudgetHelperTests(unittest.TestCase):
    def test_compute_budget_bounded_and_cpu_count_none_fallback(self) -> None:
        self.assertEqual(compute_rvc_cpu_thread_budget(cpu_count=1), 1)
        self.assertEqual(compute_rvc_cpu_thread_budget(cpu_count=2), 1)
        self.assertEqual(compute_rvc_cpu_thread_budget(cpu_count=4), 2)
        self.assertEqual(compute_rvc_cpu_thread_budget(cpu_count=64), 4)
        with patch("src.voice.rvc.cpu_budget.os.cpu_count", return_value=None):
            self.assertEqual(compute_rvc_cpu_thread_budget(cpu_count=None), 1)
            self.assertEqual(compute_rvc_cpu_thread_budget(), 1)

    def test_child_env_overwrites_huge_values_without_mutating_parent(self) -> None:
        base = {
            "OMP_NUM_THREADS": "999",
            "MKL_NUM_THREADS": "888",
            "OPENBLAS_NUM_THREADS": "777",
            "NUMEXPR_NUM_THREADS": "666",
            "PATH": "keep-me",
        }
        before = dict(os.environ)
        child = child_env_with_rvc_cpu_budget(base, cpu_count=4)
        self.assertEqual(child["PATH"], "keep-me")
        n = compute_rvc_cpu_thread_budget(cpu_count=4)
        for key in (
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        ):
            self.assertEqual(child[key], str(n))
            self.assertGreaterEqual(n, 1)
            self.assertLessEqual(n, 4)
        self.assertEqual(base["OMP_NUM_THREADS"], "999")
        self.assertEqual(os.environ, before)

    def test_child_env_removes_case_variant_blas_keys(self) -> None:
        base = {
            "omp_num_threads": "999",
            "Omp_Num_Threads": "888",
            "PATH": "keep-me",
        }
        before = dict(os.environ)
        child = child_env_with_rvc_cpu_budget(base, cpu_count=4)
        n = str(compute_rvc_cpu_thread_budget(cpu_count=4))
        self.assertEqual(child["OMP_NUM_THREADS"], n)
        self.assertEqual(child["PATH"], "keep-me")
        self.assertFalse(any(k.casefold() == "omp_num_threads" and k != "OMP_NUM_THREADS" for k in child))
        self.assertEqual(os.environ, before)

    def test_apply_torch_budget_once_env_unchanged_and_fails_closed(self) -> None:
        calls: list[tuple[str, int]] = []
        env_before = dict(os.environ)

        fake_torch = SimpleNamespace(
            set_num_threads=lambda n: calls.append(("threads", n)),
            set_num_interop_threads=lambda n: calls.append(("interop", n)),
        )
        with patch.dict(sys.modules, {"torch": fake_torch}):
            n = apply_rvc_torch_thread_budget(cpu_count=8)
        self.assertEqual(n, 4)
        self.assertEqual(calls, [("threads", 4), ("interop", 1)])
        self.assertEqual(os.environ, env_before)

        def _boom(_n: int) -> None:
            raise RuntimeError("interop already set")

        fake_torch2 = SimpleNamespace(
            set_num_threads=lambda n: None,
            set_num_interop_threads=_boom,
        )
        with patch.dict(sys.modules, {"torch": fake_torch2}):
            with self.assertRaises(SystemExit) as ctx:
                apply_rvc_torch_thread_budget(cpu_count=2)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(os.environ, env_before)


class RvcCpuBudgetSpawnEnvTests(unittest.TestCase):
    def _worker_launch(self, model: Path) -> RvcWorkerLaunch:
        return RvcWorkerLaunch(
            model_path=str(model),
            device="cpu",
            pitch=0,
            index_rate=0.55,
            protect=0.5,
            filter_radius=3,
            rms_mix_rate=0.75,
            f0_method="rmvpe",
            parent_pid=os.getpid(),
        )

    def _infer_launch(
        self, model: Path, owned_in: str, owned_out: str
    ) -> RvcInferLaunch:
        return RvcInferLaunch(
            input_path=owned_in,
            output_path=owned_out,
            model_path=str(model),
            device="cpu",
            pitch=0,
            index_rate=0.55,
            protect=0.5,
            filter_radius=3,
            rms_mix_rate=0.75,
            f0_method="rmvpe",
        )

    def test_three_paths_inject_blas_env_on_spawn(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, _python, model = _make_policy(root)
            backend = FakeOwnedWorkerJobBackend()
            pops: list[tuple[list, dict]] = []
            procs: list = []
            mgr: TempFileManager | None = None
            try:
                def fake_popen(argv, **kwargs):
                    pops.append((list(argv), dict(kwargs)))
                    redirector = MagicMock()
                    redirector.stdin = MagicMock()
                    redirector.stdout = io.BytesIO(b"")
                    redirector.stderr = io.BytesIO(b"")
                    redirector.poll = MagicMock(return_value=0)
                    redirector.terminate = MagicMock(
                        side_effect=lambda: setattr(redirector, "_code", 0)
                    )
                    redirector.wait = MagicMock(return_value=0)
                    procs.append(redirector)
                    handle = backend.last_created_handle
                    if handle is not None:
                        backend.attach_redirector(handle, redirector)
                    return redirector

                bind_fake_rvc_runners(
                    policy,
                    popen_fn=fake_popen,
                    owned_worker_job_backend=backend,
                )

                parent_before = dict(os.environ)
                with patch.dict(
                    os.environ,
                    {"OMP_NUM_THREADS": "64", "MKL_NUM_THREADS": "64"},
                    clear=False,
                ):
                    mgr = TempFileManager(safety_policy=policy, default_dir=root)
                    owned_in = mgr.create(".wav")
                    owned_out = mgr.create(".wav")
                    worker_proc = policy.spawn_rvc_worker(
                        self._worker_launch(model), stdin=subprocess.PIPE
                    )
                    procs.append(worker_proc)
                    policy.run_rvc_infer(
                        self._infer_launch(model, owned_in, owned_out),
                        temp_file_manager=mgr,
                        capture_output=True,
                    )
                    policy.run_rvc_probe(
                        RvcProbeKind.CUDA_AVAILABLE, capture_output=True
                    )

                self.assertEqual(
                    os.environ.get("OMP_NUM_THREADS"),
                    parent_before.get("OMP_NUM_THREADS"),
                )
                self.assertEqual(len(pops), 3)
                expected_n = str(compute_rvc_cpu_thread_budget())
                for _argv, kw in pops:
                    env = kw.get("env")
                    self.assertIsInstance(env, dict)
                    self.assertEqual(env["OMP_NUM_THREADS"], expected_n)
                    self.assertEqual(env["MKL_NUM_THREADS"], expected_n)
                    self.assertEqual(env["OPENBLAS_NUM_THREADS"], expected_n)
                    self.assertEqual(env["NUMEXPR_NUM_THREADS"], expected_n)
            finally:
                _cleanup_spawn_fixture(policy, backend, procs, mgr)

    def test_reject_external_env_and_cwd_zero_spawn(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, _python, model = _make_policy(root)
            called = {"n": 0}

            def boom(*_a, **_k):
                called["n"] += 1
                raise AssertionError("must not spawn")

            policy.bind_process_runners(popen_fn=boom, run_fn=boom)
            mgr: TempFileManager | None = None
            try:
                mgr = TempFileManager(safety_policy=policy, default_dir=root)
                owned_in = mgr.create(".wav")
                owned_out = mgr.create(".wav")
                marker = "rvc_cpu_budget_gate_marker"
                with patch.dict(os.environ, {marker: "1"}, clear=False):
                    before = os.environ.get(marker)
                    with self.assertRaises(PermissionError):
                        policy.run_rvc_infer(
                            self._infer_launch(model, owned_in, owned_out),
                            temp_file_manager=mgr,
                            env={"EVIL": "1"},
                        )
                    with self.assertRaises(PermissionError):
                        policy.run_rvc_infer(
                            self._infer_launch(model, owned_in, owned_out),
                            temp_file_manager=mgr,
                            cwd=str(root),
                        )
                    self.assertEqual(os.environ.get(marker), before)
                self.assertEqual(called["n"], 0)
            finally:
                if mgr is not None:
                    _assert_tfm_quiet(mgr)

    def test_non_windows_run_injects_child_env_internally(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, _python, model = _make_policy(root)
            runs: list[tuple[list, dict]] = []

            def fake_run(argv, **kwargs):
                runs.append((list(argv), dict(kwargs)))
                return subprocess.CompletedProcess(argv, 0)

            policy.bind_process_runners(run_fn=fake_run, popen_fn=fake_run)
            mgr: TempFileManager | None = None
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            owned_in = mgr.create(".wav")
            owned_out = mgr.create(".wav")
            try:
                with patch("src.core.safety.sys.platform", "linux"):
                    policy.run_rvc_infer(
                        self._infer_launch(model, owned_in, owned_out),
                        temp_file_manager=mgr,
                        capture_output=True,
                    )
                self.assertEqual(len(runs), 1)
                env = runs[0][1].get("env")
                self.assertIsInstance(env, dict)
                n = str(compute_rvc_cpu_thread_budget())
                self.assertEqual(env["OMP_NUM_THREADS"], n)
            finally:
                _assert_tfm_quiet(mgr)


class RvcCpuBudgetScriptOrderTests(unittest.TestCase):
    def test_load_vc_applies_budget_before_patch_and_vc(self) -> None:
        from scripts import rvc_worker

        order: list[str] = []

        def track_apply(**_kw):
            order.append("apply")
            return 2

        def track_patch():
            order.append("patch")

        class FakeVC:
            def __init__(self, lib_dir, config):
                order.append("vc_init")

            def get_vc(self, model_path, version):
                order.append("get_vc")

        class FakeConfig:
            def __init__(self, lib_dir, device):
                order.append("config")

        with patch(
            "src.voice.rvc.local_assets.ensure_rvc_python_local_assets",
            return_value=MagicMock(lib_dir="/fake/lib"),
        ), patch(
            "src.voice.rvc.cpu_budget.apply_rvc_torch_thread_budget",
            side_effect=track_apply,
        ), patch.object(
            rvc_worker, "_patch_torch_load_for_fairseq", side_effect=track_patch
        ), patch.dict(
            "sys.modules",
            {
                "rvc_python.configs.config": MagicMock(Config=FakeConfig),
                "rvc_python.modules.vc.modules": MagicMock(VC=FakeVC),
            },
        ):
            rvc_worker._load_vc("m.pth", "cpu")
        self.assertEqual(order[:3], ["apply", "patch", "config"])
        self.assertIn("get_vc", order)

    def test_infer_main_applies_budget_before_patch_and_model_load(self) -> None:
        from scripts import rvc_infer

        order: list[str] = []

        def track_apply(**_kw):
            order.append("apply")
            return 2

        patch_mock = MagicMock(side_effect=lambda: order.append("patch"))

        class FakeVC:
            tgt_sr = 24000

            def __init__(self, lib_dir, config):
                order.append("vc_init")

            def get_vc(self, model_path, version):
                order.append("get_vc")

            def vc_single(self, **kwargs):
                order.append("infer")
                return None, [0]

        class FakeConfig:
            def __init__(self, lib_dir, device):
                order.append("config")

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            model_path = tmp_path / "m.pth"
            model_path.write_bytes(b"x")
            input_path = tmp_path / "in.wav"
            input_path.write_bytes(b"x")
            output_path = tmp_path / "out.wav"
            argv = [
                "rvc_infer.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--model",
                str(model_path),
            ]
            fake_wav = MagicMock()
            with patch.object(sys, "argv", argv), patch.object(
                rvc_infer, "_early_join_owned_job"
            ), patch(
                "src.voice.rvc.local_assets.ensure_rvc_python_local_assets",
                return_value=MagicMock(lib_dir="/fake/lib"),
            ), patch(
                "src.voice.rvc.cpu_budget.apply_rvc_torch_thread_budget",
                side_effect=track_apply,
            ), patch.object(
                rvc_infer, "_patch_torch_load_for_fairseq", patch_mock
            ), patch.dict(
                "sys.modules",
                {
                    "rvc_python.configs.config": MagicMock(Config=FakeConfig),
                    "rvc_python.modules.vc.modules": MagicMock(VC=FakeVC),
                    "scipy.io": MagicMock(wavfile=fake_wav),
                },
            ):
                rc = rvc_infer.main()
            self.assertEqual(rc, 0)
            self.assertEqual(order[0], "apply")
            self.assertEqual(order[1], "patch")
            self.assertIn("config", order)
            self.assertIn("get_vc", order)

    def test_budget_failure_skips_patch_and_model_load(self) -> None:
        from scripts import rvc_worker

        patch_mock = MagicMock()

        def fail_apply(**_kw):
            raise SystemExit(1)

        with patch(
            "src.voice.rvc.local_assets.ensure_rvc_python_local_assets",
            return_value=MagicMock(lib_dir="/fake/lib"),
        ), patch(
            "src.voice.rvc.cpu_budget.apply_rvc_torch_thread_budget",
            side_effect=fail_apply,
        ), patch.object(rvc_worker, "_patch_torch_load_for_fairseq", patch_mock):
            with self.assertRaises(SystemExit) as ctx:
                rvc_worker._load_vc("m.pth", "cpu")
        self.assertEqual(ctx.exception.code, 1)
        patch_mock.assert_not_called()

    def test_infer_budget_failure_skips_patch(self) -> None:
        from scripts import rvc_infer

        patch_mock = MagicMock()
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            model_path = tmp_path / "m.pth"
            model_path.write_bytes(b"x")
            input_path = tmp_path / "in.wav"
            input_path.write_bytes(b"x")
            output_path = tmp_path / "out.wav"
            argv = [
                "rvc_infer.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--model",
                str(model_path),
            ]
            with patch.object(sys, "argv", argv), patch.object(
                rvc_infer, "_early_join_owned_job"
            ), patch(
                "src.voice.rvc.local_assets.ensure_rvc_python_local_assets",
                return_value=MagicMock(lib_dir="/fake/lib"),
            ), patch(
                "src.voice.rvc.cpu_budget.apply_rvc_torch_thread_budget",
                side_effect=lambda **_k: (_ for _ in ()).throw(SystemExit(1)),
            ), patch.object(rvc_infer, "_patch_torch_load_for_fairseq", patch_mock):
                with self.assertRaises(SystemExit) as ctx:
                    rvc_infer.main()
            self.assertEqual(ctx.exception.code, 1)
            patch_mock.assert_not_called()


class RvcCpuBudgetProbeTests(unittest.TestCase):
    def test_fixed_probe_runs_budget_before_torch_import(self) -> None:
        order: list[str] = []
        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: order.append("cuda") or True),
            set_num_threads=lambda n: order.append(f"threads:{n}"),
            set_num_interop_threads=lambda n: order.append(f"interop:{n}"),
        )

        def track_apply(**_kw):
            order.append("apply")
            return 2

        code = build_probe_argv(Path("x"), RvcProbeKind.CUDA_AVAILABLE)[2]
        g: dict = {"__name__": "__main__"}
        with patch(
            "src.core.owned_worker_job.worker_entry_join_owned_job"
        ), patch(
            "src.voice.rvc.cpu_budget.apply_rvc_torch_thread_budget",
            side_effect=track_apply,
        ), patch.dict(sys.modules, {"torch": fake_torch}):
            with self.assertRaises(SystemExit) as ctx:
                exec(code, g)
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(order[0], "apply")
        self.assertIn("cuda", order)
        self.assertLess(order.index("apply"), order.index("cuda"))

    def test_probe_budget_failure_exits_before_rvc_python(self) -> None:
        order: list[str] = []

        class BlockRvc:
            def find_module(self, name: str, path=None):
                if name == "rvc_python" or name.startswith("rvc_python."):
                    order.append("rvc_import")
                    raise ImportError("blocked")
                return None

        code = build_probe_argv(Path("x"), RvcProbeKind.IMPORT_RVC)[2]
        sys.meta_path.insert(0, BlockRvc())
        try:
            with patch(
                "src.core.owned_worker_job.worker_entry_join_owned_job"
            ), patch(
                "src.voice.rvc.cpu_budget.apply_rvc_torch_thread_budget",
                side_effect=lambda **_k: (_ for _ in ()).throw(SystemExit(1)),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    exec(code, {"__name__": "__main__"})
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(order, [])
        finally:
            sys.meta_path.pop(0)


if __name__ == "__main__":
    unittest.main()
