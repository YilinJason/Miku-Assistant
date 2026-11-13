"""TFM 有界 pending 绝对 due / 仅到期 ready / 单次 OS 尝试（独占 TemporaryDirectory）。"""
from __future__ import annotations

import errno
import os
import queue
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator
from unittest.mock import patch

from src.core.safety import Operation, SafetyPolicy
from src.voice.audio.temp_files import TempFileManager


def _policy(root: Path) -> SafetyPolicy:
    p = SafetyPolicy({"permissions": {Operation.FILE_DELETE.value: True}})
    p.register_file_delete_dir(root)
    return p


def _wait_inflight_quiet(mgr: TempFileManager, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while mgr.inflight_count() != 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    if mgr.inflight_count() != 0:
        raise AssertionError(
            f"inflight still active: {mgr.inflight_count()!r}"
        )


def _finish_tfm(mgr: TempFileManager, *, timeout: float = 3.0) -> None:
    status = mgr.shutdown(timeout=timeout)
    if not status.complete:
        raise AssertionError(f"shutdown incomplete: {status!r}")
    if mgr.owned_count() != 0:
        raise AssertionError("owned not drained")
    worker = mgr._worker  # noqa: SLF001
    if worker is not None:
        worker.join(timeout=timeout)
        if worker.is_alive():
            raise AssertionError("worker still alive after join")
    if mgr.delayed_worker_alive():
        raise AssertionError("delayed_worker_alive after shutdown")


@contextmanager
def _gate_ready_dequeue(
    mgr: TempFileManager, hold_path: str
) -> Iterator[tuple[threading.Event, threading.Event]]:
    """在 worker 从 ready 取出 ``hold_path`` 后、处理前暂停（锁外 patch）。"""
    arrived = threading.Event()
    release = threading.Event()
    real_get = mgr._delayed_queue.get_nowait  # noqa: SLF001

    def gated_get_nowait():
        try:
            item = real_get()
        except queue.Empty:
            raise
        if item[0] == hold_path:
            arrived.set()
            if not release.wait(5.0):
                raise AssertionError("ready dequeue gate timeout")
        return item

    patcher = patch.object(mgr._delayed_queue, "get_nowait", side_effect=gated_get_nowait)
    with patcher:
        try:
            yield arrived, release
        finally:
            release.set()


class RemoveDelayedValidationTests(unittest.TestCase):
    def test_rejects_non_finite_or_bool_params(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            try:
                for bad_delay in (True, float("nan"), float("inf"), -1.0):
                    with self.subTest(delay=bad_delay):
                        with self.assertRaises(ValueError):
                            mgr.remove_delayed(
                                path, delay=bad_delay, max_retries=1
                            )
                for bad_retries in (True, 1.5, -1):
                    with self.subTest(max_retries=bad_retries):
                        with self.assertRaises(ValueError):
                            mgr.remove_delayed(
                                path, delay=0.0, max_retries=bad_retries  # type: ignore[arg-type]
                            )
            finally:
                _finish_tfm(mgr)


class FutureDoesNotBlockDueZeroTests(unittest.TestCase):
    def test_long_future_pending_does_not_block_immediate_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy,
                default_dir=root,
                delayed_queue_capacity=2,
            )
            future_path = mgr.create(".wav")
            due_now = mgr.create(".wav")
            deleted = threading.Event()
            raw_remove = os.remove

            def tracked_remove(target: str) -> None:
                raw_remove(target)
                if target == due_now:
                    deleted.set()

            try:
                mgr.remove_delayed(future_path, delay=3600.0, max_retries=1)
                self.assertTrue(mgr.delayed_worker_alive())
                self.assertEqual(mgr.pending_delayed(), 0)
                with patch(
                    "src.voice.audio.temp_files.os.remove",
                    side_effect=tracked_remove,
                ):
                    try:
                        mgr.remove_delayed(due_now, delay=0.0, max_retries=1)
                        self.assertTrue(deleted.wait(2.0))
                    finally:
                        _finish_tfm(mgr)
            finally:
                _finish_tfm(mgr)


class FutureOnlyAutoDueTests(unittest.TestCase):
    def test_future_only_starts_worker_and_deletes_at_due(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            deleted = threading.Event()
            raw_remove = os.remove

            def tracked_remove(target: str) -> None:
                raw_remove(target)
                if target == path:
                    deleted.set()

            try:
                mgr.remove_delayed(path, delay=0.15, max_retries=1)
                self.assertTrue(mgr.delayed_worker_alive())
                self.assertEqual(mgr.pending_delayed(), 0)
                with patch(
                    "src.voice.audio.temp_files.os.remove",
                    side_effect=tracked_remove,
                ):
                    try:
                        self.assertTrue(deleted.wait(2.0))
                        self.assertFalse(Path(path).exists())
                    finally:
                        _finish_tfm(mgr)
            finally:
                _finish_tfm(mgr)


class LockBackoffYieldsWorkerTests(unittest.TestCase):
    def test_locked_path_backoff_does_not_block_other_due_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy,
                default_dir=root,
                delayed_queue_capacity=4,
            )
            locked_path = mgr.create(".wav")
            peer = mgr.create(".wav")
            peer_deleted = threading.Event()
            allow_cleanup = threading.Event()
            raw_remove = os.remove
            lock_err = PermissionError(errno.EACCES, "locked")

            def tracked_remove(target: str) -> None:
                if target == locked_path and not allow_cleanup.is_set():
                    raise lock_err
                raw_remove(target)
                if target == peer:
                    peer_deleted.set()

            try:
                with patch(
                    "src.voice.audio.temp_files.os.remove",
                    side_effect=tracked_remove,
                ):
                    try:
                        mgr.remove_delayed(locked_path, delay=0.0, max_retries=3)
                        mgr.remove_delayed(peer, delay=0.0, max_retries=1)
                        self.assertTrue(peer_deleted.wait(2.0))
                        self.assertTrue(Path(locked_path).exists())
                        self.assertTrue(mgr.is_owned(locked_path))
                    finally:
                        allow_cleanup.set()
                        _finish_tfm(mgr)
            finally:
                allow_cleanup.set()
                _finish_tfm(mgr)


class ExplicitExhaustedPublicRetryTests(unittest.TestCase):
    def test_lock_exhaust_then_public_remove_delayed_retries_and_deletes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            raw_remove = os.remove
            lock_err = PermissionError(errno.EACCES, "locked")
            lock_attempts = {"count": 0}
            deleted = threading.Event()
            allow_cleanup = threading.Event()

            def tracked_remove(target: str) -> None:
                if (
                    target == path
                    and not allow_cleanup.is_set()
                    and lock_attempts["count"] < 2
                ):
                    lock_attempts["count"] += 1
                    raise lock_err
                raw_remove(target)
                if target == path:
                    deleted.set()

            try:
                with patch(
                    "src.voice.audio.temp_files.os.remove",
                    side_effect=tracked_remove,
                ):
                    try:
                        mgr.remove_delayed(path, delay=0.0, max_retries=2)
                        deadline = time.monotonic() + 3.0
                        while (
                            lock_attempts["count"] < 2
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.02)
                        self.assertEqual(lock_attempts["count"], 2)
                        _wait_inflight_quiet(mgr)
                        self.assertTrue(Path(path).exists())
                        mgr.remove_delayed(path, delay=0.0, max_retries=1)
                        self.assertTrue(deleted.wait(2.0))
                        self.assertFalse(Path(path).exists())
                    finally:
                        allow_cleanup.set()
                        _finish_tfm(mgr)
            finally:
                allow_cleanup.set()
                _finish_tfm(mgr)

    def test_public_retry_after_exhaust_with_max_retries_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            raw_remove = os.remove

            def fail_once(target: str) -> None:
                raw_remove(target)

            try:
                with patch(
                    "src.voice.audio.temp_files.os.remove",
                    side_effect=OSError(errno.EIO, "permanent"),
                ):
                    mgr.remove(path)
                self.assertTrue(mgr.is_owned(path))
                with patch(
                    "src.voice.audio.temp_files.os.remove",
                    side_effect=fail_once,
                ):
                    try:
                        mgr.remove_delayed(path, delay=0.0, max_retries=0)
                        deadline = time.monotonic() + 2.0
                        while Path(path).exists() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertFalse(Path(path).exists())
                    finally:
                        _finish_tfm(mgr)
            finally:
                _finish_tfm(mgr)


class PinDeferReadyDequeueTests(unittest.TestCase):
    def test_pin_after_ready_defers_item_and_peer_still_deletes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy,
                default_dir=root,
                delayed_queue_capacity=4,
            )
            pinned_path = mgr.create(".wav")
            peer = mgr.create(".wav")
            peer_deleted = threading.Event()
            token: str | None = None
            raw_remove = os.remove

            def tracked_remove(target: str) -> None:
                raw_remove(target)
                if target == peer:
                    peer_deleted.set()

            try:
                with _gate_ready_dequeue(mgr, pinned_path) as (arrived, release):
                    with patch(
                        "src.voice.audio.temp_files.os.remove",
                        side_effect=tracked_remove,
                    ):
                        try:
                            mgr.remove_delayed(pinned_path, delay=0.0, max_retries=1)
                            self.assertTrue(arrived.wait(2.0))
                            token = mgr.pin_io_paths((pinned_path,))
                            mgr.remove_delayed(peer, delay=0.0, max_retries=1)
                            release.set()
                            self.assertTrue(peer_deleted.wait(2.0))
                            self.assertTrue(Path(pinned_path).exists())
                        finally:
                            release.set()
                            if token is not None:
                                mgr.release_io_lease(token)
                            _finish_tfm(mgr)
            finally:
                _finish_tfm(mgr)


