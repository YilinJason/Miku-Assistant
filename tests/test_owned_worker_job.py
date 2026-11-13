"""Windows 本地 TTS owned Job 生命周期（纯 fake 后端，无真实模型/子进程树）。"""
from __future__ import annotations

import io
import os
import runpy
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from src.core.owned_worker_job import (
    JOB_ENV_NAME,
    JOB_ENV_PARENT_PID,
    OwnedWorkerProcess,
    _JobResources,
    _OWNED_WORKER_EXCEPTION_ATTR,
    _Win32OwnedWorkerJobBackend,
    apply_owned_job_env,
    attach_owned_worker_to_exception,
    make_owned_job_name,
    parse_owned_job_name,
    spawn_owned_local_tts_worker,
    take_owned_worker_from_exception,
    worker_entry_join_owned_job,
)
from src.voice.cosyvoice.client import CosyVoiceWorkerClient
from src.core.safety import SafetyPolicy
from src.voice.cosyvoice.capabilities import LocalTtsWorkerLaunch
from src.voice.cosyvoice.settings import parse_cosyvoice_settings
from src.voice.cosyvoice.worker_env import build_worker_env
from tests.support.fake_owned_worker_job_backend import FakeOwnedWorkerJobBackend
from tests.support.local_tts_runners import bind_fake_local_tts_runners
from tests.test_cosyvoice_production_slice_a import (
    _policy_with_local_tts,
)


class _FakeRedirector:
    def __init__(self, *, pid: int = 4242):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = None
        self.pid = pid
        self.args = ["fake"]
        self._rc: int | None = None
        self.terminated = False
        self.kill_called = False
        self.close_after_create = False

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        if self._rc is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = 1

    def kill(self):
        self.kill_called = True
        self.terminated = True
        self._rc = 1

    def exit_now(self, code: int = 0) -> None:
        self._rc = code


def _settings():
    cfg = parse_cosyvoice_settings({"engine": "cosyvoice"})
    assert cfg is not None
    return cfg


