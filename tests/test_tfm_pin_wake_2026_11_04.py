"""TFM 即时 remove 遇 IO pin 零延迟与空队列 wake 及时性（独占 TemporaryDirectory）。"""
from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core.safety import Operation, SafetyPolicy
from src.voice.audio.temp_files import TempFileManager


def _policy(root: Path) -> SafetyPolicy:
    p = SafetyPolicy({"permissions": {Operation.FILE_DELETE.value: True}})
    p.register_file_delete_dir(root)
    return p


def _release_leases(mgr: TempFileManager, tokens: list[object]) -> None:
    for token in tokens:
        mgr.release_io_lease(token)


def _finish_tfm(
    mgr: TempFileManager, tokens: list[object], *, timeout: float = 3.0
) -> None:
    _release_leases(mgr, tokens)
    if mgr.shutdown_status().complete and not mgr.delayed_worker_alive():
        return
    status = mgr.shutdown(timeout=timeout)
    if not status.complete:
        raise AssertionError(f"TempFileManager shutdown incomplete: {status!r}")
    if mgr.owned_count() != 0:
        raise AssertionError("owned not drained")
    if mgr.active_io_lease_count() != 0:
        raise AssertionError("IO leases still active")
    worker = mgr._worker  # noqa: SLF001
    if worker is not None:
        worker.join(timeout=timeout)
        if worker.is_alive():
            raise AssertionError("delayed worker thread still alive after join")
    if mgr.delayed_worker_alive():
        raise AssertionError("delayed_worker_alive after shutdown join")


