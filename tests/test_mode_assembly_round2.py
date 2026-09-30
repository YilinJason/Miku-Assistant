"""Round 2：按模式装配与资源协调离线契约（fake，无真实设备/服务）。"""
from __future__ import annotations

import threading
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from src.core.assembly import assemble_default
from src.core.assistant import MikuAssistant, VoiceStackLoadResult
from src.core.perf_timing import SessionTimer
from src.core.resource_coord import run_coordinated_warmup
from src.core.resource_settings import ResourceSettings, parse_resource_settings
from src.core.resource_soak import (
    SoakSample,
    assert_stable_upper_bounds,
    collect_assistant_queue_depths,
    sample_resource_snapshot,
)
from src.core.startup_mode import normalize_startup_mode
from src.core.router import IntentRouter
from src.llm.ollama_client import OllamaClient


class _Cfg:
    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self.data = data or {
            "ollama": {
                "base_url": "http://x",
                "model": "m",
                "timeout": 1,
                "keep_alive": "30m",
                "warmup_on_start": True,
                "warmup_timeout": 3,
            },
            "voice": {
                "tts": {
                    "engine": "edge-tts",
                    "language": "zh-CN",
                    "rate": 150,
                    "volume": 0.8,
                    "voice": "v",
                    "fallback_enabled": True,
                    "fallback_engine": "pyttsx3",
                    "pipeline_enabled": True,
                },
                "stt": {
                    "engine": "speech_recognition",
                    "language": "zh-CN",
                    "energy_threshold": 1,
                },
            },
            "system": {"windows_control_enabled": False},
            "search": {"auto_open": False},
            "extensions": {"character_model": {"enabled": False}},
            "resource": {
                "profile": "balanced",
                "ollama": {"unload_on_exit": False},
                "warmup": {"serialize_gpu": True, "order": "ollama_then_rvc"},
            },
        }

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur if cur is not None else default

    def get_rvc_config(self) -> Optional[dict]:
        return None

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        cur = self.data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value


class TrackingTTS:
    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs
        self.prepare_calls = 0
        self.shutdown_calls = 0

    def prepare_rvc(self) -> bool:
        self.prepare_calls += 1
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class TrackingSTT:
    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs

    def is_ready(self) -> bool:
        return True

    def check_microphone(self):
        return True, "ok"


class FakeLLM:
    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs
        self.warmup_starts = 0
        self.release_calls = 0
        self._warmup_done = threading.Event()
        self._warmup_done.set()
        self.model = kwargs.get("model", "m")

    def start_warmup(self, *, timeout: Optional[float] = None) -> bool:
        self.warmup_starts += 1
        self._warmup_done.set()
        return True

    def wait_for_warmup(self, *, timeout: Optional[float] = None) -> bool:
        return True

    def release_owned_model(self) -> bool:
        self.release_calls += 1
        return True

    def model_touch_succeeded(self) -> bool:
        return bool(getattr(self, "_model_touch_succeeded", False))


