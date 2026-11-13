# -*- coding: utf-8 -*-
"""表达型本地 TTS 评估 Round 1：dry-run / F1–F6 契约 / 串行 RVC fail-closed。"""
from __future__ import annotations

import io
import json
import math
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from src.voice.audio.temp_files import TempFileManager


def _suite():
    import cosyvoice_eval_suite as suite

    return suite


def _write_tiny_wav(path: Path, *, frames: int = 2205, sr: int = 22050) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    audio = np.zeros(frames, dtype=np.float32)
    sf.write(str(path), audio, sr, format="WAV", subtype="PCM_16")
    return path.stat().st_size


class DryRunTests(unittest.TestCase):
    def test_default_dry_run_zero_side_effects_and_six_cases(self) -> None:
        import diagnose_expressive_tts as diag

        suite = _suite()
        buf = io.StringIO()
        with patch.object(Path, "mkdir") as mkdir, patch(
            "subprocess.Popen"
        ) as popen, patch("subprocess.run") as run, patch.object(
            diag, "TTSEngine"
        ) as tts, patch.object(diag, "check_rvc_health") as health, patch.object(
            diag, "Config"
        ) as cfg:
            with redirect_stdout(buf):
                code = diag.main([])
        self.assertEqual(code, 0)
        self.assertEqual(mkdir.call_count, 0)
        self.assertEqual(popen.call_count, 0)
        self.assertEqual(run.call_count, 0)
        self.assertEqual(tts.call_count, 0)
        self.assertEqual(health.call_count, 0)
        self.assertEqual(cfg.call_count, 0)
        out = buf.getvalue()
        self.assertIn(suite.OUTPUT_DIR_REL, out)
        self.assertIn("不应联网", out)
        self.assertIn("严格串行", out)
        self.assertEqual(suite.EXPECTED_COUNT, 6)


class ArgparseAndAssetsTests(unittest.TestCase):
    def test_argparse_rejects_arbitrary_options(self) -> None:
        import diagnose_expressive_tts as diag

        for bad in (
            ["--suite", "core"],
            ["--text", "hi"],
            ["--style", "x"],
            ["--output", "somewhere"],
            ["--model-dir", "x"],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as ctx:
                    diag.main(bad)
                self.assertNotEqual(ctx.exception.code, 0)

    def test_worker_env_allowlist_excludes_secrets(self) -> None:
        import diagnose_expressive_tts as diag

        suite = _suite()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _prepare_vendor_layout(root)
            with patch.object(diag, "_repo_root", return_value=root), patch.dict(
                os.environ,
                {
                    "PATH": "C:\\Windows\\System32",
                    "SystemRoot": "C:\\Windows",
                    "OPENAI_API_KEY": "sk-secret",
                    "HF_TOKEN": "hf_secret",
                    "HTTP_PROXY": "http://user:pass@proxy:8080",
                    "PYTHONPATH": "C:\\evil",
                    "PYTHONHOME": "C:\\evilpy",
                    "MY_CREDENTIAL": "x",
                },
                clear=False,
            ):
                paths, err = diag.resolve_assets()
                self.assertEqual(err, "")
                assert paths is not None
                env = diag.build_worker_env(paths)
        self.assertEqual(env.get("PATH"), "C:\\Windows\\System32")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("HF_TOKEN", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("MY_CREDENTIAL", env)
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertIn("vendor", env["HF_HOME"].replace("\\", "/"))
        self.assertTrue(env["HF_HOME"].replace("\\", "/").endswith(
            suite.SUITE_CACHE_REL.replace("\\", "/") + "/hf"
        ) or suite.SUITE_CACHE_REL.replace("\\", "/") in env["HF_HOME"].replace("\\", "/"))

    def test_resolve_assets_fail_closed_before_spawn(self) -> None:
        import diagnose_expressive_tts as diag

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(diag, "_repo_root", return_value=root):
                paths, err = diag.resolve_assets()
            self.assertIsNone(paths)
            self.assertTrue(err.startswith("missing_"))


class AtomicWavWriteTests(unittest.TestCase):
    """F1：真实 soundfile 原子写（不加载 CosyVoice）。"""

    def test_atomic_write_wav_tmp_explicit_format(self) -> None:
        import importlib.util

        suite = _suite()
        worker_path = _SCRIPTS / "cosyvoice_eval_worker.py"
        spec = importlib.util.spec_from_file_location(
            "cosyvoice_eval_worker_f1", worker_path
        )
        assert spec and spec.loader
        sys.modules["cosyvoice_eval_suite"] = suite
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)

        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "probe_base.wav"
            n = worker._atomic_write_wav(
                out, np.sin(np.linspace(0, 1, 512, dtype=np.float32)), 22050
            )
            self.assertGreater(n, 0)
            self.assertTrue(out.is_file())
            self.assertFalse(Path(str(out) + ".tmp").exists())
            info = sf.info(str(out))
            self.assertGreater(info.frames, 0)
            self.assertEqual(info.samplerate, 22050)
            self.assertGreater(info.duration, 0)


class _FakeConfig:
    def __init__(self, rvc: dict) -> None:
        self._rvc = rvc

    def get(self, key: str, default: Any = None) -> Any:
        if key == "voice.tts":
            return {"engine": "edge-tts"}
        if key == "security":
            return {"permissions": {"cloud_tts": True, "file_delete": True}}
        return default

    def get_rvc_config(self) -> dict:
        return dict(self._rvc)


def _fake_rvc_assets(tmp: Path) -> dict:
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
        "filter_radius": 1,
        "rms_mix_rate": 0.68,
        "pitch": 0,
        "index_rate": 0.55,
    }


