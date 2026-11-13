# -*- coding: utf-8 -*-
"""Paraformer ASR 准备脚本与 preparation helper 的离线契约测试。"""
from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from src.core.owned_worker_job import JOB_ENV_NAME  # noqa: E402
from src.core.safety import SafetyPolicy  # noqa: E402
from src.voice.paraformer.assets import (  # noqa: E402
    MODEL_DIR_REL,
    OFFICIAL_FILE_BYTES,
    OFFICIAL_GIT_BLOB_SHA1,
    fixed_layout_paths,
    verify_pinned_assets,
)
from src.voice.paraformer import preparation as prep  # noqa: E402


class PrepareCliTests(unittest.TestCase):
    def test_check_dry_run_no_subprocess(self) -> None:
        import prepare_paraformer as setup

        buf = io.StringIO()
        with patch("prepare_paraformer.run_execute_setup") as execute, patch.object(
            setup, "run_check", return_value=prep.CheckReport(ok=False, components=())
        ), redirect_stdout(buf):
            code = setup.main(["--check"])
        execute.assert_not_called()
        self.assertIn("prepare_paraformer", buf.getvalue())
        self.assertIsInstance(code, int)

    def test_import_does_not_replace_process_stdout(self) -> None:
        import importlib

        import prepare_paraformer as setup

        original = sys.stdout
        with redirect_stdout(io.StringIO()):
            importlib.reload(setup)
        self.assertIs(sys.stdout, original)

    def test_scripts_contain_owned_job_marker(self) -> None:
        for rel in ("scripts/prepare_paraformer.py", "diagnose_asr.py"):
            text = (_REPO / rel).read_text(encoding="utf-8")
            self.assertIn("worker_entry_join_owned_job", text)

    def test_rejects_unknown_flags(self) -> None:
        import prepare_paraformer as setup

        with self.assertRaises(SystemExit) as ctx:
            setup.main(["--download"])
        self.assertNotEqual(ctx.exception.code, 0)


