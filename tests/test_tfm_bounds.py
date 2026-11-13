"""TempFileManager 有界 owned / pin 待删 / 单 Worker 契约（纯 fixture 目录）。"""
from __future__ import annotations

import os
import queue
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core.safety import Operation, SafetyPolicy
from src.voice.audio.temp_files import (
    OwnedCapacityExceededError,
    TempFileManager,
)


def _policy(root: Path) -> SafetyPolicy:
    p = SafetyPolicy({"permissions": {Operation.FILE_DELETE.value: True}})
    p.register_file_delete_dir(root)
    return p


class OwnedCapacityTests(unittest.TestCase):
    def test_owned_capacity_validation(self) -> None:
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        for bad in (True, 1.5, 0, -1, 1025, "128"):
            with self.assertRaises(ValueError):
                TempFileManager(safety_policy=policy, owned_capacity=bad)  # type: ignore[arg-type]

    def test_create_and_adopt_respect_capacity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root, owned_capacity=2)
            a = mgr.create(".wav")
            b = mgr.create(".wav")
            self.assertEqual(mgr.owned_count(), 2)
            with self.assertRaises(OwnedCapacityExceededError):
                mgr.create(".wav")
            self.assertEqual(mgr.owned_count(), 2)
            self.assertTrue(Path(a).exists())
            self.assertTrue(Path(b).exists())

            extra = root / "external.wav"
            extra.write_bytes(b"x")
            with self.assertRaises(OwnedCapacityExceededError):
                mgr.adopt_existing(extra)
            self.assertFalse(mgr.is_owned(extra))

    def test_duplicate_adopt_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root, owned_capacity=1)
            p = mgr.create(".wav")
            self.assertEqual(mgr.adopt_existing(p), p)
            self.assertEqual(mgr.owned_count(), 1)

    def test_concurrent_create_never_exceeds_capacity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root, owned_capacity=4)
            errors: list[BaseException] = []
            lock = threading.Lock()

            def worker() -> None:
                try:
                    for _ in range(8):
                        mgr.create(".wav")
                except BaseException as exc:
                    with lock:
                        errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
            self.assertLessEqual(mgr.owned_count(), 4)
            cap_errors = [e for e in errors if isinstance(e, OwnedCapacityExceededError)]
            self.assertTrue(cap_errors or mgr.owned_count() == 4)
            mgr.shutdown(timeout=2.0)

    def test_recycle_slot_after_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root, owned_capacity=1)
            first = mgr.create(".wav")
            mgr.remove(first)
            second = mgr.create(".wav")
            self.assertNotEqual(first, second)
            self.assertEqual(mgr.owned_count(), 1)
            mgr.shutdown(timeout=1.0)


class PinPendingCleanupTests(unittest.TestCase):
    def test_last_release_triggers_worker_delete_without_main_remove(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy,
                default_dir=root,
                delayed_queue_capacity=8,
            )
            path = mgr.create(".wav")
            t1 = mgr.pin_io_paths((path,))
            t2 = mgr.pin_io_paths((path,))
            remove_calls: list[str] = []
            start_calls: list[threading.Thread] = []
            real_start = threading.Thread.start

            def track_start(self, *a, **k):
                start_calls.append(self)
                return real_start(self, *a, **k)

            def do_remove(p: str) -> None:
                remove_calls.append(p)
                Path(p).unlink(missing_ok=True)

            with patch("src.voice.audio.temp_files.os.remove", side_effect=do_remove), patch(
                "threading.Thread.start", track_start
            ):
                mgr.remove(path)
                mgr.release_io_lease(t1)
                self.assertTrue(Path(path).exists())
                self.assertEqual(mgr.pending_cleanup_count(), 1)
                mgr.release_io_lease(t2)
                deadline = time.monotonic() + 5.0
                while Path(path).exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
            self.assertFalse(Path(path).exists())
            self.assertEqual(remove_calls, [path])
            self.assertTrue(
                all(t.name == "TempFileDelayedDelete" for t in start_calls)
            )
            mgr.shutdown(timeout=1.0)

    def test_remove_while_pinned_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            token = mgr.pin_io_paths((path,))
            mgr.remove(path)
            mgr.remove(path)
            self.assertEqual(mgr.pending_cleanup_count(), 1)
            mgr.release_io_lease(token)
            mgr.shutdown(timeout=1.0)


