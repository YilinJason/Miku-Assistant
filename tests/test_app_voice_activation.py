"""App 语音候选所有权与 prepare 接线（纯 fake，走真实 App.ensure 路径）。"""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from src.core.assistant import MikuAssistant, VoiceStackLoadResult
from src.core.resource_settings import ResourceSettings
from src.core.safety import SafetyPolicy, TrustedRvcProfile
from src.core.shutdown_status import TtsShutdownStatus
from src.core.voice_activation import (
    bound_tts_models_ready,
    config_expects_edge_rvc_warmup,
    edge_rvc_warmup_required,
    warmup_registered_tts_candidate,
)


_SUPPORTED = ResourceSettings(
    serialize_gpu_warmup=True,
    warmup_order="ollama_then_rvc",
    ollama_warmup_on_start=True,
    rvc_warmup_on_voice_start=True,
    warmup_timeout=5.0,
)


class _CosyTts:
    engine_type = "cosyvoice"

    def __init__(self) -> None:
        self.prepare_calls = 0
        self._prepare_ok = True
        self.complete = False
        self.shutdown_count = 0

    def prepare_local_tts(self, *, cancel_event=None) -> bool:
        self.prepare_calls += 1
        if cancel_event is not None and cancel_event.is_set():
            return False
        return self._prepare_ok

    def prepare_rvc(self) -> bool:
        return True

    def shutdown(self, **kwargs) -> None:
        self.shutdown_count += 1

    def get_shutdown_status(self):
        return TtsShutdownStatus(
            complete=self.complete,
            local_tts_complete=self.complete,
            persistent_rvc_complete=True,
            synthesis_idle=True,
            temp_files_complete=True,
        )


def _policy(tmp: Path) -> SafetyPolicy:
    py = tmp / "python.exe"
    model = tmp / "m.pth"
    py.write_bytes(b"")
    model.write_bytes(b"x")
    return SafetyPolicy(
        {"permissions": {"rvc_worker": True, "local_tts_worker": True}},
        trusted_rvc_profile=TrustedRvcProfile(python_path=py, model_path=model),
    )


