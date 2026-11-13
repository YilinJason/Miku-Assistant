"""Paraformer round-2/3 race, seal, and lifecycle regression (controlled schedules, no real models)."""
from __future__ import annotations

import socket
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import patch

from src.core.assembly import build_assembly_safety_policy
from src.core.config import Config
from src.core.safety import SafetyPolicy
from src.voice.paraformer.assets import (
    ASR_PYTHON_REL,
    MODEL_DIR_REL,
    MODEL_REQUIRED_FILES,
    RUNTIME_WORKER_SCRIPT_REL,
)
from src.voice.paraformer.client import ParaformerWorkerClient
from src.voice.paraformer.network_guard import (
    install_outbound_network_guard,
    restore_outbound_network_guard,
)
from src.voice.paraformer.settings import ParaformerSettings
from src.voice.recognition import RecognitionStatus
from src.voice.stt import STTEngine, _CombinedCancelView
from tests.test_cosyvoice_production_slice_a import _seed_repo


class _MemConfig:
    """Minimal config double matching assembly ``get`` / ``get_rvc_config`` contract."""

    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self.data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur if cur is not None else default

    def get_rvc_config(self) -> Optional[dict]:
        return None


def _base_cfg(**tts_overrides: Any) -> _MemConfig:
    tts: Dict[str, Any] = {
        "engine": "edge-tts",
        "language": "zh-CN",
        "rate": 150,
        "volume": 0.8,
        "voice": "v",
        "fallback_enabled": True,
        "fallback_engine": "pyttsx3",
        "pipeline_enabled": True,
    }
    tts.update(tts_overrides)
    return _MemConfig(
        {
            "voice": {"tts": tts, "stt": {"engine": "speech_recognition", "language": "zh-CN"}},
            "security": {
                "permissions": {
                    "local_tts_worker": False,
                    "local_asr_worker": False,
                    "file_delete": True,
                }
            },
        }
    )


def _seed_paraformer_repo(root: Path) -> None:
    for rel in (ASR_PYTHON_REL, RUNTIME_WORKER_SCRIPT_REL):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"py")
    model_root = root / MODEL_DIR_REL
    for rel in MODEL_REQUIRED_FILES:
        path = model_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"m")


class LiveProcess:
    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass


class ClientNoOwnerShutdownTests(unittest.TestCase):
    def test_no_owner_shutdown_completes_immediately(self) -> None:
        c = ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        ended = threading.Event()

        def run() -> None:
            self.assertTrue(c.shutdown(budget_sec=0.25))
            ended.set()

        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(ended.wait(0.7), "no-owner shutdown blocked")
        t.join(2.0)
        self.assertFalse(t.is_alive())

    def test_incomplete_owner_then_quiet_retry_completes(self) -> None:
        c = ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        proc = LiveProcess()
        c._proc = proc
        self.assertFalse(c.shutdown(budget_sec=0.25))
        proc.poll = lambda: 0
        self.assertTrue(c.shutdown(budget_sec=0.25))

    def test_contended_shutdown_threads_join_without_deadlock(self) -> None:
        c = ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        proc = LiveProcess()
        c._proc = proc
        proc.poll = lambda: 0

        def run() -> None:
            c.shutdown(budget_sec=0.35)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(2.0)
            self.assertFalse(th.is_alive())


class SpawnShutdownRaceTests(unittest.TestCase):
    def test_shutdown_during_spawn_not_complete(self) -> None:
        c = ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        entered, release = threading.Event(), threading.Event()

        def spawn(_launch):
            entered.set()
            release.wait(2.0)
            return LiveProcess()

        c._spawn_fn = spawn
        errors: list[BaseException] = []

        def run_transcribe() -> None:
            try:
                c.transcribe(b"\x00\x01")
            except BaseException as exc:
                errors.append(exc)

        with patch(
            "src.voice.paraformer.client.check_model_startup_memory",
            return_value=SimpleNamespace(allowed=True),
        ):
            t = threading.Thread(target=run_transcribe)
            t.start()
            try:
                self.assertTrue(entered.wait(1.0))
                self.assertFalse(c.shutdown(budget_sec=0.25))
            finally:
                release.set()
                t.join(2.0)
        self.assertEqual(errors, [])
        self.assertFalse(t.is_alive())


class PartialIoStartTests(unittest.TestCase):
    def test_reader_start_failure_retains_process(self) -> None:
        c = ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        proc = LiveProcess()
        proc.stdin = SimpleNamespace()
        proc.stdout = SimpleNamespace()
        c._spawn_fn = lambda _launch: proc
        with patch(
            "src.voice.paraformer.client.check_model_startup_memory",
            return_value=SimpleNamespace(allowed=True),
        ), patch("src.voice.paraformer.client.ParaformerStdoutReader") as reader_cls:
            reader_cls.return_value.start.side_effect = RuntimeError("reader start")
            out = c.transcribe(b"\x00\x01")
        self.assertFalse(out.ok)
        self.assertIs(c._proc, proc)

    def test_writer_start_failure_retains_process(self) -> None:
        c = ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        proc = LiveProcess()
        proc.stdin = SimpleNamespace()
        proc.stdout = SimpleNamespace()
        c._spawn_fn = lambda _launch: proc
        with patch(
            "src.voice.paraformer.client.check_model_startup_memory",
            return_value=SimpleNamespace(allowed=True),
        ), patch("src.voice.paraformer.client.CosyVoiceStdinWriter") as writer_cls:
            writer_cls.return_value.start.side_effect = RuntimeError("writer start")
            out = c.transcribe(b"\x00\x01")
        self.assertFalse(out.ok)
        self.assertIs(c._proc, proc)