def _prepare_vendor_layout(root: Path) -> Dict[str, Path]:
    suite = _suite()
    py = root / suite.COSYVOICE_PYTHON_REL
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_bytes(b"PY")
    worker = root / suite.WORKER_SCRIPT_REL
    worker.parent.mkdir(parents=True, exist_ok=True)
    worker.write_text("# worker", encoding="utf-8")
    model = root / suite.MODEL_DIR_REL
    model.mkdir(parents=True, exist_ok=True)
    wetext = root / suite.WETEXT_DIR_REL
    wetext.mkdir(parents=True, exist_ok=True)
    matcha = root / suite.MATCHA_DIR_REL
    matcha.mkdir(parents=True, exist_ok=True)
    vendor = root / suite.VENDOR_COSYVOICE_REL
    vendor.mkdir(parents=True, exist_ok=True)
    out = root / suite.OUTPUT_DIR_REL
    return {
        "python": py,
        "worker": worker,
        "model": model,
        "wetext": wetext,
        "matcha": matcha,
        "vendor": vendor,
        "output": out,
    }


def _valid_worker_report(*, sample_bytes: Optional[Dict[str, int]] = None) -> dict:
    suite = _suite()
    samples = []
    for label, _t, _i in suite.SAMPLE_CASES:
        samples.append(
            {
                "label": label,
                "synth_seconds": 0.1,
                "audio_seconds": 0.1,
                "bytes": int((sample_bytes or {}).get(label, 1000)),
            }
        )
    return {
        "schema_version": suite.SCHEMA_VERSION,
        "cosyvoice_commit_expected": suite.COSYVOICE_COMMIT,
        "cosyvoice_commit_actual": suite.COSYVOICE_COMMIT,
        "matcha_commit_expected": suite.MATCHA_COMMIT,
        "matcha_commit_actual": suite.MATCHA_COMMIT,
        "wetext_commit_expected": suite.WETEXT_COMMIT,
        "wetext_commit_actual": suite.WETEXT_COMMIT,
        "model_revision_expected": suite.MODEL_REVISION,
        "model_revision_actual": suite.MODEL_REVISION,
        "model_bytes_expected": suite.MODEL_BYTES_EXPECTED,
        "model_bytes_actual": suite.MODEL_BYTES_EXPECTED,
        "speaker": suite.SPEAKER,
        "cold_load_seconds": 1.0,
        "generation_seconds": 2.0,
        "cuda_peak_allocated_mb": 100.0,
        "cuda_peak_reserved_mb": 120.0,
        "samples": samples,
    }


class _AlwaysLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.released = False

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        self.released = True


class _BusyLock(_AlwaysLock):
    def acquire(self) -> bool:
        return False


