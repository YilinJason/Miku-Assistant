"""CosyVoice model_lock：Windows 字节锁与遗留 NUL 锁迁移（无模型/无网络）。"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.voice.cosyvoice import model_lock as model_lock_mod
from src.voice.cosyvoice.model_lock import release_model_lock, try_acquire_model_lock

_PY = sys.executable
_TIMEOUT = 15.0


def _held_lock_bytes() -> bytes:
    handle = model_lock_mod._LOCK_HANDLE
    if handle is None:
        raise AssertionError("expected held model lock handle")
    handle.seek(0)
    return handle.read()


def _child_script() -> str:
    return textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from src.voice.cosyvoice.model_lock import (
            release_model_lock,
            try_acquire_model_lock,
        )

        lock = Path(sys.argv[1])
        mode = sys.argv[2]
        if mode == "acquire":
            ok = try_acquire_model_lock(lock)
            print("1" if ok else "0", flush=True)
            if ok and len(sys.argv) > 3:
                import time
                time.sleep(float(sys.argv[3]))
                release_model_lock()
        elif mode == "size":
            ok = try_acquire_model_lock(lock)
            if not ok:
                print("busy", flush=True)
                sys.exit(0)
            try:
                lock.write_bytes(b"XX")
            except OSError:
                print("write_fail", flush=True)
            release_model_lock()
        else:
            raise SystemExit(2)
        """
    ).strip()


def _run_child(*args: str, timeout: float = _TIMEOUT) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    repo = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(repo)
    return subprocess.run(
        [_PY, "-X", "utf8", "-c", _child_script(), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(repo),
    )


@unittest.skipUnless(os.name == "nt", "msvcrt model lock")
class CosyVoiceModelLockTests(unittest.TestCase):
    def tearDown(self) -> None:
        release_model_lock()

    def test_empty_file_migrates_to_one_byte_same_inode(self) -> None:
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "cosyvoice_model.lock"
            lock.write_bytes(b"")
            before = os.stat(lock)
            self.assertTrue(try_acquire_model_lock(lock))
            after = os.stat(lock)
            self.assertEqual(before.st_ino, after.st_ino)
            self.assertEqual(_held_lock_bytes(), b"\0")
            release_model_lock()
            self.assertEqual(lock.read_bytes(), b"\0")

    def test_legacy_seven_nuls_migrates_same_inode(self) -> None:
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "cosyvoice_model.lock"
            lock.write_bytes(b"\0" * 7)
            before = os.stat(lock)
            self.assertTrue(try_acquire_model_lock(lock))
            after = os.stat(lock)
            self.assertEqual(before.st_ino, after.st_ino)
            self.assertEqual(_held_lock_bytes(), b"\0")
            release_model_lock()

    def test_one_byte_lock_ok_without_rewrite(self) -> None:
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "cosyvoice_model.lock"
            lock.write_bytes(b"\0")
            before = os.stat(lock)
            self.assertTrue(try_acquire_model_lock(lock))
            self.assertEqual(os.stat(lock).st_ino, before.st_ino)
            self.assertEqual(_held_lock_bytes(), b"\0")
            release_model_lock()

    def test_rejects_non_nul_legacy_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "cosyvoice_model.lock"
            lock.write_bytes(b"\0\x01\0")
            self.assertFalse(try_acquire_model_lock(lock))
            self.assertEqual(lock.read_bytes(), b"\0\x01\0")

    def test_rejects_oversize_legacy(self) -> None:
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "cosyvoice_model.lock"
            lock.write_bytes(b"\0" * 65)
            self.assertFalse(try_acquire_model_lock(lock))
            self.assertEqual(len(lock.read_bytes()), 65)

    def test_rejects_symlink_target(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real.lock"
            real.write_bytes(b"\0")
            link = root / "cosyvoice_model.lock"
            try:
                link.symlink_to(real)
            except OSError:
                self.skipTest("symlink unavailable without elevation")
            self.assertFalse(try_acquire_model_lock(link))

    def test_exclusive_create_then_migrate(self) -> None:
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "new.lock"
            self.assertFalse(lock.exists())
            self.assertTrue(try_acquire_model_lock(lock))
            self.assertTrue(lock.is_file())
            self.assertEqual(_held_lock_bytes(), b"\0")
            release_model_lock()

    def test_second_process_busy_while_held(self) -> None:
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "held.lock"
            lock.write_bytes(b"\0")
            self.assertTrue(try_acquire_model_lock(lock))
            proc = _run_child(str(lock), "acquire")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0")
            release_model_lock()

    def test_child_cannot_mutate_while_parent_holds_lock(self) -> None:
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "mutate.lock"
            lock.write_bytes(b"\0")
            self.assertTrue(try_acquire_model_lock(lock))
            proc = _run_child(str(lock), "size")
            self.assertEqual(proc.returncode, 0)
            self.assertIn(proc.stdout.strip(), ("busy", "write_fail"))
            self.assertEqual(_held_lock_bytes(), b"\0")
            release_model_lock()

    def test_reacquire_after_release(self) -> None:
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "re.lock"
            lock.write_bytes(b"\0" * 7)
            self.assertTrue(try_acquire_model_lock(lock))
            release_model_lock()
            self.assertTrue(try_acquire_model_lock(lock))
            self.assertEqual(_held_lock_bytes(), b"\0")
            release_model_lock()

    def test_child_hold_blocks_then_parent_can_acquire(self) -> None:
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "handoff.lock"
            lock.write_bytes(b"\0")
            repo = Path(__file__).resolve().parents[1]
            proc = subprocess.Popen(
                [_PY, "-X", "utf8", "-c", _child_script(), str(lock), "acquire", "0.8"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONPATH": str(repo)},
                cwd=str(repo),
            )
            try:
                assert proc.stdout is not None
                line = proc.stdout.readline()
                self.assertEqual(line.strip(), "1")
                self.assertFalse(try_acquire_model_lock(lock))
                proc.wait(timeout=_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                raise
            finally:
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()
            self.assertTrue(try_acquire_model_lock(lock))
            release_model_lock()


if __name__ == "__main__":
    unittest.main()