class CombinedCancelTests(unittest.TestCase):
    def test_combined_view_or_engine_shutdown(self) -> None:
        caller = threading.Event()
        engine = threading.Event()
        view = _CombinedCancelView(caller, engine)
        self.assertFalse(view.is_set())
        engine.set()
        self.assertTrue(view.is_set())
        self.assertFalse(caller.is_set())

    def test_engine_shutdown_during_recording_calls_client(self) -> None:
        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        calls: list[str] = []

        class FakeClient:
            def shutdown(self, *, budget_sec=None):
                calls.append("shutdown")
                return False

        engine._paraformer_client = FakeClient()
        engine._recording = True
        self.assertFalse(engine.shutdown())
        engine._recording = False
        self.assertEqual(calls, ["shutdown"])

    def test_transcribe_sees_engine_shutdown_without_caller_cancel(self) -> None:
        from src.voice.paraformer.client import TranscribeOutcome

        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        caller = threading.Event()
        observed: list[bool] = []
        case = self

        class FakeClient:
            def transcribe(self, pcm, *, cancel_event=None):
                case.assertFalse(caller.is_set())
                case.assertFalse(engine._shutdown_cancel.is_set())
                case.assertIsNotNone(cancel_event)
                case.assertFalse(cancel_event.is_set())
                engine._shutdown_cancel.set()
                case.assertTrue(cancel_event.is_set())
                case.assertFalse(caller.is_set())
                observed.append(cancel_event.is_set())
                return TranscribeOutcome(False, "", False, error_label="cancelled")

        engine._paraformer_client = FakeClient()
        result = engine._recognize_paraformer_result(
            SimpleNamespace(get_raw_data=lambda **_: b"\x00\x01"),
            cancel_event=caller,
        )
        self.assertEqual(observed, [True])
        self.assertNotEqual(result.status, RecognitionStatus.SUCCESS)
        self.assertEqual(result.status, RecognitionStatus.CANCELLED)


