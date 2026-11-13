"""CosyVoice 生产切片 C1：TTSEngine 可选本地后端（纯离线 fake）。"""
from __future__ import annotations

import builtins
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from src.core.paths import TMP_DIR
from src.core.safety import SafetyPolicy
from src.voice.audio.player import AudioPlayer
from src.voice.audio.temp_files import TempFileManager
from src.voice.cosyvoice.client import CosyVoiceShutdownStatus, CosyVoiceWorkerClient
from src.voice.cosyvoice.settings import parse_cosyvoice_settings
from src.voice.tts.cosyvoice_backend import (
    COSYVOICE_STYLE_ID,
    CosyVoiceSpeechError,
    CosyVoiceSpeechLimitError,
    CosyVoiceTtsBackend,
    MAX_SPEECH_SEGMENTS,
    MAX_TOTAL_SPEECH_CHARS,
    plan_cosyvoice_segments,
    resolve_cosyvoice_settings_from_engine_config,
)
from src.voice.tts.engine import TTSEngine
from tests.support.host_memory_fixtures import install_module_sufficient_host_memory
from tests.test_cosyvoice_production_slice_a import _policy_with_local_tts


def setUpModule() -> None:
    install_module_sufficient_host_memory()


class _FakeAudioPlayer:
    init_calls = 0

    def __init__(self, *args, **kwargs) -> None:
        _FakeAudioPlayer.init_calls += 1

    def play(self, path: str) -> None:
        pass

    def stop(self) -> None:
        pass

    def stop_current(self) -> None:
        pass


def _settings(**overrides):
    base = parse_cosyvoice_settings({"engine": "cosyvoice"})
    assert base is not None
    from src.voice.cosyvoice.settings import CosyVoiceSettings

    data = {**base.__dict__, **overrides}
    return CosyVoiceSettings(**data)