class OwnedJobNameTests(unittest.TestCase):
    def test_name_unique_and_parseable(self) -> None:
        name = make_owned_job_name(12345)
        self.assertIn("MikuLocalTts_12345_", name)
        self.assertTrue(parse_owned_job_name(name, expected_parent_pid=12345))
        self.assertFalse(parse_owned_job_name(name, expected_parent_pid=999))

    def test_ambient_job_env_stripped_from_worker_env(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c"
            cache.mkdir()
            env = build_worker_env(
                cache_root=cache,
                environ={
                    "PATH": "/x",
                    JOB_ENV_NAME: "evil",
                    JOB_ENV_PARENT_PID: "1",
                },
            )
            self.assertNotIn(JOB_ENV_NAME, env)
            self.assertNotIn(JOB_ENV_PARENT_PID, env)


class SafetyPolicyRunnerBindingTests(unittest.TestCase):
    def test_custom_popen_keeps_native_containment_backend_unset(self) -> None:
        policy = SafetyPolicy({"permissions": {"local_tts_worker": True}})
        policy.bind_process_runners(popen_fn=lambda *_a, **_k: None)
        self.assertIsNone(policy._owned_worker_job_backend)


class WorkerJoinTests(unittest.TestCase):
    def test_missing_token_exits_before_heavy_imports(self) -> None:
        seen: list[str] = []
        worker_script = (
            Path(__file__).resolve().parents[1] / "scripts" / "cosyvoice_runtime_worker.py"
        )

        class _Blocker:
            def find_module(self, name: str, path=None):
                if name == "src.voice" or name.startswith("src.voice."):
                    seen.append(name)
                    raise ImportError("blocked for test")
                return None

        blocker = _Blocker()
        sys.meta_path.insert(0, blocker)
        try:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit) as ctx:
                    runpy.run_path(str(worker_script), run_name="__main__")
            self.assertEqual(ctx.exception.code, 2)
        finally:
            sys.meta_path.remove(blocker)
        self.assertEqual(seen, [])

    def test_join_success_assigns_and_closes_child_handle(self) -> None:
        backend = FakeOwnedWorkerJobBackend()
        parent_pid = 77
        name = make_owned_job_name(parent_pid)
        backend.create_named_job(name)
        env = {
            JOB_ENV_NAME: name,
            JOB_ENV_PARENT_PID: str(parent_pid),
        }
        assign_calls: list[int] = []
        close_calls: list[int] = []
        real_assign = backend.assign_current_process
        real_close = backend.close_handle

        def assign(job_handle: int) -> None:
            assign_calls.append(job_handle)
            real_assign(job_handle)

        def close(job_handle: int) -> None:
            close_calls.append(job_handle)
            real_close(job_handle)

        backend.assign_current_process = assign  # type: ignore[method-assign]
        backend.close_handle = close  # type: ignore[method-assign]
        with patch(
            "src.core.parent_process.parent_process_is_alive",
            return_value=True,
        ):
            worker_entry_join_owned_job(backend=backend, environ=env)
        self.assertEqual(backend.joined_names, [name])
        self.assertEqual(assign_calls, close_calls)
        self.assertEqual(len(assign_calls), 1)

    def test_open_failure_exits_without_voice_import(self) -> None:
        backend = FakeOwnedWorkerJobBackend()
        parent_pid = 10
        name = make_owned_job_name(parent_pid)
        seen: list[str] = []

        class _Blocker:
            def find_module(self, name: str, path=None):
                if name == "src.voice" or name.startswith("src.voice."):
                    seen.append(name)
                    raise ImportError("blocked")
                return None

        blocker = _Blocker()
        sys.meta_path.insert(0, blocker)
        try:
            with patch(
                "src.core.parent_process.parent_process_is_alive",
                return_value=True,
            ):
                with self.assertRaises(SystemExit) as ctx:
                    worker_entry_join_owned_job(
                        backend=backend,
                        environ={
                            JOB_ENV_NAME: name,
                            JOB_ENV_PARENT_PID: str(parent_pid),
                        },
                    )
            self.assertEqual(ctx.exception.code, 2)
        finally:
            sys.meta_path.remove(blocker)
        self.assertEqual(seen, [])

    def test_parent_mismatch_rejected(self) -> None:
        backend = FakeOwnedWorkerJobBackend()
        name = make_owned_job_name(10)
        backend.create_named_job(name)
        with patch(
            "src.core.parent_process.parent_process_is_alive",
            return_value=True,
        ):
            with self.assertRaises(SystemExit):
                worker_entry_join_owned_job(
                    backend=backend,
                    environ={
                        JOB_ENV_NAME: name,
                        JOB_ENV_PARENT_PID: "11",
                    },
                )