class FakeWorkerExecuteTests(unittest.TestCase):
    def _run_with_fake_worker(
        self,
        *,
        worker_exit: int = 0,
        write_wavs: bool = True,
        zero_byte_label: Optional[str] = None,
        report: Optional[dict] = None,
        omit_report: bool = False,
        hang: bool = False,
        rvc_fail_at: Optional[int] = None,
        stale_targets: bool = False,
        lock_cls=None,
        corrupt_report_bytes: bool = False,
        cleanup_fail: bool = False,
        adopt_fail: bool = False,
        copy_fail: bool = False,
        reject_unowned_input: bool = True,
    ) -> dict:
        import diagnose_expressive_tts as diag
        import shutil

        from src.voice.rvc.health_check import RVCHealthReport

        suite = _suite()
        timeline: List[str] = []
        shutdowns = {"n": 0}
        cleanups = {"n": 0}
        locks: List[Any] = []
        rvc_inputs: List[str] = []
        rvc_temps: List[str] = []
        result: dict = {
            "timeline": timeline,
            "shutdowns": shutdowns,
            "cleanups": cleanups,
            "rvc_inputs": rvc_inputs,
            "rvc_temps": rvc_temps,
        }
        popen_kwargs: dict = {}
        rvc_calls = {"n": 0}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = _prepare_vendor_layout(root)
            rvc = _fake_rvc_assets(root)
            cfg = _FakeConfig(rvc)
            out_dir = layout["output"]
            if stale_targets:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "01_warm_companion_base.wav").write_bytes(b"OLD")
                (out_dir / "01_warm_companion_rvc.wav").write_bytes(b"OLD")

            class FakeProc:
                def __init__(self, staging: Path) -> None:
                    self.staging = staging
                    self._rc: Optional[int] = None
                    self._wrote = False
                    self._killed = False

                def wait(self, timeout=None):
                    if hang and not self._killed:
                        raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
                    if self._wrote:
                        return self._rc
                    self.staging.mkdir(parents=True, exist_ok=True)
                    sample_bytes: Dict[str, int] = {}
                    if write_wavs:
                        for i, (label, _t, _ins) in enumerate(suite.SAMPLE_CASES, 1):
                            p = self.staging / f"{i:02d}_{label}_base.wav"
                            if zero_byte_label == label:
                                p.write_bytes(b"")
                                sample_bytes[label] = 0
                            else:
                                sample_bytes[label] = _write_tiny_wav(p)
                    if not omit_report:
                        payload = (
                            report
                            if report is not None
                            else _valid_worker_report(sample_bytes=sample_bytes)
                        )
                        if corrupt_report_bytes and payload.get("samples"):
                            payload = json.loads(json.dumps(payload))
                            payload["samples"][0]["bytes"] = 1
                        (self.staging / "worker_report.json").write_text(
                            json.dumps(payload), encoding="utf-8"
                        )
                    self._wrote = True
                    self._rc = worker_exit
                    return self._rc

                def poll(self):
                    return self._rc

                def terminate(self):
                    timeline.append("terminate")
                    return None

                def kill(self):
                    timeline.append("kill")
                    self._killed = True
                    self._rc = -9
                    return None

            def fake_popen(cmd, **kwargs):
                self.assertIs(kwargs.get("shell"), False)
                self.assertIs(kwargs.get("stderr"), subprocess.DEVNULL)
                self.assertIs(kwargs.get("stdout"), subprocess.DEVNULL)
                popen_kwargs.update(kwargs)
                timeline.append("popen")
                idx = list(cmd).index("--staging-dir")
                staging = Path(cmd[idx + 1])
                result["staging_name"] = staging.name
                self.assertTrue(staging.name.startswith("_staging_"))
                self.assertNotEqual(staging.name, "_staging")
                return FakeProc(staging)

            class FakeTTS:
                def __init__(self, **kwargs: Any) -> None:
                    timeline.append("rvc_init")
                    pol = kwargs["safety_policy"]
                    pol.register_file_delete_dir(out_dir)
                    self._temp_files = kwargs.get("temp_file_manager") or TempFileManager(
                        safety_policy=pol, default_dir=out_dir
                    )
                    self._n = 0

                def apply_rvc_model(self, path: str) -> Optional[str]:
                    if "first_rvc" not in timeline:
                        timeline.append("first_rvc")
                    self._n += 1
                    rvc_calls["n"] += 1
                    # F9：模拟 assert_rvc_io_paths 输入所有权门禁（不放宽）
                    if reject_unowned_input and not self._temp_files.is_owned(path):
                        raise PermissionError("RVC 子进程能力拒绝: 输入 未登记")
                    p = Path(path)
                    if not p.parent.name.startswith("_staging_"):
                        raise AssertionError("rvc_input_not_staging")
                    if not p.name.endswith("_base.wav"):
                        raise AssertionError("rvc_input_not_staging_base")
                    rvc_inputs.append(path)
                    if rvc_fail_at is not None and self._n == rvc_fail_at:
                        return None
                    # 输出经 TempFileManager.create，落在 out_dir，不污染 staging
                    out = self._temp_files.create(".wav")
                    Path(out).write_bytes(b"RVC")
                    rvc_temps.append(out)
                    return out

                def shutdown(self, *, stop_rvc: bool = True) -> None:
                    shutdowns["n"] += 1

            real_cleanup = diag.cleanup_owned_staging

            def tracking_cleanup(temp_files, staging_path):
                cleanups["n"] += 1
                timeline.append("cleanup")
                if cleanup_fail:
                    raise RuntimeError("forced_cleanup_fail")
                return real_cleanup(temp_files, staging_path)

            real_adopt = TempFileManager.adopt_existing

            def tracking_adopt(self_tm, file_path):  # noqa: ANN001
                key_path = Path(file_path)
                if (
                    adopt_fail
                    and key_path.parent.name.startswith("_staging_")
                    and key_path.name.endswith("_base.wav")
                ):
                    raise PermissionError("forced_adopt_fail")
                return real_adopt(self_tm, file_path)

            real_copy2 = shutil.copy2

            def tracking_copy2(src, dst, *args, **kwargs):  # noqa: ANN001
                dst_path = Path(dst)
                if (
                    copy_fail
                    and dst_path.parent == out_dir
                    and dst_path.name.endswith("_base.wav")
                ):
                    raise OSError("forced_copy_fail")
                return real_copy2(src, dst, *args, **kwargs)

            chosen_lock = lock_cls or _AlwaysLock

            def lock_factory(path: Path):
                lock = chosen_lock(path)
                locks.append(lock)
                return lock

            with patch.object(diag, "_repo_root", return_value=root), patch(
                "src.voice.naturalness_helpers.resolve_rvc_python",
                return_value=Path(rvc["python_path"]),
            ), patch.object(
                diag,
                "check_rvc_health",
                return_value=RVCHealthReport(ok=True),
            ), patch.object(
                diag, "cleanup_owned_staging", side_effect=tracking_cleanup
            ), patch.object(
                TempFileManager, "adopt_existing", tracking_adopt
            ), patch.object(
                shutil, "copy2", tracking_copy2
            ), patch.object(diag, "WORKER_TIMEOUT_SEC", 1), patch.object(
                diag, "TERMINATE_GRACE_SEC", 0.05
            ), patch.object(diag, "KILL_WAIT_SEC", 0.05):
                # 避免 Windows 默认 cp1252 控制台因中文 print 干扰契约断言
                with redirect_stdout(io.StringIO()):
                    code = diag.run_execute(
                        cfg,
                        popen=fake_popen,
                        tts_engine_cls=FakeTTS,
                        timeline=timeline,
                        lock_cls=lock_factory,
                    )

            result["code"] = code
            result["out_dir"] = out_dir
            result["popen_kwargs"] = dict(popen_kwargs)
            result["rvc_calls"] = rvc_calls["n"]
            result["lock_released"] = all(
                getattr(lock, "released", False) for lock in locks
            )
            report_path = out_dir / "report.json"
            if report_path.is_file():
                result["report"] = json.loads(
                    report_path.read_text(encoding="utf-8")
                )
            else:
                result["report"] = None
            result["pairs"] = {}
            for i, (label, _t, _i) in enumerate(suite.SAMPLE_CASES, 1):
                result["pairs"][label] = {
                    "base": (out_dir / f"{i:02d}_{label}_base.wav").is_file(),
                    "rvc": (out_dir / f"{i:02d}_{label}_rvc.wav").is_file(),
                }
            leftover = [
                p.name
                for p in out_dir.glob("_staging_*")
                if p.is_dir()
            ]
            result["leftover_staging"] = leftover
            result["rvc_temps_exist"] = [
                Path(p).is_file() for p in rvc_temps
            ]
            # 最终 A/B 不得被 shutdown 删掉
            result["finals_survive"] = all(
                (out_dir / f"{i:02d}_{label}_base.wav").is_file()
                and (out_dir / f"{i:02d}_{label}_rvc.wav").is_file()
                for i, (label, _t, _i) in enumerate(suite.SAMPLE_CASES, 1)
            ) if code == 0 else None
        return result

    def test_worker_success_then_rvc_order_and_six_pairs(self) -> None:
        result = self._run_with_fake_worker()
        self.assertEqual(result["code"], 0)
        timeline = result["timeline"]
        self.assertLess(
            timeline.index("worker_exit"), timeline.index("rvc_init")
        )
        self.assertLess(
            timeline.index("rvc_init"), timeline.index("first_rvc")
        )
        self.assertIn("staging_cleaned", timeline)
        self.assertEqual(result["cleanups"]["n"], 1)
        self.assertEqual(result["shutdowns"]["n"], 1)
        self.assertTrue(result["lock_released"])
        self.assertEqual(result["popen_kwargs"].get("stderr"), subprocess.DEVNULL)
        self.assertEqual(result["leftover_staging"], [])
        self.assertEqual(len(result["rvc_inputs"]), 6)
        for owned in result["rvc_inputs"]:
            parent = Path(owned).parent.name
            self.assertTrue(parent.startswith("_staging_"), owned)
            self.assertTrue(Path(owned).name.endswith("_base.wav"), owned)
        self.assertTrue(result["rvc_temps"])
        self.assertTrue(all(not exists for exists in result["rvc_temps_exist"]))
        self.assertTrue(result["finals_survive"])
        for label, flags in result["pairs"].items():
            self.assertTrue(flags["base"], label)
            self.assertTrue(flags["rvc"], label)
        report = result["report"]
        assert report is not None
        self.assertTrue(report["complete"])
        self.assertEqual(report["success_count"], 6)

    def test_f9_staging_adopt_then_rvc_then_copy_finals(self) -> None:
        """F9：RVC 收到已登记 staging 输入；成功后最终 A/B 在、staging/RVC 临时回收。"""
        result = self._run_with_fake_worker()
        self.assertEqual(result["code"], 0)
        self.assertTrue(result["report"]["complete"])
        self.assertEqual(result["leftover_staging"], [])
        self.assertEqual(result["rvc_calls"], 6)
        self.assertEqual(len(result["rvc_inputs"]), 6)
        self.assertTrue(all(not exists for exists in result["rvc_temps_exist"]))
        self.assertTrue(result["finals_survive"])
        self.assertTrue(result["lock_released"])

    def test_f9_unowned_input_still_rejected(self) -> None:
        """F9：未登记输入仍被拒绝；不得放宽 assert_rvc_io_paths 语义。"""
        from src.core.safety import SafetyPolicy

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "_staging_x"
            staging.mkdir()
            unowned = staging / "01_warm_companion_base.wav"
            unowned.write_bytes(b"WAVDATA")
            pol = SafetyPolicy({"permissions": {"file_delete": True}})
            pol.register_file_delete_dir(root)
            tm = TempFileManager(safety_policy=pol, default_dir=root)
            owned_out = tm.create(".wav")
            with self.assertRaises(PermissionError) as ctx:
                tm.assert_rvc_io_paths(str(unowned), owned_out)
            self.assertIn("未登记", str(ctx.exception))
            # 登记后应通过输入侧校验（输出已由 create 登记）
            owned_in = tm.adopt_existing(unowned)
            tm.assert_rvc_io_paths(owned_in, owned_out)
            tm.shutdown(timeout=0.2)

    def test_f9_adopt_failure_not_complete(self) -> None:
        result = self._run_with_fake_worker(adopt_fail=True)
        self.assertEqual(result["code"], 1)
        self.assertFalse(result["report"]["complete"])
        self.assertEqual(result["report"].get("failure_reason"), "rvc_stage_adopt")
        self.assertEqual(result["rvc_calls"], 0)
        self.assertTrue(result["lock_released"])
        self.assertEqual(result["shutdowns"]["n"], 1)

    def test_f9_copy_failure_not_complete(self) -> None:
        result = self._run_with_fake_worker(copy_fail=True)
        self.assertEqual(result["code"], 1)
        self.assertFalse(result["report"]["complete"])
        self.assertEqual(result["report"].get("failure_reason"), "rvc_stage_copy_final")
        self.assertEqual(result["rvc_calls"], 1)
        self.assertTrue(result["lock_released"])
        self.assertEqual(result["shutdowns"]["n"], 1)
        # 失败样本最终 A/B 不得残留为成功态
        self.assertFalse(result["pairs"]["warm_companion"]["base"])
        self.assertFalse(result["pairs"]["warm_companion"]["rvc"])

    def test_success_cleanup_failure_not_complete(self) -> None:
        """F7：六组成功后 cleanup 失败 → 非零、非 complete、锁释放、无额外 RVC。"""
        result = self._run_with_fake_worker(cleanup_fail=True)
        self.assertEqual(result["code"], 1)
        self.assertFalse(result["report"]["complete"])
        self.assertEqual(result["report"].get("failure_reason"), "staging_cleanup")
        self.assertEqual(result["report"]["success_count"], 6)
        self.assertTrue(result["lock_released"])
        self.assertEqual(result["rvc_calls"], 6)
        self.assertNotIn("staging_cleaned", result["timeline"])
        # 成功路径 cleanup 抛错后 finally 会再尽力回收一次
        self.assertGreaterEqual(result["cleanups"]["n"], 1)
        self.assertEqual(result["shutdowns"]["n"], 1)

    def test_second_instance_lock_busy_no_worker(self) -> None:
        result = self._run_with_fake_worker(lock_cls=_BusyLock)
        self.assertEqual(result["code"], 1)
        self.assertNotIn("popen", result["timeline"])
        self.assertNotIn("rvc_init", result["timeline"])

    def test_worker_nonzero_no_rvc(self) -> None:
        result = self._run_with_fake_worker(worker_exit=2)
        self.assertEqual(result["code"], 1)
        self.assertNotIn("rvc_init", result["timeline"])
        self.assertFalse(result["report"]["complete"])

    def test_worker_timeout_terminate_kill_no_rvc(self) -> None:
        result = self._run_with_fake_worker(hang=True)
        self.assertEqual(result["code"], 1)
        self.assertIn("terminate", result["timeline"])
        self.assertIn("kill", result["timeline"])
        self.assertNotIn("rvc_init", result["timeline"])

    def test_missing_or_empty_wav_no_rvc(self) -> None:
        for mode in ("missing", "empty"):
            with self.subTest(mode=mode):
                kwargs: dict = {}
                if mode == "missing":
                    kwargs["write_wavs"] = False
                else:
                    kwargs["zero_byte_label"] = "warm_companion"
                result = self._run_with_fake_worker(**kwargs)
                self.assertEqual(result["code"], 1)
                self.assertNotIn("rvc_init", result["timeline"])
                self.assertFalse(result["report"]["complete"])

    def test_bad_report_revision_no_rvc(self) -> None:
        bad = _valid_worker_report()
        bad["model_revision_actual"] = "deadbeef" * 5
        result = self._run_with_fake_worker(report=bad)
        self.assertEqual(result["code"], 1)
        self.assertNotIn("rvc_init", result["timeline"])
        self.assertFalse(result["report"]["complete"])

    def test_bytes_mismatch_matrix(self) -> None:
        result = self._run_with_fake_worker(corrupt_report_bytes=True)
        self.assertEqual(result["code"], 1)
        self.assertNotIn("rvc_init", result["timeline"])
        self.assertFalse(result["report"]["complete"])

    def test_report_nan_negative_rejected(self) -> None:
        import diagnose_expressive_tts as diag

        suite = _suite()
        with TemporaryDirectory() as tmp:
            staging = Path(tmp)
            for i, (label, _t, _i) in enumerate(suite.SAMPLE_CASES, 1):
                _write_tiny_wav(staging / f"{i:02d}_{label}_base.wav")
            sizes = {
                label: (staging / f"{i:02d}_{label}_base.wav").stat().st_size
                for i, (label, _t, _i) in enumerate(suite.SAMPLE_CASES, 1)
            }
            base = _valid_worker_report(sample_bytes=sizes)
            cases = [
                ("cold_load_seconds", float("nan")),
                ("generation_seconds", -1),
                ("cuda_peak_allocated_mb", float("inf")),
            ]
            for key, val in cases:
                with self.subTest(key=key):
                    payload = json.loads(json.dumps(base))
                    payload[key] = val
                    err = diag.validate_worker_report(payload, staging=staging)
                    self.assertIsNotNone(err)
            payload = json.loads(json.dumps(base))
            payload["samples"][0]["audio_seconds"] = 0
            self.assertEqual(
                diag.validate_worker_report(payload, staging=staging),
                "audio_seconds_nonpositive",
            )
            payload = json.loads(json.dumps(base))
            payload["samples"][0]["synth_seconds"] = "0.1"
            self.assertEqual(
                diag.validate_worker_report(payload, staging=staging),
                "bad_synth_seconds",
            )

    def test_omit_report_no_rvc(self) -> None:
        result = self._run_with_fake_worker(omit_report=True)
        self.assertEqual(result["code"], 1)
        self.assertNotIn("rvc_init", result["timeline"])

    def test_rvc_mid_failure_cleans_and_incomplete(self) -> None:
        result = self._run_with_fake_worker(rvc_fail_at=2, stale_targets=True)
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["shutdowns"]["n"], 1)
        self.assertTrue(result["pairs"]["warm_companion"]["base"])
        self.assertTrue(result["pairs"]["warm_companion"]["rvc"])
        self.assertFalse(result["pairs"]["caring_question"]["base"])
        self.assertFalse(result["pairs"]["caring_question"]["rvc"])
        self.assertFalse(result["report"]["complete"])
        self.assertEqual(result["report"]["success_count"], 1)


