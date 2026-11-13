# -*- coding: utf-8 -*-
"""diagnose_asr 与固定音频 helper 的离线契约测试。"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import diagnose_asr as diag  # noqa: E402
from src.voice.paraformer import preparation as prep  # noqa: E402
from src.voice.recognition import RecognitionResult, RecognitionStatus  # noqa: E402


class _FakeOutcome:
    def __init__(self, *, ok: bool = True, text: str = "你好", empty: bool = False, label: str = ""):
        self.ok = ok
        self.text = text
        self.empty = empty
        self.error_label = label


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self._proc = SimpleNamespace(pid=4242)

    def transcribe(self, pcm: bytes):
        return _FakeOutcome()

    def shutdown(self) -> bool:
        return True


class _FakeEngine:
    def __init__(self, *args, **kwargs):
        pass

    def _recognize_paraformer_result(self, audio):
        return RecognitionResult.success("你好")

    def shutdown(self) -> bool:
        return True


class DiagnoseDryRunTests(unittest.TestCase):
    def test_default_dry_run_no_execute(self) -> None:
        buf = io.StringIO()
        fake_report = prep.CheckReport(ok=False, components=())
        with patch.object(diag, "run_execute") as execute, patch.object(
            diag, "run_check", return_value=fake_report
        ), redirect_stdout(buf):
            code = diag.main([])
        execute.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("dry-run", buf.getvalue())

    def test_rejects_extra_args(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            diag.main(["--execute", "--clips", "3"])
        self.assertNotEqual(ctx.exception.code, 0)


class DiagnoseVerdictTests(unittest.TestCase):
    def test_finalize_rejects_false_green_shutdown(self) -> None:
        payload = {
            "report_written": True,
            "shutdown_complete": False,
            "stt_engine_shutdown_complete": True,
            "shared_wav_metadata_unchanged": True,
            "owned_pids_gone": True,
            "inference_all_ok": True,
            "wrapper_ok": True,
            "nahida_reference": {"ok": True},
        }
        complete, code = prep.finalize_diagnostic_verdict(payload)
        self.assertFalse(complete)
        self.assertEqual(code, 1)

    def test_finalize_without_report_written_for_pre_write(self) -> None:
        payload = {
            "shutdown_complete": True,
            "stt_engine_shutdown_complete": True,
            "shared_wav_metadata_unchanged": True,
            "owned_pids_gone": True,
            "inference_all_ok": True,
            "wrapper_ok": True,
            "nahida_reference": {"ok": True},
        }
        complete, code = prep.finalize_diagnostic_verdict(
            payload, require_report_written=False
        )
        self.assertTrue(complete)
        self.assertEqual(code, 0)


class DiagnoseExecuteFakeTests(unittest.TestCase):
    def _common_patches(self, root: Path):
        policy = MagicMock()
        ok_check = prep.CheckReport(ok=True, components=())
        shared_wav_dir = root / "data" / "tmp"
        report_dir = root / "tmp"
        shared_wav_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        return (
            patch.object(diag, "maybe_join_owned_job"),
            patch.object(diag, "PROJECT_ROOT", root),
            patch.object(diag, "SHARED_MIKU_WAV_DIR", shared_wav_dir),
            patch.object(diag, "DIAGNOSTIC_REPORT_DIR", report_dir),
            patch.object(diag, "build_diagnostic_safety_policy", return_value=(policy, "")),
            patch.object(diag, "run_check", return_value=ok_check),
            patch.object(diag, "check_host_memory_for_setup", return_value=prep.ComponentStatus("host_memory", True)),
            patch.object(diag, "verify_nahida_reference", return_value=prep.ComponentStatus("nahida_reference", True)),
            patch.object(diag, "snapshot_shared_miku_wavs", return_value=[]),
            patch.object(diag, "read_host_memory_snapshot", return_value=None),
            patch.object(diag, "verify_pids_gone_with_settle", return_value=True),
            patch.object(diag, "try_tree_sample", return_value={"pid": 4242, "pids": [4242, 4243]}),
        )

    @staticmethod
    def _enter_common_patches(
        stack: ExitStack,
        root: Path,
        *,
        skip: frozenset[int] = frozenset(),
    ) -> None:
        for index, ctx in enumerate(DiagnoseExecuteFakeTests()._common_patches(root)):
            if index not in skip:
                stack.enter_context(ctx)

    def test_execute_fail_closed_when_not_prepared(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(diag, "maybe_join_owned_job"), patch.object(
                diag, "PROJECT_ROOT", root
            ), patch.object(diag, "SHARED_MIKU_WAV_DIR", root / "data" / "tmp"), patch.object(
                diag, "DIAGNOSTIC_REPORT_DIR", root / "tmp"
            ), patch.object(
                diag, "build_diagnostic_safety_policy", return_value=(MagicMock(), "")
            ), patch.object(
                diag, "run_check", return_value=prep.CheckReport(ok=False, components=())
            ), patch.object(
                diag, "verify_nahida_reference",
                return_value=prep.ComponentStatus("nahida_reference", True),
            ), patch.object(diag, "snapshot_shared_miku_wavs", return_value=[]), patch.object(
                diag, "read_host_memory_snapshot", return_value=None
            ), patch.object(diag, "_write_report"), patch.object(
                diag, "_exclusive_report_path", return_value=root / "report.json"
            ):
                code = diag.run_execute(clock=lambda: 0.0, perf_counter=lambda: 0.0)
        self.assertEqual(code, 1)

    def test_execute_success_with_fake_client(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clip.wav"
            clip.write_bytes(b"unused")
            patches = self._common_patches(root)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patch.object(
                diag, "resolve_diagnostic_clips", return_value=[clip]
            ), patch.object(
                diag, "load_pcm_16k_mono", return_value=(b"\x00\x01" * 100, 0.5)
            ), patch.object(
                diag, "_exclusive_report_path", return_value=root / "report.json"
            ), patch.object(
                diag, "_write_report"
            ) as write_report:
                code = diag.run_execute(
                    clock=lambda: 0.0,
                    perf_counter=lambda: 0.0,
                    client_cls=_FakeClient,
                    stt_engine_cls=_FakeEngine,
                )
                self.assertEqual(write_report.call_count, 1)
                written = write_report.call_args[0][1]
                self.assertTrue(written.get("complete"))
                self.assertTrue(written.get("report_written"))
                self.assertFalse(written.get("failure"))
                self.assertIs(written.get("stt_engine_shutdown_complete"), True)
        self.assertEqual(code, 0)

    def test_execute_fails_on_empty_transcript(self) -> None:
        class EmptyClient(_FakeClient):
            def transcribe(self, pcm: bytes):
                return _FakeOutcome(ok=False, text="", empty=True, label="empty")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clip.wav"
            clip.write_bytes(b"x")
            patches = self._common_patches(root)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patch.object(
                diag, "resolve_diagnostic_clips", return_value=[clip]
            ), patch.object(
                diag, "load_pcm_16k_mono", return_value=(b"\x00\x01" * 10, 0.1)
            ), patch.object(
                diag, "_exclusive_report_path", return_value=root / "report.json"
            ), patch.object(diag, "_write_report"), patch.object(
                diag, "ParaformerWorkerClient", EmptyClient
            ):
                code = diag.run_execute(
                    clock=lambda: 0.0,
                    perf_counter=lambda: 0.0,
                )
        self.assertEqual(code, 1)

    def test_execute_fails_when_report_write_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clip.wav"
            clip.write_bytes(b"x")
            patches = self._common_patches(root)

            def boom(*args, **kwargs):
                raise OSError("report_fail")

            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patch.object(
                diag, "resolve_diagnostic_clips", return_value=[clip]
            ), patch.object(
                diag, "load_pcm_16k_mono", return_value=(b"\x00\x01" * 10, 0.1)
            ), patch.object(
                diag, "_exclusive_report_path", return_value=root / "report.json"
            ), patch.object(diag, "_write_report", side_effect=boom), patch.object(
                diag, "ParaformerWorkerClient", _FakeClient
            ), patch.object(
                diag, "STTEngine"
            ) as stt_cls:
                engine = MagicMock()
                engine.shutdown.return_value = True
                stt_cls.return_value = engine
                engine._recognize_paraformer_result.return_value = RecognitionResult.success(
                    "你好"
                )
                code = diag.run_execute(
                    clock=lambda: 0.0,
                    perf_counter=lambda: 0.0,
                )
        self.assertEqual(code, 1)


class DiagnosticArtifactScopeTests(unittest.TestCase):
    def test_report_path_under_repo_tmp_not_shared_data_tmp(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "data" / "tmp"
            report_root = root / "tmp"
            shared.mkdir(parents=True)
            with patch.object(diag, "PROJECT_ROOT", root), patch.object(
                diag, "SHARED_MIKU_WAV_DIR", shared
            ), patch.object(diag, "DIAGNOSTIC_REPORT_DIR", report_root):
                path = diag._exclusive_report_path()
            self.assertEqual(path.parent.resolve(), report_root.resolve())
            self.assertNotEqual(path.parent.resolve(), shared.resolve())

    def test_run_execute_snapshots_shared_dir_not_report_dir(self) -> None:
        observed: list[Path] = []

        def capture(tmp_dir: Path):
            observed.append(tmp_dir)
            return []

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "data" / "tmp"
            report_root = root / "tmp"
            shared.mkdir(parents=True)
            report_root.mkdir(parents=True)
            clip = root / "clip.wav"
            clip.write_bytes(b"x")
            with ExitStack() as stack:
                DiagnoseExecuteFakeTests._enter_common_patches(
                    stack, root, skip=frozenset({8})
                )
                stack.enter_context(
                    patch.object(
                        diag, "snapshot_shared_miku_wavs", side_effect=capture
                    )
                )
                stack.enter_context(
                    patch.object(
                        diag, "resolve_diagnostic_clips", return_value=[clip]
                    )
                )
                stack.enter_context(
                    patch.object(
                        diag, "load_pcm_16k_mono", return_value=(b"\x00\x01" * 8, 0.1)
                    )
                )
                stack.enter_context(
                    patch.object(
                        diag,
                        "_exclusive_report_path",
                        return_value=report_root / "report.json",
                    )
                )
                stack.enter_context(patch.object(diag, "_write_report"))
                stack.enter_context(
                    patch.object(diag, "ParaformerWorkerClient", _FakeClient)
                )
                diag.run_execute(
                    clock=lambda: 0.0,
                    perf_counter=lambda: 0.0,
                    client_cls=_FakeClient,
                    stt_engine_cls=_FakeEngine,
                )
            self.assertGreaterEqual(len(observed), 2)
            self.assertTrue(all(p.resolve() == shared.resolve() for p in observed))


class DiagnosticPidEvidenceTests(unittest.TestCase):
    def test_verify_pids_gone_with_settle_waits_for_observable_exit(self) -> None:
        state = {"alive": True}
        clock = {"t": 0.0}

        def fake_clock() -> float:
            return clock["t"]

        def fake_sleep(sec: float) -> None:
            clock["t"] += sec
            state["alive"] = False

        with patch.object(prep, "process_alive", side_effect=lambda _pid: state["alive"]), patch.object(
            prep.time, "sleep", fake_sleep
        ):
            self.assertTrue(
                prep.verify_pids_gone_with_settle([4242], clock=fake_clock, settle_sec=2.0)
            )

    def test_verify_pids_gone_with_settle_fail_closed_when_still_alive(self) -> None:
        clock = {"t": 0.0}

        def fake_clock() -> float:
            return clock["t"]

        def fake_sleep(sec: float) -> None:
            clock["t"] += sec

        with patch.object(prep, "process_alive", return_value=True), patch.object(
            prep.time, "sleep", fake_sleep
        ):
            self.assertFalse(
                prep.verify_pids_gone_with_settle([1], clock=fake_clock, settle_sec=0.15)
            )

    def test_execute_runs_pid_settle_after_shutdown_without_mutating_owners(self) -> None:
        events: list[str] = []
        shared_proc = SimpleNamespace(pid=4242)

        class Client(_FakeClient):
            def __init__(self, *args, **kwargs):
                self._proc = shared_proc

            def shutdown(self) -> bool:
                events.append("shutdown")
                return True

        def fake_verify(pids, **kwargs):
            events.append("verify")
            return True

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clip.wav"
            clip.write_bytes(b"x")
            with ExitStack() as stack:
                DiagnoseExecuteFakeTests._enter_common_patches(
                    stack, root, skip=frozenset({10})
                )
                stack.enter_context(
                    patch.object(
                        diag, "verify_pids_gone_with_settle", side_effect=fake_verify
                    )
                )
                stack.enter_context(
                    patch.object(
                        diag, "resolve_diagnostic_clips", return_value=[clip]
                    )
                )
                stack.enter_context(
                    patch.object(
                        diag, "load_pcm_16k_mono", return_value=(b"\x00\x01" * 8, 0.1)
                    )
                )
                stack.enter_context(
                    patch.object(
                        diag,
                        "_exclusive_report_path",
                        return_value=root / "tmp" / "report.json",
                    )
                )
                stack.enter_context(patch.object(diag, "_write_report"))
                diag.run_execute(
                    clock=lambda: 0.0,
                    perf_counter=lambda: 0.0,
                    client_cls=Client,
                    stt_engine_cls=_FakeEngine,
                )
        self.assertEqual(events, ["shutdown", "verify"])

    def test_incomplete_close_retains_client_proc_and_engine_client_ref(self) -> None:
        shared_proc = SimpleNamespace(pid=4242)
        holder: dict[str, object] = {}

        class Client(_FakeClient):
            def __init__(self, *args, **kwargs):
                self._proc = shared_proc
                holder["client"] = self

            def shutdown(self) -> bool:
                return False

        class Engine(_FakeEngine):
            def __init__(self, *args, **kwargs):
                holder["engine"] = self

            def shutdown(self) -> bool:
                return False

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clip.wav"
            clip.write_bytes(b"x")
            patches = DiagnoseExecuteFakeTests()._common_patches(root)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patch.object(
                diag, "resolve_diagnostic_clips", return_value=[clip]
            ), patch.object(
                diag, "load_pcm_16k_mono", return_value=(b"\x00\x01" * 8, 0.1)
            ), patch.object(
                diag, "_exclusive_report_path", return_value=root / "tmp" / "report.json"
            ), patch.object(diag, "_write_report") as write_report:
                code = diag.run_execute(
                    clock=lambda: 0.0,
                    perf_counter=lambda: 0.0,
                    client_cls=Client,
                    stt_engine_cls=Engine,
                )
                written = write_report.call_args[0][1]
        client = holder["client"]
        engine = holder["engine"]
        self.assertIsNotNone(client)
        self.assertIsNotNone(engine)
        assert client is not None and engine is not None
        self.assertIs(client._proc, shared_proc)
        self.assertIs(engine._paraformer_client, client)
        self.assertEqual(code, 1)
        self.assertFalse(written.get("complete"))
        self.assertFalse(written.get("shutdown_complete"))


class PcmLoaderTests(unittest.TestCase):
    def test_load_pcm_rejects_over_15s(self) -> None:
        import numpy as np
        import soundfile as sf

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "long.wav"
            sr = 16000
            samples = np.zeros(sr * 20, dtype="float32")
            sf.write(str(path), samples, sr, format="WAV", subtype="PCM_16")
            with self.assertRaises(ValueError) as ctx:
                prep.load_pcm_16k_mono(path)
            self.assertEqual(str(ctx.exception), "wav_too_long")

    def test_load_pcm_accepts_short_clip(self) -> None:
        import numpy as np
        import soundfile as sf

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.wav"
            sr = 16000
            samples = np.zeros(sr, dtype="float32")
            sf.write(str(path), samples, sr, format="WAV", subtype="PCM_16")
            pcm, duration = prep.load_pcm_16k_mono(path)
            self.assertEqual(len(pcm) % 2, 0)
            self.assertLessEqual(duration, prep.MAX_UTTERANCE_SEC)


class DiagnoseImportSafetyTests(unittest.TestCase):
    def test_fresh_import_preserves_process_streams(self) -> None:
        import importlib.util

        real_stdout = sys.stdout
        real_stderr = sys.stderr
        path = _REPO / "diagnose_asr.py"
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            spec = importlib.util.spec_from_file_location(
                "diagnose_asr_import_isolation_nov5",
                path,
            )
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        self.assertIs(sys.stdout, real_stdout)
        self.assertIs(sys.stderr, real_stderr)


if __name__ == "__main__":
    unittest.main()
