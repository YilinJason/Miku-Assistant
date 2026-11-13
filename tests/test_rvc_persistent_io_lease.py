"""持久 RVC convert IO lease 与全局 client 生命周期（纯 fake）。"""
from __future__ import annotations

import io
import queue
import subprocess
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from src.core.safety import SafetyPolicy, TrustedRvcProfile
from src.voice.audio.temp_files import TempFileManager
from src.voice.rvc.infer import (
    RvcWorkerUnavailableError,
    _config_key,
    get_rvc_worker,
    stop_rvc_worker,
)
from src.voice.rvc.worker import RVCWorkerClient, _RVCStdoutReader

from tests.support.host_memory_fixtures import install_module_sufficient_host_memory


def setUpModule() -> None:
    install_module_sufficient_host_memory()


def _policy(tmp: Path) -> SafetyPolicy:
    python = tmp / "python.exe"
    python.write_bytes(b"")
    model = tmp / "m.pth"
    model.write_bytes(b"x")
    return SafetyPolicy(
        {
            "permissions": {
                "rvc_worker": True,
                "rvc_infer": True,
                "subprocess": False,
            }
        },
        trusted_rvc_profile=TrustedRvcProfile(python_path=python, model_path=model),
    )


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self._code: int | None = None
        self.terminated = False

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        if self._code is not None:
            return self._code
        if self.terminated:
            self._code = 0
            return 0
        raise subprocess.TimeoutExpired(cmd=["fake-proc"], timeout=timeout or 0)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self._code = 1


class _SurviveKillProc(_FakeProc):
    """terminate/kill 后仍 poll None，直至显式允许 kill 生效。"""

    def __init__(self, *, survive_kill: bool = True) -> None:
        super().__init__()
        self._survive_kill = survive_kill

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        if self._code is not None:
            return self._code
        raise subprocess.TimeoutExpired(cmd=["fake-proc"], timeout=timeout or 0)

    def kill(self) -> None:
        if self._survive_kill:
            return
        super().kill()


@contextmanager
def _patch_infer_singleton(
    client: RVCWorkerClient | None = None,
    config_key: str | None = None,
):
    import src.voice.rvc.infer as infer_mod

    prev_client = infer_mod._worker_client
    prev_key = infer_mod._worker_config_key
    infer_mod._worker_client = client
    infer_mod._worker_config_key = config_key
    try:
        yield infer_mod
    finally:
        infer_mod._worker_client = prev_client
        infer_mod._worker_config_key = prev_key


def _install_reader(client: RVCWorkerClient, payload: dict) -> None:
    q: queue.Queue[dict] = queue.Queue(maxsize=1)
    q.put(payload)

    class _StaticReader:
        def poll_message(self, timeout: float):
            try:
                return q.get(timeout=max(0.0, timeout))
            except queue.Empty:
                return None

        def close(self) -> None:
            pass

        def join(self, timeout: float) -> bool:
            return True

        def is_alive(self) -> bool:
            return False

    client._reader = _StaticReader()  # noqa: SLF001


def _release_client_lease(client: RVCWorkerClient) -> None:
    proc = getattr(client, "_proc", None)
    if isinstance(proc, _SurviveKillProc) and proc._survive_kill:
        proc._survive_kill = False
        proc.kill()
    client.stop()


def _clear_infer_singleton() -> None:
    import src.voice.rvc.infer as infer_mod

    client = infer_mod._worker_client
    if client is not None:
        proc = getattr(client, "_proc", None)
        if isinstance(proc, _SurviveKillProc) and proc._survive_kill:
            proc._survive_kill = False
            proc.kill()
        stop_rvc_worker()
    infer_mod._worker_client = None
    infer_mod._worker_config_key = None