class AssetPinTests(unittest.TestCase):
    """F5：SHA/bytes/清单不符时零模型加载。"""

    def test_verify_pinned_assets_mismatch_before_model(self) -> None:
        suite = _suite()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cosy = root / "cosy"
            matcha = root / "matcha"
            wetext = root / "wetext"
            model = root / "model"
            for d in (cosy, matcha, wetext, model):
                d.mkdir()

            def _fake_git(repo: Path, commit: str) -> None:
                git = repo / ".git"
                git.mkdir()
                (git / "HEAD").write_text(
                    "ref: refs/heads/main\n", encoding="utf-8"
                )
                ref = git / "refs" / "heads"
                ref.mkdir(parents=True)
                (ref / "main").write_text(commit + "\n", encoding="utf-8")

            _fake_git(cosy, suite.COSYVOICE_COMMIT)
            _fake_git(matcha, suite.MATCHA_COMMIT)
            _fake_git(wetext, "0" * 40)  # mismatch
            for rel in suite.MODEL_REQUIRED_FILES:
                (model / rel).write_bytes(b"x")
            meta = model / ".cache" / "huggingface" / "download"
            meta.mkdir(parents=True)
            (meta / "llm.pt.metadata").write_text(
                suite.MODEL_REVISION + "\n", encoding="utf-8"
            )
            with self.assertRaises(RuntimeError) as ctx:
                suite.verify_pinned_assets(
                    cosyvoice_root=cosy,
                    matcha_root=matcha,
                    wetext_root=wetext,
                    model_dir=model,
                )
            self.assertIn("wetext_commit_mismatch", str(ctx.exception))

    def test_worker_pin_fail_skips_automodel(self) -> None:
        import importlib.util

        suite = _suite()
        worker_path = _SCRIPTS / "cosyvoice_eval_worker.py"
        spec = importlib.util.spec_from_file_location(
            "cosyvoice_eval_worker_pin", worker_path
        )
        assert spec and spec.loader
        sys.modules["cosyvoice_eval_suite"] = suite
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)

        loads = {"n": 0}

        class BoomModel:
            def __init__(self, **kwargs):
                loads["n"] += 1

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / suite.OUTPUT_DIR_REL
            staging = out / "_staging_x"
            staging.mkdir(parents=True)
            report = staging / "worker_report.json"
            (root / suite.VENDOR_COSYVOICE_REL).mkdir(parents=True)
            (root / suite.MATCHA_DIR_REL).mkdir(parents=True)
            (root / suite.MODEL_DIR_REL).mkdir(parents=True)
            (root / suite.WETEXT_DIR_REL).mkdir(parents=True)

            with patch.object(worker, "_REPO_ROOT", root), patch.object(
                worker,
                "verify_pinned_assets",
                side_effect=RuntimeError("cosyvoice_commit_mismatch"),
            ), patch.dict(
                sys.modules,
                {
                    "cosyvoice": MagicMock(),
                    "cosyvoice.cli": MagicMock(),
                    "cosyvoice.cli.cosyvoice": MagicMock(AutoModel=BoomModel),
                    "torch": MagicMock(),
                },
            ):
                code = worker.run_worker(
                    staging_dir=staging, report_path=report
                )
            self.assertEqual(code, 2)
            self.assertEqual(loads["n"], 0)