class _FakeCosyClient:
    def __init__(self) -> None:
        self.synth_calls: list[tuple[str, str, str]] = []
        self.interrupts = 0
        self.shutdown_calls = 0
        self._epoch = 0
        self._stop_failed = False

    @property
    def stop_failed(self) -> bool:
        return self._stop_failed

    def synthesize(self, text, *, style_id, output_path, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return False
        self.synth_calls.append((text, style_id, output_path))
        Path(output_path).write_bytes(b"RIFF")
        return True

    def interrupt(self):
        self.interrupts += 1
        self._epoch += 1
        return False

    def shutdown(self, timeout: float = 2.0):
        self.shutdown_calls += 1
        return CosyVoiceShutdownStatus(
            complete=True,
            worker_alive=False,
            reader_alive=False,
            writer_alive=False,
        )

    def continue_shutdown(self, timeout: float = 2.0):
        return self.shutdown(timeout=timeout)

    def retire_worker(self, timeout: float = 2.0):
        return CosyVoiceShutdownStatus(
            complete=True,
            worker_alive=False,
            reader_alive=False,
            writer_alive=False,
        )


def _make_engine(
    tmp: str,
    *,
    cloud_tts: bool = False,
    rvc_model: bool = True,
    client: _FakeCosyClient | None = None,
    cosyvoice_config: dict | None = None,
    perm_overrides: dict | None = None,
    engine: str = "cosyvoice",
) -> tuple[TTSEngine, _FakeCosyClient | None, SafetyPolicy]:
    perms = {
        "local_tts_worker": True,
        "file_delete": True,
        "cloud_tts": cloud_tts,
    }
    perms.update(perm_overrides or {})
    policy, _root = _policy_with_local_tts(tmp, **perms)
    fake = client or _FakeCosyClient()
    rvc_config = (
        {"model_path": str(Path(tmp) / "model.pth")}
        if rvc_model
        else {}
    )
    patches = [
        patch(
            "src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}
        ),
        patch("src.voice.tts.engine.AudioPlayer", _FakeAudioPlayer),
        patch.object(TTSEngine, "prepare_rvc", return_value=False),
    ]
    if engine == "cosyvoice":
        patches.append(
            patch(
                "src.voice.tts.cosyvoice_backend.CosyVoiceWorkerClient",
                return_value=fake,
            )
        )
    for p in patches:
        p.start()
    try:
        engine_obj = TTSEngine(
            engine=engine,
            safety_policy=policy,
            rvc_config=rvc_config,
            warm_rvc_on_init=False,
            cosyvoice_config=cosyvoice_config,
        )
        if engine == "cosyvoice":
            assert engine_obj._cosy_backend is not None
            engine_obj._cosy_backend._client = fake  # type: ignore[assignment]
            engine_obj._rvc_ready = True
    finally:
        for p in reversed(patches):
            p.stop()
    return engine_obj, fake if engine == "cosyvoice" else None, policy


class CosyVoiceSegmentPlannerTests(unittest.TestCase):
    def test_hard_split_unpunctuated(self) -> None:
        settings = _settings(max_text_chars=240)
        text = "a" * 250
        segments = plan_cosyvoice_segments(text, settings)
        self.assertEqual(len(segments), 3)
        self.assertEqual("".join(segments), text)

    def test_rejects_over_3200_chars(self) -> None:
        settings = _settings()
        text = "x" * (MAX_TOTAL_SPEECH_CHARS + 1)
        with self.assertRaises(CosyVoiceSpeechLimitError):
            plan_cosyvoice_segments(text, settings)

    def test_rejects_over_32_segments(self) -> None:
        settings = _settings(max_text_chars=10)
        text = "。".join(["一二三四五六七八九十"] * 40)
        with self.assertRaises(CosyVoiceSpeechLimitError):
            plan_cosyvoice_segments(text, settings)

    def test_tiny_max_chars_per_segment(self) -> None:
        settings = _settings(max_text_chars=5)
        segments = plan_cosyvoice_segments("一二三四五六七八", settings)
        self.assertTrue(all(len(s) <= 5 for s in segments))
        self.assertEqual("".join(segments), "一二三四五六七八")


class CosyVoiceEngineInitTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeAudioPlayer.init_calls = 0

    def test_bad_config_before_player(self) -> None:
        with patch("src.voice.tts.engine.AudioPlayer", _FakeAudioPlayer):
            with self.assertRaises(ValueError):
                TTSEngine(
                    engine="cosyvoice",
                    cosyvoice_config={"cpu_threads": True},
                    warm_rvc_on_init=False,
                )
        self.assertEqual(_FakeAudioPlayer.init_calls, 0)

    def test_bad_config_zero_cosy_client(self) -> None:
        with patch(
            "src.voice.tts.cosyvoice_backend.CosyVoiceWorkerClient"
        ) as client_cls, patch(
            "src.voice.tts.engine.AudioPlayer", _FakeAudioPlayer
        ), patch.object(TTSEngine, "prepare_rvc", return_value=False):
            with self.assertRaises(ValueError):
                TTSEngine(
                    engine="cosyvoice",
                    cosyvoice_config={"startup_timeout_sec": 61},
                    warm_rvc_on_init=False,
                )
            client_cls.assert_not_called()
        self.assertEqual(_FakeAudioPlayer.init_calls, 0)

    def test_rejects_non_dict_cosyvoice_config(self) -> None:
        with self.assertRaises(ValueError):
            resolve_cosyvoice_settings_from_engine_config([])  # type: ignore[arg-type]

    def test_rejects_wrapper_config(self) -> None:
        with self.assertRaises(ValueError):
            resolve_cosyvoice_settings_from_engine_config(
                {"engine": "cosyvoice", "cosyvoice": {}}
            )

    def test_construct_does_not_import_edge_or_torch(self) -> None:
        real_import = builtins.__import__

        def _blocking_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "torch" or name.startswith("torch."):
                raise ImportError("blocked torch for test")
            return real_import(name, globals, locals, fromlist, level)

        with TemporaryDirectory() as tmp:
            policy, root = _policy_with_local_tts(tmp)
            with patch(
                "src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}
            ), patch(
                "src.voice.tts.cosyvoice_backend.CosyVoiceWorkerClient"
            ) as client_cls, patch(
                "src.voice.tts.engine.AudioPlayer", _FakeAudioPlayer
            ), patch.object(
                TTSEngine, "prepare_rvc", return_value=False
            ) as prep, patch(
                "builtins.__import__", side_effect=_blocking_import
            ):
                engine = TTSEngine(
                    engine="cosyvoice",
                    safety_policy=policy,
                    warm_rvc_on_init=False,
                )
                self.addCleanup(engine.shutdown, stop_rvc=False)
                prep.assert_not_called()
                client_cls.assert_called_once()
                self.assertEqual(engine.engine_type, "cosyvoice")

    def test_edge_engine_does_not_create_cosy_client(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            with patch(
                "src.voice.tts.cosyvoice_backend.CosyVoiceWorkerClient"
            ) as client_cls, patch(
                "src.voice.tts.engine.AudioPlayer", _FakeAudioPlayer
            ), patch.object(TTSEngine, "prepare_rvc", return_value=False):
                engine = TTSEngine(
                    engine="edge-tts",
                    safety_policy=policy,
                    warm_rvc_on_init=False,
                )
                client_cls.assert_not_called()
                self.assertIsNone(engine._cosy_backend)
                self.addCleanup(engine.shutdown, stop_rvc=False)


class CosyVoiceSpeakTests(unittest.TestCase):
    def test_cloud_false_still_speaks(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp, cloud_tts=False)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            plays: list[str] = []
            engine._player.play = lambda p: plays.append(p)  # type: ignore[method-assign]
            engine.speak("你好，今天天气不错。")
            assert fake is not None
            self.assertEqual(len(fake.synth_calls), 1)
            self.assertEqual(fake.synth_calls[0][1], COSYVOICE_STYLE_ID)
            self.assertEqual(len(plays), 1)

    def test_wav_then_rvc_order(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            order: list[str] = []

            def _synth(text, *, style_id, output_path, cancel_event=None):
                order.append("synth")
                Path(output_path).write_bytes(b"RIFF")
                return True

            assert fake is not None
            fake.synthesize = _synth  # type: ignore[method-assign]
            engine._rvc.convert_file = lambda p: (order.append("rvc") or f"{p}.rvc")  # type: ignore[method-assign]
            engine._player.play = lambda p: order.append(f"play:{p}")  # type: ignore[method-assign]
            engine.speak("测试一句。")
            self.assertEqual(order[0], "synth")
            self.assertEqual(order[1], "rvc")
            self.assertTrue(order[2].startswith("play:"))

    def test_rvc_failure_uses_base_wav(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            played: list[str] = []
            engine._rvc.convert_file = lambda _p: None  # type: ignore[method-assign]
            engine._player.play = lambda p: played.append(p)  # type: ignore[method-assign]
            engine.speak("回退测试。")
            self.assertEqual(len(played), 1)
            self.assertTrue(played[0].endswith(".wav"))

    def test_failure_no_pyttsx3_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            assert fake is not None
            fake.synthesize = lambda *a, **k: False  # type: ignore[method-assign]
            with patch.object(engine, "_speak_pyttsx3") as pyttsx3:
                engine.speak("应失败")
                pyttsx3.assert_not_called()

    def test_first_segment_failure_aborts_without_retrying_rest(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            assert fake is not None
            calls = {"n": 0}

            def _synth(text, *, style_id, output_path, cancel_event=None):
                calls["n"] += 1
                return False

            fake.synthesize = _synth  # type: ignore[method-assign]
            long_text = "第一句失败。第二句不应再试。第三句也不应。"
            engine.speak(long_text)
            self.assertEqual(calls["n"], 1)

    def test_interrupt_skips_late_play(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            plays: list[str] = []

            def _synth(text, *, style_id, output_path, cancel_event=None):
                engine.interrupt()
                Path(output_path).write_bytes(b"RIFF")
                return True

            assert fake is not None
            fake.synthesize = _synth  # type: ignore[method-assign]
            engine._player.play = lambda p: plays.append(p)  # type: ignore[method-assign]
            engine.speak("第一句。第二句。")
            self.assertEqual(plays, [])

    def test_owned_files_bounded_after_segments(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, root = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy, default_dir=TMP_DIR)
            self.addCleanup(tfm.shutdown)
            fake = _FakeCosyClient()
            backend = CosyVoiceTtsBackend(
                _settings(max_text_chars=20),
                policy,
                tfm,
                client=fake,  # type: ignore[arg-type]
            )
            plays = 0

            def _play(_p: str) -> None:
                nonlocal plays
                plays += 1

            backend.speak_sequential(
                "第一句比较长一些。第二句也读一下。",
                play_fn=_play,
                rvc=None,
                rvc_ready=False,
                cancel_event=None,
                interrupted=lambda: False,
            )
            self.assertGreaterEqual(plays, 2)
            with tfm._lock:
                self.assertEqual(len(tfm._owned), 0)

    def test_repeated_synth_failure_bounded_owner_high_water(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy, default_dir=TMP_DIR)
            self.addCleanup(tfm.shutdown)
            fake = _FakeCosyClient()
            fake.synthesize = lambda *a, **k: False  # type: ignore[method-assign]
            backend = CosyVoiceTtsBackend(
                _settings(),
                policy,
                tfm,
                client=fake,  # type: ignore[arg-type]
            )
            for _ in range(10):
                backend.synthesize_owned_wav("失败")
            with tfm._lock:
                self.assertEqual(len(tfm._owned), 1)
            self.assertEqual(backend.retained_output_count(), 1)

    def test_repeated_synth_failure_100_calls_one_owner(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy, default_dir=TMP_DIR)
            self.addCleanup(tfm.shutdown)
            fake = _FakeCosyClient()
            fake.synthesize = lambda *a, **k: False  # type: ignore[method-assign]
            backend = CosyVoiceTtsBackend(
                _settings(),
                policy,
                tfm,
                client=fake,  # type: ignore[arg-type]
            )
            for _ in range(100):
                backend.synthesize_owned_wav("x")
            with tfm._lock:
                self.assertEqual(len(tfm._owned), 1)
            self.assertEqual(backend.retained_output_count(), 1)

    def test_delayed_remove_keeps_single_tfm_owner_high_water(self) -> None:
        class _LockedTempFileManager(TempFileManager):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.removed_paths: list[str] = []
                self._simulate_os_lock = True

            def remove(self, file_path: str, max_retries: int = 8, delay: float = 0.5) -> None:
                self.removed_paths.append(file_path)
                if self._simulate_os_lock:
                    return
                super().remove(file_path, max_retries=max_retries, delay=delay)

        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = _LockedTempFileManager(safety_policy=policy, default_dir=TMP_DIR)

            def _cleanup_locked_tfm() -> None:
                tfm._simulate_os_lock = False
                tfm.shutdown()

            self.addCleanup(_cleanup_locked_tfm)

            class _RecoveringClient(_FakeCosyClient):
                def retire_worker(self, timeout: float = 2.0):
                    return CosyVoiceShutdownStatus(
                        complete=True,
                        worker_alive=False,
                        reader_alive=False,
                        writer_alive=False,
                    )

            fake = _RecoveringClient()
            fake.synthesize = lambda *a, **k: False  # type: ignore[method-assign]
            backend = CosyVoiceTtsBackend(
                _settings(),
                policy,
                tfm,
                client=fake,  # type: ignore[arg-type]
            )
            for _ in range(100):
                backend.synthesize_owned_wav("占用删除")
            with tfm._lock:
                self.assertEqual(len(tfm._owned), 1)
            self.assertEqual(backend.retained_output_count(), 1)

    def test_recovery_after_retire_allows_new_synth(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy, default_dir=TMP_DIR)
            self.addCleanup(tfm.shutdown)
            fake = _FakeCosyClient()
            state = {"fail_once": True}

            def _synth(text, *, style_id, output_path, cancel_event=None):
                if state["fail_once"]:
                    state["fail_once"] = False
                    return False
                Path(output_path).write_bytes(b"RIFF")
                return True

            fake.synthesize = _synth  # type: ignore[method-assign]
            backend = CosyVoiceTtsBackend(
                _settings(),
                policy,
                tfm,
                client=fake,  # type: ignore[arg-type]
            )
            self.assertIsNone(backend.synthesize_owned_wav("第一次"))
            path = backend.synthesize_owned_wav("第二次")
            self.assertIsNotNone(path)
            self.assertEqual(backend.retained_output_count(), 0)

    def test_stop_failed_blocks_new_output_creation(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy, default_dir=TMP_DIR)
            self.addCleanup(tfm.shutdown)
            fake = _FakeCosyClient()
            fake._stop_failed = True
            backend = CosyVoiceTtsBackend(
                _settings(),
                policy,
                tfm,
                client=fake,  # type: ignore[arg-type]
            )
            with tfm._lock:
                before = len(tfm._owned)
            self.assertIsNone(backend.synthesize_owned_wav("应拒绝"))
            with tfm._lock:
                self.assertEqual(len(tfm._owned), before)

    def test_synth_failure_retains_output_path(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy, default_dir=TMP_DIR)
            self.addCleanup(tfm.shutdown)
            fake = _FakeCosyClient()
            fake.synthesize = lambda *a, **k: False  # type: ignore[method-assign]
            backend = CosyVoiceTtsBackend(
                _settings(),
                policy,
                tfm,
                client=fake,  # type: ignore[arg-type]
            )
            path = backend.synthesize_owned_wav("失败保留")
            self.assertIsNone(path)
            self.assertEqual(backend.retained_output_count(), 1)
            with tfm._lock:
                self.assertEqual(len(tfm._owned), 1)

    def test_incomplete_retire_blocks_remove_and_second_create(self) -> None:
        class _CountingTempFileManager(TempFileManager):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.remove_calls = 0
                self.create_calls = 0

            def create(self, *args, **kwargs):
                self.create_calls += 1
                return super().create(*args, **kwargs)

            def remove(self, file_path: str, max_retries: int = 8, delay: float = 0.5) -> None:
                self.remove_calls += 1
                super().remove(file_path, max_retries=max_retries, delay=delay)

        class _UnretiredClient(_FakeCosyClient):
            def synthesize(self, text, *, style_id, output_path, cancel_event=None):
                return False

            def retire_worker(self, timeout: float = 2.0):
                return CosyVoiceShutdownStatus(
                    complete=False,
                    worker_alive=True,
                    reader_alive=False,
                    writer_alive=False,
                )

        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = _CountingTempFileManager(safety_policy=policy, default_dir=TMP_DIR)
            self.addCleanup(tfm.shutdown)
            backend = CosyVoiceTtsBackend(
                _settings(),
                policy,
                tfm,
                client=_UnretiredClient(),  # type: ignore[arg-type]
            )
            self.assertIsNone(backend.synthesize_owned_wav("第一次"))
            self.assertIsNone(backend.synthesize_owned_wav("第二次"))
            self.assertEqual(tfm.create_calls, 1)
            self.assertEqual(tfm.remove_calls, 0)


class CosyVoiceBackendShutdownTests(unittest.TestCase):
    def test_shutdown_signals_client_before_waiting_inflight(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy, default_dir=TMP_DIR)
            self.addCleanup(tfm.shutdown)
            started = threading.Event()
            released = threading.Event()
            stopped_at: list[float] = []

            class _BlockingClient(_FakeCosyClient):
                def synthesize(self, text, *, style_id, output_path, cancel_event=None):
                    started.set()
                    released.wait(1.0)
                    return False

                def shutdown(self, timeout: float = 2.0):
                    stopped_at.append(time.monotonic())
                    released.set()
                    return super().shutdown(timeout=timeout)

            backend = CosyVoiceTtsBackend(
                _settings(),
                policy,
                tfm,
                client=_BlockingClient(),  # type: ignore[arg-type]
            )

            def _work() -> None:
                try:
                    backend.speak_sequential(
                        "正在合成",
                        play_fn=lambda _p: None,
                        rvc=None,
                        rvc_ready=False,
                        cancel_event=None,
                        interrupted=lambda: False,
                    )
                except CosyVoiceSpeechError:
                    pass

            worker = threading.Thread(target=_work, daemon=True)
            worker.start()
            self.assertTrue(started.wait(1.0))
            t0 = time.monotonic()
            status = backend.shutdown_local(0.5)
            worker.join(1.0)
            self.assertTrue(status.complete)
            self.assertTrue(stopped_at)
            delay_ms = (stopped_at[0] - t0) * 1000
            self.assertLess(delay_ms, 250.0)

    def test_shutdown_budget_not_spent_on_client_interrupt(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy, default_dir=TMP_DIR)
            self.addCleanup(tfm.shutdown)

            class _SlowInterruptClient(_FakeCosyClient):
                def interrupt(self):
                    time.sleep(0.25)
                    return super().interrupt()

            backend = CosyVoiceTtsBackend(
                _settings(),
                policy,
                tfm,
                client=_SlowInterruptClient(),  # type: ignore[arg-type]
            )
            t0 = time.monotonic()
            backend.shutdown_local(0.1)
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.assertLess(elapsed_ms, 200.0)


class CosyVoiceShutdownTests(unittest.TestCase):
    def test_incomplete_shutdown_retains_client_and_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            assert fake is not None

            def _shutdown(timeout: float = 2.0):
                fake.shutdown_calls += 1
                return CosyVoiceShutdownStatus(
                    complete=False,
                    worker_alive=True,
                    reader_alive=False,
                    writer_alive=False,
                )

            fake.shutdown = _shutdown  # type: ignore[method-assign]
            engine.shutdown(stop_rvc=False)
            self.assertIsNotNone(engine._cosy_backend)
            status = engine.local_cosyvoice_shutdown_status()
            assert status is not None
            self.assertFalse(status.complete)
            self.assertFalse(engine._temp_files_shutdown_done)

            def _shutdown_complete(timeout: float = 2.0):
                fake.shutdown_calls += 1
                return CosyVoiceShutdownStatus(
                    complete=True,
                    worker_alive=False,
                    reader_alive=False,
                    writer_alive=False,
                )

            fake.shutdown = _shutdown_complete  # type: ignore[method-assign]
            engine.shutdown(stop_rvc=False)
            status2 = engine.local_cosyvoice_shutdown_status()
            assert status2 is not None
            self.assertTrue(status2.complete)
            self.assertEqual(fake.shutdown_calls, 4)

    def test_shutdown_closes_client_before_tfm(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            order: list[str] = []

            def _shutdown(timeout: float = 2.0):
                order.append("client_shutdown")
                return CosyVoiceShutdownStatus(
                    complete=True,
                    worker_alive=False,
                    reader_alive=False,
                    writer_alive=False,
                )

            assert fake is not None
            fake.shutdown = _shutdown  # type: ignore[method-assign]
            orig = engine._temp_files.shutdown

            def _tfm_shutdown():
                order.append("tfm_shutdown")
                return orig()

            engine._temp_files.shutdown = _tfm_shutdown  # type: ignore[method-assign]
            engine.shutdown(stop_rvc=False)
            self.assertEqual(order[0], "client_shutdown")
            self.assertIn("tfm_shutdown", order)

    def test_shutdown_waits_for_inflight_play(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            gate = threading.Event()
            release = threading.Event()

            def _play(_path: str) -> None:
                gate.set()
                release.wait(timeout=2.0)

            engine._player.play = _play  # type: ignore[method-assign]
            worker = threading.Thread(
                target=engine.speak, args=("播放中。",), daemon=True
            )
            worker.start()
            self.assertTrue(gate.wait(timeout=2.0))
            shutdown_done = threading.Event()

            def _shutdown():
                engine.shutdown(stop_rvc=False)
                shutdown_done.set()

            closer = threading.Thread(target=_shutdown, daemon=True)
            closer.start()
            time.sleep(0.05)
            self.assertFalse(shutdown_done.is_set())
            release.set()
            worker.join(timeout=2.0)
            shutdown_done.wait(timeout=2.0)
            self.assertTrue(shutdown_done.is_set())


class CosyVoiceSaveTests(unittest.TestCase):
    def test_save_after_interrupt_clears_stale_cancel(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            engine.interrupt()
            target = engine._temp_files.create(".wav", directory=TMP_DIR)
            cancel_seen: list[bool] = []

            def _save(
                text, *, style_id, output_path, cancel_event=None
            ):
                cancel_seen.append(
                    cancel_event is not None and cancel_event.is_set()
                )
                Path(output_path).write_bytes(b"RIFF")
                return True

            assert fake is not None
            fake.synthesize = _save  # type: ignore[method-assign]
            engine.save_to_file("新一轮保存", target)
            self.assertEqual(cancel_seen, [False])

    def test_queued_save_cancelled_by_interrupt_does_not_dispatch(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            gate = threading.Event()
            release = threading.Event()
            save_synth = threading.Event()

            def _speak_block(_text: str) -> None:
                gate.set()
                release.wait(2.0)

            def _synth(text, *, style_id, output_path, cancel_event=None):
                if text.startswith("排队"):
                    save_synth.set()
                Path(output_path).write_bytes(b"RIFF")
                return True

            assert fake is not None
            fake.synthesize = _synth  # type: ignore[method-assign]
            engine._player.play = _speak_block  # type: ignore[method-assign]
            speaker = threading.Thread(
                target=engine.speak, args=("长播报占用锁。",), daemon=True
            )
            speaker.start()
            self.assertTrue(gate.wait(2.0))
            target = engine._temp_files.create(".wav", directory=TMP_DIR)
            save_errors: list[BaseException] = []

            def _save() -> None:
                try:
                    engine.save_to_file("排队保存", target)
                except BaseException as exc:
                    save_errors.append(exc)

            saver = threading.Thread(target=_save, daemon=True)
            saver.start()
            try:
                time.sleep(0.05)
                engine.interrupt()
            finally:
                release.set()
                speaker.join(2.0)
                saver.join(2.0)
            self.assertFalse(save_synth.is_set())
            self.assertEqual(len(save_errors), 1)
            self.assertIsInstance(save_errors[0], RuntimeError)
            self.assertIn("已取消", str(save_errors[0]))

    def test_save_and_speak_mutex(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            order: list[str] = []
            gate = threading.Event()
            release = threading.Event()

            def _play(_path: str) -> None:
                order.append("speak")
                gate.set()
                release.wait(2.0)

            def _synth(text, *, style_id, output_path, cancel_event=None):
                order.append(f"synth:{text[:2]}")
                Path(output_path).write_bytes(b"RIFF")
                return True

            assert fake is not None
            fake.synthesize = _synth  # type: ignore[method-assign]
            engine._player.play = _play  # type: ignore[method-assign]
            speaker = threading.Thread(
                target=engine.speak, args=("播报。",), daemon=True
            )
            speaker.start()
            self.assertTrue(gate.wait(2.0))
            target = engine._temp_files.create(".wav", directory=TMP_DIR)
            save_done = threading.Event()
            errors: list[BaseException] = []

            def _save() -> None:
                try:
                    engine.save_to_file("保存", target)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    save_done.set()

            saver = threading.Thread(target=_save, daemon=True)
            saver.start()
            time.sleep(0.05)
            self.assertFalse(save_done.is_set())
            release.set()
            speaker.join(2.0)
            saver.join(2.0)
            self.assertTrue(save_done.is_set())
            self.assertEqual(errors, [])
            self.assertIn("speak", order)
            self.assertIn("synth:保存", order)
            self.assertLess(order.index("speak"), order.index("synth:保存"))

    def test_save_unowned_target_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, policy = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            outsider = Path(tmp) / "outside.wav"
            outsider.write_bytes(b"ORIG")
            with self.assertRaises((PermissionError, ValueError, RuntimeError)):
                engine.save_to_file("保存测试", str(outsider))
            self.assertEqual(outsider.read_bytes(), b"ORIG")
            assert fake is not None
            fake.synthesize = MagicMock()  # type: ignore[method-assign]

    def test_save_invalid_text_does_not_touch_owned_target(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            target = engine._temp_files.create(".wav", directory=TMP_DIR)
            Path(target).write_bytes(b"KEEP")
            with self.assertRaises(ValueError):
                engine.save_to_file("   ", target)
            self.assertEqual(Path(target).read_bytes(), b"KEEP")
            assert fake is not None
            self.assertEqual(len(fake.synth_calls), 0)

    def test_save_owned_wav_success(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, policy = _make_engine(tmp)
            self.addCleanup(engine.shutdown, stop_rvc=False)
            target = engine._temp_files.create(".wav", directory=TMP_DIR)
            engine.save_to_file("导出一句", target)
            self.assertTrue(Path(target).is_file())
            assert fake is not None
            self.assertEqual(len(fake.synth_calls), 1)
            self.assertEqual(fake.synth_calls[0][1], COSYVOICE_STYLE_ID)


class CosyVoiceClientShutdownGuardTests(unittest.TestCase):
    def test_continue_shutdown_requires_closing_client(self) -> None:
        settings = parse_cosyvoice_settings({"engine": "cosyvoice"})
        policy, _ = _policy_with_local_tts(str(TMP_DIR.parent))
        tfm = TempFileManager(safety_policy=policy, default_dir=TMP_DIR)
        self.addCleanup(tfm.shutdown)
        client = CosyVoiceWorkerClient(settings, policy, tfm)
        self.addCleanup(client.shutdown, 0.1)
        with self.assertRaises(RuntimeError):
            client.continue_shutdown(0.01)
        self.assertFalse(client._closing)


class CosyVoiceConfigParseTests(unittest.TestCase):
    def test_resolve_inner_cosyvoice_config(self) -> None:
        cfg = resolve_cosyvoice_settings_from_engine_config({"max_text_chars": 100})
        self.assertEqual(cfg.max_text_chars, 100)


class CosyVoiceStubCompatTests(unittest.TestCase):
    def test_interrupt_without_cosy_backend_field(self) -> None:
        engine = object.__new__(TTSEngine)
        engine._interrupt_event = threading.Event()
        engine._interrupt_epoch_lock = threading.Lock()
        engine._interrupt_epoch = 0
        engine._player = MagicMock()
        engine._pyttsx3 = None
        with patch("src.voice.rvc.infer.interrupt_rvc_worker"):
            engine.interrupt()
        self.assertTrue(engine._interrupt_event.is_set())


if __name__ == "__main__":
    unittest.main()
