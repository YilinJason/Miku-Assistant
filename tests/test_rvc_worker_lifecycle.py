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

from src.core.safety import SafetyPolicy
from src.voice.rvc.audio_prep import warm_audio_preprocessing
from src.voice.rvc.infer import (
    RvcWorkerUnavailableError,
    _config_key,
    get_rvc_worker,
    interrupt_rvc_worker,
    run_rvc_infer,
    stop_rvc_worker,
    warm_rvc_worker,
)
from src.voice.rvc.worker import RVCWorkerClient, _read_json_line
from src.voice.tts.engine import TTSEngine, TtsShutdownStatus
from scripts.rvc_worker import _parent_is_alive, _warm_up_vc

from tests.support.host_memory_fixtures import install_module_sufficient_host_memory


def setUpModule() -> None:
    install_module_sufficient_host_memory()


def _allow_rvc_policy() -> SafetyPolicy:
    return SafetyPolicy(
        {
            "permissions": {
                "rvc_worker": True,
                "rvc_infer": True,
                "rvc_probe": True,
                "subprocess": False,
            }
        }
    )


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
    def tearDown(self) -> None:
        import src.voice.rvc.infer as infer_mod

        client = infer_mod._worker_client
        if client is not None:
            if getattr(client, "process_alive", lambda: False)():
                client.stop()
            infer_mod._worker_client = None
            infer_mod._worker_config_key = None

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
        client = RVCWorkerClient({"device": "cpu"}, safety_policy=_allow_rvc_policy())
        proc = _FakeProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001

        self.assertTrue(client.stop())

        self.assertTrue(proc.terminated)
        self.assertIsNone(client._proc)  # noqa: SLF001
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)

    def test_stop_returns_false_when_process_survives_kill(self) -> None:
        class _StuckProcess(_FakeProcess):
            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("stuck-rvc", timeout)

        client = RVCWorkerClient({"device": "cpu"}, safety_policy=_allow_rvc_policy())
        proc = _StuckProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001

        self.assertFalse(client.stop())
        self.assertFalse(client.last_stop_exited)
        self.assertTrue(client.process_alive())
        self.assertIs(client._proc, proc)  # noqa: SLF001
        self.assertFalse(proc.stdin.closed)

    def test_stop_failure_allows_bounded_retry(self) -> None:
        class _StuckProcess(_FakeProcess):
            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("stuck-rvc", timeout)

        client = RVCWorkerClient({"device": "cpu"}, safety_policy=_allow_rvc_policy())
        proc = _StuckProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001

        self.assertFalse(client.stop())
        self.assertFalse(client.stop())
        self.assertTrue(client.process_alive())

    def test_start_locked_refuses_while_process_still_alive(self) -> None:
        class _StuckProcess(_FakeProcess):
            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("stuck-rvc", timeout)

        client = RVCWorkerClient({"device": "cpu"}, safety_policy=_allow_rvc_policy())
        proc = _StuckProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = False  # noqa: SLF001
        client._last_stop_exited = False  # noqa: SLF001

        with patch.object(client, "stop") as stop_mock:
            self.assertFalse(client.start())
            stop_mock.assert_not_called()

    def test_exited_process_handle_cleared_after_external_exit(self) -> None:
        class _StuckProcess(_FakeProcess):
            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

            def poll(self):
                if self.terminated or self.killed:
                    return 0
                return None

            def wait(self, timeout=None):
                if self.terminated or self.killed:
                    return 0
                raise subprocess.TimeoutExpired("stuck-rvc", timeout)

        client = RVCWorkerClient({"device": "cpu"}, safety_policy=_allow_rvc_policy())
        proc = _StuckProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001
        self.assertFalse(client.stop())
        self.assertTrue(client.process_alive())

        proc.terminated = True
        client._release_exited_process(proc)  # noqa: SLF001
        self.assertIsNone(client._proc)  # noqa: SLF001
        self.assertFalse(client.process_alive())

    def test_get_rvc_worker_refuses_rebuild_when_stop_fails(self) -> None:
        import src.voice.rvc.infer as infer_mod

        policy = _allow_rvc_policy()
        cfg_a = {"model_path": "voice-a.pth", "device": "cpu", "pitch": 0}
        cfg_b = {"model_path": "voice-a.pth", "device": "cpu", "pitch": 1}
        client = RVCWorkerClient(cfg_a, safety_policy=policy)
        proc = _FakeProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = False  # noqa: SLF001
        client._last_stop_exited = False  # noqa: SLF001
        infer_mod._worker_client = client
        infer_mod._worker_config_key = _config_key(cfg_a)

        with patch.object(client, "stop", return_value=False):
            with self.assertRaises(RvcWorkerUnavailableError):
                get_rvc_worker(cfg_b, policy)
        self.assertIs(infer_mod._worker_client, client)
        self.assertEqual(infer_mod._worker_config_key, _config_key(cfg_a))

    def test_stop_rvc_worker_retains_global_client_when_stop_fails(self) -> None:
        import src.voice.rvc.infer as infer_mod

        client = MagicMock()
        client.converting = False
        client.interrupt_current = MagicMock(return_value=False)
        client.stop = MagicMock(return_value=False)
        client.process_alive = MagicMock(return_value=True)
        infer_mod._worker_client = client
        infer_mod._worker_config_key = "k"

        self.assertFalse(stop_rvc_worker())
        self.assertIs(infer_mod._worker_client, client)
        self.assertEqual(infer_mod._worker_config_key, "k")

    def test_convert_timeout_terminates_worker(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from src.voice.audio.temp_files import TempFileManager

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _allow_rvc_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            client = RVCWorkerClient(
                {"device": "cpu"},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            proc = _FakeProcess()
            proc.stdout = _BlockingStream()  # type: ignore[assignment]
            client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            owned_in = mgr.create(".wav")
            owned_out = mgr.create(".wav")

            started = time.perf_counter()
            ok = client.convert(owned_in, owned_out, timeout=0.05)

            self.assertFalse(ok)
            self.assertLess(time.perf_counter() - started, 0.5)
            self.assertTrue(proc.terminated)
            mgr.shutdown()

    def test_current_parent_process_is_detected_alive(self) -> None:
        self.assertTrue(_parent_is_alive(os.getpid()))

    def test_warmup_command_uses_loaded_worker_without_restarting(self) -> None:
        client = RVCWorkerClient({"device": "cpu"}, safety_policy=_allow_rvc_policy())
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
        client = RVCWorkerClient({"device": "cpu"}, safety_policy=_allow_rvc_policy())
        proc = _FakeProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001

        self.assertFalse(client.interrupt_current())
        self.assertIs(client._proc, proc)  # noqa: SLF001
        self.assertTrue(client.ready)
        self.assertFalse(proc.terminated)

    def test_active_interrupt_terminates_worker_but_client_can_be_reused(self) -> None:
        client = RVCWorkerClient({"device": "cpu"}, safety_policy=_allow_rvc_policy())
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
            ok = run_rvc_infer("in.wav", "out.wav", {"worker_enabled": True}, _allow_rvc_policy())
        self.assertFalse(ok)
        fallback.assert_not_called()

    def test_startup_warmup_failure_returns_false(self) -> None:
        worker = MagicMock()
        worker.start.return_value = True
        worker.warm_up.return_value = False
        worker.ready = True
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker):
            self.assertFalse(
                warm_rvc_worker(
                    {
                        "model_path": "voice.pth",
                        "worker_enabled": True,
                        "worker_warmup_inference": True,
                        "worker_warmup_timeout": 12,
                    },
                    _allow_rvc_policy(),
                )
            )
        worker.start.assert_called_once_with()
        worker.warm_up.assert_called_once_with(timeout=12.0)

    def test_startup_inference_warmup_can_be_disabled(self) -> None:
        worker = MagicMock()
        worker.start.return_value = True
        worker.ready = True
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker):
            self.assertTrue(
                warm_rvc_worker(
                    {
                        "model_path": "voice.pth",
                        "worker_enabled": True,
                        "worker_warmup_inference": False,
                    },
                    _allow_rvc_policy(),
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
            policy = _allow_rvc_policy()
            self.assertTrue(
                run_rvc_infer(
                    "in.wav",
                    "out.wav",
                    {
                        "worker_enabled": True,
                        "fallback_on_worker_failure": True,
                    },
                    policy,
                )
            )
            self.assertTrue(
                run_rvc_infer(
                    "in2.wav",
                    "out2.wav",
                    {"worker_enabled": False},
                    policy,
                )
            )
        self.assertEqual(fallback.call_count, 2)


class WarmRvcWorkerPrepareContractTests(unittest.TestCase):
    """warm_rvc_worker 准备结果契约（纯 fake，不默认 truthy ready）。"""

    def _cfg(self, **overrides: object) -> dict:
        base = {
            "model_path": "voice.pth",
            "worker_enabled": True,
            "worker_warmup_inference": True,
            "worker_warmup_timeout": 12,
        }
        base.update(overrides)
        return base

    def test_start_false_skips_warmup(self) -> None:
        worker = MagicMock()
        worker.start.return_value = False
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker):
            self.assertFalse(warm_rvc_worker(self._cfg(), _allow_rvc_policy()))
        worker.warm_up.assert_not_called()

    def test_warm_false_returns_false(self) -> None:
        worker = MagicMock()
        worker.start.return_value = True
        worker.warm_up.return_value = False
        worker.ready = True
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker):
            self.assertFalse(warm_rvc_worker(self._cfg(), _allow_rvc_policy()))

    def test_warm_true_but_not_ready_returns_false(self) -> None:
        worker = MagicMock()
        worker.start.return_value = True
        worker.warm_up.return_value = True
        worker.ready = False
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker):
            self.assertFalse(warm_rvc_worker(self._cfg(), _allow_rvc_policy()))

    def test_warm_true_and_ready_returns_true(self) -> None:
        worker = MagicMock()
        worker.start.return_value = True
        worker.warm_up.return_value = True
        worker.ready = True
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker):
            self.assertTrue(warm_rvc_worker(self._cfg(), _allow_rvc_policy()))

    def test_warmup_disabled_skips_warm_when_ready(self) -> None:
        worker = MagicMock()
        worker.start.return_value = True
        worker.ready = True
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker):
            self.assertTrue(
                warm_rvc_worker(
                    self._cfg(worker_warmup_inference=False),
                    _allow_rvc_policy(),
                )
            )
        worker.warm_up.assert_not_called()

    def test_warmup_disabled_not_ready_returns_false(self) -> None:
        worker = MagicMock()
        worker.start.return_value = True
        worker.ready = False
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker):
            self.assertFalse(
                warm_rvc_worker(
                    self._cfg(worker_warmup_inference=False),
                    _allow_rvc_policy(),
                )
            )
        worker.warm_up.assert_not_called()

    def test_worker_disabled_returns_false_without_warm(self) -> None:
        worker = MagicMock()
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker) as get_worker:
            self.assertFalse(
                warm_rvc_worker(
                    self._cfg(worker_enabled=False),
                    _allow_rvc_policy(),
                )
            )
        get_worker.assert_not_called()
        worker.warm_up.assert_not_called()

    def test_missing_policy_returns_false(self) -> None:
        worker = MagicMock()
        with patch("src.voice.rvc.infer.get_rvc_worker", return_value=worker) as get_worker:
            self.assertFalse(warm_rvc_worker(self._cfg(), None))
        get_worker.assert_not_called()


