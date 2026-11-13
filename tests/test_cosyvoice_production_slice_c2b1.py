"""CosyVoice 生产切片 C2b1：显式 prepare API（纯离线 fake，不接装配/自动预热）。"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List
from unittest.mock import patch

from src.core.safety import SafetyPolicy
from src.voice.cosyvoice.settings import CosyVoiceSettings
from src.voice.audio.temp_files import TempFileManager
from src.voice.cosyvoice.client import CosyVoiceWorkerClient
from src.voice.cosyvoice.worker_inbound import parse_inbound_line
from src.voice.tts.cosyvoice_backend import CosyVoiceTtsBackend
from src.voice.tts.engine import TTSEngine
from tests.support.local_tts_runners import bind_fake_local_tts_runners
from tests.test_cosyvoice_production_slice_a import _policy_with_local_tts
from tests.test_cosyvoice_production_slice_b import (
    _AdvanceableClock,
    _IdleTeardownWorkerProcess,
    _ProtocolWorkerProcess,
    _fast_settings,
    _register_tfm_shutdown,
    _shutdown_all_fake_workers,
)


def _track_stdin_writes(proc: _ProtocolWorkerProcess) -> List[str]:
    history: List[str] = []
    real_write = proc.stdin.write

    def _write(data: str) -> int:
        if data:
            history.append(data)
        return real_write(data)

    proc.stdin.write = _write  # type: ignore[method-assign]
    return history


def _synth_line_count(lines: List[str]) -> int:
    n = 0
    for line in lines:
        kind, _ = parse_inbound_line(line, max_text_chars=240)
        if kind == "synth":
            n += 1
    return n


def _finish_registry_cleanup() -> None:
    import src.voice.cosyvoice.client as client_mod

    inst = client_mod._ACTIVE_CLIENT
    if inst is not None:
        status = inst.shutdown(timeout=1.0)
        if not status.complete:
            inst.continue_shutdown(timeout=2.0)
    _shutdown_all_fake_workers(join_timeout=2.0)
    if client_mod._ACTIVE_CLIENT is not None:
        raise AssertionError("CosyVoice _ACTIVE_CLIENT 未通过正常 shutdown 释放")


class _FakeCosyClient:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.synth_calls = 0
        self._stop_failed = False

    @property
    def stop_failed(self) -> bool:
        return self._stop_failed

    def prepare(self, *, cancel_event=None) -> bool:
        self.prepare_calls += 1
        if cancel_event is not None and cancel_event.is_set():
            return False
        return True

    def synthesize(self, *args, **kwargs) -> bool:
        self.synth_calls += 1
        return True

    def interrupt(self) -> bool:
        return False

    def shutdown(self, timeout: float = 2.0):
        from src.voice.cosyvoice.client import CosyVoiceShutdownStatus

        return CosyVoiceShutdownStatus(
            complete=True,
            worker_alive=False,
            reader_alive=False,
            writer_alive=False,
        )

    def continue_shutdown(self, timeout: float = 2.0):
        return self.shutdown(timeout=timeout)

    def retire_worker(self, timeout: float = 2.0):
        return self.shutdown(timeout=timeout)


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


def _make_cosy_engine(
    tmp: str,
    *,
    client: _FakeCosyClient | None = None,
    engine: str = "cosyvoice",
    cloud_tts: bool = False,
) -> tuple[TTSEngine, _FakeCosyClient | None]:
    perms = {
        "local_tts_worker": True,
        "file_delete": True,
        "cloud_tts": cloud_tts,
    }
    policy, _ = _policy_with_local_tts(tmp, **perms)
    fake = client or _FakeCosyClient()
    patches = [
        patch("src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}),
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
        eng = TTSEngine(
            engine=engine,
            safety_policy=policy,
            warm_rvc_on_init=False,
            rvc_config={"model_path": str(Path(tmp) / "m.pth")},
        )
        if engine == "cosyvoice":
            assert eng._cosy_backend is not None
            eng._cosy_backend._client = fake  # type: ignore[assignment]
    finally:
        for p in reversed(patches):
            p.stop()
    return eng, fake if engine == "cosyvoice" else None


class CosyVoiceClientPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pin = patch(
            "src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}
        )
        self._pin.start()

    def tearDown(self) -> None:
        self._pin.stop()
        _finish_registry_cleanup()

    def _client_with_proc(
        self, tmp: str, proc: _ProtocolWorkerProcess
    ) -> tuple[CosyVoiceWorkerClient, TempFileManager, list[int], list[int]]:
        spawns: list[int] = []

        def _popen(*_a, **_k):
            spawns.append(1)
            proc.start()
            return proc

        policy, _ = _policy_with_local_tts(tmp)
        bind_fake_local_tts_runners(policy, popen_fn=_popen)
        tfm = TempFileManager(safety_policy=policy)
        creates: list[int] = []
        real_create = tfm.create

        def _track_create(*args, **kwargs):
            creates.append(1)
            return real_create(*args, **kwargs)

        tfm.create = _track_create  # type: ignore[method-assign]
        client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
        _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
        return client, tfm, spawns, creates

    def test_prepare_spawns_once_no_synth_no_tfm_create(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess()
            stdin_history = _track_stdin_writes(proc)
            client, _tfm, spawns, creates = self._client_with_proc(tmp, proc)
            self.assertTrue(client.prepare())
            self.assertEqual(spawns, [1])
            self.assertEqual(creates, [])
            self.assertEqual(_synth_line_count(stdin_history), 0)

    def test_prepare_rejects_known_dead_endpoints(self) -> None:
        from unittest.mock import Mock

        class Proc:
            def __init__(self, alive: bool) -> None:
                self.alive = alive
                self.stdin = Mock()
                self.stdout = None
                self.stderr = None

            def poll(self):
                return None if self.alive else 0

            def terminate(self) -> None:
                self.alive = False

            def kill(self) -> None:
                self.alive = False

            def wait(self, timeout=None):
                self.alive = False
                return 0

        class Endpoint:
            def __init__(self, alive: bool, fatal: bool = False) -> None:
                self.alive = alive
                self.fatal = fatal

            def is_alive(self) -> bool:
                return self.alive

            def had_fatal_protocol_error(self) -> bool:
                return self.fatal

            def close(self) -> None:
                self.alive = False

            def join(self, timeout=None) -> bool:
                self.alive = False
                return True

        cases = (
            ("dead_proc", False, True, True, False),
            ("dead_reader", True, False, True, False),
            ("dead_writer", True, True, False, False),
            ("fatal_reader", True, True, True, True),
        )
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy)
            _register_tfm_shutdown(self, tfm)
            for _label, proc_alive, reader_alive, writer_alive, fatal in cases:
                client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
                client._proc = Proc(proc_alive)  # noqa: SLF001
                client._reader = Endpoint(reader_alive, fatal)  # noqa: SLF001
                client._stdin_writer = Endpoint(writer_alive)  # noqa: SLF001
                client._ready = True  # noqa: SLF001
                with patch.object(
                    client,
                    "_ensure_worker_started",
                    return_value=True,
                ):
                    self.assertFalse(client.prepare())
                status = client.shutdown(timeout=0.5)
                if not status.complete:
                    client.continue_shutdown(timeout=1.0)

    def test_prepare_reuses_healthy_worker(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess()
            client, _tfm, spawns, _creates = self._client_with_proc(tmp, proc)
            self.assertTrue(client.prepare())
            self.assertTrue(client.prepare())
            self.assertEqual(spawns, [1])

    def test_prepare_rejects_while_synth_busy(self) -> None:
        with TemporaryDirectory() as tmp:
            hold = threading.Event()
            proc = _ProtocolWorkerProcess(hold_synth=hold)
            client, tfm, _spawns, _creates = self._client_with_proc(tmp, proc)
            out = tfm.create(".wav")
            results: list[bool] = []

            def _synth() -> None:
                results.append(
                    client.synthesize(
                        "段",
                        style_id="warm_companion",
                        output_path=out,
                    )
                )

            t = threading.Thread(target=_synth)
            t.start()
            time.sleep(0.12)
            self.assertFalse(client.prepare())
            hold.set()
            t.join(timeout=3.0)
            self.assertTrue(results[0])

    def test_synth_rejects_during_prepare(self) -> None:
        hold_ready = threading.Event()

        class _BlockedReadyWorker(_ProtocolWorkerProcess):
            def _emit_ready(self) -> None:
                hold_ready.wait(timeout=2.0)
                super()._emit_ready()

        with TemporaryDirectory() as tmp:
            proc = _BlockedReadyWorker()
            client, tfm, _spawns, _creates = self._client_with_proc(tmp, proc)
            prep_ok: list[bool] = []

            def _prep() -> None:
                prep_ok.append(client.prepare())

            t = threading.Thread(target=_prep)
            t.start()
            time.sleep(0.12)
            out = tfm.create(".wav")
            self.assertFalse(
                client.synthesize(
                    "段",
                    style_id="warm_companion",
                    output_path=out,
                )
            )
            hold_ready.set()
            t.join(timeout=5.0)
            self.assertEqual(len(prep_ok), 1)
            self.assertTrue(prep_ok[0])

    def test_prepare_permission_denied(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = SafetyPolicy(
                {
                    "permissions": {
                        "local_tts_worker": False,
                        "file_delete": True,
                    }
                }
            )
            tfm = TempFileManager(safety_policy=policy)
            proc = _ProtocolWorkerProcess()

            def _popen(*_a, **_k):
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            with patch(
                "src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}
            ):
                client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            self.assertFalse(client.prepare())

    def test_prepare_honours_external_cancel(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess(ready_delay=0.3)
            client, _tfm, spawns, _ = self._client_with_proc(tmp, proc)
            cancel = threading.Event()
            cancel.set()
            self.assertFalse(client.prepare(cancel_event=cancel))
            self.assertEqual(spawns, [])

    def test_prepare_cancel_during_startup(self) -> None:
        gate = threading.Event()
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess(
                spawn_gate=gate,
                ready_delay=2.0,
                ready_delay_after_start=False,
            )
            stdin_history = _track_stdin_writes(proc)
            client, _tfm, _spawns, creates = self._client_with_proc(tmp, proc)
            cancel = threading.Event()
            prep_ok: list[bool] = []

            def _prep() -> None:
                prep_ok.append(client.prepare(cancel_event=cancel))

            t = threading.Thread(target=_prep)
            t.start()
            time.sleep(0.08)
            gate.set()
            time.sleep(0.12)
            cancel.set()
            t.join(timeout=5.0)
            self.assertEqual(len(prep_ok), 1)
            self.assertFalse(prep_ok[0])
            self.assertEqual(creates, [])
            self.assertEqual(_synth_line_count(stdin_history), 0)

    def test_prepare_interrupt_during_startup(self) -> None:
        enter = threading.Event()
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess(ready_delay=2.0)
            stdin_history = _track_stdin_writes(proc)
            policy, _ = _policy_with_local_tts(tmp)

            def _popen(*_a, **_k):
                enter.set()
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            prep_ok: list[bool] = []

            def _prep() -> None:
                prep_ok.append(client.prepare())

            t = threading.Thread(target=_prep)
            t.start()
            self.assertTrue(enter.wait(2.0))
            time.sleep(0.05)
            client.interrupt()
            t.join(timeout=5.0)
            self.assertEqual(len(prep_ok), 1)
            self.assertFalse(prep_ok[0])
            self.assertFalse(client._ready)
            self.assertEqual(_synth_line_count(stdin_history), 0)

    def test_prepare_shutdown_before_ready_then_retry(self) -> None:
        allow_popen_return = threading.Event()
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess(ready_delay=2.0)
            policy, _ = _policy_with_local_tts(tmp)
            enter = threading.Event()

            def _popen(*_a, **_k):
                enter.set()
                allow_popen_return.wait(timeout=5.0)
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            prep_ok: list[bool] = []

            def _prep() -> None:
                prep_ok.append(client.prepare())

            t = threading.Thread(target=_prep)
            t.start()
            self.assertTrue(enter.wait(2.0))
            client.shutdown(timeout=0.5)
            allow_popen_return.set()
            t.join(timeout=5.0)
            self.assertEqual(len(prep_ok), 1)
            self.assertFalse(prep_ok[0])
            proc.stop(join_timeout=2.0)
            import src.voice.cosyvoice.client as client_mod

            if client_mod._ACTIVE_CLIENT is client:
                client.shutdown(timeout=2.0)
            proc2 = _ProtocolWorkerProcess()
            policy2, _ = _policy_with_local_tts(tmp)

            def _popen2(*_a, **_k):
                proc2.start()
                return proc2

            bind_fake_local_tts_runners(policy2, popen_fn=_popen2)
            tfm2 = TempFileManager(safety_policy=policy2)
            client2 = CosyVoiceWorkerClient(_fast_settings(), policy2, tfm2)
            _register_tfm_shutdown(self, tfm2, client=client2, procs=[proc2])
            self.assertTrue(client2.prepare())

    def test_prepare_startup_timeout(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess(ready_delay=3.0)
            stdin_history = _track_stdin_writes(proc)
            policy, _ = _policy_with_local_tts(tmp)

            def _popen(*_a, **_k):
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            settings = CosyVoiceSettings(
                startup_timeout_sec=1,
                synth_timeout_sec=10,
                idle_timeout_sec=60,
                max_text_chars=_fast_settings().max_text_chars,
                cpu_threads=4,
                min_free_ram_mb=8192,
                min_free_vram_mb=4096,
                cuda_memory_limit_mb=4096,
            )
            client = CosyVoiceWorkerClient(settings, policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            t0 = time.monotonic()
            self.assertFalse(client.prepare())
            self.assertLess(time.monotonic() - t0, 2.5)
            self.assertEqual(_synth_line_count(stdin_history), 0)

    def test_prepare_spawn_permission_denied(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy)
            client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client)

            def _deny(_launch):
                raise PermissionError("spawn denied")

            with patch.object(policy, "spawn_local_tts_worker", side_effect=_deny):
                self.assertFalse(client.prepare())

    def test_prepare_after_close_rejects(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess()
            client, _tfm, _s, _ = self._client_with_proc(tmp, proc)
            client.shutdown(timeout=0.5)
            self.assertFalse(client.prepare())

    def test_idle_retire_before_fresh_prepare_spawn(self) -> None:
        clock = _AdvanceableClock()
        with TemporaryDirectory() as tmp:
            procs: list[_IdleTeardownWorkerProcess] = []

            def _popen(*_a, **_k):
                p = _IdleTeardownWorkerProcess()
                procs.append(p)
                p.start()
                return p

            policy, _ = _policy_with_local_tts(tmp)
            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            idle = CosyVoiceSettings(
                startup_timeout_sec=5,
                synth_timeout_sec=10,
                idle_timeout_sec=10,
                max_text_chars=_fast_settings().max_text_chars,
                cpu_threads=4,
                min_free_ram_mb=8192,
                min_free_vram_mb=4096,
                cuda_memory_limit_mb=4096,
            )
            client = CosyVoiceWorkerClient(idle, policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=procs)
            out = tfm.create(".wav")
            with patch("src.voice.cosyvoice.client._MONOTONIC", clock):
                self.assertTrue(
                    client.synthesize(
                        "一",
                        style_id="warm_companion",
                        output_path=out,
                    )
                )
                clock.advance(9.6)
                self.assertTrue(client.prepare())
            self.assertEqual(len(procs), 2)


class CosyVoiceBackendPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pin = patch(
            "src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}
        )
        self._pin.start()

    def tearDown(self) -> None:
        self._pin.stop()
        _finish_registry_cleanup()

    def test_backend_prepare_uses_gate_without_create(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess()
            policy, _ = _policy_with_local_tts(tmp)

            def _popen(*_a, **_k):
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            creates: list[int] = []
            real_create = tfm.create

            def _track(*a, **k):
                creates.append(1)
                return real_create(*a, **k)

            tfm.create = _track  # type: ignore[method-assign]
            with patch(
                "src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}
            ):
                backend = CosyVoiceTtsBackend(
                    _fast_settings(), policy, tfm, client=None
                )
            _register_tfm_shutdown(self, tfm, client=backend.client, procs=[proc])
            self.assertTrue(backend.prepare())
            self.assertEqual(creates, [])
            self.assertEqual(backend.inflight_calls(), 0)


class TTSEnginePrepareLocalTtsTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeAudioPlayer.init_calls = 0

    def test_edge_returns_false_without_client(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch(
                "src.voice.tts.cosyvoice_backend.CosyVoiceWorkerClient"
            ) as client_cls:
                eng, _ = _make_cosy_engine(tmp, engine="edge-tts", cloud_tts=True)
                self.assertFalse(eng.prepare_local_tts())
                client_cls.assert_not_called()

    def test_prepare_does_not_call_rvc_or_edge(self) -> None:
        with TemporaryDirectory() as tmp:
            fake = _FakeCosyClient()
            with patch.object(TTSEngine, "prepare_rvc") as prep_rvc:
                eng, fake = _make_cosy_engine(tmp, client=fake)
                self.assertTrue(eng.prepare_local_tts())
                prep_rvc.assert_not_called()
            self.assertEqual(fake.prepare_calls, 1)
            self.assertEqual(fake.synth_calls, 0)

    def test_constructor_does_not_prepare(self) -> None:
        with TemporaryDirectory() as tmp:
            fake = _FakeCosyClient()
            _make_cosy_engine(tmp, client=fake)
            self.assertEqual(fake.prepare_calls, 0)

    def test_stale_epoch_rejected_without_clearing_external_cancel(self) -> None:
        with TemporaryDirectory() as tmp:
            fake = _FakeCosyClient()
            eng, _ = _make_cosy_engine(tmp, client=fake)
            external = threading.Event()
            eng._speak_lock.acquire()  # noqa: SLF001
            result: list[bool] = []

            def _prep() -> None:
                result.append(eng.prepare_local_tts(cancel_event=external))

            t = threading.Thread(target=_prep)
            t.start()
            time.sleep(0.05)
            eng.interrupt()
            eng._speak_lock.release()  # noqa: SLF001
            t.join(timeout=3.0)
            self.assertEqual(result, [False])
            self.assertEqual(fake.prepare_calls, 0)
            self.assertFalse(external.is_set())

    def test_prepare_clears_engine_interrupt_for_new_round(self) -> None:
        with TemporaryDirectory() as tmp:
            fake = _FakeCosyClient()
            eng, _ = _make_cosy_engine(tmp, client=fake)
            eng._interrupt_event.set()  # noqa: SLF001
            self.assertTrue(eng.prepare_local_tts())
            self.assertFalse(eng._interrupt_event.is_set())  # noqa: SLF001

    def test_merged_cancel_includes_shutdown(self) -> None:
        with TemporaryDirectory() as tmp:
            fake = _FakeCosyClient()

            def _prep(*, cancel_event=None):
                fake.prepare_calls += 1
                return not (cancel_event is not None and cancel_event.is_set())

            fake.prepare = _prep  # type: ignore[method-assign]
            eng, _ = _make_cosy_engine(tmp, client=fake)
            eng._shutdown_event.set()  # noqa: SLF001
            self.assertFalse(eng.prepare_local_tts())

    def test_speak_lock_busy_returns_false_immediately(self) -> None:
        with TemporaryDirectory() as tmp:
            fake = _FakeCosyClient()
            eng, _ = _make_cosy_engine(tmp, client=fake)
            eng._speak_lock.acquire()  # noqa: SLF001
            try:
                t0 = time.monotonic()
                self.assertFalse(eng.prepare_local_tts())
                self.assertLess(time.monotonic() - t0, 0.25)
                self.assertEqual(fake.prepare_calls, 0)
            finally:
                eng._speak_lock.release()  # noqa: SLF001

    def test_shutdown_during_prepare_retains_tfm_for_retry(self) -> None:
        hold_prep = threading.Event()
        release_prep = threading.Event()
        fake = _FakeCosyClient()

        def _blocking_prepare(*, cancel_event=None) -> bool:
            fake.prepare_calls += 1
            hold_prep.set()
            release_prep.wait(timeout=3.0)
            return cancel_event is None or not cancel_event.is_set()

        fake.prepare = _blocking_prepare  # type: ignore[method-assign]

        with TemporaryDirectory() as tmp:
            eng, _ = _make_cosy_engine(tmp, client=fake)
            self.addCleanup(eng.shutdown, stop_rvc=False)
            done: list[bool] = []

            def _prep() -> None:
                done.append(eng.prepare_local_tts())

            t = threading.Thread(target=_prep)
            t.start()
            self.assertTrue(hold_prep.wait(2.0))
            eng.shutdown(stop_rvc=False)
            self.assertFalse(eng._temp_files_shutdown_done)  # noqa: SLF001
            release_prep.set()
            t.join(timeout=3.0)
            self.assertIsNotNone(eng.local_cosyvoice_shutdown_status())


if __name__ == "__main__":
    unittest.main(verbosity=2)
