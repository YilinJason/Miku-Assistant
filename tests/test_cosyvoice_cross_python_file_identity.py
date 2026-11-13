"""CosyVoice portable 文件身份：跨 Python 版本与同进程回归（无模型/无网络）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.voice.cosyvoice.cross_file_identity import (
    capture_portable_file_identity,
    capture_portable_file_identity_fd,
    portable_identity_equal,
)
from src.voice.cosyvoice.worker_output import validate_worker_output_path

_REPO = Path(__file__).resolve().parents[1]
_MAIN_PY = Path(sys.executable)
_COSY_PY = _REPO / "vendor" / "CosyVoice" / ".venv" / "Scripts" / "python.exe"
_SUBPROC_TIMEOUT = 15.0


def _child_probe_script() -> str:
    return textwrap.dedent(
        """
        import json
        import os
        import sys
        from pathlib import Path

        from src.voice.cosyvoice.cross_file_identity import (
            capture_portable_file_identity,
            capture_portable_file_identity_fd,
        )

        path = Path(sys.argv[1])
        mode = sys.argv[2]
        if mode == "path":
            ident = capture_portable_file_identity(path)
        elif mode == "fd":
            fd = os.open(path, os.O_RDONLY | os.O_BINARY)
            try:
                ident = capture_portable_file_identity_fd(fd)
            finally:
                os.close(fd)
        else:
            raise SystemExit(2)
        print(json.dumps({"dev": ident.dev, "ino": ident.ino}), flush=True)
        """
    ).strip()


def _run_probe(py: Path, path: Path, mode: str, *, timeout: float = _SUBPROC_TIMEOUT):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO)
    return subprocess.run(
        [str(py), "-X", "utf8", "-c", _child_probe_script(), str(path), mode],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(_REPO),
    )


def _probe_pair(py: Path, path: Path) -> tuple[int, int]:
    proc = _run_probe(py, path, "path")
    proc.check_returncode()
    payload = json.loads(proc.stdout.strip())
    return int(payload["dev"]), int(payload["ino"])


class CosyVoicePortableIdentityLocalTests(unittest.TestCase):
    def test_path_and_fd_agree(self) -> None:
        with TemporaryDirectory() as td:
            target = Path(td) / "same.wav"
            target.write_bytes(b"x")
            id_path = capture_portable_file_identity(target)
            fd = os.open(target, os.O_RDONLY | os.O_BINARY)
            try:
                id_fd = capture_portable_file_identity_fd(fd)
            finally:
                os.close(fd)
            self.assertTrue(
                portable_identity_equal(
                    id_path.dev, id_path.ino, id_fd.dev, id_fd.ino
                )
            )

    def test_different_files_differ(self) -> None:
        with TemporaryDirectory() as td:
            a = Path(td) / "a.wav"
            b = Path(td) / "b.wav"
            a.write_bytes(b"a")
            b.write_bytes(b"b")
            id_a = capture_portable_file_identity(a)
            id_b = capture_portable_file_identity(b)
            self.assertFalse(
                portable_identity_equal(id_a.dev, id_a.ino, id_b.dev, id_b.ino)
            )

    def test_validate_rejects_wrong_portable_pair(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            tmp = root / "tmp"
            tmp.mkdir()
            owned = tmp / "owned.wav"
            owned.write_bytes(b"wav")
            ident = capture_portable_file_identity(owned)
            dev, ino = ident.dev, ident.ino
            self.assertIsNone(
                validate_worker_output_path(
                    str(owned),
                    dev,
                    ino + 1,
                    repo_root=root,
                    tmp_root=tmp,
                )
            )

    def test_invalid_fd_raises(self) -> None:
        with self.assertRaises(OSError):
            capture_portable_file_identity_fd(-1)


@unittest.skipUnless(os.name == "nt", "FILE_ID_INFO")
class CosyVoicePortableIdentityWindowsTests(unittest.TestCase):
    def test_native_api_failure_does_not_fallback(self) -> None:
        import ctypes

        with TemporaryDirectory() as td:
            target = Path(td) / "x.bin"
            target.write_bytes(b"z")
            real = ctypes.windll.kernel32.GetFileInformationByHandleEx

            def _fail(*_a, **_k):
                return 0

            with patch.object(
                ctypes.windll.kernel32, "GetFileInformationByHandleEx", _fail
            ):
                with self.assertRaises(OSError):
                    capture_portable_file_identity(target)
            ident = capture_portable_file_identity(target)
            self.assertGreaterEqual(ident.dev, 0)
            self.assertGreaterEqual(ident.ino, 0)


@unittest.skipUnless(os.name == "nt", "cross-python FILE_ID_INFO")
@unittest.skipUnless(_COSY_PY.is_file(), "vendor CosyVoice python missing")
class CosyVoiceCrossPythonIdentityTests(unittest.TestCase):
    def test_main_312_matches_vendor_311_path(self) -> None:
        with TemporaryDirectory() as td:
            isolated = Path(td) / "cross_py_identity.bin"
            isolated.write_bytes(os.urandom(32))
            main_pair = _probe_pair(_MAIN_PY, isolated)
            cosy_pair = _probe_pair(_COSY_PY, isolated)
            self.assertEqual(main_pair, cosy_pair)

    def test_main_312_matches_vendor_311_fd(self) -> None:
        with TemporaryDirectory() as td:
            isolated = Path(td) / "cross_py_identity_fd.bin"
            isolated.write_bytes(os.urandom(32))
            for py in (_MAIN_PY, _COSY_PY):
                proc = _run_probe(py, isolated, "fd")
                proc.check_returncode()
            main_fd = json.loads(
                _run_probe(_MAIN_PY, isolated, "fd").stdout.strip()
            )
            cosy_fd = json.loads(
                _run_probe(_COSY_PY, isolated, "fd").stdout.strip()
            )
            self.assertEqual(main_fd, cosy_fd)

    @unittest.skipUnless(
        sys.version_info[:2] >= (3, 12),
        "documents 3.12 vs 3.11 st_dev divergence",
    )
    def test_os_stat_dev_may_differ_while_portable_matches(self) -> None:
        with TemporaryDirectory() as td:
            isolated = Path(td) / "stat_vs_portable.bin"
            isolated.write_bytes(b"probe")
            main_stat = isolated.stat()
            cosy_proc = subprocess.run(
                [
                    str(_COSY_PY),
                    "-X",
                    "utf8",
                    "-c",
                    "import os,sys; print(os.stat(sys.argv[1]).st_dev)",
                    str(isolated),
                ],
                capture_output=True,
                text=True,
                timeout=_SUBPROC_TIMEOUT,
                cwd=str(_REPO),
            )
            cosy_proc.check_returncode()
            cosy_dev = int(cosy_proc.stdout.strip())
            portable_main = capture_portable_file_identity(isolated)
            portable_cosy = _probe_pair(_COSY_PY, isolated)
            self.assertEqual(
                (portable_main.dev, portable_main.ino), portable_cosy
            )
            # 记录性：若两解释器 os.stat().st_dev 不一致，portable 仍应对齐（修复目标）。
            _ = (int(main_stat.st_dev), cosy_dev)