class WorkerUnitTests(unittest.TestCase):
    def test_wetext_local_patch_and_single_load(self) -> None:
        import importlib.util
        import types

        suite = _suite()
        worker_path = _SCRIPTS / "cosyvoice_eval_worker.py"
        spec = importlib.util.spec_from_file_location(
            "cosyvoice_eval_worker_unit", worker_path
        )
        assert spec and spec.loader
        sys.modules["cosyvoice_eval_suite"] = suite
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)

        with TemporaryDirectory() as tmp:
            wetext = Path(tmp) / "wetext"
            wetext.mkdir()
            fake = types.SimpleNamespace(
                snapshot_download=lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("net")
                )
            )
            with patch.dict(sys.modules, {"wetext": types.ModuleType("wetext"), "wetext.wetext": fake}):
                worker._apply_local_wetext(wetext)
                self.assertEqual(
                    fake.snapshot_download("pengzhendong/wetext"),
                    str(wetext.resolve()),
                )

            load_count = {"n": 0}
            seen: List[tuple] = []

            class FakeModel:
                sample_rate = 22050

                def __init__(self, **kwargs):
                    load_count["n"] += 1

                def inference_instruct(self, text, spk, instruct, stream=False):
                    seen.append((text, spk, instruct, stream))
                    yield {
                        "tts_speech": np.zeros(100, dtype=np.float32)
                    }

            out_root = (
                Path(tmp)
                / "data"
                / "test"
                / "voice-naturalness"
                / "cosyvoice-instruct"
            )
            staging = out_root / "_staging"
            report = staging / "worker_report.json"
            staging.mkdir(parents=True, exist_ok=True)

            fake_torch = MagicMock()
            fake_torch.cuda.is_available.return_value = False
            fake_torch.cat = lambda xs, dim=-1: xs[0]

            pin = {
                "cosyvoice_commit_expected": suite.COSYVOICE_COMMIT,
                "cosyvoice_commit_actual": suite.COSYVOICE_COMMIT,
                "matcha_commit_expected": suite.MATCHA_COMMIT,
                "matcha_commit_actual": suite.MATCHA_COMMIT,
                "wetext_commit_expected": suite.WETEXT_COMMIT,
                "wetext_commit_actual": suite.WETEXT_COMMIT,
                "model_revision_expected": suite.MODEL_REVISION,
                "model_revision_actual": suite.MODEL_REVISION,
                "model_bytes_expected": suite.MODEL_BYTES_EXPECTED,
                "model_bytes_actual": suite.MODEL_BYTES_EXPECTED,
            }

            with patch.object(worker, "_REPO_ROOT", Path(tmp)), patch.object(
                worker, "verify_pinned_assets", return_value=pin
            ), patch.object(worker, "_apply_local_wetext"), patch.dict(
                sys.modules,
                {
                    "torch": fake_torch,
                    "soundfile": sf,
                    "numpy": np,
                    "cosyvoice": MagicMock(),
                    "cosyvoice.cli": MagicMock(),
                    "cosyvoice.cli.cosyvoice": MagicMock(AutoModel=FakeModel),
                },
            ):
                (Path(tmp) / suite.VENDOR_COSYVOICE_REL).mkdir(parents=True)
                (Path(tmp) / suite.MATCHA_DIR_REL).mkdir(parents=True)
                (Path(tmp) / suite.MODEL_DIR_REL).mkdir(parents=True)
                (Path(tmp) / suite.WETEXT_DIR_REL).mkdir(parents=True)
                code = worker.run_worker(
                    staging_dir=staging, report_path=report
                )
            self.assertEqual(code, 0)
            self.assertEqual(load_count["n"], 1)
            self.assertEqual(len(seen), 6)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["cosyvoice_commit_actual"], suite.COSYVOICE_COMMIT
            )
            self.assertEqual(payload["model_bytes_actual"], suite.MODEL_BYTES_EXPECTED)