class MissingFileDoesNotStopPeerTests(unittest.TestCase):
    def test_missing_file_item_skipped_and_peer_still_deleted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy,
                default_dir=root,
                delayed_queue_capacity=4,
            )
            ghost = mgr.create(".wav")
            peer = mgr.create(".wav")
            peer_deleted = threading.Event()
            raw_remove = os.remove

            def tracked_remove(target: str) -> None:
                raw_remove(target)
                if target == peer:
                    peer_deleted.set()

            try:
                with _gate_ready_dequeue(mgr, ghost) as (arrived, release):
                    with patch(
                        "src.voice.audio.temp_files.os.remove",
                        side_effect=tracked_remove,
                    ):
                        try:
                            mgr.remove_delayed(ghost, delay=0.0, max_retries=1)
                            mgr.remove_delayed(peer, delay=0.0, max_retries=1)
                            self.assertTrue(arrived.wait(2.0))
                            self.assertTrue(mgr.is_owned(ghost))
                            self.assertTrue(
                                mgr.safety_policy.is_path_deletable(ghost)
                            )
                            raw_remove(ghost)
                            self.assertFalse(Path(ghost).exists())
                            release.set()
                            self.assertTrue(peer_deleted.wait(2.0))
                            self.assertFalse(mgr.is_owned(ghost))
                        finally:
                            release.set()
                            _finish_tfm(mgr)
            finally:
                _finish_tfm(mgr)


