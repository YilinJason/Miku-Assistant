# -*- coding: utf-8 -*-
"""RVC 伪影参数矩阵：dry-run、固定 profile、串行 Worker 与所有权 fail-closed。"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import patch
from contextlib import redirect_stdout

import numpy as np
import soundfile as sf

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.voice.audio.temp_files import TempFileManager


def _write_tiny_wav(path: Path, *, frames: int = 2205, sr: int = 22050) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    audio = np.zeros(frames, dtype=np.float32)
    sf.write(str(path), audio, sr, format="WAV", subtype="PCM_16")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _diag():
    import diagnose_rvc_artifacts as diag

    return diag


class _FakeConfig:
    def __init__(self, rvc: dict) -> None:
        self._rvc = rvc

    def get(self, key: str, default: Any = None) -> Any:
        if key == "security":
            return {"permissions": {"file_delete": True, "cloud_tts": True}}
        return default

    def get_rvc_config(self) -> dict:
        return dict(self._rvc)


def _fake_assets(tmp: Path) -> dict:
    tmp.mkdir(parents=True, exist_ok=True)
    python = tmp / "python.exe"
    model = tmp / "model.pth"
    index = tmp / "model.index"
    python.write_bytes(b"PY")
    model.write_bytes(b"MODEL")
    index.write_bytes(b"IDX")
    return {
        "model_path": str(model),
        "index_path": str(index),
        "python_path": str(python),
        "pitch": 12,
        "index_rate": True,
        "filter_radius": 99,
        "protect": 0.1,
        "rms_mix_rate": 1,
        "f0_method": "crepe",
        "fallback_on_worker_failure": True,
        "worker_enabled": False,
        "postprocess_enabled": True,
        "postprocess_highpass_hz": 80,
        "postprocess_peak": 0.95,
    }


class DryRunTests(unittest.TestCase):
    def test_default_dry_run_has_zero_side_effects(self) -> None:
        diag = _diag()
        buf = io.StringIO()
        with patch.object(Path, "mkdir") as mkdir, patch(
            "subprocess.Popen"
        ) as popen, patch("subprocess.run") as run, patch.object(
            diag, "Config"
        ) as cfg, patch.object(
            diag, "check_rvc_health"
        ) as health, patch.object(
            diag, "RvcArtifactSession"
        ) as session, patch.object(
            diag, "read_wav_metrics", side_effect=AssertionError("wav")
        ):
            with redirect_stdout(buf):
                code = diag.main([])
        self.assertEqual(code, 0)
        self.assertEqual(mkdir.call_count, 0)
        self.assertEqual(popen.call_count, 0)
        self.assertEqual(run.call_count, 0)
        self.assertEqual(cfg.call_count, 0)
        self.assertEqual(health.call_count, 0)
        self.assertEqual(session.call_count, 0)
        out = buf.getvalue()
        self.assertIn(diag.OUTPUT_DIR_REL, out)
        self.assertIn(diag.INPUT_DIR_REL, out)
        self.assertIn("idx055_fr1", out)
        self.assertIn("protect=0.5", out)
        self.assertIn("protect=0.33", out)
        self.assertIn("filter_radius=1", out)
        self.assertIn("不重跑", out)
        self.assertIn("shutdown(stop_rvc=True)", out)
        self.assertIn("data/memory", out)
        for profile in diag.RUN_PROFILES:
            self.assertIn(profile.profile_id, out)
            self.assertIn(f"protect={profile.protect}", out)
            self.assertIn(f"index_rate={profile.index_rate}", out)
        for label, _text, _instruct in diag.SAMPLE_CASES:
            self.assertIn(label, out)

    def test_argparse_rejects_extra_options(self) -> None:
        diag = _diag()
        for bad in (
            ["--profile", "idx000_p050"],
            ["--index-rate", "0"],
            ["--output", "somewhere"],
            ["--input", "x.wav"],
            ["--execute", "--filter-radius", "3"],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as ctx:
                    diag.main(bad)
                self.assertNotEqual(ctx.exception.code, 0)

    def test_execute_flag_delegates_without_dry_run_plan(self) -> None:
        diag = _diag()
        with patch.object(diag, "Config", return_value=object()) as cfg, patch.object(
            diag, "run_execute", return_value=0
        ) as run:
            code = diag.main(["--execute"])
        self.assertEqual(code, 0)
        self.assertEqual(cfg.call_count, 1)
        self.assertEqual(run.call_count, 1)


class ProfileMatrixTests(unittest.TestCase):
    def test_matrix_exact_and_rejects_bool_nan_oob(self) -> None:
        diag = _diag()
        diag.assert_profile_matrix()
        self.assertEqual(
            [
                (p.profile_id, p.index_rate, p.protect, p.filter_radius)
                for p in diag.RUN_PROFILES
            ],
            [
                ("idx025_p050", 0.25, 0.5, 1),
                ("idx055_p033", 0.55, 0.33, 1),
                ("idx025_p033", 0.25, 0.33, 1),
                ("idx000_p050", 0.0, 0.5, 1),
            ],
        )
        self.assertEqual(diag.BASELINE_PROFILE.profile_id, "idx055_fr1")
        self.assertEqual(diag.BASELINE_PROFILE.protect, 0.5)
        self.assertEqual(diag.BASELINE_PROFILE.filter_radius, 1)
        self.assertNotIn(
            diag.BASELINE_PROFILE.profile_id,
            {p.profile_id for p in diag.RUN_PROFILES},
        )
        self.assertTrue(diag.profile_pair_allowed(0.25, 0.5))
        self.assertTrue(diag.profile_pair_allowed(0.55, 0.33))
        self.assertTrue(diag.profile_pair_allowed(0.25, 0.33))
        self.assertTrue(diag.profile_pair_allowed(0.0, 0.5))
        self.assertFalse(diag.profile_pair_allowed(0.55, 0.5))
        self.assertFalse(diag.profile_pair_allowed(True, 0.5))
        self.assertFalse(diag.profile_pair_allowed(0.25, True))
        self.assertFalse(diag.profile_pair_allowed(float("nan"), 0.33))
        self.assertFalse(diag.profile_pair_allowed(0.2, 0.5))
        self.assertFalse(diag.profile_pair_allowed(0.25, 0.25))
        self.assertFalse(diag.profile_pair_allowed(0.0, 0.33))
        self.assertFalse(diag.profile_pair_allowed(1, 0.5))
        self.assertIs(type(diag.FIXED_PITCH), int)
        self.assertIs(type(diag.FIXED_FILTER_RADIUS), int)
        self.assertEqual(diag.FIXED_FILTER_RADIUS, 1)
        self.assertIs(type(diag.FIXED_RMS_MIX_RATE), float)
        for profile in diag.RUN_PROFILES:
            self.assertIs(type(profile.protect), float)
            self.assertEqual(profile.filter_radius, 1)

    def test_build_config_locks_params_and_rejects_baseline_id(self) -> None:
        diag = _diag()
        effective = {
            "model_path": "sealed.pth",
            "index_path": "sealed.index",
            "index_rate": 9.9,
            "filter_radius": 99,
            "pitch": 7,
            "protect": 0.1,
            "rms_mix_rate": 1.0,
            "f0_method": "crepe",
            "fallback_on_worker_failure": True,
            "worker_enabled": False,
            "postprocess_enabled": True,
            "postprocess_highpass_hz": 80,
            "evil_path": "C:/should-not-be-used-as-profile",
        }
        cfg = diag.build_profile_rvc_config(effective, "idx025_p050")
        self.assertEqual(cfg["model_path"], "sealed.pth")
        self.assertEqual(cfg["index_path"], "sealed.index")
        self.assertEqual(cfg["index_rate"], 0.25)
        self.assertEqual(cfg["filter_radius"], 1)
        self.assertEqual(cfg["pitch"], 0)
        self.assertEqual(cfg["protect"], 0.5)
        self.assertEqual(cfg["rms_mix_rate"], 0.68)
        self.assertEqual(cfg["f0_method"], "rmvpe")
        self.assertTrue(cfg["worker_enabled"])
        self.assertFalse(cfg["fallback_on_worker_failure"])
        self.assertTrue(cfg["postprocess_enabled"])
        self.assertEqual(cfg["postprocess_highpass_hz"], 80)
        voiced = diag.build_profile_rvc_config(effective, "idx055_p033")
        self.assertEqual(voiced["index_rate"], 0.55)
        self.assertEqual(voiced["protect"], 0.33)
        self.assertEqual(voiced["filter_radius"], 1)
        combined = diag.build_profile_rvc_config(effective, "idx025_p033")
        self.assertEqual(combined["index_rate"], 0.25)
        self.assertEqual(combined["protect"], 0.33)
        bare = diag.build_profile_rvc_config(effective, "idx000_p050")
        self.assertEqual(bare["index_rate"], 0.0)
        self.assertEqual(bare["protect"], 0.5)
        self.assertEqual(bare["filter_radius"], 1)
        with self.assertRaises(diag.ArtifactStageError) as ctx:
            diag.build_profile_rvc_config(effective, "idx055_fr1")
        self.assertEqual(ctx.exception.stage, "profile_params")
        with self.assertRaises(diag.ArtifactStageError):
            diag.build_profile_rvc_config(effective, "../evil")

    def test_nan_duration_is_fixed_stage(self) -> None:
        diag = _diag()

        class _Info:
            samplerate = 22050
            frames = 10
            duration = float("nan")

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.wav"
            _write_tiny_wav(path)
            with patch("soundfile.info", return_value=_Info()):
                with self.assertRaises(diag.ArtifactStageError) as ctx:
                    diag.read_wav_metrics(path, role="input")
        self.assertEqual(ctx.exception.stage, "input_wav_invalid")

    def test_memory_scope_rejected(self) -> None:
        diag = _diag()
        with self.assertRaises(diag.ArtifactStageError):
            diag.assert_eval_scope(Path("data/memory/private.sqlite"))
        self.assertNotIn("memory", diag.INPUT_DIR_REL.split("/"))
        self.assertNotIn("memory", diag.OUTPUT_DIR_REL.split("/"))

    def test_source_does_not_launch_tts_or_raw_rvc_scripts(self) -> None:
        text = (_REPO / "diagnose_rvc_artifacts.py").read_text(encoding="utf-8")
        self.assertNotIn("TTSEngine", text)
        self.assertNotIn("AudioPlayer", text)
        self.assertNotIn("pygame", text)
        self.assertNotIn("rvc_worker.py", text)
        self.assertNotIn("rvc_infer.py", text)


class SessionLifecycleTests(unittest.TestCase):
    def test_shutdown_requires_worker_exit(self) -> None:
        diag = _diag()
        with patch.object(diag, "warm_rvc_worker", return_value=True) as warm, patch.object(
            diag, "rvc_worker_running", return_value=False
        ), patch.object(diag, "stop_rvc_worker", return_value=True) as stop, patch.object(
            diag, "RVCConverter"
        ):
            session = diag.RvcArtifactSession(
                profile_id="idx025_p050",
                rvc_config={"fallback_on_worker_failure": False},
                safety_policy=object(),
                temp_files=object(),
            )
            session.shutdown(stop_rvc=True)
        self.assertEqual(warm.call_count, 1)
        self.assertEqual(stop.call_count, 1)

        with patch.object(diag, "warm_rvc_worker", return_value=True), patch.object(
            diag, "rvc_worker_running", return_value=False
        ), patch.object(diag, "stop_rvc_worker", return_value=False), patch.object(
            diag, "RVCConverter"
        ):
            session = diag.RvcArtifactSession(
                profile_id="idx025_p050",
                rvc_config={},
                safety_policy=object(),
                temp_files=object(),
            )
            with self.assertRaises(diag.ArtifactStageError) as ctx:
                session.shutdown(stop_rvc=True)
        self.assertEqual(ctx.exception.stage, "shutdown")

    def test_warm_failure_stops_and_does_not_retry(self) -> None:
        diag = _diag()
        with patch.object(diag, "warm_rvc_worker", return_value=False) as warm, patch.object(
            diag, "rvc_worker_running", return_value=False
        ), patch.object(diag, "stop_rvc_worker", return_value=True) as stop, patch.object(
            diag, "RVCConverter"
        ):
            with self.assertRaises(diag.ArtifactStageError) as ctx:
                diag.RvcArtifactSession(
                    profile_id="idx055_p033",
                    rvc_config={},
                    safety_policy=object(),
                    temp_files=object(),
                )
        self.assertEqual(ctx.exception.stage, "rvc")
        self.assertEqual(warm.call_count, 1)
        self.assertEqual(stop.call_count, 1)

    def test_refuses_to_init_while_worker_alive(self) -> None:
        diag = _diag()
        with patch.object(diag, "rvc_worker_running", return_value=True), patch.object(
            diag, "warm_rvc_worker"
        ) as warm, patch.object(diag, "RVCConverter") as conv:
            with self.assertRaises(diag.ArtifactStageError) as ctx:
                diag.RvcArtifactSession(
                    profile_id="idx000_p050",
                    rvc_config={},
                    safety_policy=object(),
                    temp_files=object(),
                )
        self.assertEqual(ctx.exception.stage, "worker_alive")
        self.assertEqual(warm.call_count, 0)
        self.assertEqual(conv.call_count, 0)


class _BusyLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.released = False

    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        self.released = True


class ExecuteContractTests(unittest.TestCase):
    def _populate(
        self,
        root: Path,
        *,
        missing: Optional[str] = None,
        empty: Optional[str] = None,
        garbage: Optional[str] = None,
    ) -> Dict[str, str]:
        diag = _diag()
        input_dir = root / diag.INPUT_DIR_REL
        hashes: Dict[str, str] = {}
        for index, (label, _text, _instruct) in enumerate(diag.SAMPLE_CASES, 1):
            base = input_dir / diag.sample_base_name(index, label)
            if label == missing:
                continue
            if label == empty:
                base.parent.mkdir(parents=True, exist_ok=True)
                base.write_bytes(b"")
            elif label == garbage:
                base.parent.mkdir(parents=True, exist_ok=True)
                base.write_bytes(b"notwav")
            else:
                _write_tiny_wav(base, frames=2205)
            baseline = input_dir / diag.baseline_rvc_name(index, label)
            _write_tiny_wav(baseline, frames=400)
            if base.is_file() and base.stat().st_size > 0 and label != garbage:
                hashes[str(base.resolve())] = _sha(base)
            hashes[str(baseline.resolve())] = _sha(baseline)
        sentinel = input_dir / "report.json"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text('{"sentinel":"keep"}\n', encoding="utf-8")
        hashes[str(sentinel.resolve())] = _sha(sentinel)
        return hashes

    def _run(
        self,
        root: Path,
        *,
        fail_profile: Optional[str] = None,
        fail_convert_at: Optional[int] = None,
        fail_shutdown: Optional[str] = None,
        secret: str = "",
        lock_cls: Any = None,
        cleanup_fail: bool = False,
        report_fail: bool = False,
        adopt_fail: bool = False,
        copy_fail: bool = False,
        publish_partial: bool = False,
        health_ok: bool = True,
        worker_busy_at: Optional[int] = None,
        empty_rvc: bool = False,
    ) -> Dict[str, Any]:
        diag = _diag()
        rvc = _fake_assets(root / "assets")
        cfg = _FakeConfig(rvc)
        timeline: List[str] = []
        adopted: List[Path] = []
        temps: List[str] = []
        seen: List[dict] = []
        health_calls = {"n": 0}
        converts = {"n": 0}
        locks: List[Any] = []

        class FakeSession:
            def __init__(
                self,
                *,
                profile_id: str,
                rvc_config: dict,
                safety_policy: Any,
                temp_files: TempFileManager,
            ) -> None:
                self.profile_id = profile_id
                self.rvc_config = rvc_config
                self._temp_files = temp_files
                self._n = 0
                seen.append(dict(rvc_config))

            def convert_owned(self, owned_src: str) -> str:
                converts["n"] += 1
                self._n += 1
                if not self._temp_files.is_owned(owned_src):
                    raise PermissionError("unowned")
                path = Path(owned_src)
                if not path.parent.name.startswith("_staging_"):
                    raise AssertionError("not-staging")
                if self.profile_id not in path.name or not path.name.endswith("_base.wav"):
                    raise AssertionError("not-staged-base")
                if (
                    fail_profile == self.profile_id
                    and fail_convert_at is not None
                    and self._n == fail_convert_at
                ):
                    raise RuntimeError(secret or "forced-rvc")
                out = self._temp_files.create(".wav")
                if not empty_rvc:
                    _write_tiny_wav(Path(out))
                temps.append(out)
                return out

            def shutdown(self, *, stop_rvc: bool = True) -> None:
                if not stop_rvc:
                    raise diag.ArtifactStageError("shutdown", profile_id=self.profile_id)
                if fail_shutdown == self.profile_id:
                    raise diag.ArtifactStageError("shutdown", profile_id=self.profile_id)

        def health(_cfg: dict, *, safety_policy: Any) -> Any:
            health_calls["n"] += 1
            return type("Health", (), {"ok": health_ok})()

        running_calls = {"n": 0}

        def running() -> bool:
            running_calls["n"] += 1
            return worker_busy_at is not None and running_calls["n"] >= worker_busy_at

        real_adopt = TempFileManager.adopt_existing

        def tracking_adopt(self_tm: TempFileManager, file_path: Any) -> str:
            adopted.append(Path(file_path))
            name = Path(file_path).name
            if (
                adopt_fail
                and "_staging_" in Path(file_path).parent.name
                and name.endswith("_base.wav")
                and tracking_adopt.fired is False  # type: ignore[attr-defined]
            ):
                tracking_adopt.fired = True  # type: ignore[attr-defined]
                raise PermissionError("forced-adopt")
            return real_adopt(self_tm, file_path)

        tracking_adopt.fired = False  # type: ignore[attr-defined]

        real_copy = diag.shutil.copy2

        def tracking_copy(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
            dst_path = Path(dst)
            if (
                copy_fail
                and "_staging_" in dst_path.parent.name
                and dst_path.name.endswith("_base.wav")
            ):
                raise OSError("forced-copy")
            if publish_partial and dst_path.name.endswith(".publishing"):
                dst_path.write_bytes(b"PARTIAL")
                raise OSError("forced-publish")
            return real_copy(src, dst, *args, **kwargs)

        real_cleanup = diag.cleanup_owned_staging

        def tracking_cleanup(*args: Any, **kwargs: Any) -> None:
            if cleanup_fail:
                raise diag.ArtifactStageError("cleanup")
            return real_cleanup(*args, **kwargs)

        real_write = diag.atomic_write_json

        def tracking_write(path: Path, payload: dict) -> None:
            if report_fail and payload.get("complete") is True:
                raise OSError("forced-report")
            real_write(path, payload)

        def lock_factory(path: Path) -> Any:
            lock = (lock_cls or diag.EvalRunLock)(path)
            locks.append(lock)
            return lock

        buf = io.StringIO()
        with patch(
            "src.voice.naturalness_helpers.resolve_rvc_python",
            return_value=Path(rvc["python_path"]),
        ), patch.object(diag, "cleanup_owned_staging", tracking_cleanup), patch.object(
            diag.shutil, "copy2", tracking_copy
        ), patch.object(
            TempFileManager, "adopt_existing", tracking_adopt
        ), patch.object(
            diag, "atomic_write_json", tracking_write
        ), patch.object(diag, "_repo_root", return_value=root):
            with redirect_stdout(buf):
                code = diag.run_execute(
                    cfg,
                    session_cls=FakeSession,
                    timeline=timeline,
                    lock_cls=lock_factory,
                    health_check=health,
                    worker_running=running,
                )
        out_dir = root / diag.OUTPUT_DIR_REL
        report_path = out_dir / diag.REPORT_NAME
        report = None
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
        return {
            "code": code,
            "timeline": timeline,
            "stdout": buf.getvalue(),
            "report": report,
            "report_text": report_path.read_text(encoding="utf-8") if report else "",
            "out_dir": out_dir,
            "adopted": adopted,
            "temps": temps,
            "seen": seen,
            "converts": converts["n"],
            "health_calls": health_calls["n"],
            "locks": locks,
            "leftover_staging": [
                p.name for p in out_dir.glob("_staging_*") if p.exists()
            ] if out_dir.exists() else [],
            "miku": [p.name for p in out_dir.glob("miku_*.wav")] if out_dir.exists() else [],
            "publishing": [
                p.name
                for p in out_dir.iterdir()
                if ".publishing" in p.name
            ] if out_dir.exists() else [],
        }

    def _assert_inputs_unchanged(self, hashes: Dict[str, str]) -> None:
        for raw, digest in hashes.items():
            path = Path(raw)
            self.assertTrue(path.is_file(), raw)
            self.assertEqual(_sha(path), digest, raw)

    def _assert_not_adopted_inputs(self, result: Dict[str, Any], input_dir: Path) -> None:
        diag = _diag()
        for path in result["adopted"]:
            self.assertFalse(diag._is_under(path, input_dir), path)

    def test_success_serial_profiles_keep_finals_and_inputs(self) -> None:
        diag = _diag()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hashes = self._populate(root)
            input_dir = root / diag.INPUT_DIR_REL
            result = self._run(root)
            self.assertEqual(result["code"], 0, result["stdout"])
            self.assertEqual(result["converts"], 24)
            self.assertEqual(result["health_calls"], 1)
            self.assertEqual(result["leftover_staging"], [])
            self.assertEqual(result["miku"], [])
            self.assertEqual(result["publishing"], [])
            self.assertTrue(all(not Path(p).is_file() for p in result["temps"]))
            self._assert_inputs_unchanged(hashes)
            self._assert_not_adopted_inputs(result, input_dir)
            timeline = result["timeline"]
            ids = [p.profile_id for p in diag.RUN_PROFILES]
            for index, profile_id in enumerate(ids):
                self.assertLess(
                    timeline.index(f"init:{profile_id}"),
                    timeline.index(f"stop:{profile_id}"),
                )
                if index + 1 < len(ids):
                    self.assertLess(
                        timeline.index(f"stop:{profile_id}"),
                        timeline.index(f"init:{ids[index + 1]}"),
                    )
            self.assertLess(
                timeline.index("staging_cleaned"),
                timeline.index("report_published"),
            )
            report = result["report"]
            assert report is not None
            self.assertIs(report["complete"], True)
            self.assertIs(report["baseline"]["rerun"], False)
            self.assertEqual(report["baseline"]["profile_id"], "idx055_fr1")
            self.assertEqual(report["baseline"]["protect"], 0.5)
            self.assertEqual(report["baseline"]["index_rate"], 0.55)
            self.assertEqual(report["baseline"]["filter_radius"], 1)
            self.assertEqual(report["fixed"]["filter_radius"], 1)
            self.assertEqual(report["fixed"]["pitch"], 0)
            self.assertEqual(report["fixed"]["f0_method"], "rmvpe")
            self.assertEqual(
                [row["profile_id"] for row in report["profiles"]],
                ids,
            )
            self.assertNotIn(str(root), result["report_text"])
            for profile, seen in zip(diag.RUN_PROFILES, result["seen"]):
                self.assertEqual(seen["index_rate"], profile.index_rate)
                self.assertEqual(seen["filter_radius"], profile.filter_radius)
                self.assertEqual(seen["pitch"], 0)
                self.assertEqual(seen["protect"], profile.protect)
                self.assertEqual(seen["rms_mix_rate"], 0.68)
                self.assertEqual(seen["f0_method"], "rmvpe")
                self.assertTrue(seen["worker_enabled"])
                self.assertFalse(seen["fallback_on_worker_failure"])
                self.assertTrue(seen["postprocess_enabled"])
                self.assertEqual(seen["model_path"], str(root / "assets" / "model.pth"))
            for profile in diag.RUN_PROFILES:
                block = next(
                    row for row in report["profiles"] if row["profile_id"] == profile.profile_id
                )
                self.assertEqual(block["index_rate"], profile.index_rate)
                self.assertEqual(block["protect"], profile.protect)
                self.assertEqual(block["filter_radius"], 1)
                self.assertEqual(
                    [sample["label"] for sample in block["samples"]],
                    [label for label, _t, _i in diag.SAMPLE_CASES],
                )
                for index, sample in enumerate(block["samples"], 1):
                    name = sample["file"]
                    self.assertIn(sample["label"], name)
                    self.assertIn(profile.profile_id, name)
                    final = result["out_dir"] / name
                    self.assertTrue(final.is_file(), name)
                    self.assertEqual(sample["bytes"], final.stat().st_size)
                    self.assertGreater(sample["sample_rate"], 0)
                    self.assertGreater(sample["duration_sec"], 0)
                    self.assertIs(type(sample["bytes"]), int)
                    self.assertNotIsInstance(sample["rvc_seconds"], bool)
            self.assertIsNone(result["locks"][0]._fd)

    def test_publish_partial_copy_leaves_no_residue(self) -> None:
        diag = _diag()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hashes = self._populate(root)
            result = self._run(root, publish_partial=True)
            self.assertNotEqual(result["code"], 0)
            report = result["report"]
            assert report is not None
            self.assertIs(report["complete"], False)
            self.assertEqual(result["publishing"], [])
            self.assertEqual(report["failure_stage"], "copy")
            self._assert_inputs_unchanged(hashes)

    def test_mid_profile_failure_keeps_previous_profiles(self) -> None:
        diag = _diag()
        secret = r"D:\Users\uitra\secret-token"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hashes = self._populate(root)
            input_dir = root / diag.INPUT_DIR_REL
            out_dir = root / diag.OUTPUT_DIR_REL
            preseeded: Dict[str, str] = {}
            for index, (label, _t, _i) in enumerate(diag.SAMPLE_CASES, 1):
                path = out_dir / diag.sample_output_name(index, label, "idx055_p033")
                _write_tiny_wav(path, frames=120)
                preseeded[str(path.resolve())] = _sha(path)
            kept = out_dir / diag.sample_output_name(1, "warm_companion", "idx000_p050")
            _write_tiny_wav(kept, frames=80)
            preseeded[str(kept.resolve())] = _sha(kept)
            result = self._run(
                root,
                fail_profile="idx055_p033",
                fail_convert_at=1,
                secret=secret,
            )
            self.assertEqual(result["code"], 1)
            self.assertEqual(result["converts"], 7)
            self.assertNotIn("init:idx025_p033", result["timeline"])
            self.assertNotIn("init:idx000_p050", result["timeline"])
            self.assertLess(
                result["timeline"].index("stop:idx025_p050"),
                result["timeline"].index("init:idx055_p033"),
            )
            self.assertIn("stop:idx055_p033", result["timeline"])
            report = result["report"]
            assert report is not None
            self.assertIs(report["complete"], False)
            self.assertEqual(report["failure_stage"], "rvc")
            self.assertEqual(report["failure_profile_id"], "idx055_p033")
            self.assertEqual(report["failure_label"], "warm_companion")
            self.assertEqual(report["profiles_completed"], ["idx025_p050"])
            self.assertNotIn(secret, result["report_text"])
            self.assertNotIn(secret, result["stdout"])
            self.assertNotIn(str(root), result["report_text"])
            self._assert_inputs_unchanged(hashes)
            self._assert_not_adopted_inputs(result, input_dir)
            for raw, digest in preseeded.items():
                path = Path(raw)
                self.assertTrue(path.is_file(), raw)
                self.assertEqual(_sha(path), digest)
            for index, (label, _t, _i) in enumerate(diag.SAMPLE_CASES, 1):
                final = out_dir / diag.sample_output_name(index, label, "idx025_p050")
                self.assertTrue(final.is_file(), final.name)
            self.assertEqual(result["leftover_staging"], [])
            self.assertEqual(result["miku"], [])
            self.assertTrue(result["locks"])
            self.assertIsNone(result["locks"][0]._fd)

    def test_missing_empty_and_garbage_inputs_never_start_rvc(self) -> None:
        diag = _diag()
        cases = (
            ("missing", "warm_companion", "input_missing"),
            ("empty", "caring_question", "input_empty"),
            ("garbage", "pleasant_surprise", "input_unreadable"),
        )
        for kind, label, stage in cases:
            with self.subTest(kind=kind):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    kwargs = {kind: label}
                    self._populate(root, **kwargs)
                    result = self._run(root)
                self.assertEqual(result["code"], 1)
                self.assertEqual(result["converts"], 0)
                self.assertEqual(result["health_calls"], 0)
                self.assertEqual(result["timeline"], [])
                self.assertEqual(result["leftover_staging"], [])
                report = result["report"]
                assert report is not None
                self.assertIs(report["complete"], False)
                self.assertEqual(report["failure_stage"], stage)
                self.assertEqual(report["failure_label"], label)

    def test_lock_busy_health_adopt_copy_shutdown_and_report(self) -> None:
        diag = _diag()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._populate(root)
            busy = self._run(root, lock_cls=_BusyLock)
            self.assertEqual(busy["code"], 1)
            self.assertEqual(busy["converts"], 0)
            self.assertEqual(busy["health_calls"], 0)
            self.assertIsNone(busy["report"])
            self.assertEqual(busy["timeline"], [])
            self.assertTrue(busy["locks"][0].released)

            health = self._run(root, health_ok=False)
            self.assertEqual(health["code"], 1)
            self.assertEqual(health["converts"], 0)
            self.assertEqual(health["timeline"], [])
            self.assertEqual(health["report"]["failure_stage"], "health")
            self.assertIs(health["report"]["complete"], False)
            self.assertEqual(health["leftover_staging"], [])

            adopted = self._run(root, adopt_fail=True)
            self.assertEqual(adopted["code"], 1)
            self.assertEqual(adopted["converts"], 0)
            self.assertEqual(adopted["report"]["failure_stage"], "adopt")
            self.assertEqual(adopted["leftover_staging"], [])
            self.assertIsNone(adopted["locks"][0]._fd)

            copied = self._run(root, copy_fail=True)
            self.assertEqual(copied["code"], 1)
            self.assertEqual(copied["converts"], 0)
            self.assertEqual(copied["report"]["failure_stage"], "copy")
            self.assertEqual(copied["leftover_staging"], [])

            stopped = self._run(root, fail_shutdown="idx025_p050")
            self.assertEqual(stopped["code"], 1)
            self.assertEqual(stopped["converts"], 6)
            self.assertNotIn("init:idx055_p033", stopped["timeline"])
            self.assertEqual(stopped["report"]["failure_stage"], "shutdown")
            self.assertEqual(stopped["report"]["failure_profile_id"], "idx025_p050")
            self.assertEqual(stopped["report"]["profiles_completed"], [])
            self.assertEqual(stopped["leftover_staging"], [])
            self.assertFalse(any(stopped["out_dir"].glob("*_idx025_p050.wav")))

            alive = self._run(root, worker_busy_at=2)
            self.assertEqual(alive["code"], 1)
            self.assertNotIn("init:idx055_p033", alive["timeline"])
            self.assertEqual(alive["report"]["failure_stage"], "worker_alive")
            self.assertEqual(alive["report"]["failure_profile_id"], "idx055_p033")
            self.assertTrue(
                (alive["out_dir"] / diag.sample_output_name(1, "warm_companion", "idx025_p050")).is_file()
            )

            reported = self._run(root, report_fail=True)
            self.assertEqual(reported["code"], 1)
            self.assertIn("staging_cleaned", reported["timeline"])
            self.assertNotIn("report_published", reported["timeline"])
            self.assertIs(reported["report"]["complete"], False)
            self.assertEqual(reported["report"]["failure_stage"], "report")
            self.assertEqual(reported["leftover_staging"], [])
            self.assertIsNone(reported["locks"][0]._fd)

            cleanup = self._run(root, cleanup_fail=True)
            self.assertEqual(cleanup["code"], 1)
            self.assertEqual(cleanup["converts"], 24)
            self.assertNotIn("staging_cleaned", cleanup["timeline"])
            self.assertIs(cleanup["report"]["complete"], False)
            self.assertEqual(cleanup["report"]["failure_stage"], "cleanup")
            self.assertEqual(len(cleanup["report"]["profiles_completed"]), 4)
            self.assertTrue(
                (cleanup["out_dir"] / diag.sample_output_name(1, "warm_companion", "idx000_p050")).is_file()
            )

    def test_policy_failure_does_not_create_output(self) -> None:
        diag = _diag()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _FakeConfig({})
            with patch.object(diag, "_repo_root", return_value=root), redirect_stdout(io.StringIO()):
                code = diag.run_execute(cfg, session_cls=lambda **kwargs: None)
            self.assertEqual(code, 1)
            self.assertFalse((root / diag.OUTPUT_DIR_REL).exists())


if __name__ == "__main__":
    unittest.main()