class OwnedWorkerProcessTests(unittest.TestCase):
    def _wrap(self, backend: FakeOwnedWorkerJobBackend, redirector: _FakeRedirector):
        handle = backend.create_named_job(make_owned_job_name(1))
        resources = _JobResources(backend=backend, job_handle=handle)
        return OwnedWorkerProcess(resources, redirector=redirector), handle, backend

    def test_raw_exit_with_active_child_incomplete(self) -> None:
        redirector = _FakeRedirector()
        adapter, handle, backend = self._wrap(FakeOwnedWorkerJobBackend(), redirector)
        backend._jobs[handle]["active"] = 2
        redirector.exit_now(0)
        self.assertIsNone(adapter.poll())

    def test_query_failure_fail_closed(self) -> None:
        redirector = _FakeRedirector()
        backend = FakeOwnedWorkerJobBackend()
        adapter, handle, backend = self._wrap(backend, redirector)
        backend._jobs.pop(handle)
        redirector.exit_now(0)
        self.assertIsNone(adapter.poll())

    def test_natural_exit_releases(self) -> None:
        redirector = _FakeRedirector()
        adapter, handle, backend = self._wrap(FakeOwnedWorkerJobBackend(), redirector)
        backend._jobs[handle]["active"] = 0
        redirector.exit_now(0)
        self.assertEqual(adapter.poll(), 0)
        self.assertEqual(adapter.wait(timeout=1.0), 0)

    def test_close_failure_retains_owner(self) -> None:
        class _Backend(FakeOwnedWorkerJobBackend):
            def close_handle(self, handle: int) -> None:
                raise OSError("close failed")

        redirector = _FakeRedirector()
        backend = _Backend()
        adapter, handle, _ = self._wrap(backend, redirector)
        backend._jobs[handle]["active"] = 0
        redirector.exit_now(0)
        self.assertIsNone(adapter.poll())
        resources = adapter._resources
        self.assertFalse(resources.closed)
        self.assertEqual(resources.job_handle, handle)

    def test_poll_no_redirector_quiescent_job_retires(self) -> None:
        backend = FakeOwnedWorkerJobBackend()
        handle = backend.create_named_job(make_owned_job_name(1))
        backend._jobs[handle]["active"] = 0
        resources = _JobResources(backend=backend, job_handle=handle)
        adapter = OwnedWorkerProcess(resources, redirector=None)
        self.assertEqual(adapter.poll(), 2)
        self.assertTrue(resources.closed)

    def test_poll_no_redirector_close_failure_retains_handle(self) -> None:
        class _Backend(FakeOwnedWorkerJobBackend):
            def close_handle(self, handle: int) -> None:
                raise OSError("close failed")

        backend = _Backend()
        handle = backend.create_named_job(make_owned_job_name(1))
        backend._jobs[handle]["active"] = 0
        resources = _JobResources(backend=backend, job_handle=handle)
        adapter = OwnedWorkerProcess(resources, redirector=None)
        self.assertIsNone(adapter.poll())
        self.assertFalse(resources.closed)
        self.assertEqual(resources.job_handle, handle)

    def test_wait_rejects_non_finite_timeout(self) -> None:
        redirector = _FakeRedirector()
        adapter, _handle, backend = self._wrap(FakeOwnedWorkerJobBackend(), redirector)
        backend._jobs[_handle]["force_active"] = 1
        with self.assertRaises(ValueError):
            adapter.wait(timeout=float("nan"))
        with self.assertRaises(ValueError):
            adapter.wait(timeout=float("inf"))

    def test_terminate_hits_job_and_redirector(self) -> None:
        redirector = _FakeRedirector()
        adapter, handle, backend = self._wrap(FakeOwnedWorkerJobBackend(), redirector)
        adapter.terminate()
        self.assertTrue(redirector.terminated)
        self.assertTrue(backend._jobs[handle]["terminated"])

    def test_wait_timeout_not_extended_by_active_child(self) -> None:
        redirector = _FakeRedirector()
        adapter, handle, backend = self._wrap(FakeOwnedWorkerJobBackend(), redirector)
        backend._jobs[handle]["force_active"] = 1
        with self.assertRaises(subprocess.TimeoutExpired):
            adapter.wait(timeout=0.08)

    def test_concurrent_poll_and_terminate_no_double_close(self) -> None:
        redirector = _FakeRedirector()
        backend = FakeOwnedWorkerJobBackend()
        adapter, handle, backend = self._wrap(backend, redirector)
        backend._jobs[handle]["active"] = 0
        redirector.exit_now(0)
        close_calls: list[int] = []
        real_close = backend.close_handle

        def counting_close(h: int) -> None:
            close_calls.append(h)
            real_close(h)

        backend.close_handle = counting_close  # type: ignore[method-assign]
        errors: list[BaseException] = []

        def poll_loop() -> None:
            try:
                for _ in range(50):
                    adapter.poll()
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=poll_loop)
        t.start()
        adapter.terminate()
        t.join(timeout=2.0)
        self.assertEqual(errors, [])
        self.assertLessEqual(len(close_calls), 1)