class SttShutdownPropagationTests(unittest.TestCase):
    def test_shutdown_during_transcription_suppresses_success(self) -> None:
        from src.voice.paraformer.client import TranscribeOutcome

        engine = STTEngine(
            engine="paraformer",
            safety_policy=SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        engine._recording = True

        def fake_transcribe(*_args, **_kwargs):
            self.assertFalse(engine.shutdown())
            return TranscribeOutcome(True, "stale answer", False)

        engine._paraformer_client = SimpleNamespace(transcribe=fake_transcribe)
        try:
            result = engine._recognize_paraformer_result(
                SimpleNamespace(get_raw_data=lambda **_: b"\x00\x01")
            )
        finally:
            engine._recording = False
        self.assertNotEqual(result.status, RecognitionStatus.SUCCESS)


class NetworkGuardTests(unittest.TestCase):
    def test_direct_connect_blocked_without_calling_original(self) -> None:
        with patch.object(socket.socket, "connect", return_value=None) as original:
            install_outbound_network_guard()
            try:
                with socket.socket() as sock:
                    with self.assertRaises(OSError):
                        sock.connect(("203.0.113.1", 9))
                original.assert_not_called()
            finally:
                restore_outbound_network_guard()


class AssemblyConditionalSealTests(unittest.TestCase):
    def _cfg_paraformer_enabled(self) -> _MemConfig:
        cfg = _base_cfg()
        cfg.data["voice"]["stt"] = {"engine": "paraformer", "language": "zh-CN"}
        cfg.data["security"]["permissions"]["local_asr_worker"] = True
        return cfg

    def test_missing_asr_layout_not_sealed_via_config_set_api(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            cfg = Config(str(config_path))
            cfg.set("voice.stt.engine", "paraformer")
            cfg.set("security.permissions.local_asr_worker", True)
            with patch("src.core.assembly.PROJECT_ROOT", root):
                policy = build_assembly_safety_policy(cfg, startup_mode="text")
            self.assertFalse(policy.local_asr_profile_sealed())

    def test_valid_asr_layout_sealed_in_text_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_paraformer_repo(root)
            cfg = self._cfg_paraformer_enabled()
            with patch("src.core.assembly.PROJECT_ROOT", root):
                policy = build_assembly_safety_policy(cfg, startup_mode="text")
            self.assertTrue(policy.local_asr_profile_sealed())

    def test_missing_asr_layout_does_not_block_policy_construction(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = self._cfg_paraformer_enabled()
            with patch("src.core.assembly.PROJECT_ROOT", Path(tmp)):
                policy = build_assembly_safety_policy(cfg, startup_mode="interactive")
            self.assertFalse(policy.local_asr_profile_sealed())

    def test_cosy_tts_still_sealed_in_text_mode_when_assets_present(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_repo(root)
            cfg = _base_cfg(engine="cosyvoice")
            cfg.data["security"]["permissions"]["local_tts_worker"] = True
            with patch("src.core.assembly.PROJECT_ROOT", root):
                policy = build_assembly_safety_policy(cfg, startup_mode="text")
            self.assertTrue(policy.local_tts_profile_sealed())

    def test_missing_asr_does_not_drop_valid_cosy_tts_seal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_repo(root)
            cfg = _base_cfg(engine="cosyvoice")
            cfg.data["voice"]["stt"] = {"engine": "paraformer", "language": "zh-CN"}
            perms = cfg.data["security"]["permissions"]
            perms["local_tts_worker"] = True
            perms["local_asr_worker"] = True
            with patch("src.core.assembly.PROJECT_ROOT", root):
                policy = build_assembly_safety_policy(cfg, startup_mode="text")
            self.assertTrue(policy.local_tts_profile_sealed())
            self.assertFalse(policy.local_asr_profile_sealed())


class DeadProcess:
    def poll(self):
        return 0


class _PendingIoOwner:
    """Faithful pending-I/O double: close does not clear alive; join succeeds only when quiet."""

    def __init__(self, alive_state: dict[str, bool]) -> None:
        self._alive_state = alive_state
        self.close_calls = 0
        self.join_calls = 0

    def is_alive(self) -> bool:
        return self._alive_state["alive"]

    def close(self) -> None:
        self.close_calls += 1

    def join(self, timeout: float) -> bool:
        self.join_calls += 1
        if not self._alive_state["alive"]:
            return True
        return False


class _PendingIoReader(_PendingIoOwner):
    def had_fatal_protocol_error(self) -> bool:
        return False


class _PendingIoWriter(_PendingIoOwner):
    pass


class PendingIoOwnershipTests(unittest.TestCase):
    def _transcribe_with_admission(self, client: ParaformerWorkerClient) -> Any:
        with patch(
            "src.voice.paraformer.client.check_model_startup_memory",
            return_value=SimpleNamespace(allowed=True),
        ):
            return client.transcribe(b"\x00\x01")

    def test_dead_process_unjoined_reader_refuses_spawn_and_retains_refs(self) -> None:
        spawns: list[int] = []
        c = ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        dead = DeadProcess()
        reader_state = {"alive": True}
        reader = _PendingIoReader(reader_state)
        c._proc = dead
        c._reader = reader

        def spawn(_launch):
            spawns.append(1)
            return LiveProcess()

        c._spawn_fn = spawn
        out = self._transcribe_with_admission(c)
        self.assertFalse(out.ok)
        self.assertEqual(spawns, [])
        self.assertIs(c._proc, dead)
        self.assertIs(c._reader, reader)
        self._transcribe_with_admission(c)
        self.assertEqual(spawns, [])

    def test_dead_process_unjoined_writer_refuses_spawn_and_retains_refs(self) -> None:
        spawns: list[int] = []
        c = ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        dead = DeadProcess()
        writer_state = {"alive": True}
        writer = _PendingIoWriter(writer_state)
        c._proc = dead
        c._writer = writer

        def spawn(_launch):
            spawns.append(1)
            return LiveProcess()

        c._spawn_fn = spawn
        out = self._transcribe_with_admission(c)
        self.assertFalse(out.ok)
        self.assertEqual(spawns, [])
        self.assertIs(c._proc, dead)
        self.assertIs(c._writer, writer)

    def test_quiet_unjoined_reader_allows_spawn_attempt(self) -> None:
        reader_state = {"alive": True}
        reader = _PendingIoReader(reader_state)
        spawns: list[int] = []
        c = ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        c._proc = DeadProcess()
        c._reader = reader

        def spawn(_launch):
            spawns.append(1)
            raise PermissionError("spawn blocked for test")

        c._spawn_fn = spawn
        self._transcribe_with_admission(c)
        self.assertEqual(spawns, [])
        reader_state["alive"] = False
        self._transcribe_with_admission(c)
        self.assertEqual(spawns, [1])

    def test_repeated_rejected_spawn_does_not_accumulate_threads(self) -> None:
        spawns: list[int] = []
        c = ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )
        c._proc = DeadProcess()
        reader_state = {"alive": True}
        c._reader = _PendingIoReader(reader_state)

        def spawn(_launch):
            spawns.append(1)
            return LiveProcess()

        c._spawn_fn = spawn
        before = threading.active_count()
        for _ in range(5):
            self._transcribe_with_admission(c)
        self.assertEqual(spawns, [])
        self.assertLessEqual(threading.active_count(), before + 2)


if __name__ == "__main__":
    unittest.main()