class PreparationCheckTests(unittest.TestCase):
    def test_run_check_reports_missing_venv(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements-asr.txt").write_text(
                (_REPO / "requirements-asr.txt").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            report = prep.run_check(root)
            venv = next(c for c in report.components if c.name == "venv_asr")
            self.assertFalse(venv.ok)
            self.assertIn(venv.detail, ("missing", "identity_missing"))
            self.assertFalse(report.ok)


class AssetsSizeTests(unittest.TestCase):
    def test_verify_pinned_assets_rejects_wrong_size(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_dir = root / MODEL_DIR_REL
            model_dir.mkdir(parents=True)
            for rel in OFFICIAL_FILE_BYTES:
                path = model_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * 8)
            with self.assertRaises(RuntimeError):
                verify_pinned_assets(model_dir=model_dir, repo_root=root)


class DownloadGuardTests(unittest.TestCase):
    def test_untrusted_existing_file_blocks_download(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / MODEL_DIR_REL / "config.yaml"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_bytes(b"wrong-content")
            policy = prep.build_setup_safety_policy(root)
            with self.assertRaises(RuntimeError) as ctx:
                prep.download_pinned_assets(
                    policy, fixed_layout_paths(root), clock=lambda: 0.0
                )
            self.assertIn("existing_untrusted", str(ctx.exception))

    def test_valid_existing_skips_network_for_that_file(self) -> None:
        rel = "config.yaml"
        single_download = (
            (
                rel,
                "git_blob",
                OFFICIAL_FILE_BYTES[rel],
                OFFICIAL_GIT_BLOB_SHA1[rel],
            ),
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / MODEL_DIR_REL / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"pinned")
            policy = prep.build_setup_safety_policy(root)

            def fake_verify(path: Path, **kwargs: object) -> bool:
                return path.name == rel

            with patch.object(prep, "DOWNLOAD_FILES", single_download), patch.object(
                prep, "_verify_existing_file", side_effect=fake_verify
            ), patch.object(prep, "_stream_download") as dl, patch.object(
                prep, "_git_blob_sha1_bounded"
            ) as blob_hash, patch.object(prep, "verify_pinned_assets"):
                reused = prep.download_pinned_assets(
                    policy, fixed_layout_paths(root), clock=lambda: 1000.0
                )
            self.assertEqual(reused, [rel])
            dl.assert_not_called()
            blob_hash.assert_not_called()

    def test_maybe_join_owned_job_only_with_token(self) -> None:
        with patch(
            "src.voice.paraformer.preparation.worker_entry_join_owned_job"
        ) as join:
            prep.maybe_join_owned_job(environ={})
            join.assert_not_called()
            prep.maybe_join_owned_job(environ={JOB_ENV_NAME: "Local\\x"})
            join.assert_called_once()


class AtomicPublishTests(unittest.TestCase):
    def test_publish_does_not_overwrite_existing_destination(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "model.pt"
            dest.write_bytes(b"keep-me")
            partial = root / "model.pt.partial"
            partial.write_bytes(b"new-bytes")
            with self.assertRaises(RuntimeError) as ctx:
                prep._atomic_publish_new_file(partial, dest)
            self.assertIn("dest_raced", str(ctx.exception))
            self.assertEqual(dest.read_bytes(), b"keep-me")


class ProcessAliveTests(unittest.TestCase):
    def test_process_alive_delegates_to_parent_process(self) -> None:
        with patch(
            "src.core.parent_process.parent_process_is_alive", return_value=True
        ) as probe:
            self.assertTrue(prep.process_alive(1234))
        probe.assert_called_once_with(1234)


class ExecuteSetupFakeTests(unittest.TestCase):
    def test_execute_wires_steps_and_writes_report(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements-asr.txt").write_text(
                (_REPO / "requirements-asr.txt").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            layout = fixed_layout_paths(root)
            layout["asr_python"].parent.mkdir(parents=True, exist_ok=True)
            layout["asr_python"].write_bytes(b"stub")

            calls: List[str] = []

            policy = SafetyPolicy(
                {
                    "permissions": {"subprocess": True, "file_delete": True},
                    "file_delete_allowed_dirs": [str(root / "tmp")],
                }
            )
            with patch.object(prep, "ensure_venv", side_effect=lambda *a, **k: calls.append("venv")), patch.object(
                prep, "pip_install_asr_env", side_effect=lambda *a, **k: calls.append("pip")
            ), patch.object(
                prep,
                "verify_asr_env_identity",
                side_effect=lambda *a, **k: (
                    calls.append("verify"),
                    {
                        "python": "3.12.0",
                        "torch": "2.6.0+cpu",
                        "torchaudio": "2.6.0+cpu",
                        "funasr": "1.4.22",
                        "numpy": "1.26.4",
                        "transformers": "4.49.0",
                        "torch_cuda_is_none": True,
                    },
                )[1],
            ), patch.object(
                prep, "write_env_identity", side_effect=lambda *a, **k: root / "tmp" / "id.json"
            ), patch.object(
                prep, "download_pinned_assets", side_effect=lambda *a, **k: calls.append("download") or []
            ), patch.object(
                prep, "capture_pip_inventory", side_effect=lambda *a, **k: calls.append("freeze") or ["funasr==1.4.22"]
            ), patch.object(
                prep,
                "check_host_memory_for_setup",
                return_value=prep.ComponentStatus("host_memory", True),
            ), patch.object(
                prep,
                "check_disk_space",
                return_value=prep.ComponentStatus("disk", True),
            ), patch.object(
                prep,
                "check_main_python",
                return_value=prep.ComponentStatus("main_python", True),
            ):
                code, report_path = prep.run_execute_setup(root, policy=policy)
            self.assertEqual(code, 0)
            self.assertIsNotNone(report_path)
            assert report_path is not None
            data = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertEqual(calls, ["venv", "pip", "verify", "download", "freeze"])


if __name__ == "__main__":
    unittest.main()