class F4ParallelLoadGuardTests(unittest.TestCase):
    """F4：存活 Worker 且停止失败时禁止 fallback / worker_disabled / 策略切换并行冷启动。"""

    def _stuck_alive_client(self) -> tuple[RVCWorkerClient, _FakeProcess]:
        class _StuckProcess(_FakeProcess):
            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

            def poll(self):
                if self.terminated or self.killed:
                    return 0
                return None

            def wait(self, timeout=None):
                if self.terminated or self.killed:
                    return 0
                raise subprocess.TimeoutExpired("stuck-rvc", timeout)

        client = RVCWorkerClient({"device": "cpu"}, safety_policy=_allow_rvc_policy())
        proc = _StuckProcess()
        client._proc = proc  # type: ignore[assignment]  # noqa: SLF001
        client._ready = True  # noqa: SLF001
        self.assertTrue(client.process_alive())
        self.assertFalse(client.stop())
        self.assertTrue(client.process_alive())
        return client, proc

    def tearDown(self) -> None:
        import src.voice.rvc.infer as infer_mod

        client = infer_mod._worker_client
        if client is not None:
            proc = getattr(client, "_proc", None)
            if proc is not None:
                proc.terminated = True
            if getattr(client, "process_alive", lambda: False)():
                client.stop()
            infer_mod._worker_client = None
            infer_mod._worker_config_key = None

    def _install_global(self, client: RVCWorkerClient, cfg: dict) -> None:
        import src.voice.rvc.infer as infer_mod

        infer_mod._worker_client = client
        infer_mod._worker_config_key = _config_key(cfg)

    def test_explicit_fallback_skips_subprocess_when_worker_stop_fails(self) -> None:
        import src.voice.rvc.infer as infer_mod

        policy = _allow_rvc_policy()
        cfg = {"model_path": "voice.pth", "device": "cpu", "pitch": 0}
        client, _proc = self._stuck_alive_client()
        self._install_global(client, cfg)
        builds = {"n": 0}

        class _UnexpectedClient:
            def __init__(self, *a, **k):
                builds["n"] += 1

        with patch.object(client, "convert", return_value=False), patch(
            "src.voice.rvc.worker.RVCWorkerClient", _UnexpectedClient
        ), patch(
            "src.voice.rvc.infer.run_rvc_infer_subprocess", return_value=True
        ) as fallback:
            ok = run_rvc_infer(
                "in.wav",
                "out.wav",
                {**cfg, "worker_enabled": True, "fallback_on_worker_failure": True},
                policy,
            )
        self.assertFalse(ok)
        fallback.assert_not_called()
        self.assertEqual(builds["n"], 0)
        self.assertIs(infer_mod._worker_client, client)
        self.assertTrue(client.process_alive())

    def test_worker_disabled_skips_subprocess_when_global_worker_stop_fails(self) -> None:
        import src.voice.rvc.infer as infer_mod

        policy = _allow_rvc_policy()
        cfg = {"model_path": "voice.pth", "device": "cpu", "pitch": 0}
        client, _proc = self._stuck_alive_client()
        self._install_global(client, cfg)

        with patch(
            "src.voice.rvc.infer.run_rvc_infer_subprocess", return_value=True
        ) as fallback:
            ok = run_rvc_infer(
                "in.wav",
                "out.wav",
                {"worker_enabled": False},
                policy,
            )
        self.assertFalse(ok)
        fallback.assert_not_called()
        self.assertIs(infer_mod._worker_client, client)

    def test_policy_switch_returns_false_without_subprocess_when_stop_fails(self) -> None:
        import src.voice.rvc.infer as infer_mod

        allow = _allow_rvc_policy()
        deny = SafetyPolicy(
            {
                "permissions": {
                    "rvc_worker": True,
                    "rvc_infer": True,
                    "rvc_probe": True,
                    "subprocess": False,
                }
            }
        )
        cfg = {"model_path": "voice.pth", "device": "cpu", "pitch": 0}
        client, _proc = self._stuck_alive_client()
        client.safety_policy = allow
        self._install_global(client, cfg)
        builds = {"n": 0}

        class _UnexpectedClient:
            def __init__(self, *a, **k):
                builds["n"] += 1

        with patch("src.voice.rvc.worker.RVCWorkerClient", _UnexpectedClient), patch(
            "src.voice.rvc.infer.run_rvc_infer_subprocess", return_value=True
        ) as fallback:
            ok = run_rvc_infer(
                "in.wav",
                "out.wav",
                {"worker_enabled": True},
                deny,
            )
        self.assertFalse(ok)
        fallback.assert_not_called()
        self.assertEqual(builds["n"], 0)
        self.assertIs(infer_mod._worker_client, client)
        self.assertIs(client.safety_policy, allow)

    def test_subprocess_allowed_after_worker_confirmed_exited(self) -> None:
        import src.voice.rvc.infer as infer_mod

        policy = _allow_rvc_policy()
        cfg = {"model_path": "voice.pth", "device": "cpu", "pitch": 0}
        client, proc = self._stuck_alive_client()
        self._install_global(client, cfg)

        proc.terminated = True
        client._release_exited_process(proc)  # noqa: SLF001
        self.assertFalse(client.process_alive())

        with patch.object(client, "convert", return_value=False), patch(
            "src.voice.rvc.infer.run_rvc_infer_subprocess", return_value=True
        ) as fallback:
            ok = run_rvc_infer(
                "in.wav",
                "out.wav",
                {**cfg, "worker_enabled": True, "fallback_on_worker_failure": True},
                policy,
            )
        self.assertTrue(ok)
        fallback.assert_called_once()
        self.assertIsNone(infer_mod._worker_client)

    def test_warm_rvc_worker_false_when_rebuild_blocked(self) -> None:
        import src.voice.rvc.infer as infer_mod

        policy = _allow_rvc_policy()
        cfg_a = {"model_path": "voice-a.pth", "device": "cpu", "pitch": 0}
        cfg_b = {"model_path": "voice-a.pth", "device": "cpu", "pitch": 1}
        client, _proc = self._stuck_alive_client()
        self._install_global(client, cfg_a)
        builds = {"n": 0}

        class _UnexpectedClient:
            def __init__(self, *a, **k):
                builds["n"] += 1
                self.start = MagicMock(return_value=True)
                self.warm_up = MagicMock(return_value=True)

        with patch("src.voice.rvc.worker.RVCWorkerClient", _UnexpectedClient):
            ok = warm_rvc_worker(
                {**cfg_b, "worker_enabled": True},
                policy,
            )
        self.assertFalse(ok)
        self.assertEqual(builds["n"], 0)
        self.assertIs(infer_mod._worker_client, client)