class NativeBackendTests(unittest.TestCase):
    def test_create_collision_closes_opened_handle(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _Kernel:
            def __init__(self) -> None:
                self.closed: list[int] = []

            def CreateJobObjectW(self, *_a):
                ctypes.set_last_error(183)
                return 19

            def GetLastError(self):
                return ctypes.get_last_error()

            def CloseHandle(self, h):
                self.closed.append(h)
                return 1

        kernel = _Kernel()
        native = _Win32OwnedWorkerJobBackend.__new__(_Win32OwnedWorkerJobBackend)
        native._kernel32 = kernel
        native._ctypes = ctypes
        native._wintypes = wintypes
        with self.assertRaises(OSError):
            native.create_named_job("fake-existing")
        self.assertEqual(kernel.closed, [19])

    def test_close_handle_checks_bool(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _Kernel:
            def CloseHandle(self, _h):
                ctypes.set_last_error(6)
                return 0

            def GetLastError(self):
                return ctypes.get_last_error()

        kernel = _Kernel()
        native = _Win32OwnedWorkerJobBackend.__new__(_Win32OwnedWorkerJobBackend)
        native._kernel32 = kernel
        native._ctypes = ctypes
        native._wintypes = wintypes
        with self.assertRaises(OSError):
            native.close_handle(5)


class SpawnIntegrationTests(unittest.TestCase):
    def test_spawn_injects_env_and_fake_popen(self) -> None:
        backend = FakeOwnedWorkerJobBackend()
        captured: list = []

        def fake_popen(argv, **kwargs):
            captured.append(kwargs.get("env") or {})
            redirector = _FakeRedirector()
            handle = backend.last_created_handle
            if handle is not None:
                backend.attach_redirector(handle, redirector)
            return redirector

        with patch("src.core.owned_worker_job.sys.platform", "win32"):
            proc = spawn_owned_local_tts_worker(
                ["py", "script"],
                {"env": {"PATH": "x"}},
                popen_fn=fake_popen,
                parent_pid=55,
                job_backend=backend,
            )
        self.assertEqual(len(backend.created_names), 1)
        env = captured[0]
        self.assertEqual(env.get("PATH"), "x")
        self.assertIn(JOB_ENV_NAME, env)
        self.assertEqual(env.get(JOB_ENV_PARENT_PID), "55")
        self.assertIsInstance(proc, OwnedWorkerProcess)

    def test_popen_interrupt_closes_owned_job(self) -> None:
        backend = FakeOwnedWorkerJobBackend()

        def interrupted_spawn(*_a, **_k):
            raise KeyboardInterrupt()

        with patch("src.core.owned_worker_job.sys.platform", "win32"):
            with self.assertRaises(KeyboardInterrupt):
                spawn_owned_local_tts_worker(
                    ["fake"],
                    {"env": {}},
                    popen_fn=interrupted_spawn,
                    parent_pid=123,
                    job_backend=backend,
                )
        self.assertEqual(len(backend.created_names), 1)
        self.assertEqual(len(backend._jobs), 0)

    def test_failure_after_popen_retires_redirector(self) -> None:
        backend = FakeOwnedWorkerJobBackend()
        redirector = _FakeRedirector()
        real_bind = OwnedWorkerProcess._bind_redirector

        def broken_bind(self, proc: subprocess.Popen) -> None:
            real_bind(self, proc)
            raise RuntimeError("after popen")

        with patch("src.core.owned_worker_job.sys.platform", "win32"):
            with patch.object(OwnedWorkerProcess, "_bind_redirector", broken_bind):
                with self.assertRaises(RuntimeError):
                    spawn_owned_local_tts_worker(
                        ["fake"],
                        {"env": {}},
                        popen_fn=lambda *_a, **_k: redirector,
                        parent_pid=9,
                        job_backend=backend,
                    )
        self.assertTrue(redirector.kill_called or redirector._rc is not None)
        self.assertEqual(len(backend._jobs), 0)

    def test_interrupt_before_bind_still_attaches_returned_redirector(self) -> None:
        class _CloseFailBackend(FakeOwnedWorkerJobBackend):
            def close_handle(self, handle: int) -> None:
                raise OSError("close temporarily failed")

        backend = _CloseFailBackend()
        redirector = _FakeRedirector()

        def interrupt_before_bind(self, proc: subprocess.Popen) -> None:
            raise KeyboardInterrupt("on bind line")

        with patch("src.core.owned_worker_job.sys.platform", "win32"):
            with patch.object(OwnedWorkerProcess, "_bind_redirector", interrupt_before_bind):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    spawn_owned_local_tts_worker(
                        ["fake"],
                        {"env": {}},
                        popen_fn=lambda *_a, **_k: redirector,
                        parent_pid=11,
                        job_backend=backend,
                    )
        owner = getattr(ctx.exception, _OWNED_WORKER_EXCEPTION_ATTR, None)
        self.assertIsInstance(owner, OwnedWorkerProcess)
        self.assertIs(owner._redirector, redirector)

    def test_interrupt_after_popen_hands_off_owner_when_cleanup_incomplete(self) -> None:
        class _CloseFailBackend(FakeOwnedWorkerJobBackend):
            def close_handle(self, handle: int) -> None:
                raise OSError("close temporarily failed")

        backend = _CloseFailBackend()
        redirector = _FakeRedirector()

        def popen_after_interrupt(*_a, **_k):
            handle = backend.last_created_handle
            if handle is not None:
                backend.attach_redirector(handle, redirector)
                backend._jobs[handle]["active"] = 1
            raise KeyboardInterrupt("after popen return")

        with patch("src.core.owned_worker_job.sys.platform", "win32"):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                spawn_owned_local_tts_worker(
                    ["fake"],
                    {"env": {}},
                    popen_fn=popen_after_interrupt,
                    parent_pid=123,
                    job_backend=backend,
                )
        owner = getattr(ctx.exception, _OWNED_WORKER_EXCEPTION_ATTR, None)
        self.assertIsInstance(owner, OwnedWorkerProcess)
        self.assertIsNone(owner.poll())

    def test_interrupt_after_popen_no_residual_owner_when_cleanup_succeeds(self) -> None:
        backend = FakeOwnedWorkerJobBackend()
        redirector = _FakeRedirector()

        def popen_then_interrupt(*_a, **_k):
            handle = backend.last_created_handle
            if handle is not None:
                backend.attach_redirector(handle, redirector)
            raise KeyboardInterrupt("after popen return")

        with patch("src.core.owned_worker_job.sys.platform", "win32"):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                spawn_owned_local_tts_worker(
                    ["fake"],
                    {"env": {}},
                    popen_fn=popen_then_interrupt,
                    parent_pid=5,
                    job_backend=backend,
                )
        self.assertIsNone(getattr(ctx.exception, _OWNED_WORKER_EXCEPTION_ATTR, None))
        self.assertEqual(len(backend._jobs), 0)

    def test_pre_bind_interrupt_preserves_handle_when_close_fails(self) -> None:
        class _CloseFailBackend(FakeOwnedWorkerJobBackend):
            def close_handle(self, handle: int) -> None:
                raise OSError("close temporarily failed")

        backend = _CloseFailBackend()
        real_bind = OwnedWorkerProcess._bind_job_handle
        bind_calls = 0

        def interrupt_before_bind(self, job_handle: int) -> None:
            nonlocal bind_calls
            bind_calls += 1
            if bind_calls == 1:
                raise KeyboardInterrupt("before bind line")
            real_bind(self, job_handle)

        with patch("src.core.owned_worker_job.sys.platform", "win32"):
            with patch.object(OwnedWorkerProcess, "_bind_job_handle", interrupt_before_bind):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    spawn_owned_local_tts_worker(
                        ["fake"],
                        {"env": {}},
                        popen_fn=lambda *_a, **_k: _FakeRedirector(),
                        parent_pid=17,
                        job_backend=backend,
                    )
        owner = getattr(ctx.exception, _OWNED_WORKER_EXCEPTION_ATTR, None)
        self.assertIsInstance(owner, OwnedWorkerProcess)
        self.assertEqual(owner._resources.job_handle, backend.last_created_handle)

    def test_interrupt_before_popen_no_owner_attachment(self) -> None:
        backend = FakeOwnedWorkerJobBackend()

        def popen_interrupt(*_a, **_k):
            raise KeyboardInterrupt("before popen return")

        with patch("src.core.owned_worker_job.sys.platform", "win32"):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                spawn_owned_local_tts_worker(
                    ["fake"],
                    {"env": {}},
                    popen_fn=popen_interrupt,
                    parent_pid=8,
                    job_backend=backend,
                )
        self.assertIsNone(getattr(ctx.exception, _OWNED_WORKER_EXCEPTION_ATTR, None))
        self.assertEqual(len(backend._jobs), 0)

    def test_cleanup_errors_do_not_replace_original_exception(self) -> None:
        backend = FakeOwnedWorkerJobBackend()
        redirector = _FakeRedirector()

        def popen_then_interrupt(*_a, **_k):
            handle = backend.last_created_handle
            if handle is not None:
                backend.attach_redirector(handle, redirector)
                backend._jobs[handle]["active"] = 1
            raise KeyboardInterrupt("primary")

        real_kill = OwnedWorkerProcess.kill

        def exploding_kill(self) -> None:
            real_kill(self)
            raise RuntimeError("cleanup side effect")

        with patch("src.core.owned_worker_job.sys.platform", "win32"):
            with patch.object(OwnedWorkerProcess, "kill", exploding_kill):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    spawn_owned_local_tts_worker(
                        ["fake"],
                        {"env": {}},
                        popen_fn=popen_then_interrupt,
                        parent_pid=3,
                        job_backend=backend,
                    )
        self.assertIsInstance(ctx.exception, KeyboardInterrupt)
        self.assertEqual(str(ctx.exception), "primary")

    def test_post_bind_interrupt_keeps_owner_handle_open(self) -> None:
        class _TrackCloseBackend(FakeOwnedWorkerJobBackend):
            def __init__(self) -> None:
                super().__init__()
                self.closed_handles: list[int] = []

            def close_handle(self, handle: int) -> None:
                self.closed_handles.append(handle)
                super().close_handle(handle)

        backend = _TrackCloseBackend()
        original = KeyboardInterrupt("after bind before local clear")
        old_trace = sys.gettrace()

        def trace(frame, event, _arg):
            if (
                event == "line"
                and frame.f_code is spawn_owned_local_tts_worker.__code__
            ):
                owner = frame.f_locals.get("owned")
                if (
                    owner is not None
                    and owner._resources.job_handle == backend.last_created_handle
                    and frame.f_locals.get("orphan_handle")
                    == backend.last_created_handle
                ):
                    sys.settrace(None)
                    raise original
            return trace

        try:
            sys.settrace(trace)
            with patch("src.core.owned_worker_job.sys.platform", "win32"):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    spawn_owned_local_tts_worker(
                        ["fake"],
                        {"env": {}},
                        popen_fn=lambda *_a, **_k: None,
                        parent_pid=123,
                        job_backend=backend,
                    )
        finally:
            sys.settrace(old_trace)
        self.assertIs(ctx.exception, original)
        owner = getattr(ctx.exception, _OWNED_WORKER_EXCEPTION_ATTR, None)
        if owner is not None:
            self.assertNotIn(
                owner._resources.job_handle,
                backend.closed_handles,
            )

    def test_cleanup_query_error_preserves_original_spawn_exception(self) -> None:
        class _QueryFailBackend(FakeOwnedWorkerJobBackend):
            def query_active_process_count(self, handle: int):
                raise OSError("cleanup query failed")

        backend = _QueryFailBackend()
        original = RuntimeError("original spawn failure")

        def fail_popen(*_a, **_k):
            raise original

        with patch("src.core.owned_worker_job.sys.platform", "win32"):
            with self.assertRaises(RuntimeError) as ctx:
                spawn_owned_local_tts_worker(
                    ["fake"],
                    {"env": {}},
                    popen_fn=fail_popen,
                    parent_pid=123,
                    job_backend=backend,
                )
        self.assertIs(ctx.exception, original)
        self.assertIsInstance(
            getattr(ctx.exception, _OWNED_WORKER_EXCEPTION_ATTR, None),
            OwnedWorkerProcess,
        )

    def test_take_owned_worker_from_exception_once(self) -> None:
        redirector = _FakeRedirector()
        backend = FakeOwnedWorkerJobBackend()
        handle = backend.create_named_job(make_owned_job_name(1))
        resources = _JobResources(backend=backend, job_handle=handle)
        owner = OwnedWorkerProcess(resources, redirector=redirector)
        exc = KeyboardInterrupt()
        attach_owned_worker_to_exception(exc, owner)
        taken = take_owned_worker_from_exception(exc)
        self.assertIs(taken, owner)
        self.assertIsNone(take_owned_worker_from_exception(exc))

    def test_safety_spawn_requires_matching_parent_pid_on_win32(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"MIKU_DRY_RUN": ""}):
            policy, _root = _policy_with_local_tts(tmp)
            backend = FakeOwnedWorkerJobBackend()
            bind_fake_local_tts_runners(
                policy,
                popen_fn=lambda *_a, **_k: _FakeRedirector(),
                owned_worker_job_backend=backend,
            )
            launch = LocalTtsWorkerLaunch(parent_pid=999, settings=_settings())
            with patch(
                "src.voice.cosyvoice.assets.verify_pinned_assets",
                return_value={},
            ):
                with patch("src.core.owned_worker_job.sys.platform", "win32"):
                    with self.assertRaises(PermissionError):
                        policy.spawn_local_tts_worker(launch)


class ClientPendingOwnerTests(unittest.TestCase):
    def test_permission_error_adopts_pending_owner(self) -> None:
        from tempfile import TemporaryDirectory

        from src.voice.audio.temp_files import TempFileManager

        with TemporaryDirectory() as tmp:
            policy, _root = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy)
            backend = FakeOwnedWorkerJobBackend()
            handle = backend.create_named_job(make_owned_job_name(os.getpid()))
            backend._jobs[handle]["active"] = 1
            resources = _JobResources(backend=backend, job_handle=handle)
            pending = OwnedWorkerProcess(resources, redirector=None)

            def spawn_permission_denied(_launch):
                exc = PermissionError("injected partial launch permission failure")
                attach_owned_worker_to_exception(exc, pending)
                raise exc

            policy.spawn_local_tts_worker = spawn_permission_denied  # type: ignore[method-assign]
            client = CosyVoiceWorkerClient(_settings(), policy, tfm)
            self.assertFalse(client._start_worker_poll(0, None))  # noqa: SLF001
            self.assertIs(client._proc, pending)
            self.assertTrue(client.stop_failed)
            self.assertFalse(client._ensure_worker_started(0, None))  # noqa: SLF001
            if client._proc is not None:
                job = backend._jobs.get(handle)
                if job is not None:
                    job["active"] = 0
            status = client.shutdown(timeout=1.0)
            self.assertTrue(status.complete)
            self.assertIsNone(client._proc)

    def test_adopted_owner_blocks_restart_until_shutdown(self) -> None:
        from tempfile import TemporaryDirectory

        from src.voice.audio.temp_files import TempFileManager

        with TemporaryDirectory() as tmp:
            policy, _root = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy)
            backend = FakeOwnedWorkerJobBackend()
            redirector = _FakeRedirector()
            handle = backend.create_named_job(make_owned_job_name(os.getpid()))
            backend._jobs[handle]["active"] = 1
            resources = _JobResources(backend=backend, job_handle=handle)
            pending = OwnedWorkerProcess(resources, redirector=redirector)

            def spawn_interrupt(_launch):
                exc = KeyboardInterrupt("partial spawn")
                attach_owned_worker_to_exception(exc, pending)
                raise exc

            policy.spawn_local_tts_worker = spawn_interrupt  # type: ignore[method-assign]
            client = CosyVoiceWorkerClient(_settings(), policy, tfm)
            with self.assertRaises(KeyboardInterrupt):
                client._start_worker_poll(0, None)  # noqa: SLF001
            self.assertIs(client._proc, pending)
            self.assertTrue(client.stop_failed)
            self.assertFalse(client._ensure_worker_started(0, None))  # noqa: SLF001
            redirector.exit_now(0)
            job = backend._jobs.get(handle)
            if job is not None:
                job["active"] = 0
            status = client.shutdown(timeout=1.0)
            self.assertTrue(status.complete)
            self.assertIsNone(client._proc)


class HandleGrowthTests(unittest.TestCase):
    def _wrap(self, backend: FakeOwnedWorkerJobBackend, redirector: _FakeRedirector):
        handle = backend.create_named_job(make_owned_job_name(1))
        resources = _JobResources(backend=backend, job_handle=handle)
        return OwnedWorkerProcess(resources, redirector=redirector), handle, backend

    def test_repeated_cycles_no_unbounded_backend_jobs(self) -> None:
        backend = FakeOwnedWorkerJobBackend()
        for _ in range(30):
            redirector = _FakeRedirector()
            adapter, handle, backend = self._wrap(backend, redirector)
            backend._jobs[handle]["active"] = 0
            redirector.exit_now(0)
            adapter.poll()
        self.assertEqual(len(backend._jobs), 0)


if __name__ == "__main__":
    unittest.main()