class QueueBoundsTests(unittest.TestCase):
    def test_queue_overflow_dedupes_and_drains(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy,
                default_dir=root,
                delayed_queue_capacity=1,
            )
            paths = [mgr.create(".wav") for _ in range(4)]
            for p in paths:
                mgr.remove_delayed(p, delay=0.01, max_retries=2)
            for _ in range(3):
                mgr.remove_delayed(paths[0], delay=0.01, max_retries=2)
            self.assertEqual(mgr.pending_cleanup_count(), 4)
            status = mgr.shutdown(timeout=3.0)
            self.assertTrue(status.complete)
            for p in paths:
                self.assertFalse(Path(p).exists())


class SafetyIdentityTests(unittest.TestCase):
    def test_replaced_file_not_deleted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            decoy = root / "decoy.wav"
            decoy.write_bytes(b"keep")
            Path(path).unlink()
            os.link(decoy, path) if hasattr(os, "link") else decoy.replace(path)
            if not Path(path).exists():
                decoy.rename(path)
            mgr.remove(path)
            self.assertTrue(Path(path).exists())
            self.assertFalse(mgr.is_owned(path))
            mgr.shutdown(timeout=0.5)


class WorkerLifecycleTests(unittest.TestCase):
    def test_release_after_ctor_failure_does_not_start_worker(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            token = mgr.pin_io_paths((path,))

            def boom(*a, **k):
                raise MemoryError("no thread")

            with patch("threading.Thread", side_effect=boom):
                with self.assertRaises(MemoryError):
                    mgr.remove(path)
            self.assertEqual(mgr.pending_cleanup_count(), 1)
            real_start = threading.Thread.start
            starts: list[threading.Thread] = []

            def track_start(self, *a, **k):
                starts.append(self)
                return real_start(self, *a, **k)

            with patch.object(threading.Thread, "start", track_start):
                mgr.release_io_lease(token)
            self.assertEqual(starts, [])
            mgr.shutdown(timeout=1.0)

    def test_ensure_worker_reraises_keyboard_interrupt(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            real_start = threading.Thread.start
            sentinel = KeyboardInterrupt("stop bootstrap")

            def ki_start(self, *a, **k):
                raise sentinel

            with patch.object(threading.Thread, "start", ki_start):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    mgr.remove_delayed(path, delay=0.01)
                self.assertIs(ctx.exception, sentinel)
            st = mgr.shutdown_status()
            self.assertTrue(st.worker_start_unknown)
            mgr.shutdown(timeout=1.0)

    def test_thread_ctor_failure_releases_slot(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)

            def boom(*a, **k):
                raise MemoryError("no thread")

            with patch("threading.Thread", side_effect=boom):
                with self.assertRaises(MemoryError):
                    mgr.remove_delayed(mgr.create(".wav"), delay=0.01)
            status = mgr.shutdown_status()
            self.assertFalse(status.worker_creator_pending)
            self.assertFalse(status.worker_alive)

    def test_start_unknown_shutdown_incomplete(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            real_start = threading.Thread.start
            gate = threading.Event()

            def slow_start(self, *a, **k):
                gate.set()
                time.sleep(0.3)
                return real_start(self, *a, **k)

            with patch("threading.Thread.start", slow_start):
                threading.Thread(
                    target=lambda: mgr.remove_delayed(path, delay=0.01),
                    daemon=True,
                ).start()
                self.assertTrue(gate.wait(timeout=2.0))
                st = mgr.shutdown(timeout=0.05)
            self.assertFalse(st.complete)
            self.assertTrue(
                st.worker_start_unknown
                or st.worker_creator_pending
                or st.pending_cleanup > 0
            )
            mgr.shutdown(timeout=2.0)

    def test_shutdown_joins_worker_no_leak(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            mgr.remove_delayed(path, delay=0.05)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                st = mgr.shutdown_status()
                if not st.worker_alive and st.inflight == 0:
                    break
                time.sleep(0.02)
            st = mgr.shutdown(timeout=2.0)
            self.assertFalse(st.worker_alive)
            self.assertTrue(st.complete or mgr.pending_cleanup_count() == 0)

    def test_stop_after_outer_check_prevents_worker_start(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            real_check = mgr._stop.is_set
            first = [True]

            def check() -> bool:
                if first[0]:
                    first[0] = False
                    mgr._stop.set()
                    return False
                return real_check()

            with patch.object(mgr._stop, "is_set", side_effect=check), patch(
                "threading.Thread"
            ) as create:
                mgr._ensure_worker()
            create.assert_not_called()
            mgr.shutdown(timeout=0.5)

    def test_retry_exhausted_not_auto_requeued_by_pump(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy,
                default_dir=root,
                delayed_queue_capacity=4,
            )
            path = mgr.create(".wav")
            key = mgr._normalize_key(path)
            assert key is not None
            ident = mgr._owned[key]
            with mgr._lock:
                mgr._note_pending_cleanup_locked(key, ident, 0.0, 1)
                mgr._retry_exhausted.add(key)
                mgr._pump_pending_into_queue_locked()
            self.assertEqual(mgr.pending_delayed(), 0)
            self.assertEqual(mgr.pending_cleanup_count(), 1)
            mgr.shutdown(timeout=1.0)


class DeleteClaimTests(unittest.TestCase):
    def test_blocked_delete_does_not_block_bounded_shutdown(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            entered = threading.Event()
            release = threading.Event()
            closed = threading.Event()
            results: list[object] = []
            errors: list[BaseException] = []
            raw_remove = os.remove

            def remove(p: str) -> None:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("delete gate timeout")
                raw_remove(p)

            def close() -> None:
                try:
                    results.append(mgr.shutdown(timeout=0.03))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    closed.set()

            closer = threading.Thread(target=close)
            try:
                with patch("src.voice.audio.temp_files.os.remove", side_effect=remove):
                    mgr.remove_delayed(path, delay=0, max_retries=1)
                    self.assertTrue(entered.wait(3))
                    closer.start()
                    self.assertTrue(closed.wait(0.7), "shutdown blocked on registry lock")
                    if errors:
                        raise errors[0]
                    self.assertEqual(len(results), 1)
                    self.assertFalse(getattr(results[0], "complete", True))
            finally:
                release.set()
                if closer.ident is not None:
                    closer.join(3)
                self.assertFalse(closer.is_alive())
                mgr.shutdown(timeout=2.0)

    def test_pin_rejected_while_delete_claim_active(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            entered = threading.Event()
            release = threading.Event()
            raw_remove = os.remove

            def remove(p: str) -> None:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("delete gate timeout")
                raw_remove(p)

            try:
                with patch("src.voice.audio.temp_files.os.remove", side_effect=remove):
                    mgr.remove_delayed(path, delay=0, max_retries=1)
                    self.assertTrue(entered.wait(3))
                    with self.assertRaises(PermissionError):
                        mgr.pin_io_paths((path,))
            finally:
                release.set()
                mgr.shutdown(timeout=2.0)


    def test_adopt_rejected_while_delete_in_progress(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            entered, release = threading.Event(), threading.Event()
            raw_remove = os.remove

            def remove(p: str) -> None:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("delete gate timeout")
                raw_remove(p)

            try:
                with patch("src.voice.audio.temp_files.os.remove", side_effect=remove):
                    mgr.remove_delayed(path, delay=0, max_retries=1)
                    self.assertTrue(entered.wait(3))
                    with self.assertRaises(RuntimeError):
                        mgr.adopt_existing(path)
            finally:
                release.set()
                mgr.shutdown(timeout=2.0)

    def test_external_unlink_during_delete_retains_capacity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy, default_dir=root, owned_capacity=1
            )
            path = mgr.create(".wav")
            entered, release = threading.Event(), threading.Event()
            raw_remove = os.remove
            worker: threading.Thread | None = None

            def remove(p: str) -> None:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("delete gate timeout")
                raw_remove(p)

            try:
                with patch("src.voice.audio.temp_files.os.remove", side_effect=remove):
                    mgr.remove_delayed(path, delay=0, max_retries=1)
                    worker = mgr._worker
                    self.assertTrue(entered.wait(3))
                    raw_remove(path)
                    self.assertFalse(mgr.is_owned(path))
                    with self.assertRaises(OwnedCapacityExceededError):
                        mgr.create(".wav")
            finally:
                release.set()
                mgr.shutdown(timeout=2.0)
                if worker is not None and worker.ident is not None:
                    worker.join(3)
                self.assertTrue(mgr.shutdown(timeout=1.0).complete)


class GhostPendingTests(unittest.TestCase):
    def test_replaced_file_after_pin_release_drains_pending_on_shutdown(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy, default_dir=root, owned_capacity=2
            )
            path = mgr.create(".wav")
            lease = mgr.pin_io_paths((path,))
            try:
                mgr.remove(path)
                replacement = root / "replacement.wav"
                replacement.write_bytes(b"foreign")
                os.replace(replacement, path)
                mgr.release_io_lease(lease)
                self.assertFalse(mgr.is_owned(path))
                status = mgr.shutdown()
                self.assertTrue(status.complete, repr(status))
                self.assertEqual(mgr.pending_cleanup_count(), 0)
                self.assertEqual(Path(path).read_bytes(), b"foreign")
            finally:
                mgr.release_io_lease(lease)
                mgr.shutdown(timeout=2.0)

    def test_stale_queue_task_on_stop_does_not_recreate_pending(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            try:
                mgr.remove_delayed(path, delay=50, max_retries=1)
                self.assertEqual(mgr.pending_cleanup_count(), 1)
                self.assertEqual(mgr.pending_delayed(), 0)
                replacement = root / "replacement.wav"
                replacement.write_bytes(b"foreign")
                os.replace(replacement, path)
                self.assertFalse(mgr.is_owned(path))
                mgr._stop.set()
                mgr._worker_wakeup.set()
                status = mgr.shutdown(timeout=2.0)
                self.assertTrue(status.complete, repr(status))
                self.assertEqual(mgr.pending_cleanup_count(), 0)
            finally:
                mgr.shutdown(timeout=1.0)


class StaleQueueWorkerTests(unittest.TestCase):
    def test_old_queue_item_does_not_drop_new_identity_or_stop_worker(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy, default_dir=root, owned_capacity=4
            )
            blocker, path, other = [mgr.create(".wav") for _ in range(3)]
            replacement = root / "replacement.wav"
            replacement.write_bytes(b"new owned identity")
            entered = threading.Event()
            release = threading.Event()
            done = threading.Event()
            removed: list[str] = []
            raw_remove = os.remove

            def remove(p: str) -> None:
                if p == blocker:
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("blocker delete gate timeout")
                raw_remove(p)
                removed.append(p)
                if other in removed:
                    done.set()

            worker: threading.Thread | None = None
            path_key = mgr._normalize_key(path)
            assert path_key is not None
            obsolete_path_checks: list[bool] = []
            real_clear = mgr._clear_pending_for_obsolete_queue_item_locked

            def observe_obsolete_clear(key: str, identity) -> None:
                if key == path_key:
                    with mgr._lock:
                        obsolete_path_checks.append(path_key in mgr._queued_paths)
                real_clear(key, identity)

            try:
                with patch(
                    "src.voice.audio.temp_files.os.remove", side_effect=remove
                ), patch.object(
                    mgr,
                    "_clear_pending_for_obsolete_queue_item_locked",
                    side_effect=observe_obsolete_clear,
                ):
                    mgr.remove_delayed(blocker, delay=0, max_retries=1)
                    worker = mgr._worker
                    self.assertTrue(entered.wait(3))
                    mgr.remove_delayed(path, delay=0, max_retries=1)
                    # 不用 mgr.remove(path)：worker 持 blocker claim 时前台 remove 可能互等；
                    # 身份替换由 replace+adopt 驱动，与 stale ready 断言一致。
                    os.replace(replacement, path)
                    self.assertEqual(mgr.adopt_existing(path), path)
                    mgr.remove_delayed(path, delay=0, max_retries=1)
                    with mgr._lock:
                        self.assertIn(
                            path_key,
                            mgr._queued_paths,
                            "new identity must stay queued until valid dequeue",
                        )
                    mgr.remove_delayed(other, delay=0, max_retries=1)
                    self.assertEqual(mgr.pending_cleanup_count(), 3)
                    release.set()
                    self.assertTrue(
                        done.wait(3),
                        "stale queue item stopped the cleanup worker",
                    )
                    self.assertTrue(
                        any(obsolete_path_checks),
                        "expected obsolete handler for replaced path",
                    )
                    self.assertTrue(
                        all(obsolete_path_checks),
                        "obsolete dequeue must not clear newer identity queued marker",
                    )
                    self.assertTrue(mgr.shutdown().complete)
                    self.assertFalse(Path(path).exists())
                    self.assertFalse(Path(other).exists())
                    self.assertEqual(mgr.pending_cleanup_count(), 0)
            finally:
                release.set()
                mgr.shutdown(timeout=2.0)
                if worker is not None and worker.ident is not None:
                    worker.join(3)
                if worker is not None:
                    self.assertFalse(worker.is_alive())


class DeleteErrorReleaseTests(unittest.TestCase):
    def test_interrupted_or_unexpected_delete_releases_claim(self) -> None:
        for failure in (
            KeyboardInterrupt("injected delete interrupt"),
            ValueError("injected unexpected delete error"),
        ):
            with self.subTest(kind=type(failure).__name__), TemporaryDirectory() as tmp:
                root = Path(tmp)
                policy = _policy(root)
                mgr = TempFileManager(
                    safety_policy=policy, default_dir=root, owned_capacity=1
                )
                path = mgr.create(".wav")
                caught: BaseException | None = None
                token: str | None = None
                try:
                    with patch(
                        "src.voice.audio.temp_files.os.remove",
                        side_effect=failure,
                    ):
                        try:
                            mgr.remove(path)
                        except BaseException as exc:
                            caught = exc
                    self.assertIs(caught, failure)
                    self.assertTrue(mgr.is_owned(path))
                    self.assertEqual(mgr.pending_cleanup_count(), 1)
                    token = mgr.pin_io_paths((path,))
                    mgr.release_io_lease(token)
                    token = None
                    self.assertTrue(mgr.shutdown().complete)
                finally:
                    if token is not None:
                        mgr.release_io_lease(token)
                    mgr.shutdown(timeout=1.0)

    def test_permanent_os_error_keeps_pending_without_worker(self) -> None:
        import errno

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy, default_dir=root, owned_capacity=1
            )
            path = mgr.create(".wav")
            try:
                with patch(
                    "src.voice.audio.temp_files.os.remove",
                    side_effect=OSError(errno.EIO, "injected IO error"),
                ) as remove:
                    mgr.remove(path)
                    self.assertEqual(remove.call_count, 1)
                self.assertTrue(mgr.is_owned(path))
                self.assertEqual(mgr.pending_cleanup_count(), 1)
                self.assertFalse(mgr.delayed_worker_alive())
                self.assertTrue(mgr.shutdown().complete)
            finally:
                mgr.shutdown(timeout=1.0)


class WorkerControlTests(unittest.TestCase):
    """使用 threading.Event 控制 bootstrap / 迟启 / 终态。"""

    def test_late_start_sees_stop_and_exits(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            allow_loop = threading.Event()
            loop_ready = threading.Event()
            loop_done = threading.Event()
            worker_ref: list[threading.Thread] = []
            real_target = None

            def hooked_loop() -> None:
                loop_ready.set()
                if not allow_loop.wait(5):
                    raise AssertionError("allow_loop handshake timeout")
                try:
                    if real_target:
                        real_target()
                finally:
                    loop_done.set()

            orig_init = threading.Thread.__init__

            def hook_init(self, *a, **k):
                nonlocal real_target
                name = k.get("name")
                if name == "TempFileDelayedDelete":
                    real_target = k.get("target")
                    k = dict(k)
                    k["target"] = hooked_loop
                orig_init(self, *a, **k)
                if name == "TempFileDelayedDelete":
                    worker_ref.append(self)

            try:
                with patch.object(threading.Thread, "__init__", hook_init):
                    mgr.remove_delayed(path, delay=0.01)
                self.assertTrue(loop_ready.wait(timeout=3.0))
                self.assertEqual(len(worker_ref), 1)
                st = mgr.shutdown(timeout=0.2)
                self.assertFalse(st.complete)
            finally:
                allow_loop.set()
                self.assertTrue(loop_done.wait(5), "worker target did not finish")
                worker_ref[0].join(3)
                self.assertFalse(worker_ref[0].is_alive())
                mgr.shutdown(timeout=2.0)


class WorkerTerminalAliveTests(unittest.TestCase):
    def test_loop_terminal_while_thread_alive_blocks_second_worker(self) -> None:
        hold = threading.Event()
        real_loop = None
        orig_init = threading.Thread.__init__

        def hook_init(self, *a, **k):
            nonlocal real_loop
            if k.get("name") == "TempFileDelayedDelete":
                real_loop = k.get("target")

                def wrapped() -> None:
                    assert real_loop is not None
                    real_loop()
                    if not hold.wait(5):
                        raise AssertionError("post-terminal hold timeout")

                k = dict(k)
                k["target"] = wrapped
            return orig_init(self, *a, **k)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            try:
                with patch.object(threading.Thread, "__init__", hook_init):
                    mgr.remove_delayed(path, delay=0.01, max_retries=1)
                self.assertTrue(mgr._worker_entered.wait(timeout=3))
                gone = time.monotonic() + 5.0
                while Path(path).exists() and time.monotonic() < gone:
                    time.sleep(0.02)
                self.assertFalse(Path(path).exists())
                real_is_set = mgr._stop.is_set

                def stop_after_delete() -> bool:
                    if Path(path).exists():
                        return real_is_set()
                    return True

                with patch.object(mgr._stop, "is_set", side_effect=stop_after_delete):
                    mgr._worker_wakeup.set()
                    self.assertTrue(
                        mgr._worker_terminal.wait(5),
                        "worker loop did not reach terminal",
                    )
                self.assertTrue(mgr.delayed_worker_alive())
                start_calls: list[threading.Thread] = []
                real_start = threading.Thread.start

                def track_start(self, *a, **k):
                    start_calls.append(self)
                    return real_start(self, *a, **k)

                with patch.object(threading.Thread, "start", track_start):
                    mgr._ensure_worker()
                self.assertEqual(start_calls, [])
            finally:
                hold.set()
                worker = mgr._worker
                if worker is not None and worker.ident is not None:
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                st = mgr.shutdown(timeout=2.0)
                self.assertFalse(st.worker_alive)


if __name__ == "__main__":
    unittest.main()