class ModeAssemblyTests(unittest.TestCase):
    def _assemble(self, mode: str, **overrides: Any):
        tts_holder: List[TrackingTTS] = []
        stt_holder: List[TrackingSTT] = []

        def tts_cls(**kwargs: Any) -> TrackingTTS:
            obj = TrackingTTS(**kwargs)
            tts_holder.append(obj)
            return obj

        def stt_cls(**kwargs: Any) -> TrackingSTT:
            obj = TrackingSTT(**kwargs)
            stt_holder.append(obj)
            return obj

        events: List[str] = []
        parts = assemble_default(
            startup_mode=mode,
            config_loader=lambda _p: _Cfg(),
            logging_setup=lambda _c: None,
            dir_preparer=lambda: None,
            ollama_client_cls=FakeLLM,
            tts_engine_cls=tts_cls,
            stt_engine_cls=stt_cls,
            windows_controller_cls=lambda **k: None,
            chrome_searcher_cls=lambda **k: object(),
            character_interface_cls=lambda **k: None,
            file_cleaner_from_config=lambda *a, **k: None,
            router_builder=lambda **k: IntentRouter([]),
            start_cleaner=False,
            warmup_events=events,
            **overrides,
        )
        return parts, tts_holder, stt_holder, events

    def test_text_mode_skips_voice_stack(self) -> None:
        parts, tts, stt, events = self._assemble("text")
        self.assertEqual(parts.startup_mode, "text")
        self.assertIsNone(parts.tts_engine)
        self.assertIsNone(parts.stt_engine)
        self.assertEqual(tts, [])
        self.assertEqual(stt, [])
        self.assertTrue(parts.voice_autoload_enabled)
        self.assertIn("ollama_start", events)
        self.assertNotIn("rvc_ok", events)

    def test_interactive_defers_voice_stack(self) -> None:
        parts, tts, stt, _ = self._assemble("interactive")
        self.assertIsNone(parts.tts_engine)
        self.assertIsNone(parts.stt_engine)
        self.assertEqual(tts, [])
        self.assertTrue(parts.voice_autoload_enabled)

    def test_voice_mode_constructs_stack_without_init_rvc_warm(self) -> None:
        parts, tts, stt, events = self._assemble("voice")
        self.assertIsNotNone(parts.tts_engine)
        self.assertIsNotNone(parts.stt_engine)
        self.assertEqual(len(tts), 1)
        self.assertEqual(len(stt), 1)
        self.assertFalse(tts[0].kwargs.get("warm_rvc_on_init", True))
        self.assertGreaterEqual(tts[0].prepare_calls, 1)
        self.assertFalse(parts.voice_autoload_enabled)
        # 串行：ollama 先于 rvc
        self.assertLess(events.index("ollama_start"), events.index("rvc_ok"))
        self.assertIn("ollama_wait_completed", events)

    def test_invalid_startup_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_startup_mode("gpu")


class LazyVoiceLoadTests(unittest.TestCase):
    def test_ensure_voice_stack_autoload(self) -> None:
        parts, _, _, _ = ModeAssemblyTests()._assemble("interactive")
        assistant = MikuAssistant(_parts=parts)
        self.assertIsNone(assistant.tts_engine)
        result = assistant.ensure_voice_stack()
        self.assertIsInstance(result, VoiceStackLoadResult)
        self.assertTrue(result.ok)
        self.assertIsNotNone(assistant.tts_engine)
        self.assertIsNotNone(assistant.stt_engine)
        # 幂等
        self.assertTrue(assistant.ensure_voice_stack().ok)

    def test_from_dependencies_without_autoload_fails_structured(self) -> None:
        assistant = MikuAssistant.from_dependencies(config=_Cfg())
        result = assistant.ensure_voice_stack()
        self.assertFalse(result.ok)
        self.assertTrue(result.errors)
        self.assertIsNone(assistant.tts_engine)


class ResourceCoordTests(unittest.TestCase):
    def test_serialize_order_ollama_then_rvc(self) -> None:
        llm = FakeLLM()
        tts = TrackingTTS()
        events: List[str] = []
        settings = ResourceSettings(
            serialize_gpu_warmup=True,
            warmup_order="ollama_then_rvc",
            ollama_warmup_on_start=True,
            rvc_warmup_on_voice_start=True,
        )
        run_coordinated_warmup(
            llm_client=llm, tts_engine=tts, settings=settings, events=events
        )
        self.assertEqual(
            events[:3], ["ollama_start", "ollama_wait_completed", "rvc_ok"]
        )
        self.assertEqual(tts.prepare_calls, 1)
        self.assertEqual(llm.warmup_starts, 1)

    def test_parse_balanced_defaults(self) -> None:
        settings = parse_resource_settings(_Cfg())
        self.assertEqual(settings.profile, "balanced")
        self.assertTrue(settings.serialize_gpu_warmup)
        self.assertFalse(settings.ollama_unload_on_exit)

    def test_release_owned_model_fake_http(self) -> None:
        client = OllamaClient(base_url="http://fake-ollama", model="m", timeout=1)
        client._mark_model_touched()
        posted: List[Dict[str, Any]] = []

        class _Resp:
            def raise_for_status(self) -> None:
                return None

        def fake_post(url: str, json: Dict[str, Any] = None, timeout: float = 0):
            posted.append({"url": url, "json": json, "timeout": timeout})
            return _Resp()

        client._http_post = fake_post  # type: ignore[attr-defined]
        self.assertTrue(client.release_owned_model())
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0]["url"].endswith("/api/chat"))
        self.assertEqual(posted[0]["json"]["keep_alive"], 0)
        self.assertEqual(posted[0]["json"]["messages"], [])

    def test_unload_on_exit_via_shutdown(self) -> None:
        cfg = _Cfg()
        cfg.set("resource.ollama.unload_on_exit", True)
        llm = FakeLLM()
        llm._model_touch_succeeded = True  # type: ignore[attr-defined]
        assistant = MikuAssistant.from_dependencies(config=cfg, llm_client=llm)
        assistant.resource_settings = parse_resource_settings(cfg)
        assistant.shutdown()
        self.assertEqual(llm.release_calls, 1)


