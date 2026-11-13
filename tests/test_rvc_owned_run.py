"""单次 RVC owned run 统一状态协议行为测试（纯 fake）。"""
from __future__ import annotations

import io
import os
import runpy
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import src.core.owned_rvc_run as owned_rvc_run_module
from src.core.owned_rvc_run import OwnedRvcRunCoordinator
from src.core.rvc_capabilities import RvcInferLaunch, RvcProbeKind, build_probe_argv
from src.core.safety import SafetyPolicy, TrustedRvcProfile
from src.core.owned_worker_job import (
    OwnedWorkerProcess,
    _JobResources,
    attach_owned_worker_to_exception,
)
from src.voice.audio.temp_files import TempFileManager

from tests.support.fake_owned_worker_job_backend import FakeOwnedWorkerJobBackend
from tests.support.local_tts_runners import bind_fake_rvc_runners

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RVC_INFER_SCRIPT = _REPO_ROOT / "scripts" / "rvc_infer.py"


def _heavy_import_blocker(seen: list[str]):
    class _Blocker:
        def find_module(self, name: str, path=None):
            blocked = (
                name == "torch"
                or name.startswith("torch.")
                or name == "rvc_python"
                or name.startswith("rvc_python.")
                or name == "src.voice.rvc.vc_params"
                or name.startswith("src.voice.rvc.")
            )
            if blocked:
                seen.append(name)
                raise ImportError("blocked for bootstrap test")
            return None

    return _Blocker()


def _policy(tmp: Path) -> tuple[SafetyPolicy, Path, Path]:
    python = tmp / "python.exe"
    python.write_bytes(b"")
    model = tmp / "m.pth"
    model.write_bytes(b"x")
    policy = SafetyPolicy(
        {
            "permissions": {
                "rvc_infer": True,
                "rvc_probe": True,
                "subprocess": False,
                "file_delete": True,
            }
        },
        trusted_rvc_profile=TrustedRvcProfile(python_path=python, model_path=model),
    )
    return policy, python, model


class _Owner:
    def __init__(self, *, stdout: bytes = b"ok", code: int | None = 0) -> None:
        self.stdout = io.BytesIO(stdout) if stdout is not None else None
        self.stderr = io.BytesIO(b"") if code is not None else None
        self.stdin = None
        self.code = code
        self.killed = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.code is None:
            raise RuntimeError("fake wait before termination")
        return self.code

    def kill(self):
        self.killed = True
        self.code = 1

    def _is_spawn_quiescent(self):
        return self.code is not None


def _run_coord(coord: OwnedRvcRunCoordinator, **kw):
    return coord.execute(
        ["fake"],
        parent_pid=os.getpid(),
        popen_fn=None,
        job_backend=None,
        max_timeout_budget=2.0,
        timeout=2.0,
        capture_output=True,
        **kw,
    )