@unittest.skipUnless(__import__("sys").platform == "win32", "app voice activation")
class AppVoiceActivationTests(unittest.TestCase):
    def test_bound_cosy_not_ready_until_prepare_flag(self) -> None:
        tts = _CosyTts()
        self.assertFalse(bound_tts_models_ready(tts, local_prepare_ok=False))
        self.assertTrue(bound_tts_models_ready(tts, local_prepare_ok=True))

    def test_bound_edge_rvc_not_ready_until_attempt_completed(self) -> None:
        from types import SimpleNamespace

        from src.voice.tts.engine import RvcPrepareState

        class _Edge:
            engine_type = "edge-tts"

            def __init__(self, state: RvcPrepareState) -> None:
                self._rvc = SimpleNamespace(enabled=True)
                self._state = state

            def get_rvc_prepare_state(self) -> RvcPrepareState:
                return self._state

        pending = RvcPrepareState(
            enabled=True, inflight=False, attempt_completed=False, ready=False
        )
        tts = _Edge(pending)
        self.assertFalse(
            bound_tts_models_ready(
                tts, local_prepare_ok=True, resource_settings=_SUPPORTED
            )
        )
        done = RvcPrepareState(
            enabled=True, inflight=False, attempt_completed=True, ready=False
        )
        self.assertTrue(
            bound_tts_models_ready(
                _Edge(done), local_prepare_ok=True, resource_settings=_SUPPORTED
            )
        )

    def test_declared_rvc_state_reader_error_fail_closed(self) -> None:
        from types import SimpleNamespace

        class _BrokenEdge:
            engine_type = "edge-tts"

            def __init__(self) -> None:
                self._rvc = SimpleNamespace(enabled=True)

            def get_rvc_prepare_state(self):
                raise RuntimeError("controlled metadata failure")

        self.assertFalse(
            bound_tts_models_ready(
                _BrokenEdge(),
                local_prepare_ok=True,
                resource_settings=_SUPPORTED,
            )
        )

    def test_config_get_protocol_expects_edge_rvc_warmup(self) -> None:
        class _ConfigProtocol:
            def __init__(self) -> None:
                self.data = {
                    "voice": {
                        "tts": {
                            "engine": "edge-tts",
                            "rvc": {"model_path": "fake.pth"},
                        }
                    }
                }

            def get(self, key: str, default=None):
                value = self.data
                for part in key.split("."):
                    if not isinstance(value, dict) or part not in value:
                        return default
                    value = value[part]
                return value

            def get_rvc_config(self):
                value = self.get("voice.tts.rvc", {})
                return dict(value) if value.get("model_path") else None

        self.assertTrue(
            config_expects_edge_rvc_warmup(_ConfigProtocol(), _SUPPORTED)
        )

    def test_bound_pyttsx3_skips_edge_rvc_warmup_and_foreground(self) -> None:
        from types import SimpleNamespace

        from src.core.voice_foreground import tts_foreground_bound_path

        offline = SimpleNamespace(
            engine_type="pyttsx3",
            _rvc=SimpleNamespace(enabled=True),
        )
        self.assertFalse(edge_rvc_warmup_required(offline, _SUPPORTED))
        cfg = {
            "voice": {
                "tts": {"engine": "edge-tts", "rvc": {"model_path": "m.pth"}},
            }
        }
        assistant = MikuAssistant.from_dependencies(
            cfg, safety_policy=SafetyPolicy(), tts_engine=offline
        )
        assistant.resource_settings = _SUPPORTED
        self.assertFalse(tts_foreground_bound_path(assistant))

    def test_ensure_tts_propagates_rvc_fallback_warning(self) -> None:
        from types import SimpleNamespace

        from src.voice.tts.engine import RvcPrepareState

        class _Edge:
            engine_type = "edge-tts"

            def __init__(self) -> None:
                self._rvc = SimpleNamespace(enabled=True)
                self._state = RvcPrepareState(
                    enabled=True,
                    inflight=False,
                    attempt_completed=True,
                    ready=False,
                    user_warning="Nahida unavailable",
                )

            def get_rvc_prepare_state(self) -> RvcPrepareState:
                return self._state

        cfg = {
            "voice": {
                "tts": {"engine": "edge-tts", "rvc": {"model_path": "m.pth"}},
            }
        }
        edge = _Edge()
        assistant = MikuAssistant.from_dependencies(
            cfg, safety_policy=SafetyPolicy(), tts_engine=edge
        )
        assistant.resource_settings = _SUPPORTED
        result = assistant.ensure_tts_for_playback()
        self.assertTrue(result.ok)
        self.assertIn("Nahida unavailable", result.warnings)

    def test_ensure_voice_stack_prepares_bound_cosy(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {
                "voice": {"tts": {"engine": "cosyvoice", "cosyvoice": {}}},
                "resource": {"warmup": {}},
            }
            tts = _CosyTts()
            assistant = MikuAssistant.from_dependencies(
                cfg, safety_policy=policy, tts_engine=tts, stt_engine=MagicMock()
            )
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            with patch(
                "src.core.voice_activation.prepare_local_voice",
                return_value=MagicMock(ok=True, final_stage="completed"),
            ) as plv:
                result = assistant.ensure_voice_stack()
                plv.assert_called()
            self.assertTrue(result.ok)
            self.assertTrue(assistant._local_voice_prepare_ok)

    def test_bound_cosy_prepare_failure_blocks_auto_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
            tts = _CosyTts()
            assistant = MikuAssistant.from_dependencies(
                cfg, safety_policy=policy, tts_engine=tts, stt_engine=MagicMock()
            )
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            fail = MagicMock(ok=False, final_stage="rvc")
            with patch(
                "src.core.voice_activation.prepare_local_voice",
                return_value=fail,
            ) as plv:
                first = assistant.ensure_voice_stack()
                second = assistant.ensure_voice_stack()
                self.assertEqual(plv.call_count, 1)
            self.assertFalse(first.ok)
            self.assertFalse(second.ok)
            self.assertFalse(assistant._bound_tts_ready())

    def test_owner_slot_registered_before_build_returns(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
            assistant = MikuAssistant.from_dependencies(
                cfg, safety_policy=policy, stt_engine=MagicMock()
            )
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            gate = threading.Event()
            built = _CosyTts()

            def slow_build(*args, **kwargs):
                self.assertIsNotNone(assistant._voice_owner_slot)
                gate.set()
                time.sleep(0.05)
                return built

            with patch(
                "src.core.assistant.build_tts_engine", side_effect=slow_build
            ), patch(
                "src.core.voice_activation.prepare_local_voice",
                return_value=MagicMock(ok=True, final_stage="completed"),
            ):
                t = threading.Thread(target=assistant.ensure_tts_for_playback)
                t.start()
                self.assertTrue(gate.wait(1.0))
                self.assertIsNotNone(assistant._voice_owner_slot)
                t.join(5.0)
            self.assertIsNone(assistant._voice_owner_slot)
            self.assertIs(assistant.tts_engine, built)

    def test_stt_failure_retains_owner_until_complete(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
            assistant = MikuAssistant.from_dependencies(cfg, safety_policy=policy)
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            engine = _CosyTts()
            engine.complete = False
            builds = {"n": 0}

            def build_tts(*a, **k):
                builds["n"] += 1
                return engine

            with patch("src.core.assistant.build_tts_engine", side_effect=build_tts), patch(
                "src.core.assistant.build_stt_engine",
                side_effect=RuntimeError("fake STT failure"),
            ):
                result = assistant.ensure_voice_stack()
            self.assertFalse(result.ok)
            self.assertEqual(builds["n"], 1)
            self.assertIsNotNone(assistant._voice_owner_slot)
            retry = assistant.ensure_tts_for_playback(allow_retry=True)
            self.assertFalse(retry.ok)
            self.assertEqual(builds["n"], 1)
            engine.complete = True
            assistant.shutdown()
            assistant.shutdown()
            self.assertIsNone(assistant._voice_owner_slot)

    def test_shutdown_status_error_does_not_block_memory_step(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
            assistant = MikuAssistant.from_dependencies(cfg, safety_policy=policy)
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            engine = _CosyTts()
            engine.complete = False

            def boom():
                raise ValueError("status probe failed")

            engine.get_shutdown_status = boom  # type: ignore[method-assign]

            with patch(
                "src.core.assistant.build_tts_engine", return_value=engine
            ), patch(
                "src.core.assistant.build_stt_engine",
                side_effect=RuntimeError("stt fail"),
            ):
                assistant.ensure_voice_stack()
            memory = MagicMock(is_closed=False)
            close_status = MagicMock(
                worker_alive=False, timed_out=False, drained=True, pending=0
            )
            memory.close.return_value = close_status
            assistant.memory_runtime = memory
            assistant.shutdown()
            memory.close.assert_called()

    def test_shutdown_wakes_loader_waiter_under_300ms(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "edge-tts"}}}
            assistant = MikuAssistant.from_dependencies(cfg, safety_policy=policy)
            assistant._voice_autoload_enabled = True
            assistant._voice_loading = True
            done = threading.Event()

            def waiter() -> None:
                with assistant._voice_loader_lock:
                    assistant._wait_while_voice_loading_locked(label="test")
                done.set()

            t = threading.Thread(target=waiter)
            t.start()
            time.sleep(0.05)
            with assistant._lifecycle_lock:
                assistant._shutting_down = True
                assistant._voice_load_cancel.set()
            assistant._wake_voice_loaders()
            self.assertTrue(done.wait(0.3))
            t.join(2.0)

    def test_repeat_ensure_false_until_explicit_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
            tts = _CosyTts()
            assistant = MikuAssistant.from_dependencies(
                cfg, safety_policy=policy, tts_engine=tts, stt_engine=MagicMock()
            )
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            fail = MagicMock(ok=False, final_stage="cosyvoice")
            with patch(
                "src.core.voice_activation.prepare_local_voice",
                return_value=fail,
            ) as plv:
                assistant.ensure_voice_stack()
                assistant.ensure_voice_stack()
                self.assertEqual(plv.call_count, 1)
            with patch(
                "src.core.voice_activation.prepare_local_voice",
                return_value=MagicMock(ok=True, final_stage="completed"),
            ):
                ok = assistant.ensure_tts_for_playback(allow_retry=True)
            self.assertTrue(ok.ok)
            self.assertTrue(assistant._bound_tts_ready())

    def test_edge_uses_coordinated_warmup_not_prepare_local(self) -> None:
        tts = MagicMock(engine_type="edge-tts")
        log = MagicMock()
        with patch(
            "src.core.voice_activation.run_coordinated_warmup"
        ) as coord, patch("src.core.voice_activation.prepare_local_voice") as plv:
            ok, errs, warns = warmup_registered_tts_candidate(
                llm_client=MagicMock(),
                tts_engine=tts,
                settings=_SUPPORTED,
                cancel_event=None,
                log=log,
            )
        self.assertTrue(ok)
        self.assertEqual(errs, [])
        self.assertEqual(warns, [])
        coord.assert_called_once()
        plv.assert_not_called()

    def test_both_missing_success_does_not_shutdown_published_tts(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice", "cosyvoice": {}}}}
            assistant = MikuAssistant.from_dependencies(cfg, safety_policy=policy)
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            tts = _CosyTts()
            stt = MagicMock()
            with patch(
                "src.core.assistant.build_tts_engine", return_value=tts
            ), patch(
                "src.core.assistant.build_stt_engine", return_value=stt
            ), patch(
                "src.core.voice_activation.prepare_local_voice",
                return_value=MagicMock(ok=True, final_stage="completed"),
            ):
                result = assistant.ensure_voice_stack()
            self.assertTrue(result.ok)
            self.assertIs(assistant.tts_engine, tts)
            self.assertIs(assistant.stt_engine, stt)
            self.assertEqual(tts.shutdown_count, 0)

    def test_bound_cosy_missing_stt_builds_stt(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice", "cosyvoice": {}}}}
            tts = _CosyTts()
            assistant = MikuAssistant.from_dependencies(
                cfg, safety_policy=policy, tts_engine=tts
            )
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            stt = MagicMock()
            builds = {"stt": 0}

            def build_stt(*a, **k):
                builds["stt"] += 1
                return stt

            with patch(
                "src.core.voice_activation.prepare_local_voice",
                return_value=MagicMock(ok=True, final_stage="completed"),
            ), patch("src.core.assistant.build_stt_engine", side_effect=build_stt):
                result = assistant.ensure_voice_stack()
            self.assertTrue(result.ok)
            self.assertEqual(builds["stt"], 1)
            self.assertIs(assistant.stt_engine, stt)
            self.assertTrue(assistant._bound_tts_ready())

    def test_sticky_recheck_after_lane_wait_blocks_second_prepare(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
            tts = _CosyTts()
            assistant = MikuAssistant.from_dependencies(
                cfg, safety_policy=policy, tts_engine=tts, stt_engine=MagicMock()
            )
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            gate = threading.Event()
            calls = {"n": 0}
            fail = MagicMock(ok=False, final_stage="cosyvoice")

            def slow_prepare(*a, **k):
                calls["n"] += 1
                gate.set()
                time.sleep(0.15)
                return fail

            outcomes: list[VoiceStackLoadResult] = []

            def run_ensure() -> None:
                outcomes.append(assistant.ensure_voice_stack())

            with patch(
                "src.core.voice_activation.prepare_local_voice",
                side_effect=slow_prepare,
            ):
                t1 = threading.Thread(target=run_ensure)
                t2 = threading.Thread(target=run_ensure)
                t1.start()
                self.assertTrue(gate.wait(1.0))
                t2.start()
                t1.join(3.0)
                t2.join(3.0)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(len(outcomes), 2)
            self.assertTrue(all(not x.ok for x in outcomes))

    def test_stt_fail_after_ready_tts_does_not_block_tts_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
            tts = _CosyTts()
            assistant = MikuAssistant.from_dependencies(
                cfg, safety_policy=policy, tts_engine=tts
            )
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            with patch(
                "src.core.voice_activation.prepare_local_voice",
                return_value=MagicMock(ok=True, final_stage="completed"),
            ), patch(
                "src.core.assistant.build_stt_engine",
                side_effect=RuntimeError("stt once"),
            ):
                first = assistant.ensure_voice_stack()
            self.assertFalse(first.ok)
            self.assertTrue(assistant._bound_tts_ready())
            self.assertFalse(assistant._tts_load_failed)
            stt = MagicMock()
            with patch("src.core.assistant.build_stt_engine", return_value=stt):
                second = assistant.ensure_voice_stack()
            self.assertTrue(second.ok)
            self.assertIs(assistant.stt_engine, stt)


if __name__ == "__main__":
    unittest.main()