@unittest.skipUnless(__import__("sys").platform == "win32", "rvc worker lease tests")
class PersistentRvcIoLeaseTests(unittest.TestCase):
    def tearDown(self) -> None:
        _clear_infer_singleton()

    def test_first_pin_does_not_deadlock(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            client = RVCWorkerClient(
                {"model_path": str(root / "m.pth"), "device": "cpu"},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            proc = _FakeProc()
            client._proc = proc  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            client._start_locked = lambda: True  # noqa: SLF001
            _install_reader(client, {"ok": True})
            inp = mgr.create(".wav")
            out = mgr.create(".wav")
            try:
                self.assertTrue(client.convert(inp, out, timeout=1.0))
                self.assertEqual(mgr.active_io_lease_count(), 0)
            finally:
                _release_client_lease(client)
                mgr.shutdown()

    def test_convert_success_releases_pin(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            client = RVCWorkerClient(
                {"model_path": str(root / "m.pth"), "device": "cpu"},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            proc = _FakeProc()
            client._proc = proc  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            client._start_locked = lambda: True  # noqa: SLF001
            _install_reader(client, {"ok": True, "infer_ms": 1, "total_ms": 2})
            inp = mgr.create(".wav")
            out = mgr.create(".wav")
            try:
                self.assertTrue(client.convert(inp, out, timeout=1.0))
                self.assertEqual(mgr.active_io_lease_count(), 0)
            finally:
                _release_client_lease(client)
                mgr.shutdown()

    def test_pin_two_paths_refcount_two(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            a = mgr.create(".wav")
            b = mgr.create(".wav")
            token = mgr.pin_io_paths((a, b))
            try:
                self.assertEqual(mgr.active_io_lease_count(), 2)
                self.assertTrue(mgr.io_lease_active(token))
            finally:
                mgr.release_io_lease(token)
                mgr.shutdown()

    def test_unknown_response_retains_pin_until_owner_quiescent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            client = RVCWorkerClient(
                {"model_path": str(root / "m.pth"), "device": "cpu"},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            proc = _SurviveKillProc(survive_kill=True)
            client._proc = proc  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            client._start_locked = lambda: True  # noqa: SLF001
            reader = _RVCStdoutReader(io.StringIO())
            reader.start()
            client._reader = reader  # noqa: SLF001
            inp = mgr.create(".wav")
            out = mgr.create(".wav")
            try:
                self.assertFalse(client.convert(inp, out, timeout=0.05))
                self.assertEqual(mgr.active_io_lease_count(), 2)
                proc._survive_kill = False
                proc.kill()
                reader.close()
                reader.join(1.0)
                binding = client._owner_binding()  # noqa: SLF001
                self.assertIsNotNone(binding)
                self.assertTrue(client._try_release_owner(binding))  # noqa: SLF001
                self.assertEqual(mgr.active_io_lease_count(), 0)
            finally:
                reader.close()
                reader.join(1.0)
                _release_client_lease(client)
                mgr.shutdown()

    def test_second_convert_blocked_while_lease_retained(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            client = RVCWorkerClient(
                {"model_path": str(root / "m.pth"), "device": "cpu"},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            pin_in = mgr.create(".wav")
            pin_out = mgr.create(".wav")
            token = mgr.pin_io_paths((pin_in, pin_out))
            client._io_lease_token = token  # noqa: SLF001
            client._io_lease_mgr = mgr  # noqa: SLF001
            client._io_lease_owner_generation = 0  # noqa: SLF001
            client._io_lease_request_epoch = 0  # noqa: SLF001
            inp = mgr.create(".wav")
            out = mgr.create(".wav")
            client._start_locked = lambda: True  # noqa: SLF001
            try:
                self.assertFalse(client.convert(inp, out))
            finally:
                mgr.release_io_lease(token)
                mgr.shutdown()

    def test_pinned_file_not_removed_by_delayed_cleanup(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            out = mgr.create(".wav")
            token = mgr.pin_io_paths((out, out))
            try:
                mgr.remove_delayed(out, delay=0.01)
                threading.Event().wait(0.05)
                self.assertTrue(Path(out).exists())
            finally:
                mgr.release_io_lease(token)
                mgr.shutdown()

    def test_multiple_success_cycles_pin_count_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            client = RVCWorkerClient(
                {"model_path": str(root / "m.pth"), "device": "cpu"},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            proc = _FakeProc()
            client._proc = proc  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            client._start_locked = lambda: True  # noqa: SLF001
            try:
                for _ in range(3):
                    _install_reader(client, {"ok": True})
                    inp = mgr.create(".wav")
                    out = mgr.create(".wav")
                    self.assertTrue(client.convert(inp, out, timeout=1.0))
                self.assertEqual(mgr.active_io_lease_count(), 0)
            finally:
                _release_client_lease(client)
                mgr.shutdown()

    def test_get_rvc_worker_requires_stop_before_rebuild(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            cfg_a = {"model_path": str(root / "m.pth"), "device": "cpu", "pitch": 0}
            cfg_b = {**cfg_a, "pitch": 1}
            client = RVCWorkerClient(cfg_a, safety_policy=policy)
            proc = _FakeProc()
            client._proc = proc  # noqa: SLF001
            client._ready = False  # noqa: SLF001
            client._last_stop_exited = False  # noqa: SLF001
            with _patch_infer_singleton(client, _config_key(cfg_a)):
                with patch.object(client, "stop", return_value=False):
                    with self.assertRaises(RvcWorkerUnavailableError):
                        get_rvc_worker(cfg_b, policy)
                import src.voice.rvc.infer as infer_mod

                self.assertIs(infer_mod._worker_client, client)

    def test_concurrent_get_single_construct(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            cfg = {"model_path": str(root / "m.pth"), "device": "cpu"}
            builds: list[int] = []
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []
            successes: list[object] = []

            class CountingClient(RVCWorkerClient):
                def __init__(self, *a, **k):
                    builds.append(1)
                    super().__init__(*a, **k)
                    self.stop = MagicMock(return_value=True)  # type: ignore[method-assign]

            def worker() -> None:
                try:
                    barrier.wait(2)
                    successes.append(get_rvc_worker(cfg, policy))
                except RvcWorkerUnavailableError as exc:
                    errors.append(exc)
                except BaseException as exc:
                    errors.append(exc)

            with patch("src.voice.rvc.worker.RVCWorkerClient", CountingClient):
                threads = [threading.Thread(target=worker) for _ in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(3.0)
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertEqual(len(builds), 1)
            self.assertGreaterEqual(len(successes), 1)
            self.assertEqual(len(successes) + len(errors), 2)
            for exc in errors:
                self.assertIsInstance(exc, RvcWorkerUnavailableError)

    def test_stop_during_slow_pin_returns_false_without_waiting(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        stop_done = threading.Event()
        stopped: list[bool] = []

        class SlowMgr(TempFileManager):
            def pin_io_paths(self, paths):  # type: ignore[override]
                entered.set()
                if not release.wait(3.0):
                    raise RuntimeError("release missing")
                return super().pin_io_paths(paths)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = SlowMgr(safety_policy=policy, default_dir=root)
            client = RVCWorkerClient(
                {"model_path": str(root / "m.pth"), "device": "cpu"},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            proc = _FakeProc()
            client._proc = proc  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            client._start_locked = lambda: True  # noqa: SLF001
            _install_reader(client, {"ok": True})
            inp = mgr.create(".wav")
            out = mgr.create(".wav")

            def convert() -> None:
                client.convert(inp, out, timeout=1.0)

            def stop() -> None:
                stopped.append(client.stop())
                stop_done.set()

            threads = [
                threading.Thread(target=convert),
                threading.Thread(target=stop),
            ]
            try:
                threads[0].start()
                self.assertTrue(entered.wait(1.0))
                threads[1].start()
                self.assertTrue(stop_done.wait(0.5))
                self.assertEqual(stopped, [False])
            finally:
                release.set()
                for t in threads:
                    t.join(4.0)
                self.assertTrue(client.stop())
                self.assertEqual(mgr.active_io_lease_count(), 0)
                mgr.shutdown()

    def test_global_stop_false_while_convert_pin_inflight(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class SlowMgr(TempFileManager):
            def pin_io_paths(self, paths):  # type: ignore[override]
                entered.set()
                if not release.wait(3.0):
                    raise RuntimeError("release missing")
                return super().pin_io_paths(paths)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = SlowMgr(safety_policy=policy, default_dir=root)
            cfg = {"model_path": str(root / "m.pth"), "device": "cpu"}
            client = RVCWorkerClient(
                cfg,
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            proc = _FakeProc()
            client._proc = proc  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            client._start_locked = lambda: True  # noqa: SLF001
            _install_reader(client, {"ok": True})
            inp = mgr.create(".wav")
            out = mgr.create(".wav")
            with _patch_infer_singleton(client, _config_key(cfg)):
                def convert() -> None:
                    client.convert(inp, out, timeout=1.0)

                t = threading.Thread(target=convert)
                try:
                    t.start()
                    self.assertTrue(entered.wait(1.0))
                    self.assertFalse(stop_rvc_worker())
                    import src.voice.rvc.infer as infer_mod

                    self.assertIs(infer_mod._worker_client, client)
                finally:
                    release.set()
                    t.join(4.0)
                    self.assertTrue(stop_rvc_worker())
                    mgr.shutdown()

    def test_repeat_stop_false_while_caller_active_then_true_after_clear(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            client = RVCWorkerClient(
                {"model_path": str(root / "m.pth"), "device": "cpu"},
                safety_policy=policy,
            )
            proc = _FakeProc()
            client._proc = proc  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            client._converting.set()  # noqa: SLF001

            self.assertFalse(client.stop())
            self.assertIsNone(client._proc)  # noqa: SLF001
            self.assertFalse(client.last_stop_exited)

            self.assertFalse(client.stop())
            self.assertFalse(client.last_stop_exited)

            client._converting.clear()  # noqa: SLF001
            self.assertTrue(client.stop())
            self.assertTrue(client.last_stop_exited)

    def test_lease_release_failure_after_raw_cleared_stop_false_then_retry(self) -> None:
        class FailReleaseMgr(TempFileManager):
            release_fail = True

            def release_io_lease(self, token):  # type: ignore[override]
                if self.release_fail:
                    raise OSError("controlled release failure")
                return super().release_io_lease(token)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = FailReleaseMgr(safety_policy=policy, default_dir=root)
            client = RVCWorkerClient(
                {"model_path": str(root / "m.pth"), "device": "cpu"},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            pin_in = mgr.create(".wav")
            pin_out = mgr.create(".wav")
            token = mgr.pin_io_paths((pin_in, pin_out))
            client._io_lease_token = token  # noqa: SLF001
            client._io_lease_mgr = mgr  # noqa: SLF001
            client._io_lease_release_failed = True  # noqa: SLF001
            client._proc = None  # noqa: SLF001

            self.assertFalse(client.stop())
            self.assertFalse(client.last_stop_exited)
            self.assertTrue(mgr.io_lease_active(token))

            mgr.release_fail = False
            self.assertTrue(client.stop())
            self.assertTrue(client.last_stop_exited)
            self.assertFalse(mgr.io_lease_active(token))
            mgr.shutdown()

    def test_global_retained_when_lease_release_fails_until_retry(self) -> None:
        class FailReleaseMgr(TempFileManager):
            release_fail = True

            def release_io_lease(self, token):  # type: ignore[override]
                if self.release_fail:
                    raise OSError("controlled release failure")
                return super().release_io_lease(token)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = FailReleaseMgr(safety_policy=policy, default_dir=root)
            cfg = {"model_path": str(root / "m.pth"), "device": "cpu"}
            client = RVCWorkerClient(
                cfg,
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            token = mgr.pin_io_paths((mgr.create(".wav"), mgr.create(".wav")))
            client._io_lease_token = token  # noqa: SLF001
            client._io_lease_mgr = mgr  # noqa: SLF001
            client._io_lease_release_failed = True  # noqa: SLF001
            client._proc = None  # noqa: SLF001
            with _patch_infer_singleton(client, _config_key(cfg)):
                self.assertFalse(stop_rvc_worker())
                import src.voice.rvc.infer as infer_mod

                self.assertIs(infer_mod._worker_client, client)
                mgr.release_fail = False
                self.assertTrue(stop_rvc_worker())
                self.assertIsNone(infer_mod._worker_client)
            mgr.shutdown()


if __name__ == "__main__":
    unittest.main()
