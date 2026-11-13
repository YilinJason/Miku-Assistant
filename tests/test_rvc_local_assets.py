"""Offline tests for rvc_python local base asset preflight (fake specs, no real weights)."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.paths import PROJECT_ROOT
from src.core.safety import SafetyPolicy, safe_subprocess_run
from src.voice.rvc.local_assets import (
    RVC_BASE_MODEL_ASSETS_MISSING,
    RVC_PYTHON_LIB_DIR_INVALID,
    RVC_PYTHON_NOT_INSTALLED,
    RVC_PYTHON_ORIGIN_UNAVAILABLE,
    RvcLocalAssetsError,
    ensure_rvc_python_local_assets,
)

POLICY = SafetyPolicy({"permissions": {"subprocess": True}})


def _fake_spec(lib: Path) -> importlib.machinery.ModuleSpec:
    init_py = lib / "__init__.py"
    init_py.write_text("# fake\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("rvc_python", str(init_py))
    assert spec is not None
    return spec


def _fake_spec_pyc(lib: Path) -> importlib.machinery.ModuleSpec:
    init_pyc = lib / "__init__.pyc"
    init_pyc.write_bytes(b"\x00" * 8)
    spec = importlib.util.spec_from_file_location("rvc_python", str(init_pyc))
    assert spec is not None
    return spec


def _write_assets(lib: Path, *, hubert_size: int = 8, rmvpe_size: int = 8) -> None:
    base = lib / "base_model"
    base.mkdir(parents=True, exist_ok=True)
    (base / "hubert_base.pt").write_bytes(b"x" * hubert_size)
    (base / "rmvpe.pt").write_bytes(b"y" * rmvpe_size)


class LocalAssetsHelperTests(unittest.TestCase):
    def test_missing_package_spec(self) -> None:
        with patch("importlib.util.find_spec", return_value=None):
            with self.assertRaises(RvcLocalAssetsError) as ctx:
                ensure_rvc_python_local_assets()
        self.assertEqual(str(ctx.exception), RVC_PYTHON_NOT_INSTALLED)

    def test_find_spec_metadata_errors_normalized(self) -> None:
        for exc in (ImportError("broken"), ValueError("bad"), OSError("io")):
            with patch("importlib.util.find_spec", side_effect=exc):
                with self.assertRaises(RvcLocalAssetsError) as ctx:
                    ensure_rvc_python_local_assets()
                self.assertEqual(str(ctx.exception), RVC_PYTHON_ORIGIN_UNAVAILABLE)

    def test_namespace_origin_rejected(self) -> None:
        spec = importlib.machinery.ModuleSpec("rvc_python", loader=None)
        spec.origin = "namespace"
        with patch("importlib.util.find_spec", return_value=spec):
            with self.assertRaises(RvcLocalAssetsError) as ctx:
                ensure_rvc_python_local_assets()
        self.assertEqual(str(ctx.exception), RVC_PYTHON_ORIGIN_UNAVAILABLE)

    def test_non_str_origin_rejected(self) -> None:
        cases = (123, b"bad-origin", object())
        for bad_origin in cases:
            with self.subTest(origin=repr(bad_origin)):
                spec = importlib.machinery.ModuleSpec("rvc_python", loader=None)
                spec.origin = bad_origin  # type: ignore[assignment]
                with patch("importlib.util.find_spec", return_value=spec):
                    with self.assertRaises(RvcLocalAssetsError) as ctx:
                        ensure_rvc_python_local_assets()
                    self.assertEqual(str(ctx.exception), RVC_PYTHON_ORIGIN_UNAVAILABLE)
        with patch("importlib.util.find_spec", side_effect=KeyboardInterrupt("kbi")):
            with self.assertRaises(KeyboardInterrupt):
                ensure_rvc_python_local_assets()

    def test_single_file_module_origin_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "rvc_python.py"
            mod.write_text("# stub\n", encoding="utf-8")
            spec = importlib.util.spec_from_file_location("rvc_python", str(mod))
            assert spec is not None
            with patch("importlib.util.find_spec", return_value=spec):
                with self.assertRaises(RvcLocalAssetsError) as ctx:
                    ensure_rvc_python_local_assets()
                self.assertEqual(str(ctx.exception), RVC_PYTHON_ORIGIN_UNAVAILABLE)

    def test_pyc_package_origin_resolves_lib_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            _write_assets(lib)
            spec = _fake_spec_pyc(lib)
            with patch("importlib.util.find_spec", return_value=spec):
                result = ensure_rvc_python_local_assets()
            self.assertEqual(Path(result.lib_dir), lib.resolve())

    def test_missing_or_empty_assets_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            spec = _fake_spec(lib)
            with patch("importlib.util.find_spec", return_value=spec):
                with self.assertRaises(RvcLocalAssetsError) as ctx:
                    ensure_rvc_python_local_assets()
                self.assertEqual(str(ctx.exception), RVC_BASE_MODEL_ASSETS_MISSING)

                _write_assets(lib, hubert_size=0, rmvpe_size=8)
                with self.assertRaises(RvcLocalAssetsError):
                    ensure_rvc_python_local_assets()

                _write_assets(lib, hubert_size=8, rmvpe_size=8)
                (lib / "base_model" / "rmvpe.pt").unlink()
                (lib / "base_model" / "rmvpe.pt").mkdir()
                with self.assertRaises(RvcLocalAssetsError):
                    ensure_rvc_python_local_assets()

    def test_symlink_to_nonempty_file_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            _write_assets(lib)
            hubert = lib / "base_model" / "hubert_base.pt"
            real = lib / "base_model" / "hubert_real.pt"
            real.write_bytes(b"z" * 16)
            hubert.unlink()
            try:
                hubert.symlink_to(real)
            except OSError:
                self.skipTest("platform cannot create symlinks without privilege")
            spec = _fake_spec(lib)
            with patch("importlib.util.find_spec", return_value=spec):
                with self.assertRaises(RvcLocalAssetsError) as ctx:
                    ensure_rvc_python_local_assets()
                self.assertEqual(str(ctx.exception), RVC_BASE_MODEL_ASSETS_MISSING)

    def test_complete_assets_return_lib_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            _write_assets(lib)
            spec = _fake_spec(lib)
            with patch("importlib.util.find_spec", return_value=spec):
                result = ensure_rvc_python_local_assets()
            self.assertEqual(Path(result.lib_dir), lib.resolve())

    def test_invalid_lib_dir_after_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "pkg"
            pkg.mkdir()
            init = pkg / "__init__.py"
            init.write_text("#", encoding="utf-8")
            spec = importlib.util.spec_from_file_location("rvc_python", str(init))
            assert spec is not None
            shutil.rmtree(pkg)
            with patch("importlib.util.find_spec", return_value=spec):
                with self.assertRaises(RvcLocalAssetsError) as ctx:
                    ensure_rvc_python_local_assets()
                self.assertEqual(str(ctx.exception), RVC_PYTHON_LIB_DIR_INVALID)

    def test_keyboard_interrupt_not_swallowed(self) -> None:
        with patch("importlib.util.find_spec", side_effect=KeyboardInterrupt("kbi")):
            with self.assertRaises(KeyboardInterrupt):
                ensure_rvc_python_local_assets()


class ScriptEntryOrderTests(unittest.TestCase):
    def test_load_vc_fails_before_patch_when_assets_missing(self) -> None:
        from scripts import rvc_worker

        patch_mock = MagicMock()
        with patch(
            "src.voice.rvc.local_assets.ensure_rvc_python_local_assets",
            side_effect=RvcLocalAssetsError(RVC_BASE_MODEL_ASSETS_MISSING),
        ), patch.object(rvc_worker, "_patch_torch_load_for_fairseq", patch_mock):
            with self.assertRaises(RvcLocalAssetsError):
                rvc_worker._load_vc("fake.pth", "cpu")
        patch_mock.assert_not_called()

    def test_worker_main_emits_ready_false_on_missing_assets(self) -> None:
        from scripts import rvc_worker

        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "m.pth"
            model.write_bytes(b"x")
            argv = ["rvc_worker.py", "--model", str(model), "--device", "cpu"]
            out = io.StringIO()
            with patch.object(sys, "argv", argv), patch.object(
                rvc_worker, "_start_parent_watchdog"
            ), patch(
                "src.voice.rvc.local_assets.ensure_rvc_python_local_assets",
                side_effect=RvcLocalAssetsError(RVC_BASE_MODEL_ASSETS_MISSING),
            ), patch.object(sys, "stdout", out):
                rc = rvc_worker.main()
            self.assertEqual(rc, 1)
            line = out.getvalue().strip().splitlines()[0]
            payload = json.loads(line)
            self.assertFalse(payload["ready"])
            self.assertEqual(payload["error"], RVC_BASE_MODEL_ASSETS_MISSING)

    def test_infer_main_reports_missing_assets_without_heavy_imports(self) -> None:
        from scripts import rvc_infer

        patch_mock = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            model_path = tmp_path / "m.pth"
            model_path.write_bytes(b"x")
            input_path = tmp_path / "in.wav"
            input_path.write_bytes(b"x")
            output_path = tmp_path / "out.wav"
            argv = [
                "rvc_infer.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--model",
                str(model_path),
            ]
            with patch.object(sys, "argv", argv), patch.object(
                rvc_infer, "_early_join_owned_job"
            ), patch(
                "src.voice.rvc.local_assets.ensure_rvc_python_local_assets",
                side_effect=RvcLocalAssetsError(RVC_BASE_MODEL_ASSETS_MISSING),
            ), patch.object(rvc_infer, "_patch_torch_load_for_fairseq", patch_mock):
                rc = rvc_infer.main()
            self.assertEqual(rc, 1)
            patch_mock.assert_not_called()

    def test_load_vc_uses_lib_dir_and_skips_download(self) -> None:
        from scripts.rvc_worker import _load_vc

        calls: list[str] = []

        class FakeVC:
            def __init__(self, lib_dir, config):
                calls.append(f"vc:{lib_dir}")

            def get_vc(self, model_path, version):
                calls.append(f"get:{model_path}:{version}")

        class FakeConfig:
            def __init__(self, lib_dir, device):
                calls.append(f"cfg:{lib_dir}:{device}")

        with patch(
            "src.voice.rvc.local_assets.ensure_rvc_python_local_assets",
            return_value=MagicMock(lib_dir="/fake/lib"),
        ), patch("scripts.rvc_worker._patch_torch_load_for_fairseq"), patch.dict(
            "sys.modules",
            {
                "rvc_python.configs.config": MagicMock(Config=FakeConfig),
                "rvc_python.modules.vc.modules": MagicMock(VC=FakeVC),
            },
        ):
            vc, device = _load_vc("m.pth", "cpu")
        self.assertEqual(device, "cpu")
        self.assertIsInstance(vc, FakeVC)
        self.assertEqual(calls[0], "cfg:/fake/lib:cpu")
        self.assertIn("get:m.pth:v2", calls)

    def test_infer_main_success_path_with_fake_vc(self) -> None:
        from scripts import rvc_infer

        calls: list[str] = []
        wav_writes: list[tuple[str, int, object]] = []

        class FakeVC:
            tgt_sr = 24000

            def __init__(self, lib_dir, config):
                calls.append(f"vc:{lib_dir}")

            def get_vc(self, model_path, version):
                calls.append(f"get:{model_path}:{version}")

            def vc_single(self, **kwargs):
                calls.append(f"vc_single:{kwargs.get('input_audio_path')}")
                return None, [0, 1, 2]

        class FakeConfig:
            def __init__(self, lib_dir, device):
                calls.append(f"cfg:{lib_dir}:{device}")

        def fake_wav_write(path, sr, data):
            wav_writes.append((path, sr, data))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            model_path = tmp_path / "m.pth"
            model_path.write_bytes(b"x")
            input_path = tmp_path / "in.wav"
            input_path.write_bytes(b"x")
            output_path = tmp_path / "out.wav"
            argv = [
                "rvc_infer.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--model",
                str(model_path),
                "--device",
                "cpu",
            ]
            fake_wav = MagicMock()
            fake_wav.write = fake_wav_write
            with patch.object(sys, "argv", argv), patch.object(
                rvc_infer, "_early_join_owned_job"
            ), patch(
                "src.voice.rvc.local_assets.ensure_rvc_python_local_assets",
                return_value=MagicMock(lib_dir="/fake/lib"),
            ), patch.object(rvc_infer, "_patch_torch_load_for_fairseq"), patch.dict(
                "sys.modules",
                {
                    "rvc_python.configs.config": MagicMock(Config=FakeConfig),
                    "rvc_python.modules.vc.modules": MagicMock(VC=FakeVC),
                    "scipy.io": MagicMock(wavfile=fake_wav),
                },
            ):
                rc = rvc_infer.main()
            self.assertEqual(rc, 0)
            self.assertEqual(calls[0], "cfg:/fake/lib:cpu")
            self.assertTrue(any(c.startswith("get:") and c.endswith(":v2") for c in calls))
            self.assertTrue(any("vc_single:" in c for c in calls))
            self.assertEqual(len(wav_writes), 1)
            self.assertEqual(wav_writes[0][0], str(output_path))
            self.assertEqual(wav_writes[0][1], 24000)


class SubprocessGuardTests(unittest.TestCase):
    def test_helper_does_not_import_rvc_python(self) -> None:
        code = f"""