class PerfAndSoakTests(unittest.TestCase):
    def test_session_timer_logs_ids_not_body(self) -> None:
        timer = SessionTimer()
        turn = timer.new_turn_id()
        records: List[str] = []

        class _Log:
            def info(self, msg: str, *args: Any) -> None:
                records.append(msg % args if args else msg)

        with timer.measure("reply", turn_id=turn, logger=_Log(), counts_as_request=True):  # type: ignore[arg-type]
            pass
        self.assertTrue(records)
        joined = " ".join(records)
        self.assertIn(timer.session_id, joined)
        self.assertIn(turn, joined)
        self.assertIn("stage=reply", joined)
        self.assertNotIn("你好", joined)
        timer.record_stage("next", 1.0, turn_id=turn, logger=_Log(), counts_as_request=True)  # type: ignore[arg-type]
        self.assertFalse(timer.stages()[f"{turn}::next"].cold)
        # 非请求阶段不翻转
        timer2 = SessionTimer()
        timer2.record_stage("lazy", 1.0, logger=_Log(), counts_as_request=False)  # type: ignore[arg-type]
        self.assertTrue(timer2.cold_start)

    def test_offline_soak_bounds_text_turns_and_close(self) -> None:
        from src.core.dispatch_outcome import DispatchOutcome

        samples: List[SoakSample] = []
        samples.append(SoakSample("base", sample_resource_snapshot()))

        class _R:
            def dispatch_outcome(self, text: str):
                if text.startswith("fail"):
                    return DispatchOutcome.module_unavailable("boom")
                return DispatchOutcome(text="reply", allow_tts=False, allow_character=False)

        assistant = MikuAssistant.from_dependencies(
            config=_Cfg(),
            llm_client=MagicMock(),
            router=_R(),  # type: ignore[arg-type]
        )

        for i in range(10000):
            assistant.process_text_input(f"u{i}", use_tts=False)

        for i in range(1000):
            assistant.process_text_input(f"fail{i}", use_tts=False)
            assistant.interrupt_active_output()

        for _ in range(20):
            a = MikuAssistant.from_dependencies(config=_Cfg(), router=IntentRouter([]))
            a.shutdown()

        depths = collect_assistant_queue_depths(assistant)
        samples.append(
            SoakSample(
                "after",
                sample_resource_snapshot(queue_depths=depths),
            )
        )
        assistant.shutdown()
        samples.append(
            SoakSample(
                "closed",
                sample_resource_snapshot(
                    queue_depths=collect_assistant_queue_depths(assistant)
                ),
            )
        )
        assert_stable_upper_bounds(samples, max_thread_growth=48, max_child_growth=4)

    def test_cleaner_long_directory_respects_scan_bound(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from src.core.file_cleaner import CleanupRule, FileCleaner
        from src.core.safety import SafetyPolicy

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(300):
                (root / f"miku_{i}.wav").write_bytes(b"x")
            policy = SafetyPolicy()
            policy.register_file_delete_dir(root)
            cleaner = FileCleaner(
                rules=[
                    CleanupRule(
                        name="t",
                        directory=root,
                        patterns=["miku_*.wav"],
                        max_age_seconds=0.0,
                    )
                ],
                safety_policy=policy,
                max_scan_files=50,
                max_delete_files=20,
            )
            try:
                deleted = cleaner.cleanup_now()
                self.assertLessEqual(deleted, 20)
                remaining = list(root.glob("miku_*.wav"))
                self.assertGreaterEqual(len(remaining), 280)
            finally:
                # 关闭可能残留的活动 scandir，避免 ResourceWarning
                cleaner.stop_auto_cleanup()


if __name__ == "__main__":
    unittest.main()