class NaturalnessHelpersCompatTests(unittest.TestCase):
    def test_test_rvc_conversion_reexports_helpers(self) -> None:
        import test_rvc_conversion as ab
        from src.voice import naturalness_helpers as nh

        self.assertIs(ab.build_execute_safety_policy, nh.build_execute_safety_policy)
        self.assertIs(ab._purge_sample_artifacts, nh.purge_sample_artifacts)


class StagingCleanupFailTests(unittest.TestCase):
    def test_cleanup_unexpected_entry_fail_closed(self) -> None:
        import diagnose_expressive_tts as diag
        from src.core.safety import SafetyPolicy

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "_staging_x"
            staging.mkdir()
            (staging / "evil.bin").write_bytes(b"x")
            pol = SafetyPolicy(
                {"permissions": {"file_delete": True}}
            )
            pol.register_file_delete_dir(root)
            tm = TempFileManager(safety_policy=pol, default_dir=root)
            with self.assertRaises(RuntimeError):
                diag.cleanup_owned_staging(tm, staging)
            tm.shutdown(timeout=0.2)

    def test_worker_report_json_tmp_allowed_unknown_tmp_rejected(self) -> None:
        """F8：精确允许 worker_report.json.tmp；其它未知 .tmp/.json 仍 fail-closed。"""
        import diagnose_expressive_tts as diag
        from src.core.safety import SafetyPolicy

        suite = _suite()
        self.assertTrue(suite.staging_entry_allowed("worker_report.json.tmp"))
        self.assertFalse(suite.staging_entry_allowed("other.json.tmp"))
        self.assertFalse(suite.staging_entry_allowed("worker_report.json.bak"))
        self.assertFalse(suite.staging_entry_allowed("orphan.tmp"))
        self.assertFalse(suite.staging_entry_allowed("notes.json"))

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "_staging_ok"
            staging.mkdir()
            (staging / "worker_report.json.tmp").write_bytes(b"{")
            (staging / "01_warm_companion_base.wav").write_bytes(b"WAV")
            pol = SafetyPolicy({"permissions": {"file_delete": True}})
            pol.register_file_delete_dir(root)
            tm = TempFileManager(safety_policy=pol, default_dir=root)
            diag.cleanup_owned_staging(tm, staging)
            self.assertFalse(staging.exists())
            tm.shutdown(timeout=0.2)

        for bad_name in ("other.json.tmp", "orphan.tmp", "notes.json"):
            with self.subTest(bad_name=bad_name):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    staging = root / "_staging_bad"
                    staging.mkdir()
                    (staging / bad_name).write_bytes(b"x")
                    pol = SafetyPolicy({"permissions": {"file_delete": True}})
                    pol.register_file_delete_dir(root)
                    tm = TempFileManager(safety_policy=pol, default_dir=root)
                    with self.assertRaises(RuntimeError) as ctx:
                        diag.cleanup_owned_staging(tm, staging)
                    self.assertIn("staging_unexpected_entry", str(ctx.exception))
                    self.assertTrue(staging.is_dir())
                    tm.shutdown(timeout=0.2)


if __name__ == "__main__":
    unittest.main()
