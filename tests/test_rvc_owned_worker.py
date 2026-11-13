"""RVC persistent worker Windows Job 归属与 reader/stop 契约（纯 fake）。"""
from __future__ import annotations

import io
import os
import queue
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core.owned_worker_job import (
    JOB_ENV_NAME,
    JOB_ENV_PARENT_PID,
    OwnedWorkerProcess,
    _JobResources,
    attach_owned_worker_to_exception,
)
from src.core.rvc_capabilities import RvcWorkerLaunch, build_worker_argv
from src.core.safety import SafetyPolicy, TrustedRvcProfile
from src.voice.rvc.worker import RVCWorkerClient, _OwnerBinding, _RVCStdoutReader

from tests.support.fake_owned_worker_job_backend import FakeOwnedWorkerJobBackend
from tests.support.local_tts_runners import bind_fake_rvc_runners
from tests.support.host_memory_fixtures import install_module_sufficient_host_memory


def setUpModule() -> None:
    install_module_sufficient_host_memory()


def _allow_rvc_policy(tmp: Path) -> tuple[SafetyPolicy, Path, Path]:
    python = tmp / "python.exe"
    python.write_bytes(b"")
    model = tmp / "voice.pth"
    model.write_bytes(b"m")
    policy = SafetyPolicy(
        {
            "permissions": {
                "rvc_worker": True,
                "rvc_infer": True,
                "rvc_probe": True,
                "subprocess": False,
            }
        },
        trusted_rvc_profile=TrustedRvcProfile(
            python_path=python, model_path=model
        ),
    )
    return policy, python, model


class _FakeRedirector:
    def __init__(self, *, stdout: io.StringIO | None = None) -> None:
        self.stdin = io.StringIO()
        self.stdout = stdout or io.StringIO()
        self.stderr = io.StringIO()
        self._code: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        if self._code is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        self._code = 0

    def kill(self) -> None:
        self.killed = True
        self._code = 0


class _QueueStdoutPipe:
    """模拟 probe 的 queue-backed stdout。"""

    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.closed = False

    def readline(self) -> str:
        return self.lines.get(timeout=3.0)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.lines.put_nowait("")
            except queue.Full:
                pass


class _WarmupEchoStdin(io.StringIO):
    def __init__(self, pipe: _QueueStdoutPipe) -> None:
        super().__init__()
        self._pipe = pipe

    def write(self, value: str) -> int:
        result = super().write(value)
        if "warmup" in value:
            self._pipe.lines.put('{"ok": true, "warmup": true}\n')
        return result


