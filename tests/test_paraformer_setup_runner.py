# -*- coding: utf-8 -*-
"""Paraformer setup：真实 safe_subprocess_popen 边界 + fake owned 后端（无真实安装）。"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.core.owned_worker_job import (
    JOB_ENV_NAME,
    JOB_ENV_PARENT_PID,
    spawn_owned_local_tts_worker,
)
from src.core.safety import SafetyPolicy, safe_subprocess_popen, safe_subprocess_run
from src.voice.paraformer import preparation as prep
from src.voice.paraformer.assets import ASR_PYTHON_REL, fixed_layout_paths
from tests.support.fake_owned_worker_job_backend import FakeOwnedWorkerJobBackend


class _FakeRedirector:
    def __init__(self) -> None:
        self.stdin = None
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.pid = 4242
        self.args: list[str] = []
        self._rc: int | None = None
        self.kill_called = False

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        if self._rc is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self._rc

    def terminate(self) -> None:
        self._rc = 1

    def kill(self) -> None:
        self.kill_called = True
        self._rc = 1

    def exit_now(self, code: int = 0) -> None:
        self._rc = code


def _seed_minimal_repo(root: Path) -> None:
    src = _REPO / "scripts" / "paraformer_asr_setup_worker.py"
    dest = root / prep.SETUP_WORKER_SCRIPT_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "requirements-asr.txt").write_text(
        "torch==2.6.0+cpu\nfunasr==1.4.22\n", encoding="utf-8"
    )
    (root / "tmp").mkdir(exist_ok=True)


def _path_without_repo_root(*, script: Path, repo_root: Path) -> list[str]:
    """模拟绝对路径启动：scripts 在 path[0]，保留 stdlib/site，排除仓库根。"""
    scripts = str(script.parent.resolve())
    repo = repo_root.resolve()
    rebuilt: list[str] = [scripts]
    for entry in sys.path:
        if entry == "":
            if "" not in rebuilt:
                rebuilt.append("")
            continue
        try:
            resolved = Path(entry).resolve()
        except OSError:
            if entry not in rebuilt:
                rebuilt.append(entry)
            continue
        if resolved == repo or repo in resolved.parents:
            continue
        if entry not in rebuilt:
            rebuilt.append(entry)
    return rebuilt


def _repo_root_on_sys_path(repo_root: Path) -> bool:
    target = repo_root.resolve()
    for entry in sys.path:
        if not entry:
            continue
        try:
            if Path(entry).resolve() == target:
                return True
        except OSError:
            continue
    return False


def _bind_recording_popen(
    policy: SafetyPolicy,
    *,
    backend: FakeOwnedWorkerJobBackend,
    redirector: _FakeRedirector,
    recorded: list[dict],
) -> None:
    def leaf_popen(args, **kwargs):
        redirector.args = list(args)
        recorded.append(dict(kwargs))
        out = kwargs.get("stdout")
        if out is not None and hasattr(out, "write"):
            out.write(b"funasr==1.4.22\n")
            out.flush()
        err = kwargs.get("stderr")
        if err is not None and hasattr(err, "write"):
            err.write(b"")
            err.flush()
        return redirector

    def owned_popen(args, **kwargs):
        if sys.platform == "win32":
            return spawn_owned_local_tts_worker(
                list(args),
                kwargs,
                popen_fn=leaf_popen,
                parent_pid=os.getpid(),
                job_backend=backend,
            )
        return leaf_popen(args, **kwargs)

    policy.bind_process_runners(
        popen_fn=owned_popen,
        owned_worker_job_backend=backend,
    )


class SetupWorkerBootstrapTests(unittest.TestCase):
    def test_repo_root_inserted_before_src_import_on_scripts_only_path(self) -> None:
        script = _REPO / "scripts" / "paraformer_asr_setup_worker.py"
        module_name = "paraformer_asr_setup_worker_bootstrap_isolated"
        saved_path = sys.path[:]
        try:
            sys.path[:] = _path_without_repo_root(script=script, repo_root=_REPO)
            self.assertFalse(_repo_root_on_sys_path(_REPO))
            spec = importlib.util.spec_from_file_location(module_name, script)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertTrue(_repo_root_on_sys_path(_REPO))
            self.assertEqual(mod._REPO_ROOT.resolve(), _REPO.resolve())
            with patch.object(sys, "platform", "win32"), patch(
                "src.core.owned_worker_job.worker_entry_join_owned_job"
            ) as join:
                mod._join_owned_job_before_work()
            join.assert_called_once()
        finally:
            sys.path[:] = saved_path
            sys.modules.pop(module_name, None)


class SetupArgvTests(unittest.TestCase):
    def test_venv_create_uses_main_interpreter(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_repo(root)
            layout = fixed_layout_paths(root)
            argv = prep.build_setup_worker_argv(
                "venv_create", repo_root=root, layout=layout
            )
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[-2:], ["--mode", "venv_create"])

    def test_pip_modes_use_asr_venv_interpreter(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_repo(root)
            layout = fixed_layout_paths(root)
            for mode in ("pip_install", "env_verify", "pip_freeze"):
                argv = prep.build_setup_worker_argv(
                    mode, repo_root=root, layout=layout
                )
                self.assertTrue(
                    argv[0].replace("\\", "/").endswith(ASR_PYTHON_REL.replace("\\", "/"))
                )
                self.assertEqual(argv[-2:], ["--mode", mode])


class SetupPolicyBoundaryTests(unittest.TestCase):
    def test_safe_subprocess_run_rejects_cwd_and_env(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_repo(root)
            policy = prep.build_setup_safety_policy(root)
            with self.assertRaises(PermissionError) as ctx:
                safe_subprocess_run(
                    policy,
                    [sys.executable, "-c", "pass"],
                    cwd=str(root),
                    env={"PYTHONUTF8": "1"},
                )
            self.assertIn("env", str(ctx.exception))

    def test_spawn_setup_uses_safe_subprocess_popen(self) -> None:
        recorded: list[tuple] = []
        redirector = _FakeRedirector()
        redirector.exit_now(0)

        def fake_safe_popen(policy, argv, *, context="", **kwargs):
            recorded.append((list(argv), context, dict(kwargs)))
            return redirector

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_repo(root)
            policy = prep.build_setup_safety_policy(root)
            layout = fixed_layout_paths(root)
            stdout_path, stderr_path = prep._setup_log_paths(root, context="probe")
            with patch.object(prep, "safe_subprocess_popen", fake_safe_popen):
                with stdout_path.open("xb") as stdout_fh, stderr_path.open(
                    "xb"
                ) as stderr_fh:
                    proc = prep._spawn_setup_popen(
                        policy,
                        ["py", "script", "--mode", "x"],
                        context="probe_ctx",
                        cwd=root,
                        stdout=stdout_fh,
                        stderr=stderr_fh,
                    )
            self.assertIs(proc, redirector)
            self.assertEqual(recorded[0][1], "probe_ctx")
            self.assertIn("cwd", recorded[0][2])
            self.assertIn("env", recorded[0][2])

    def test_setup_runner_passes_cwd_env_and_job_through_policy(self) -> None:
        recorded: list[dict] = []
        redirector = _FakeRedirector()
        redirector.exit_now(0)
        backend = FakeOwnedWorkerJobBackend()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_repo(root)
            policy = prep.build_setup_safety_policy(root)
            _bind_recording_popen(
                policy, backend=backend, redirector=redirector, recorded=recorded
            )
            layout = fixed_layout_paths(root)
            with patch.object(prep.sys, "platform", "win32"):
                text = prep._run_setup_command(
                    policy,
                    "pip_freeze",
                    root,
                    layout,
                    timeout=5.0,
                    context="test_pip_freeze",
                    capture_stdout=True,
                )
            self.assertIn("funasr==1.4.22", text)
            self.assertEqual(len(recorded), 1)
            popen_kw = recorded[0]
            self.assertIn("cwd", popen_kw)
            self.assertIn("env", popen_kw)
            self.assertEqual(popen_kw["env"].get("PYTHONNOUSERSITE"), "1")
            self.assertIn(JOB_ENV_NAME, popen_kw["env"])
            self.assertIn(JOB_ENV_PARENT_PID, popen_kw["env"])
            logs = list((root / "tmp").glob("asr_setup_test_pip_freeze_*.stdout.log"))
            self.assertEqual(len(logs), 1)
            self.assertTrue(logs[0].is_file())

    def test_setup_runner_propagates_nonzero_exit(self) -> None:
        redirector = _FakeRedirector()
        redirector.exit_now(9)
        backend = FakeOwnedWorkerJobBackend()

        def popen_fn(args, **kwargs):
            return redirector

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_repo(root)
            policy = prep.build_setup_safety_policy(root)
            policy.bind_process_runners(
                popen_fn=popen_fn, owned_worker_job_backend=backend
            )
            layout = fixed_layout_paths(root)
            with patch.object(prep.sys, "platform", "linux"):
                with self.assertRaises(RuntimeError) as ctx:
                    prep._run_setup_command(
                        policy,
                        "venv_create",
                        root,
                        layout,
                        timeout=5.0,
                        context="test_venv",
                    )
        self.assertIn("test_venv_failed:9", str(ctx.exception))

    def test_setup_runner_timeout_kills_and_raises(self) -> None:
        redirector = _FakeRedirector()
        backend = FakeOwnedWorkerJobBackend()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_repo(root)
            policy = prep.build_setup_safety_policy(root)
            _bind_recording_popen(
                policy, backend=backend, redirector=redirector, recorded=[]
            )
            layout = fixed_layout_paths(root)
            with patch.object(prep.sys, "platform", "win32"):
                with self.assertRaises(TimeoutError) as ctx:
                    prep._run_setup_command(
                        policy,
                        "pip_install",
                        root,
                        layout,
                        timeout=0.05,
                        context="test_timeout",
                    )
        self.assertIn("test_timeout", str(ctx.exception))
        self.assertTrue(redirector.kill_called)

    def test_setup_runner_rejects_oversized_logs_during_poll(self) -> None:
        redirector = _FakeRedirector()
        backend = FakeOwnedWorkerJobBackend()

        def slow_popen(args, **kwargs):
            out = kwargs.get("stdout")
            if out is not None and hasattr(out, "write"):
                out.write(b"x" * (prep.MAX_SETUP_CAPTURE_BYTES + 1))
                out.flush()
            return redirector

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_repo(root)
            policy = prep.build_setup_safety_policy(root)
            policy.bind_process_runners(
                popen_fn=slow_popen, owned_worker_job_backend=backend
            )
            layout = fixed_layout_paths(root)
            with patch.object(prep.sys, "platform", "linux"):
                with self.assertRaises(RuntimeError) as ctx:
                    prep._run_setup_command(
                        policy,
                        "pip_install",
                        root,
                        layout,
                        timeout=2.0,
                        context="test_oversize",
                    )
        self.assertIn("test_oversize_log_too_large", str(ctx.exception))
        self.assertTrue(redirector.kill_called)


if __name__ == "__main__":
    unittest.main()