import importlib.abc, sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
seen = []
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "rvc_python" or fullname.startswith("rvc_python."):
            seen.append(fullname)
            raise ImportError("blocked")
        return None
sys.meta_path.insert(0, Block())
from unittest.mock import patch
from pathlib import Path
import tempfile
from src.voice.rvc import local_assets as la
import importlib.util

with tempfile.TemporaryDirectory() as tmp:
    lib = Path(tmp)
    init = lib / "__init__.py"
    init.write_text("#", encoding="utf-8")
    (lib / "base_model").mkdir()
    (lib / "base_model" / "hubert_base.pt").write_bytes(b"x")
    (lib / "base_model" / "rmvpe.pt").write_bytes(b"y")
    spec = importlib.util.spec_from_file_location("rvc_python", str(init))
    with patch("importlib.util.find_spec", return_value=spec):
        result = la.ensure_rvc_python_local_assets()
    assert seen == []
    assert Path(result.lib_dir) == lib.resolve()
print("ok")
"""
        result = safe_subprocess_run(
            POLICY,
            [sys.executable, "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)

    def test_load_vc_missing_assets_fresh_process_no_torch(self) -> None:
        code = f"""
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from unittest.mock import patch
from src.voice.rvc.local_assets import RvcLocalAssetsError, RVC_BASE_MODEL_ASSETS_MISSING
import scripts.rvc_worker as w

with patch(
    "src.voice.rvc.local_assets.ensure_rvc_python_local_assets",
    side_effect=RvcLocalAssetsError(RVC_BASE_MODEL_ASSETS_MISSING),
):
    try:
        w._load_vc("m.pth", "cpu")
    except RvcLocalAssetsError:
        pass
    else:
        raise SystemExit("expected error")
if "torch" in sys.modules:
    raise SystemExit("torch imported")
print("ok")
"""
        result = safe_subprocess_run(
            POLICY,
            [sys.executable, "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