@unittest.skipUnless(sys.platform == "win32", "owned rvc run requires win32")
class OwnedRvcRunTests(unittest.TestCase):
    def test_one_success_one_spawn_second_rejected(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        entered = threading.Event()
        release = threading.Event()
        spawned: list[int] = []
        outcomes: list[subprocess.CompletedProcess] = []

        def spawn(*_a, **_k):
            spawned.append(1)
            entered.set()
            release.wait(2)
            return _Owner()

        def first() -> None:
            outcomes.append(_run_coord(coord))

        with patch.object(
            owned_rvc_run_module, "spawn_owned_voice_worker", side_effect=spawn
        ):
            t = threading.Thread(target=first)
            try:
                t.start()
                self.assertTrue(entered.wait(2.0))
                with self.assertRaises(PermissionError):
                    _run_coord(coord)
            finally:
                release.set()
                t.join(3.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(spawned), 1)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].stdout, b"ok")

    def test_partial_owner_handoff_blocks_second_spawn(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        backend = FakeOwnedWorkerJobBackend()
        pending = OwnedWorkerProcess(
            _JobResources(backend, backend.create_named_job("fake")),
            argv=["fake"],
        )
        exc = KeyboardInterrupt("partial")
        attach_owned_worker_to_exception(exc, pending)
        entered = threading.Event()
        release = threading.Event()
        alloc: list[int] = []
        errors: list[BaseException] = []
        original_take = owned_rvc_run_module.take_owned_worker_from_exception

        def take(e: BaseException):
            if e is exc:
                entered.set()
                release.wait(2)
            return original_take(e)

        def spawn(*_a, **_k):
            alloc.append(1)
            if len(alloc) == 1:
                raise exc
            return _Owner()

        def first() -> None:
            try:
                _run_coord(coord)
            except BaseException as e:
                errors.append(e)

        with patch.object(
            owned_rvc_run_module, "spawn_owned_voice_worker", side_effect=spawn
        ), patch.object(
            owned_rvc_run_module,
            "take_owned_worker_from_exception",
            side_effect=take,
        ):
            t = threading.Thread(target=first)
            try:
                t.start()
                self.assertTrue(entered.wait(2.0))
                with self.assertRaises(PermissionError):
                    _run_coord(coord)
            finally:
                release.set()
                t.join(3.0)
        self.assertEqual(len(alloc), 1)
        self.assertEqual(errors, [exc])
        pending.kill()
        pending.wait(1)
        coord.retire(0.2)

    def test_errors_implies_text_like_python312(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        with patch.object(
            owned_rvc_run_module,
            "spawn_owned_voice_worker",
            return_value=_Owner(),
        ):
            result = _run_coord(coord, errors="ignore")
        self.assertIsInstance(result.stdout, str)

    def test_capture_only_returns_bytes(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        with patch.object(
            owned_rvc_run_module,
            "spawn_owned_voice_worker",
            return_value=_Owner(),
        ):
            result = _run_coord(coord)
        self.assertIsInstance(result.stdout, bytes)

    def test_infer_io_lease_released_on_success(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy, _python, model = _policy(root)
            bind_fake_rvc_runners(
                policy,
                popen_fn=lambda *a, **k: _Owner(),
                owned_worker_job_backend=FakeOwnedWorkerJobBackend(),
            )
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            inp = mgr.create(".wav")
            out = mgr.create(".wav")
            launch = RvcInferLaunch(
                input_path=inp,
                output_path=out,
                model_path=str(model),
                device="cpu",
                pitch=0,
                index_rate=0.55,
                protect=0.5,
                filter_radius=3,
                rms_mix_rate=0.75,
                f0_method="rmvpe",
            )
            policy.run_rvc_infer(
                launch,
                temp_file_manager=mgr,
                capture_output=True,
                timeout=90,
            )
            self.assertTrue(policy.rvc_runs_quiescent())
            self.assertEqual(mgr.active_io_lease_count(), 0)

    def test_spawn_failure_quiescent_after_retire(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        original = RuntimeError("spawn-fail")
        with patch.object(
            owned_rvc_run_module,
            "spawn_owned_voice_worker",
            side_effect=original,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _run_coord(coord)
            self.assertIs(ctx.exception, original)
        self.assertTrue(coord.retire(0.5))
        self.assertTrue(coord.quiescent())

    def test_invalid_timeout_zero_spawn(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        alloc: list[int] = []

        def spawn(*_a, **_k):
            alloc.append(1)
            return _Owner()

        with patch.object(
            owned_rvc_run_module, "spawn_owned_voice_worker", side_effect=spawn
        ):
            with self.assertRaises(PermissionError):
                coord.execute(
                    ["fake"],
                    parent_pid=os.getpid(),
                    popen_fn=None,
                    job_backend=None,
                    max_timeout_budget=2.0,
                    timeout=0,
                    capture_output=True,
                )
        self.assertEqual(alloc, [])

    def test_second_capture_start_preserves_original(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        original = ValueError("controlled second thread start failure")
        count = [0]
        real_start = owned_rvc_run_module._StreamCapturer.start

        def start_once(cap):
            count[0] += 1
            if count[0] == 2:
                raise original
            return real_start(cap)

        with patch.object(
            owned_rvc_run_module, "spawn_owned_voice_worker", return_value=_Owner()
        ), patch.object(
            owned_rvc_run_module._StreamCapturer, "start", start_once
        ):
            with self.assertRaises(ValueError) as ctx:
                _run_coord(coord)
            self.assertIs(ctx.exception, original)
        coord.retire(0.2)

    def test_rejected_execute_does_not_clear_active_ticket(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        entered = threading.Event()
        release = threading.Event()

        def spawn(*_a, **_k):
            entered.set()
            release.wait(2)
            return _Owner()

        def first() -> None:
            _run_coord(coord)

        with patch.object(
            owned_rvc_run_module, "spawn_owned_voice_worker", side_effect=spawn
        ):
            t1 = threading.Thread(target=first)
            try:
                t1.start()
                self.assertTrue(entered.wait(2.0))
                with self.assertRaises(PermissionError):
                    _run_coord(coord)
                self.assertFalse(coord.quiescent())
            finally:
                release.set()
                t1.join(3.0)
        self.assertFalse(t1.is_alive())
        coord.retire(0.3)

    def test_three_request_spawn_capture_retire_gate(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        spawn_entered = threading.Event()
        spawn_release = threading.Event()
        capture_entered = threading.Event()
        capture_release = threading.Event()
        spawned: list[int] = []

        class _SlowStdout:
            def __init__(self) -> None:
                self._sent = False

            def read(self, size: int) -> bytes:
                if self._sent:
                    return b""
                self._sent = True
                capture_entered.set()
                capture_release.wait(2)
                return b"ok"

            def close(self) -> None:
                pass

        def spawn_with_slow_stream(*_a, **_k):
            spawned.append(1)
            spawn_entered.set()
            spawn_release.wait(2)
            owner = _Owner()
            owner.stdout = _SlowStdout()  # type: ignore[assignment]
            return owner

        outcomes: list[subprocess.CompletedProcess] = []

        def first() -> None:
            outcomes.append(
                coord.execute(
                    ["fake"],
                    parent_pid=os.getpid(),
                    popen_fn=None,
                    job_backend=None,
                    max_timeout_budget=8.0,
                    timeout=8.0,
                    capture_output=True,
                )
            )

        with patch.object(
            owned_rvc_run_module,
            "spawn_owned_voice_worker",
            side_effect=spawn_with_slow_stream,
        ):
            t1 = threading.Thread(target=first)
            try:
                t1.start()
                self.assertTrue(spawn_entered.wait(2.0))
                with self.assertRaises(PermissionError):
                    _run_coord(coord)
                self.assertFalse(coord.quiescent())
                with coord._lock:
                    self.assertGreater(coord._run_inflight, 0)
                spawn_release.set()
                self.assertTrue(capture_entered.wait(2.0))
                with self.assertRaises(PermissionError):
                    _run_coord(coord)
                capture_release.set()
                t1.join(3.0)
            finally:
                spawn_release.set()
                capture_release.set()
                t1.join(1)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].stdout, b"ok")
        self.assertTrue(coord.retire(0.3))
        self.assertTrue(coord.quiescent())

    def test_retire_pending_cleanup_sets_retiring(self) -> None:
        from src.core.owned_rvc_run import _RvcRunRecord

        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        coord._record = _RvcRunRecord(generation=1, starting=False)
        seen: list[bool] = []
        original = coord._retire_record

        def track_retire(rec, deadline):
            with coord._lock:
                seen.append(coord._retiring)
            return original(rec, deadline)

        coord._retire_record = track_retire  # type: ignore[method-assign]
        self.assertTrue(coord.retire(0.2))
        self.assertEqual(seen, [True])

    def test_retire_during_reservation_before_spawn(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        reserved = threading.Event()
        release = threading.Event()
        spawned: list[int] = []
        original_ensure = coord._ensure_previous_reclaimed

        def gated_ensure():
            out = original_ensure()
            reserved.set()
            release.wait(2)
            return out

        coord._ensure_previous_reclaimed = gated_ensure  # type: ignore[method-assign]
        outcomes: list[subprocess.CompletedProcess] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                outcomes.append(_run_coord(coord))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(
            owned_rvc_run_module,
            "spawn_owned_voice_worker",
            side_effect=lambda *_a, **_k: spawned.append(1) or _Owner(),
        ):
            t = threading.Thread(target=worker)
            try:
                t.start()
                self.assertTrue(reserved.wait(2.0))
                self.assertFalse(coord.retire(0.05))
                self.assertFalse(coord.quiescent())
            finally:
                release.set()
                t.join(3.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(outcomes, [])
        if spawned:
            self.assertTrue(
                all(isinstance(e, PermissionError) for e in errors),
                errors,
            )
        coord.retire(0.5)

    def test_active_retire_requests_process_termination(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)
        owner = _Owner(code=None)
        owner.stdout = owner.stderr = None
        entered = threading.Event()
        outcome: list[str] = []

        def spawn(*_a, **_k):
            entered.set()
            return owner

        def worker() -> None:
            try:
                _run_coord(coord)
            except BaseException as e:
                outcome.append(type(e).__name__)

        with patch.object(
            owned_rvc_run_module, "spawn_owned_voice_worker", side_effect=spawn
        ):
            t = threading.Thread(target=worker)
            try:
                t.start()
                self.assertTrue(entered.wait(2.0))
                self.assertFalse(coord.retire(0.05))
                self.assertTrue(owner.killed)
            finally:
                if not owner.killed:
                    owner.kill()
                t.join(3.0)
        self.assertFalse(t.is_alive())

    def test_io_lease_query_failure_fail_closed(self) -> None:
        policy = type("P", (), {})()
        coord = OwnedRvcRunCoordinator(policy)

        class _Mgr:
            def pin_io_paths(self, paths):
                return "tok"

            def release_io_lease(self, token):
                pass

            def io_lease_active(self, token):
                raise RuntimeError("query failed")

        with patch.object(
            owned_rvc_run_module, "spawn_owned_voice_worker", return_value=_Owner()
        ):
            with self.assertRaises(PermissionError):
                coord.execute(
                    ["fake"],
                    parent_pid=os.getpid(),
                    popen_fn=None,
                    job_backend=None,
                    max_timeout_budget=2.0,
                    timeout=2.0,
                    capture_output=True,
                    io_paths=("a.wav", "b.wav"),
                    temp_file_manager=_Mgr(),
                )
        self.assertFalse(coord.retire(0.2))

    def test_rvc_infer_runpy_missing_job_before_heavy_imports(self) -> None:
        seen: list[str] = []
        blocker = _heavy_import_blocker(seen)
        sys.meta_path.insert(0, blocker)
        try:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit) as ctx:
                    runpy.run_path(str(_RVC_INFER_SCRIPT), run_name="__main__")
            self.assertEqual(ctx.exception.code, 2)
        finally:
            sys.meta_path.remove(blocker)
        self.assertEqual(seen, [])

    def test_rvc_probe_runpy_missing_job_before_rvc_python(self) -> None:
        seen: list[str] = []
        blocker = _heavy_import_blocker(seen)
        argv = build_probe_argv(Path("x"), RvcProbeKind.IMPORT_RVC)
        code = argv[2]
        sys.meta_path.insert(0, blocker)
        try:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit) as ctx:
                    exec(code, {"__name__": "__main__"})
            self.assertEqual(ctx.exception.code, 2)
        finally:
            sys.meta_path.remove(blocker)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
