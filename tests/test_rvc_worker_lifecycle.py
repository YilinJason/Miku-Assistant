"""RVC Worker 有界通信、失败降级与 TTS 关闭的纯离线测试。"""
from __future__ import annotations

import io
import logging
import os
import subprocess
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from src.voice.rvc.audio_prep import warm_audio_preprocessing
from src.voice.rvc.infer import (
    _config_key,
    interrupt_rvc_worker,
    run_rvc_infer,
    warm_rvc_worker,
)
from src.voice.rvc.worker import RVCWorkerClient, _read_json_line
from src.voice.tts.engine import TTSEngine
from scripts.rvc_worker import _parent_is_alive, _warm_up_vc


class _BlockingStream:
    def __init__(self) -> None:
        self.release = threading.Event()

    def readline(self) -> str:
        self.release.wait(timeout=2.0)
        return ""

    def close(self) -> None:
        self.release.set()


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self.terminated or self.killed else None

    def wait(self, timeout=None):
        if self.poll() is None:
            raise subprocess.TimeoutExpired("fake-rvc", timeout)
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class WorkerPipeTests(unittest.TestCase):
    def test_protocol_read_timeout_is_real_not_blocked_by_readline(self) -> None:
        stream = _BlockingStream()
        started = time.perf_counter()
        try:
            self.assertIsNone(_read_json_line(stream, timeout=0.05))
            self.assertLess(time.perf_counter() - started, 0.5)
        finally:
            stream.release.set()

    def test_protocol_reader_skips_noise_and_returns_json(self) -> None:
        stream = io.StringIO("library noise\nnot-json\n{\"ok\": true}\n")
        self.assertEqual(_read_json_line(stream, timeout=0.5), {"ok": True})

    def test_stop_has_bounded_terminate_and_closes_pipes(self) -> None:
        client = RVCWorkerClient({"device": "cpu"})
        proc = _FakeProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001

        client.stop()

        self.assertTrue(proc.terminated)
        self.assertIsNone(client._proc)  # noqa: SLF001
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)

    def test_convert_timeout_terminates_worker(self) -> None:
        client = RVCWorkerClient({"device": "cpu"})
        proc = _FakeProcess()
        proc.stdout = _BlockingStream()  # type: ignore[assignment]
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001

        started = time.perf_counter()
        ok = client.convert("in.wav", "out.wav", timeout=0.05)

        self.assertFalse(ok)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertTrue(proc.terminated)

    def test_current_parent_process_is_detected_alive(self) -> None:
        self.assertTrue(_parent_is_alive(os.getpid()))

    def test_warmup_command_uses_loaded_worker_without_restarting(self) -> None:
        client = RVCWorkerClient({"device": "cpu"})
        proc = _FakeProcess()
        proc.stdout = io.StringIO(
            '{"ok": true, "warmup": true, "infer_ms": 12, "total_ms": 13}\n'
        )
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001

        self.assertTrue(client.warm_up(timeout=0.5))
        self.assertIn('"cmd": "warmup"', proc.stdin.getvalue())
        self.assertTrue(client.ready)
        self.assertFalse(client.converting)

    def test_idle_interrupt_preserves_loaded_worker(self) -> None:
        client = RVCWorkerClient({"device": "cpu"})
        proc = _FakeProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001

        self.assertFalse(client.interrupt_current())
        self.assertIs(client._proc, proc)  # noqa: SLF001
        self.assertTrue(client.ready)
        self.assertFalse(proc.terminated)

    def test_active_interrupt_terminates_worker_but_client_can_be_reused(self) -> None:
        client = RVCWorkerClient({"device": "cpu"})
        proc = _FakeProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001
        client._converting.set()  # noqa: SLF001

        self.assertTrue(client.interrupt_current())
        self.assertTrue(proc.terminated)
        self.assertIsNone(client._proc)  # noqa: SLF001

    def test_worker_warmup_generates_temporary_input_and_discards_it(self) -> None:
        observed = {}

        def fake_convert(vc, req, worker_cfg):
            observed["input"] = req["input"]
            observed["output"] = req["output"]
            self.assertTrue(os.path.isfile(req["input"]))
            self.assertFalse(os.path.exists(req["output"]))
            return True, req["output"], 1.0

        with patch("scripts.rvc_worker._convert", side_effect=fake_convert):
            ok, _, _ = _warm_up_vc(object(), {"pitch": 0, "index_rate": 0.55})
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(observed["input"]))
        self.assertFalse(os.path.exists(observed["output"]))

    def test_audio_resampler_warmup_is_idempotent_per_sample_rate(self) -> None:
        unique_rate = 40123
        # librosa 用延迟属性导入；直接 patch ``librosa.resample`` 会触发真实
        # numba JIT。替换整个模块引用，只验证调用契约。
        fake_librosa = MagicMock()
        with patch("src.voice.rvc.audio_prep.librosa", fake_librosa):
            self.assertTrue(warm_audio_preprocessing(unique_rate))
            self.assertFalse(warm_audio_preprocessing(unique_rate))
        fake_librosa.resample.assert_called_once()