class IdleWaitEligibleDueTests(unittest.TestCase):
    """逾期 blocked 项不得遮挡较近 eligible future due（Root deadline 第 8 项）。"""

    def test_overdue_pinned_does_not_delay_nearer_eligible_future_due(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(
                safety_policy=policy,
                default_dir=root,
                delayed_queue_capacity=4,
            )
            pinned_path = mgr.create(".wav")
            peer = mgr.create(".wav")
            peer_deleted = threading.Event()
            token = mgr.pin_io_paths((pinned_path,))
            raw_remove = os.remove

            def tracked_remove(target: str) -> None:
                raw_remove(target)
                if target == peer:
                    peer_deleted.set()

            try:
                mgr.remove_delayed(pinned_path, delay=0.0, max_retries=1)
                with patch(
                    "src.voice.audio.temp_files.os.remove",
                    side_effect=tracked_remove,
                ):
                    try:
                        began = time.monotonic()
                        mgr.remove_delayed(peer, delay=0.08, max_retries=1)
                        self.assertTrue(peer_deleted.wait(2.0))
                        elapsed = time.monotonic() - began
                        self.assertLess(
                            elapsed,
                            0.35,
                            "idle wait must follow nearer eligible due, not 0.5 patrol",
                        )
                    finally:
                        mgr.release_io_lease(token)
                        _finish_tfm(mgr)
            finally:
                mgr.release_io_lease(token)
                _finish_tfm(mgr)


if __name__ == "__main__":
    unittest.main()