class TTSShutdownTests(unittest.TestCase):
    @staticmethod
    def _stub_turn_stream_for_partial_engine(engine: TTSEngine) -> None:
        engine._turn_stream_session_lock = threading.Lock()  # noqa: SLF001
        engine._turn_stream_session = None  # noqa: SLF001
        if not getattr(engine, "_temp_files", None):
            tfm = MagicMock()
            tfm.pending_cleanup_count = MagicMock(return_value=0)
            engine._temp_files = tfm  # noqa: SLF001
        else:
            engine._temp_files.pending_cleanup_count = MagicMock(return_value=0)
        if not hasattr(engine, "pipeline_enabled"):
            engine.pipeline_enabled = False

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
        engine.safety_policy = SafetyPolicy(
            {"permissions": {"cloud_tts": True}}
        )
        engine.logger = logging.getLogger("test.tts.interrupt_epoch")
        self._stub_turn_stream_for_partial_engine(engine)

        snapshot_seen = threading.Event()
        original_snapshot = engine._interrupt_epoch_snapshot  # noqa: SLF001

        def _snapshot() -> int:
            value = original_snapshot()
            snapshot_seen.set()
            return value

        engine._interrupt_epoch_snapshot = _snapshot  # type: ignore[method-assign]  # noqa: SLF001
        worker: threading.Thread | None = None
        lock_held = False
        worker_started = False
        worker_errors: list[BaseException] = []

        def _speak_old_turn() -> None:
            try:
                engine.speak("旧轮")
            except BaseException as exc:
                worker_errors.append(exc)

        try:
            engine._speak_lock.acquire()  # noqa: SLF001
            lock_held = True
            worker = threading.Thread(
                target=_speak_old_turn, name="test-speak-old", daemon=False
            )
            with patch("src.voice.rvc.infer.interrupt_rvc_worker"):
                worker.start()
                worker_started = True
                self.assertTrue(snapshot_seen.wait(timeout=1.0))
                engine.interrupt()
        finally:
            if lock_held:
                engine._speak_lock.release()  # noqa: SLF001
                lock_held = False
            if worker_started and worker is not None:
                with patch("src.voice.rvc.infer.interrupt_rvc_worker"):
                    worker.join(timeout=2.0)
                if worker.is_alive():
                    raise AssertionError("speak worker thread did not join")
            if worker_errors:
                raise worker_errors[0]

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
        engine._speak_lock = threading.Lock()  # noqa: SLF001
        engine._player = MagicMock()  # noqa: SLF001
        engine._cosy_backend = None  # noqa: SLF001
        engine._owns_temp_files = False  # noqa: SLF001
        engine._interrupt_event = threading.Event()  # noqa: SLF001
        engine._interrupt_epoch_lock = threading.Lock()  # noqa: SLF001
        engine._interrupt_epoch = 0  # noqa: SLF001
        engine._pyttsx3 = None  # noqa: SLF001
        engine._tts_shutdown_status = None  # noqa: SLF001
        self._stub_turn_stream_for_partial_engine(engine)
        with patch("src.voice.rvc.infer.stop_rvc_worker", return_value=True) as stop_worker:
            engine.shutdown()
            engine.shutdown()
        self.assertTrue(engine._shutdown_event.is_set())  # noqa: SLF001
        self.assertEqual(engine._player.stop.call_count, 2)  # noqa: SLF001
        self.assertEqual(stop_worker.call_count, 2)

    def _minimal_shutdown_engine(self) -> TTSEngine:
        engine = object.__new__(TTSEngine)
        engine._shutdown_event = threading.Event()  # noqa: SLF001
        engine._speak_lock = threading.Lock()  # noqa: SLF001
        engine._player = MagicMock()  # noqa: SLF001
        engine._cosy_backend = None  # noqa: SLF001
        engine._cosy_local_shutdown_status = None  # noqa: SLF001
        engine._owns_temp_files = True  # noqa: SLF001
        engine._temp_files_shutdown_done = False  # noqa: SLF001
        engine._temp_files = MagicMock()  # noqa: SLF001
        engine._temp_files.shutdown = MagicMock(  # noqa: SLF001
            return_value=MagicMock(complete=True)
        )
        engine.logger = logging.getLogger("test.tts.shutdown_status")
        engine._interrupt_event = threading.Event()  # noqa: SLF001
        engine._interrupt_epoch_lock = threading.Lock()  # noqa: SLF001
        engine._interrupt_epoch = 0  # noqa: SLF001
        engine._pyttsx3 = None  # noqa: SLF001
        engine._tts_shutdown_status = None  # noqa: SLF001
        self._stub_turn_stream_for_partial_engine(engine)
        return engine

    def test_get_shutdown_status_incomplete_before_shutdown(self) -> None:
        engine = self._minimal_shutdown_engine()
        status = engine.get_shutdown_status()
        self.assertFalse(status.complete)

    def test_rvc_stop_false_then_true_on_retry(self) -> None:
        engine = self._minimal_shutdown_engine()
        outcomes = iter([False, True])

        with patch(
            "src.voice.rvc.infer.stop_rvc_worker",
            side_effect=lambda: next(outcomes),
        ):
            engine.shutdown()
            first = engine.get_shutdown_status()
            self.assertFalse(first.complete)
            self.assertFalse(first.persistent_rvc_complete)
            engine.shutdown()
            second = engine.get_shutdown_status()
        self.assertTrue(second.complete)
        self.assertTrue(second.persistent_rvc_complete)
        engine._temp_files.shutdown.assert_called_once()  # noqa: SLF001

    def test_stop_rvc_false_skips_worker_and_marks_rvc_complete(self) -> None:
        engine = self._minimal_shutdown_engine()
        with patch("src.voice.rvc.infer.stop_rvc_worker") as stop_worker:
            engine.shutdown(stop_rvc=False)
        stop_worker.assert_not_called()
        status = engine.get_shutdown_status()
        self.assertTrue(status.persistent_rvc_complete)
        self.assertTrue(status.complete)

    def test_speak_lock_busy_blocks_completion_and_tfm(self) -> None:
        engine = self._minimal_shutdown_engine()
        engine._speak_lock.acquire()  # noqa: SLF001
        try:
            with patch("src.voice.rvc.infer.stop_rvc_worker", return_value=True):
                engine.shutdown()
            status = engine.get_shutdown_status()
            self.assertFalse(status.synthesis_idle)
            self.assertFalse(status.complete)
            engine._temp_files.shutdown.assert_not_called()  # noqa: SLF001
        finally:
            engine._speak_lock.release()  # noqa: SLF001

    def test_rvc_stop_false_retains_owned_tfm(self) -> None:
        engine = self._minimal_shutdown_engine()
        with patch("src.voice.rvc.infer.stop_rvc_worker", return_value=False):
            engine.shutdown()
        status = engine.get_shutdown_status()
        self.assertFalse(status.persistent_rvc_complete)
        self.assertFalse(status.temp_files_complete)
        engine._temp_files.shutdown.assert_not_called()  # noqa: SLF001

    def test_non_boolean_rvc_stop_not_complete(self) -> None:
        engine = self._minimal_shutdown_engine()
        with patch("src.voice.rvc.infer.stop_rvc_worker", return_value=object()):
            engine.shutdown()
        status = engine.get_shutdown_status()
        self.assertFalse(status.persistent_rvc_complete)
        self.assertFalse(status.complete)

    def test_non_boolean_local_tts_not_complete(self) -> None:
        from types import SimpleNamespace

        engine = self._minimal_shutdown_engine()
        engine._cosy_backend = SimpleNamespace(  # noqa: SLF001
            shutdown_local=lambda: SimpleNamespace(complete="yes"),
            inflight_calls=lambda: 0,
            retained_output_count=lambda: 0,
        )
        with patch("src.voice.rvc.infer.stop_rvc_worker", return_value=True):
            engine.shutdown()
        status = engine.get_shutdown_status()
        self.assertFalse(status.local_tts_complete)
        self.assertFalse(status.complete)


if __name__ == "__main__":
    unittest.main()