class WorkerFallbackPolicyTests(unittest.TestCase):
    def test_worker_cache_key_does_not_repeat_cuda_probe(self) -> None:
        config = {
            "model_path": "voice.pth",
            "index_path": "voice.index",
            "device": "auto",
            "pitch": 0,
        }
        with patch("src.voice.rvc.infer.resolve_rvc_device") as resolve:
            first = _config_key(config)
            second = _config_key(dict(config))
        self.assertEqual(first, second)
        resolve.assert_not_called()

    def test_worker_failure_defaults_to_edge_tts_without_cold_process(self) -> None:
        worker = MagicMock()
        worker.convert.return_value = False
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker), patch(
            "src.voice.rvc.infer.run_rvc_infer_subprocess"
        ) as fallback:
            ok = run_rvc_infer("in.wav", "out.wav", {"worker_enabled": True})
        self.assertFalse(ok)
        fallback.assert_not_called()

    def test_startup_warms_first_inference_but_failure_remains_fail_open(self) -> None:
        worker = MagicMock()
        worker.start.return_value = True
        worker.warm_up.return_value = False
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker):
            self.assertTrue(
                warm_rvc_worker(
                    {
                        "model_path": "voice.pth",
                        "worker_enabled": True,
                        "worker_warmup_inference": True,
                        "worker_warmup_timeout": 12,
                    }
                )
            )
        worker.start.assert_called_once_with()
        worker.warm_up.assert_called_once_with(timeout=12.0)

    def test_startup_inference_warmup_can_be_disabled(self) -> None:
        worker = MagicMock()
        worker.start.return_value = True
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker):
            self.assertTrue(
                warm_rvc_worker(
                    {
                        "model_path": "voice.pth",
                        "worker_enabled": True,
                        "worker_warmup_inference": False,
                    }
                )
            )
        worker.start.assert_called_once_with()
        worker.warm_up.assert_not_called()

    def test_interrupt_helper_preserves_idle_global_worker(self) -> None:
        worker = MagicMock()
        worker.interrupt_current.return_value = False
        with patch("src.voice.rvc.infer._worker_client", worker):
            self.assertFalse(interrupt_rvc_worker())
        worker.interrupt_current.assert_called_once_with()

    def test_explicit_worker_fallback_and_worker_disabled_use_subprocess(self) -> None:
        worker = MagicMock()
        worker.convert.return_value = False
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker), patch(
            "src.voice.rvc.infer.run_rvc_infer_subprocess", return_value=True
        ) as fallback:
            self.assertTrue(
                run_rvc_infer(
                    "in.wav",
                    "out.wav",
                    {
                        "worker_enabled": True,
                        "fallback_on_worker_failure": True,
                    },
                )
            )
            self.assertTrue(
                run_rvc_infer(
                    "in2.wav",
                    "out2.wav",
                    {"worker_enabled": False},
                )
            )
        self.assertEqual(fallback.call_count, 2)


class TTSShutdownTests(unittest.TestCase):
    def test_queued_speak_from_interrupted_turn_cannot_clear_new_interrupt(self) -> None:
        engine = object.__new__(TTSEngine)
        engine._interrupt_event = threading.Event()  # noqa: SLF001
        engine._interrupt_epoch_lock = threading.Lock()  # noqa: SLF001
        engine._interrupt_epoch = 0  # noqa: SLF001
        engine._shutdown_event = threading.Event()  # noqa: SLF001
        engine._speak_lock = threading.Lock()  # noqa: SLF001
        engine._player = MagicMock()  # noqa: SLF001
        engine._pyttsx3 = None  # noqa: SLF001
        engine._speak_single = MagicMock()  # noqa: SLF001
        engine.engine_type = "edge-tts"
        engine.pipeline_enabled = False
        engine.fallback_enabled = False
        engine.logger = logging.getLogger("test.tts.interrupt_epoch")

        snapshot_seen = threading.Event()
        original_snapshot = engine._interrupt_epoch_snapshot  # noqa: SLF001

        def _snapshot() -> int:
            value = original_snapshot()
            snapshot_seen.set()
            return value

        engine._interrupt_epoch_snapshot = _snapshot  # type: ignore[method-assign]  # noqa: SLF001
        engine._speak_lock.acquire()  # noqa: SLF001
        worker = threading.Thread(target=engine.speak, args=("旧轮",), daemon=True)
        worker.start()
        self.assertTrue(snapshot_seen.wait(timeout=1.0))
        try:
            with patch("src.voice.rvc.infer.interrupt_rvc_worker"):
                engine.interrupt()
        finally:
            engine._speak_lock.release()  # noqa: SLF001
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        engine._speak_single.assert_not_called()  # noqa: SLF001
        self.assertTrue(engine._interrupt_event.is_set())  # noqa: SLF001

        engine.speak("新轮")
        engine._speak_single.assert_called_once_with("新轮")  # noqa: SLF001

    def test_interrupt_stops_current_output_and_worker_without_closing_engine(self) -> None:
        engine = object.__new__(TTSEngine)
        engine._interrupt_event = threading.Event()  # noqa: SLF001
        engine._shutdown_event = threading.Event()  # noqa: SLF001
        engine._player = MagicMock()  # noqa: SLF001
        engine._pyttsx3 = MagicMock()  # noqa: SLF001
        with patch("src.voice.rvc.infer.interrupt_rvc_worker") as interrupt_worker:
            engine.interrupt()
        self.assertTrue(engine._interrupt_event.is_set())  # noqa: SLF001
        self.assertFalse(engine._shutdown_event.is_set())  # noqa: SLF001
        engine._player.stop_current.assert_called_once_with()  # noqa: SLF001
        engine._pyttsx3.stop.assert_called_once_with()  # noqa: SLF001
        interrupt_worker.assert_called_once_with()

    def test_shutdown_stops_playback_and_worker_idempotently(self) -> None:
        engine = object.__new__(TTSEngine)
        engine._shutdown_event = threading.Event()  # noqa: SLF001
        engine._player = MagicMock()  # noqa: SLF001
        with patch("src.voice.rvc.infer.stop_rvc_worker") as stop_worker:
            engine.shutdown()
            engine.shutdown()
        self.assertTrue(engine._shutdown_event.is_set())  # noqa: SLF001
        self.assertEqual(engine._player.stop.call_count, 2)  # noqa: SLF001
        self.assertEqual(stop_worker.call_count, 2)


if __name__ == "__main__":
    unittest.main()
