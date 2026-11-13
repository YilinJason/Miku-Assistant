"""音频准备事务回滚：fake TFM / infer / Cosy client，无模型与设备。"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

from src.core.safety import SafetyPolicy
from src.voice.audio.player import AudioPlayer
from src.voice.audio.preparation_rollback import request_owned_paths_rollback
from src.voice.audio.temp_files import TempFileManager
from src.voice.cosyvoice.client import CosyVoiceShutdownStatus
from src.voice.rvc.converter import RVCConverter
from src.voice.tts.cosyvoice_backend import CosyVoiceTtsBackend
from src.voice.tts.engine import TTSEngine
from tests.support.host_memory_fixtures import install_module_sufficient_host_memory
from tests.test_cosyvoice_production_slice_a import _policy_with_local_tts
from tests.test_cosyvoice_production_slice_c1 import _FakeCosyClient, _settings
from tests.test_security_round3a_review_fixes import _sealed_policy


_detect_backend_patcher: Any = None


def setUpModule() -> None:
    global _detect_backend_patcher
    install_module_sufficient_host_memory()
    _detect_backend_patcher = patch.object(
        AudioPlayer, "_detect_backend", return_value="none"
    )
    _detect_backend_patcher.start()


def tearDownModule() -> None:
    global _detect_backend_patcher
    if _detect_backend_patcher is not None:
        _detect_backend_patcher.stop()
        _detect_backend_patcher = None


def _assert_tfm_quiet(mgr: TempFileManager, timeout: float = 2.0) -> None:
    status = mgr.shutdown(timeout=timeout)
    if not getattr(status, "complete", False):
        raise AssertionError("TempFileManager shutdown incomplete")
    if mgr.delayed_worker_alive():
        raise AssertionError("TFM delayed worker still alive")
    if mgr.owned_count() != 0:
        raise AssertionError("TFM still owns paths after shutdown")


def _assert_backend_quiet(backend: CosyVoiceTtsBackend, timeout: float = 2.0) -> None:
    status = backend.shutdown_local(timeout=timeout)
    if not getattr(status, "complete", False):
        raise AssertionError("CosyVoice backend shutdown_local incomplete")


class _RecordingRemoveManager(TempFileManager):
    def __init__(self, *args, fail_on: set[str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.remove_calls: list[str] = []
        self._fail_on = fail_on or set()
        self._fail_exc: BaseException | None = None

    def remove(self, file_path: str, max_retries: int = 8, delay: float = 0.5) -> None:
        self.remove_calls.append(file_path)
        if file_path in self._fail_on:
            if self._fail_exc is not None:
                raise self._fail_exc
            raise RuntimeError(f"remove-fail:{file_path}")
        super().remove(file_path, max_retries=max_retries, delay=delay)


def _release_recording_mgr(mgr: _RecordingRemoveManager) -> None:
    mgr._fail_on = set()
    mgr._fail_exc = None
    _assert_tfm_quiet(mgr)


class _PinningSynthClient(_FakeCosyClient):
    """合成时 pin 输出路径并抛 KBI；retire 可模拟 writer 未退出。"""

    def __init__(self, tfm: TempFileManager, state: dict[str, Any]) -> None:
        super().__init__()
        self._tfm = tfm
        self._state = state

    def synthesize(self, text, *, style_id, output_path, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return False
        if self._state.get("raise_kbi"):
            Path(output_path).write_bytes(b"RIFF")
            token = self._tfm.pin_io_paths((output_path,))
            self._state["lease_token"] = token
            self._state["output_path"] = output_path
            exc = self._state.get("kbi")
            if exc is None:
                exc = KeyboardInterrupt("synth-stop")
            raise exc
        self.synth_calls.append((text, style_id, output_path))
        Path(output_path).write_bytes(b"RIFF")
        return True

    def retire_worker(self, timeout: float = 2.0):
        if self._state.get("writer_alive"):
            return CosyVoiceShutdownStatus(
                complete=False,
                worker_alive=True,
                reader_alive=False,
                writer_alive=True,
            )
        return CosyVoiceShutdownStatus(
            complete=True,
            worker_alive=False,
            reader_alive=False,
            writer_alive=False,
        )


def _cosy_case_roots(test: unittest.TestCase) -> tuple[Path, Path, SafetyPolicy]:
    case_root = Path(test.enterContext(TemporaryDirectory()))
    case_tmp = case_root / "case_tmp"
    case_tmp.mkdir()
    policy, _ = _policy_with_local_tts(str(case_root))
    test.enterContext(
        patch("src.voice.tts.cosyvoice_backend.TMP_DIR", case_tmp)
    )
    return case_root, case_tmp, policy


class PreparationRollbackHelperTests(unittest.TestCase):
    def test_continues_all_paths_when_first_remove_fails(self) -> None:
        tmp = self.enterContext(TemporaryDirectory())
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        mgr = _RecordingRemoveManager(
            safety_policy=policy,
            default_dir=Path(tmp),
        )
        self.addCleanup(lambda: _release_recording_mgr(mgr))
        pa = mgr.create(".wav")
        pb = mgr.create(".wav")
        mgr._fail_on = {pa}
        primary = RuntimeError("main")
        request_owned_paths_rollback(mgr, pa, pb, primary=primary)
        self.assertEqual(set(mgr.remove_calls), {pa, pb})

    def test_no_primary_surfaces_cleanup_error(self) -> None:
        tmp = self.enterContext(TemporaryDirectory())
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        mgr = _RecordingRemoveManager(
            safety_policy=policy, default_dir=Path(tmp)
        )
        self.addCleanup(lambda: _release_recording_mgr(mgr))
        path = mgr.create(".wav")
        mgr._fail_on = {path}
        with self.assertRaises(RuntimeError) as ctx:
            request_owned_paths_rollback(mgr, path)
        self.assertIn("remove-fail", str(ctx.exception))


class RvcConvertRollbackTests(unittest.TestCase):
    def _converter(self, root: Path, mgr: TempFileManager) -> RVCConverter:
        model = root / "m.pth"
        model.write_bytes(b"x")
        py = root / "python.exe"
        py.write_bytes(b"")
        policy = _sealed_policy(py, model)
        policy.bind_process_runners(
            run_fn=lambda *a, **k: MagicMock(returncode=0),
            popen_fn=lambda *a, **k: MagicMock(),
        )
        return RVCConverter(
            {
                "model_path": str(model),
                "device": "cpu",
                "python_path": str(py),
                "worker_enabled": False,
            },
            safety_policy=policy,
            temp_file_manager=mgr,
        )

    def test_infer_keyboard_interrupt_preserves_exception_and_output_intent(self) -> None:
        tmp = self.enterContext(TemporaryDirectory())
        root = Path(tmp)
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        mgr = _RecordingRemoveManager(safety_policy=policy, default_dir=root)
        self.addCleanup(lambda: _release_recording_mgr(mgr))
        conv = self._converter(root, mgr)
        inp = mgr.create(".wav")
        Path(inp).write_bytes(b"RIFF")
        kbi = KeyboardInterrupt("infer-stop")

        def _infer(input_audio, output_audio, *args, **kwargs):
            mgr._fail_on = {output_audio}
            raise kbi

        with patch("src.voice.rvc.converter.run_rvc_infer", side_effect=_infer):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                conv.convert_file(inp)
            self.assertIs(ctx.exception, kbi)
        self.assertTrue(mgr.remove_calls)
        self.assertTrue(mgr.is_owned(inp))

    def test_infer_false_returns_none_and_requests_output_cleanup(self) -> None:
        tmp = self.enterContext(TemporaryDirectory())
        root = Path(tmp)
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        mgr = _RecordingRemoveManager(safety_policy=policy, default_dir=root)
        self.addCleanup(lambda: _release_recording_mgr(mgr))
        conv = self._converter(root, mgr)
        inp = mgr.create(".wav")
        with patch("src.voice.rvc.converter.run_rvc_infer", return_value=False):
            self.assertIsNone(conv.convert_file(inp))
        self.assertEqual(len(mgr.remove_calls), 1)

    def test_polish_runtime_error_keeps_valid_output(self) -> None:
        tmp = self.enterContext(TemporaryDirectory())
        root = Path(tmp)
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        mgr = TempFileManager(safety_policy=policy, default_dir=root)
        self.addCleanup(lambda: _assert_tfm_quiet(mgr))
        conv = self._converter(root, mgr)
        inp = mgr.create(".wav")
        Path(inp).write_bytes(b"RIFF")
        with patch(
            "src.voice.rvc.converter.run_rvc_infer", return_value=True
        ), patch(
            "src.voice.rvc.converter.polish_rvc_wav",
            side_effect=RuntimeError("polish-soft"),
        ):
            out = conv.convert_file(inp)
        self.assertIsNotNone(out)
        self.assertTrue(mgr.is_owned(out))

    def test_polish_keyboard_interrupt_cleanup_fail_preserves_primary(self) -> None:
        tmp = self.enterContext(TemporaryDirectory())
        root = Path(tmp)
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        mgr = _RecordingRemoveManager(safety_policy=policy, default_dir=root)
        self.addCleanup(lambda: _release_recording_mgr(mgr))
        conv = self._converter(root, mgr)
        inp = mgr.create(".wav")
        Path(inp).write_bytes(b"RIFF")
        kbi = KeyboardInterrupt("polish-stop")
        cleanup_err = RuntimeError("cleanup-during-polish")

        def _polish(path, _cfg):
            mgr._fail_on = {path}
            mgr._fail_exc = cleanup_err
            raise kbi

        with patch(
            "src.voice.rvc.converter.run_rvc_infer", return_value=True
        ), patch("src.voice.rvc.converter.polish_rvc_wav", side_effect=_polish):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                conv.convert_file(inp)
            self.assertIs(ctx.exception, kbi)
        self.assertEqual(len(mgr.remove_calls), 1)

    def test_infer_cleanup_runtimeerror_does_not_replace_infer_error(self) -> None:
        tmp = self.enterContext(TemporaryDirectory())
        root = Path(tmp)
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        mgr = _RecordingRemoveManager(safety_policy=policy, default_dir=root)
        self.addCleanup(lambda: _release_recording_mgr(mgr))
        conv = self._converter(root, mgr)
        inp = mgr.create(".wav")
        main = RuntimeError("infer-main")
        cleanup_err = RuntimeError("cleanup-during-infer")

        def _infer(input_audio, output_audio, *args, **kwargs):
            mgr._fail_on = {output_audio}
            mgr._fail_exc = cleanup_err
            raise main

        with patch("src.voice.rvc.converter.run_rvc_infer", side_effect=_infer):
            self.assertIsNone(conv.convert_file(inp))
        self.assertTrue(mgr.remove_calls)


class EdgePrepareRollbackTests(unittest.TestCase):
    def _engine(self, root: Path, mgr: TempFileManager) -> TTSEngine:
        policy = SafetyPolicy(
            {
                "permissions": {
                    "cloud_tts": True,
                    "file_delete": True,
                    "local_tts_worker": True,
                }
            }
        )
        engine = TTSEngine(
            voice="zh-CN-XiaoxiaoNeural",
            safety_policy=policy,
            temp_file_manager=mgr,
            warm_rvc_on_init=False,
        )
        self.addCleanup(lambda: _assert_engine_quiet(engine))
        return engine

    def test_edge_save_keyboard_interrupt_cleans_mp3(self) -> None:
        tmp = self.enterContext(TemporaryDirectory())
        root = Path(tmp)
        mgr = _RecordingRemoveManager(
            safety_policy=SafetyPolicy(
                {"permissions": {"cloud_tts": True, "file_delete": True}}
            ),
            default_dir=root,
        )
        self.addCleanup(lambda: _release_recording_mgr(mgr))
        engine = self._engine(root, mgr)
        kbi = KeyboardInterrupt("edge-stop")
        cleanup_err = RuntimeError("cleanup-edge")

        async def boom(path):
            mgr._fail_on = {path}
            mgr._fail_exc = cleanup_err
            raise kbi

        with patch("edge_tts.Communicate") as comm:
            comm.return_value = MagicMock(save=boom)
            with self.assertRaises(KeyboardInterrupt) as ctx:
                asyncio.run(engine._edge_tts_to_mp3("hi"))
            self.assertIs(ctx.exception, kbi)
        self.assertEqual(len(mgr.remove_calls), 1)

    def test_mp3_to_wav_exit_preserves_input_and_cleans_wav(self) -> None:
        tmp = self.enterContext(TemporaryDirectory())
        root = Path(tmp)
        mgr = _RecordingRemoveManager(
            safety_policy=SafetyPolicy({"permissions": {"file_delete": True}}),
            default_dir=root,
        )
        self.addCleanup(lambda: _release_recording_mgr(mgr))
        engine = self._engine(root, mgr)
        mp3 = mgr.create(".mp3")
        exit_exc = SystemExit(3)
        cleanup_err = RuntimeError("cleanup-wav")

        def boom(_mp3, wav, _cfg):
            mgr._fail_on = {wav}
            mgr._fail_exc = cleanup_err
            raise exit_exc

        with patch("src.voice.tts.engine.mp3_to_rvc_wav", side_effect=boom):
            with self.assertRaises(SystemExit) as ctx:
                engine._mp3_to_wav(mp3, {})
            self.assertIs(ctx.exception, exit_exc)
        self.assertTrue(mgr.is_owned(mp3))
        self.assertEqual(len(mgr.remove_calls), 1)

    def test_prepare_playable_cleanup_continues_after_first_remove_error(self) -> None:
        tmp = self.enterContext(TemporaryDirectory())
        root = Path(tmp)
        mgr = _RecordingRemoveManager(
            safety_policy=SafetyPolicy(
                {"permissions": {"cloud_tts": True, "file_delete": True}}
            ),
            default_dir=root,
        )
        self.addCleanup(lambda: _release_recording_mgr(mgr))
        engine = self._engine(root, mgr)
        mp3 = mgr.create(".mp3")
        wav = mgr.create(".wav")
        main = RuntimeError("prepare-main")
        engine._rvc = MagicMock()
        engine._rvc.enabled = True
        engine._rvc_ready = True
        engine._rvc.convert_file = MagicMock(side_effect=main)

        with patch.object(
            engine, "_edge_tts_to_mp3", return_value=mp3
        ), patch.object(engine, "_mp3_to_wav", return_value=wav):
            mgr._fail_on = {mp3}
            with self.assertRaises(RuntimeError) as ctx:
                engine._prepare_playable_audio("x")
            self.assertIs(ctx.exception, main)
        self.assertIn(mp3, mgr.remove_calls)
        self.assertIn(wav, mgr.remove_calls)


def _assert_engine_quiet(engine: TTSEngine) -> None:
    engine.shutdown(stop_rvc=False)
    temp_files = getattr(engine, "_temp_files", None)
    owns = getattr(engine, "_owns_temp_files", True)
    if owns and temp_files is not None and not getattr(
        engine, "_temp_files_shutdown_done", False
    ):
        status = temp_files.shutdown(timeout=2.0)
        if not getattr(status, "complete", False):
            raise AssertionError("TTSEngine temp_file_manager shutdown incomplete")


class CosyPrepareRollbackTests(unittest.TestCase):
    def test_synth_keyboard_interrupt_pin_blocks_until_quiet(self) -> None:
        case_root, case_tmp, policy = _cosy_case_roots(self)
        tfm = TempFileManager(safety_policy=policy, default_dir=case_tmp)
        kbi = KeyboardInterrupt("synth-stop")
        state: dict[str, Any] = {
            "raise_kbi": True,
            "writer_alive": True,
            "kbi": kbi,
        }
        fake = _PinningSynthClient(tfm, state)
        backend = CosyVoiceTtsBackend(
            _settings(), policy, tfm, client=fake  # type: ignore[arg-type]
        )

        def _full_teardown() -> None:
            state["writer_alive"] = False
            token = state.get("lease_token")
            if token:
                tfm.release_io_lease(str(token))
            _assert_backend_quiet(backend)
            _assert_tfm_quiet(tfm)
            out = state.get("output_path")
            if out:
                self.assertFalse(Path(out).exists())

        self.addCleanup(_full_teardown)

        with self.assertRaises(KeyboardInterrupt) as ctx:
            backend.synthesize_owned_wav("x")
        self.assertIs(ctx.exception, kbi)
        self.assertEqual(backend.retained_output_count(), 1)
        token = state["lease_token"]
        out_path = state["output_path"]
        self.assertTrue(tfm.io_lease_active(token))
        self.assertTrue(tfm.is_owned(out_path))
        self.assertIsNone(backend.synthesize_owned_wav("blocked"))
        self.assertEqual(len(fake.synth_calls), 0)

    def test_prepare_playable_rvc_keyboard_interrupt_tracks_deferred(self) -> None:
        case_root, case_tmp, policy = _cosy_case_roots(self)
        mgr = _RecordingRemoveManager(safety_policy=policy, default_dir=case_tmp)
        self.addCleanup(lambda: _release_recording_mgr(mgr))
        fake = _FakeCosyClient()
        backend = CosyVoiceTtsBackend(
            _settings(), policy, mgr, client=fake  # type: ignore[arg-type]
        )
        self.addCleanup(lambda: _assert_backend_quiet(backend))
        rvc_root = case_root / "rvc_tmp"
        rvc_root.mkdir(exist_ok=True)
        model = case_root / "m.pth"
        model.write_bytes(b"x")
        py = case_root / "python.exe"
        py.write_bytes(b"")
        rvc_policy = _sealed_policy(py, model)
        rvc_policy.bind_process_runners(
            run_fn=lambda *a, **k: MagicMock(returncode=0),
            popen_fn=lambda *a, **k: MagicMock(),
        )
        rvc_mgr = TempFileManager(safety_policy=rvc_policy, default_dir=rvc_root)
        self.addCleanup(lambda: _assert_tfm_quiet(rvc_mgr))
        rvc = RVCConverter(
            {
                "model_path": str(model),
                "device": "cpu",
                "python_path": str(py),
                "worker_enabled": False,
            },
            safety_policy=rvc_policy,
            temp_file_manager=rvc_mgr,
        )
        kbi = KeyboardInterrupt("rvc-stop")
        with patch.object(rvc, "convert_file", side_effect=kbi):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                backend.prepare_playable(
                    "seg",
                    rvc=rvc,
                    rvc_ready=True,
                    cancel_event=None,
                    interrupted=lambda: False,
                )
            self.assertIs(ctx.exception, kbi)
        self.assertTrue(mgr.remove_calls)
        self.assertTrue(all(p.parent == case_tmp for p in map(Path, mgr.remove_calls)))