@unittest.skipUnless(sys.platform == "win32", "owned RVC worker requires win32")
class RvcOwnedSpawnTests(unittest.TestCase):
    def test_spawn_applies_job_env_and_preserves_parent_environ(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, python, model = _allow_rvc_policy(root)
            backend = FakeOwnedWorkerJobBackend()
            pops: list[tuple[list[str], dict]] = []

            def fake_popen(argv, **kwargs):
                pops.append((list(argv), dict(kwargs)))
                redirector = _FakeRedirector()
                handle = backend.last_created_handle
                if handle is not None:
                    backend.attach_redirector(handle, redirector)
                return redirector

            bind_fake_rvc_runners(
                policy, popen_fn=fake_popen, owned_worker_job_backend=backend
            )
            marker = "MIKU_RVC_OWNED_TEST_MARKER"
            with patch.dict(os.environ, {marker: "1"}, clear=False):
                launch = RvcWorkerLaunch(
                    model_path=str(model),
                    device="cpu",
                    pitch=0,
                    index_rate=0.55,
                    protect=0.5,
                    filter_radius=3,
                    rms_mix_rate=0.75,
                    f0_method="rmvpe",
                    parent_pid=os.getpid(),
                    index_path=None,
                )
                proc = policy.spawn_rvc_worker(launch, stdin=subprocess.PIPE)
            self.assertIsInstance(proc, OwnedWorkerProcess)
            self.assertEqual(len(pops), 1)
            env = pops[0][1]["env"]
            self.assertEqual(env.get(marker), "1")
            self.assertIn(JOB_ENV_NAME, env)
            self.assertEqual(env[JOB_ENV_PARENT_PID], str(os.getpid()))
            expected_argv = build_worker_argv(python, launch)
            actual_argv = pops[0][0]
            self.assertEqual(len(actual_argv), len(expected_argv))
            self.assertEqual(
                Path(actual_argv[0]).resolve(), Path(expected_argv[0]).resolve()
            )
            self.assertEqual(actual_argv[1:], expected_argv[1:])

    def test_wrong_parent_pid_rejected_before_popen(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _python, model = _allow_rvc_policy(Path(tmp))
            called = {"n": 0}

            def boom(*_a, **_k):
                called["n"] += 1
                raise AssertionError("popen must not run")

            bind_fake_rvc_runners(policy, popen_fn=boom)
            launch = RvcWorkerLaunch(
                model_path=str(model),
                device="cpu",
                pitch=0,
                index_rate=0.55,
                protect=0.5,
                filter_radius=3,
                rms_mix_rate=0.75,
                f0_method="rmvpe",
                parent_pid=os.getpid() + 999_999,
                index_path=None,
            )
            with self.assertRaises(PermissionError):
                policy.spawn_rvc_worker(launch, stdin=subprocess.PIPE)
            self.assertEqual(called["n"], 0)


@unittest.skipUnless(sys.platform == "win32", "worker bootstrap requires win32")
class RvcWorkerBootstrapTests(unittest.TestCase):
    def test_main_exits_before_model_without_job_token(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        script = repo / "scripts" / "rvc_worker.py"
        model = repo / "tests" / "support" / "_rvc_bootstrap_model.pth"
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(b"x")
        env = os.environ.copy()
        env.pop("MIKU_LOCAL_TTS_OWNED_JOB_NAME", None)
        env.pop("MIKU_LOCAL_TTS_OWNED_JOB_PARENT_PID", None)
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--model",
                str(model),
                "--device",
                "cpu",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(repo),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("missing token", proc.stderr)


class RvcProtocolReaderTests(unittest.TestCase):
    def test_reader_delivers_multiple_messages(self) -> None:
        pipe = _QueueStdoutPipe()
        pipe.lines.put('{"ready": true, "load_ms": 1}\n')
        pipe.lines.put('{"ok": true, "warmup": true}\n')
        reader = _RVCStdoutReader(pipe)  # type: ignore[arg-type]
        reader.start()
        first = reader.poll_message(1.0)
        second = reader.poll_message(1.0)
        self.assertTrue(first and first.get("ready"))
        self.assertTrue(second and second.get("warmup"))
        reader.close()
        reader.join(1.0)


@unittest.skipUnless(sys.platform == "win32", "owned stop path requires win32")
class RvcOwnedClientLifecycleTests(unittest.TestCase):
    def _owned(self, redirector: _FakeRedirector) -> OwnedWorkerProcess:
        backend = FakeOwnedWorkerJobBackend()
        handle = backend.create_named_job("Local\\test")
        backend.set_kill_on_close(handle)
        from src.core.owned_worker_job import _JobResources

        resources = _JobResources(backend=backend, job_handle=handle)
        backend.attach_redirector(handle, redirector)
        return OwnedWorkerProcess(resources=resources, redirector=redirector)

    def test_same_worker_repeated_warmup_reads(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, python, model = _allow_rvc_policy(Path(tmp))
            client = RVCWorkerClient(
                {
                    "device": "cpu",
                    "python_path": str(python),
                    "model_path": str(model),
                },
                safety_policy=policy,
            )
            pipe = _QueueStdoutPipe()
            pipe.lines.put('{"ready": true, "load_ms": 1, "device": "cpu"}\n')
            redirector = _FakeRedirector()
            redirector.stdout = pipe  # type: ignore[assignment]
            redirector.stdin = _WarmupEchoStdin(pipe)
            owner = self._owned(redirector)

            def _term() -> None:
                redirector._code = 0  # noqa: SLF001
                pipe.close()

            redirector.terminate = _term  # type: ignore[method-assign]
            redirector.kill = _term  # type: ignore[method-assign]

            with patch.object(policy, "spawn_rvc_worker", return_value=owner):
                self.assertTrue(client.start())
                self.assertTrue(client.warm_up(timeout=1.0))
                self.assertTrue(client.warm_up(timeout=1.0))
            client.stop()

    def test_idle_stop_skips_blocked_stdin_for_owned_process(self) -> None:
        policy = SafetyPolicy(
            {"permissions": {"rvc_worker": True, "subprocess": False}}
        )
        client = RVCWorkerClient({"device": "cpu"}, safety_policy=policy)
        redirector = _FakeRedirector()
        writes: list[str] = []

        class _BlockingStdin:
            def write(self, data: str) -> int:
                writes.append(data)
                raise OSError("blocked pipe write")

            def flush(self) -> None:
                writes.append("flush")
                raise OSError("blocked pipe flush")

        redirector.stdin = _BlockingStdin()  # type: ignore[assignment]
        client._proc = self._owned(redirector)  # noqa: SLF001
        client._ready = True  # noqa: SLF001
        client.stop()
        self.assertEqual(writes, [])

    def test_alive_stdout_reader_blocks_release(self) -> None:
        policy = SafetyPolicy(
            {"permissions": {"rvc_worker": True, "subprocess": False}}
        )
        client = RVCWorkerClient({"device": "cpu"}, safety_policy=policy)
        redirector = _FakeRedirector()
        redirector._code = 0  # noqa: SLF001
        pipe = _QueueStdoutPipe()

        class _BlockPipe(_QueueStdoutPipe):
            def readline(self) -> str:
                self._gate.wait(timeout=2.0)
                return ""

            def __init__(self) -> None:
                super().__init__()
                self._gate = threading.Event()

            def close(self) -> None:
                self._gate.set()
                super().close()

        blocking = _BlockPipe()
        reader = _RVCStdoutReader(blocking)  # type: ignore[arg-type]
        reader.start()
        binding = _OwnerBinding(
            generation=0,
            proc=redirector,
            reader=reader,
            stderr_thread=None,
        )
        client._owner_generation = 0  # noqa: SLF001
        client._proc = redirector  # noqa: SLF001
        client._reader = reader  # noqa: SLF001
        self.assertFalse(client._try_release_owner(binding))  # noqa: SLF001
        blocking.close()
        reader.join(1.0)

    def test_stop_during_spawn_returns_false_and_no_ready(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, python, model = _allow_rvc_policy(Path(tmp))
            client = RVCWorkerClient(
                {"device": "cpu", "model_path": str(model)},
                safety_policy=policy,
            )
            entered = threading.Event()
            release = threading.Event()
            owner_holder: dict[str, OwnedWorkerProcess] = {}

            def slow_spawn(*_a, **_k):
                entered.set()
                release.wait(timeout=3.0)
                redirector = _FakeRedirector(
                    stdout=io.StringIO('{"ready": true, "load_ms": 1}\n')
                )
                owner = self._owned(redirector)
                owner_holder["owner"] = owner
                return owner

            outcomes: list[bool] = []

            def run_start() -> None:
                with patch(
                    "src.voice.rvc.worker.ensure_policy_rvc_python",
                ), patch(
                    "src.voice.rvc.worker.resolve_rvc_python",
                    return_value=python,
                ), patch.object(policy, "spawn_rvc_worker", side_effect=slow_spawn):
                    outcomes.append(client.start())

            thread = threading.Thread(target=run_start, daemon=False)
            thread.start()
            self.assertTrue(entered.wait(timeout=1.0))
            first_stop = client.stop()
            release.set()
            thread.join(timeout=4.0)
            self.assertFalse(thread.is_alive())
            self.assertFalse(first_stop)
            self.assertEqual(outcomes, [False])
            self.assertFalse(client.ready)

    def test_keyboard_interrupt_spawn_preserves_owner(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, python, model = _allow_rvc_policy(Path(tmp))
            client = RVCWorkerClient(
                {"device": "cpu", "model_path": str(model)},
                safety_policy=policy,
            )
            backend = FakeOwnedWorkerJobBackend()
            handle = backend.create_named_job("pending")
            owner = OwnedWorkerProcess(
                _JobResources(backend, handle),
                argv=["fake"],
            )
            original = KeyboardInterrupt("controlled")
            attach_owned_worker_to_exception(original, owner)

            with patch(
                "src.voice.rvc.worker.ensure_policy_rvc_python",
            ), patch(
                "src.voice.rvc.worker.resolve_rvc_python", return_value=python
            ), patch.object(
                policy, "spawn_rvc_worker", side_effect=original
            ):
                caught: BaseException | None = None
                try:
                    client.start()
                except BaseException as exc:
                    caught = exc
            self.assertIs(caught, original)
            self.assertIs(client._proc, owner)  # noqa: SLF001
            owner.kill()
            client.stop()

    def test_late_warmup_response_rejected_after_interrupt(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, python, model = _allow_rvc_policy(Path(tmp))
            client = RVCWorkerClient(
                {
                    "device": "cpu",
                    "python_path": str(python),
                    "model_path": str(model),
                },
                safety_policy=policy,
            )
            pipe = _QueueStdoutPipe()
            pipe.lines.put('{"ready": true, "load_ms": 1, "device": "cpu"}\n')
            release_warmup = threading.Event()
            redirector = _FakeRedirector()
            redirector.stdout = pipe  # type: ignore[assignment]

            class _DelayedStdin(io.StringIO):
                def write(self, value: str) -> int:
                    result = super().write(value)
                    if "warmup" in value:
                        def _put() -> None:
                            release_warmup.wait(timeout=2.0)
                            pipe.lines.put('{"ok": true, "warmup": true}\n')

                        threading.Thread(target=_put, daemon=True).start()
                    return result

            redirector.stdin = _DelayedStdin()
            owner = self._owned(redirector)
            with patch.object(policy, "spawn_rvc_worker", return_value=owner):
                self.assertTrue(client.start())
            client.interrupt_current()
            release_warmup.set()
            with patch.object(client, "_stdin_write", return_value=True):
                self.assertFalse(client.warm_up(timeout=0.2))
            client.stop()


if __name__ == "__main__":
    unittest.main()