class TfmPinWakeSliceTests(unittest.TestCase):
    def test_immediate_remove_while_pinned_records_zero_delay(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            tokens: list[object] = []
            try:
                path = mgr.create(".wav")
                tokens.append(mgr.pin_io_paths((path,)))
                mgr.remove(path)
                key = mgr._normalize_key(path)  # noqa: SLF001
                assert key is not None
                stored = mgr._pending_cleanup.get(key)  # noqa: SLF001
                self.assertIsNotNone(stored)
                assert stored is not None
                self.assertEqual(stored.remaining_attempts, 5)
                self.assertLessEqual(stored.due_at, time.monotonic() + 0.05)
            finally:
                _finish_tfm(mgr, tokens)

    def test_last_pin_release_reclaims_under_one_second(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            tokens: list[object] = []
            deleted = threading.Event()
            remove_calls: list[str] = []
            raw_remove = os.remove

            def tracked_remove(target: str) -> None:
                raw_remove(target)
                if str(target) == path:
                    remove_calls.append(path)
                    deleted.set()

            path = mgr.create(".wav")
            tokens.extend(
                [mgr.pin_io_paths((path,)), mgr.pin_io_paths((path,))]
            )
            patcher = patch(
                "src.voice.audio.temp_files.os.remove",
                side_effect=tracked_remove,
            )
            try:
                with patcher:
                    try:
                        for _ in range(20):
                            mgr.remove(path)
                        self.assertTrue(Path(path).exists())
                        self.assertEqual(mgr.pending_cleanup_count(), 1)
                        worker = mgr._worker  # noqa: SLF001
                        mgr.release_io_lease(tokens[0])
                        self.assertFalse(deleted.wait(0.08))
                        began = time.monotonic()
                        mgr.release_io_lease(tokens[1])
                        self.assertTrue(deleted.wait(1.0))
                        deadline = began + 1.0
                        while (
                            mgr.pending_cleanup_count()
                            and time.monotonic() < deadline
                        ):
                            threading.Event().wait(0.005)
                        self.assertLess(time.monotonic() - began, 1.0)
                        self.assertEqual(mgr.pending_cleanup_count(), 0)
                        self.assertEqual(mgr.owned_count(), 0)
                        self.assertIs(mgr._worker, worker)  # noqa: SLF001
                        self.assertEqual(remove_calls, [path])
                    finally:
                        _finish_tfm(mgr, tokens)
            finally:
                _finish_tfm(mgr, tokens)

    def test_explicit_remove_delayed_not_shortened_by_pin_release(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            tokens: list[object] = []
            deleted = threading.Event()
            raw_remove = os.remove

            def tracked_remove(target: str) -> None:
                raw_remove(target)
                if str(target) == path:
                    deleted.set()

            path = mgr.create(".wav")
            tokens.append(mgr.pin_io_paths((path,)))
            patcher = patch(
                "src.voice.audio.temp_files.os.remove",
                side_effect=tracked_remove,
            )
            try:
                with patcher:
                    try:
                        mgr.remove_delayed(path, delay=0.3, max_retries=1)
                        mgr.release_io_lease(tokens[0])
                        self.assertFalse(deleted.wait(0.12))
                        self.assertTrue(deleted.wait(1.2))
                    finally:
                        _finish_tfm(mgr, tokens)
            finally:
                _finish_tfm(mgr, tokens)

    def test_stop_before_clear_does_not_enter_idle_wait(self) -> None:
        """stop+wake 已置位后 clear 会吞 wake；wait 前须复核 stop，不得 wait(0.5)。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            tokens: list[object] = []
            wait_while_stopped: list[float] = []
            inject_race = threading.Event()
            real_clear = mgr._worker_wakeup.clear  # noqa: SLF001
            real_wait = mgr._worker_wakeup.wait  # noqa: SLF001

            def clear_simulating_shutdown_race() -> None:
                if inject_race.is_set():
                    mgr._stop.set()
                    mgr._worker_wakeup.set()
                real_clear()

            def wait_captures_post_stop(timeout: float | None = None) -> bool:
                if mgr._stop.is_set() and timeout is not None and float(timeout) >= 0.5:
                    wait_while_stopped.append(float(timeout))
                return real_wait(timeout)

            path = mgr.create(".wav")
            tokens.extend(
                [mgr.pin_io_paths((path,)), mgr.pin_io_paths((path,))]
            )
            mgr.remove(path)
            inject_race.set()
            try:
                with patch.object(
                    mgr._worker_wakeup, "clear", clear_simulating_shutdown_race
                ), patch.object(
                    mgr._worker_wakeup, "wait", side_effect=wait_captures_post_stop
                ):
                    mgr.release_io_lease(tokens[0])
                    mgr.release_io_lease(tokens[1])
                    deadline = time.monotonic() + 2.0
                    while mgr.owned_count() and time.monotonic() < deadline:
                        threading.Event().wait(0.005)
                    worker = mgr._worker  # noqa: SLF001
                    if worker is not None:
                        worker.join(timeout=2.0)
                        self.assertFalse(worker.is_alive())
            finally:
                inject_race.clear()
                _finish_tfm(mgr, tokens)
            self.assertEqual(wait_while_stopped, [])

    def test_duplicate_release_and_denied_remove_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            tokens: list[object] = []
            try:
                path = mgr.create(".wav")
                token = mgr.pin_io_paths((path,))
                tokens.append(token)
                with patch.object(mgr.safety_policy, "is_allowed", return_value=False):
                    mgr.remove(path)
                self.assertTrue(Path(path).exists())
                self.assertEqual(mgr.pending_cleanup_count(), 0)
                self.assertIsNone(mgr._worker)  # noqa: SLF001
                mgr.release_io_lease(token)
                mgr.release_io_lease(token)
                self.assertEqual(mgr.active_io_lease_count(), 0)
            finally:
                _finish_tfm(mgr, tokens)

    def test_duplicate_remove_starts_single_worker(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            tokens: list[object] = []
            start_calls: list[threading.Thread] = []
            real_start = threading.Thread.start

            def track_start(self, *a, **k):
                start_calls.append(self)
                return real_start(self, *a, **k)

            try:
                path = mgr.create(".wav")
                tokens.append(mgr.pin_io_paths((path,)))
                with patch("threading.Thread.start", track_start):
                    mgr.remove(path)
                    mgr.remove(path)
                self.assertEqual(len(start_calls), 1)
                self.assertEqual(start_calls[0].name, "TempFileDelayedDelete")
            finally:
                _finish_tfm(mgr, tokens)

    def test_idle_worker_shutdown_completes_promptly(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _policy(root)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            tokens: list[object] = []
            try:
                path = mgr.create(".wav")
                tokens.append(mgr.pin_io_paths((path,)))
                mgr.remove(path)
                mgr.release_io_lease(tokens[0])
                deadline = time.monotonic() + 1.2
                while mgr.owned_count() and time.monotonic() < deadline:
                    threading.Event().wait(0.005)
                self.assertEqual(mgr.owned_count(), 0)
                tokens.clear()
                started = time.monotonic()
                status = mgr.shutdown(timeout=0.5)
                self.assertTrue(status.complete, repr(status))
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertFalse(mgr.delayed_worker_alive())
                worker = mgr._worker  # noqa: SLF001
                if worker is not None:
                    worker.join(timeout=0.5)
                    self.assertFalse(worker.is_alive())
            finally:
                _finish_tfm(mgr, tokens, timeout=2.0)


if __name__ == "__main__":
    unittest.main()
