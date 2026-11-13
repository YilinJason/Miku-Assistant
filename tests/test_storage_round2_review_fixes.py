"""Round 2 Codex review / final 收口：离线契约回归（Event 控制 / fake HTTP）。"""
from __future__ import annotations

import gc
import logging
import math
import threading
import time
import unittest
import weakref
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from src.core.assembly import build_voice_engines, _shutdown_engine
from src.core.assistant import MikuAssistant, VoiceStackLoadResult
from src.core.dispatch_outcome import DispatchOutcome
from src.core.perf_timing import DEFAULT_MAX_STAGED_RECORDS, SessionTimer
from src.core.resource_coord import run_coordinated_warmup
from src.core.resource_settings import ResourceSettings, parse_resource_settings
from src.core.resource_soak import (
    SoakSample,
    assert_stable_upper_bounds,
    collect_assistant_queue_depths,
    sample_resource_snapshot,
)
from src.core.router import IntentRouter
from src.llm.ollama_client import OllamaClient


class _Cfg:
    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self.data = data or {
            "ollama": {"warmup_on_start": True, "warmup_timeout": 2},
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
            "assistant": {"auto_tts": True},
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

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        cur = self.data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    def get_rvc_config(self) -> Optional[dict]:
        return None


class TrackingTTS:
    created = 0

    def __init__(self, **kwargs: Any):
        TrackingTTS.created += 1
        self.shutdown_calls = 0
        self.stop_rvc_flags: List[bool] = []
        self.prepare_calls = 0
        self.kwargs = kwargs
        self.speak_calls = 0

    def prepare_rvc(self) -> bool:
        self.prepare_calls += 1
        return True

    def shutdown(self, *, stop_rvc: bool = True) -> None:
        self.shutdown_calls += 1
        self.stop_rvc_flags.append(stop_rvc)

    def speak(self, text: str) -> None:
        self.speak_calls += 1


class GateSTT:
    def __init__(self, enter: threading.Event, leave: threading.Event, **kwargs: Any):
        enter.set()
        leave.wait(timeout=5.0)
        if kwargs.get("fail"):
            raise RuntimeError("stt-fail")

    def is_ready(self) -> bool:
        return True

    def check_microphone(self):
        return True, "ok"


def _make_autoload_assistant(
    *,
    tts_cls: Any = TrackingTTS,
    stt_cls: Any = None,
) -> MikuAssistant:
    parts = MagicMock()
    parts.config = _Cfg()
    parts.safety_policy = MagicMock()
    parts.log_file = None
    parts.llm_client = MagicMock(
        start_warmup=MagicMock(return_value=False),
        wait_for_warmup=MagicMock(return_value=True),
    )
    parts.tts_engine = None
    parts.stt_engine = None
    parts.windows_controller = None
    parts.chrome_searcher = None
    parts.character_interface = None
    parts.file_cleaner = None
    parts.router = IntentRouter([])
    parts.logger = None
    parts.memory_runtime = None
    parts.startup_mode = "interactive"
    parts.resource_settings = parse_resource_settings(_Cfg())
    parts.voice_autoload_enabled = True
    parts.tts_engine_cls = tts_cls
    parts.stt_engine_cls = stt_cls or (
        lambda **k: MagicMock(
            is_ready=lambda: True, check_microphone=lambda: (True, "ok")
        )
    )
    return MikuAssistant(_parts=parts)


class Finding1LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        TrackingTTS.created = 0

    def test_build_voice_engines_stt_fail_shuts_tts(self) -> None:
        tts_holder: List[TrackingTTS] = []

        def tts_cls(**kwargs: Any) -> TrackingTTS:
            obj = TrackingTTS(**kwargs)
            tts_holder.append(obj)
            return obj

        def stt_cls(**kwargs: Any) -> Any:
            raise RuntimeError("stt explode")

        with self.assertRaises(RuntimeError):
            build_voice_engines(
                _Cfg(),
                MagicMock(),
                tts_engine_cls=tts_cls,
                stt_engine_cls=stt_cls,
            )
        self.assertEqual(len(tts_holder), 1)
        self.assertEqual(tts_holder[0].shutdown_calls, 1)

    def test_tts_then_voice_creates_tts_once(self) -> None:
        """已有 TTS、仅缺 STT 时不得再构造/预热/关闭 TTS。"""
        stt_created = {"n": 0}

        def stt_cls(**kwargs: Any) -> Any:
            stt_created["n"] += 1
            return MagicMock(
                is_ready=lambda: True, check_microphone=lambda: (True, "ok")
            )

        assistant = _make_autoload_assistant(stt_cls=stt_cls)
        r1 = assistant.ensure_tts_for_playback()
        self.assertTrue(r1.ok)
        bound = assistant.tts_engine
        self.assertIsNotNone(bound)
        self.assertEqual(TrackingTTS.created, 1)

        r2 = assistant.ensure_voice_stack()
        self.assertTrue(r2.ok)
        self.assertIs(assistant.tts_engine, bound)
        self.assertEqual(TrackingTTS.created, 1)
        self.assertEqual(stt_created["n"], 1)
        self.assertEqual(bound.shutdown_calls, 0)  # type: ignore[union-attr]
        self.assertEqual(bound.prepare_calls, 1)  # type: ignore[union-attr]

        assistant.shutdown()
        self.assertEqual(bound.shutdown_calls, 1)  # type: ignore[union-attr]

    def test_voice_first_then_tts_idempotent(self) -> None:
        assistant = _make_autoload_assistant()
        self.assertTrue(assistant.ensure_voice_stack().ok)
        self.assertEqual(TrackingTTS.created, 1)
        bound = assistant.tts_engine
        self.assertTrue(assistant.ensure_tts_for_playback().ok)
        self.assertIs(assistant.tts_engine, bound)
        self.assertEqual(TrackingTTS.created, 1)

    def test_concurrent_tts_and_voice_single_flight(self) -> None:
        assistant = _make_autoload_assistant()
        results: List[VoiceStackLoadResult] = []
        barrier = threading.Barrier(2)

        def load_tts() -> None:
            barrier.wait(timeout=5.0)
            results.append(assistant.ensure_tts_for_playback())

        def load_voice() -> None:
            barrier.wait(timeout=5.0)
            results.append(assistant.ensure_voice_stack())

        threads = [
            threading.Thread(target=load_tts),
            threading.Thread(target=load_voice),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(TrackingTTS.created, 1)
        self.assertEqual(assistant.tts_engine.shutdown_calls, 0)  # type: ignore[union-attr]

    def test_ensure_shutdown_race_discards_unpublished_tts(self) -> None:
        enter = threading.Event()
        leave = threading.Event()
        tts_objs: List[TrackingTTS] = []

        def tts_cls(**kwargs: Any) -> TrackingTTS:
            enter.set()
            leave.wait(timeout=5.0)
            obj = TrackingTTS(**kwargs)
            tts_objs.append(obj)
            return obj

        assistant = _make_autoload_assistant(tts_cls=tts_cls)
        result_box: Dict[str, Any] = {}

        def loader() -> None:
            result_box["r"] = assistant.ensure_tts_for_playback()

        t = threading.Thread(target=loader)
        t.start()
        self.assertTrue(enter.wait(timeout=5.0))
        assistant.shutdown()
        leave.set()
        t.join(timeout=5.0)
        self.assertIsNotNone(result_box.get("r"))
        self.assertFalse(result_box["r"].ok)
        self.assertIsNone(assistant.tts_engine)
        self.assertTrue(tts_objs)
        self.assertEqual(tts_objs[0].shutdown_calls, 1)
        # 本轮唯一未发布 TTS：须 stop_rvc 以回收可能已预热的 Worker
        self.assertEqual(tts_objs[0].stop_rvc_flags, [True])


class Finding2AutoTtsTests(unittest.TestCase):
    def test_process_text_loads_tts_when_requested(self) -> None:
        TrackingTTS.created = 0

        class _R:
            def dispatch_outcome(self, text: str):
                return DispatchOutcome(text="hello-reply", allow_tts=True, allow_character=False)

        parts_cfg = _Cfg()
        assistant = MikuAssistant.from_dependencies(
            config=parts_cfg,
            router=_R(),  # type: ignore[arg-type]
        )
        assistant._voice_autoload_enabled = True
        assistant._tts_engine_cls = TrackingTTS
        assistant._stt_engine_cls = lambda **k: MagicMock()
        self.assertIsNone(assistant.tts_engine)
        assistant.process_text_input("hi", use_tts=True)
        self.assertIsNotNone(assistant.tts_engine)
        self.assertGreaterEqual(TrackingTTS.created, 1)
        self.assertGreaterEqual(assistant.tts_engine.speak_calls, 1)  # type: ignore[union-attr]

    def test_shutdown_blocks_tts_autoload(self) -> None:
        assistant = MikuAssistant.from_dependencies(config=_Cfg(), router=IntentRouter([]))
        assistant._voice_autoload_enabled = True
        assistant._tts_engine_cls = TrackingTTS
        assistant.shutdown()
        r = assistant.ensure_tts_for_playback()
        self.assertFalse(r.ok)

    def test_ensure_after_shutdown_fails_even_if_engine_bound(self) -> None:
        TrackingTTS.created = 0
        assistant = _make_autoload_assistant()
        self.assertTrue(assistant.ensure_tts_for_playback().ok)
        self.assertIsNotNone(assistant.tts_engine)
        assistant.shutdown()
        self.assertIsNotNone(assistant.tts_engine)
        r = assistant.ensure_tts_for_playback()
        self.assertFalse(r.ok)
        self.assertTrue(any("关闭" in e for e in r.errors))
        self.assertEqual(TrackingTTS.created, 1)

    def test_explicit_retry_after_factory_failure(self) -> None:
        TrackingTTS.created = 0
        calls = {"n": 0}

        def flaky_tts(**kwargs: Any) -> TrackingTTS:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first-fail")
            return TrackingTTS(**kwargs)

        assistant = _make_autoload_assistant(tts_cls=flaky_tts)
        first = assistant.ensure_tts_for_playback()
        self.assertFalse(first.ok)
        blocked = assistant.ensure_tts_for_playback()
        self.assertFalse(blocked.ok)
        self.assertTrue(any("显式重试" in e for e in blocked.errors))
        second = assistant.ensure_tts_for_playback(allow_retry=True)
        self.assertTrue(second.ok)
        self.assertIsNotNone(assistant.tts_engine)
        self.assertEqual(calls["n"], 2)


class Finding3SerializeWaitTests(unittest.TestCase):
    def test_wait_false_skips_rvc(self) -> None:
        class LLM:
            def start_warmup(self, *, timeout=None):
                return True

            def wait_for_warmup(self, *, timeout=None):
                return False

        tts = TrackingTTS()
        events: List[str] = []
        run_coordinated_warmup(
            llm_client=LLM(),
            tts_engine=tts,
            settings=ResourceSettings(serialize_gpu_warmup=True),
            events=events,
        )
        self.assertIn("ollama_wait_timeout", events)
        self.assertIn("rvc_skipped_serialize", events)
        self.assertEqual(tts.prepare_calls, 0)

    def test_wait_exception_skips_rvc_no_raise(self) -> None:
        class LLM:
            def start_warmup(self, *, timeout=None):
                return True

            def wait_for_warmup(self, *, timeout=None):
                raise RuntimeError("wait boom")

        tts = TrackingTTS()
        events: List[str] = []
        run_coordinated_warmup(
            llm_client=LLM(),
            tts_engine=tts,
            settings=ResourceSettings(serialize_gpu_warmup=True),
            events=events,
        )
        self.assertIn("ollama_wait_failed", events)
        self.assertEqual(tts.prepare_calls, 0)

    def test_keyboardinterrupt_propagates(self) -> None:
        class LLM:
            def start_warmup(self, *, timeout=None):
                return True

            def wait_for_warmup(self, *, timeout=None):
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run_coordinated_warmup(
                llm_client=LLM(),
                tts_engine=TrackingTTS(),
                settings=ResourceSettings(serialize_gpu_warmup=True),
            )


class Finding4SoakTests(unittest.TestCase):
    def test_snapshot_observability_fields(self) -> None:
        snap = sample_resource_snapshot()
        self.assertIsInstance(snap.rss_available, bool)
        self.assertIsInstance(snap.children_available, bool)
        if not snap.rss_available:
            self.assertIsNone(snap.rss_bytes)
        if not snap.children_available:
            self.assertIsNone(snap.child_process_count)

    def test_real_queue_depths_and_weakref_close(self) -> None:
        class _R:
            def dispatch_outcome(self, text: str):
                return DispatchOutcome(text="x", allow_tts=False, allow_character=False)

        samples = [SoakSample("base", sample_resource_snapshot())]
        a = MikuAssistant.from_dependencies(config=_Cfg(), router=_R())  # type: ignore[arg-type]
        ref = weakref.ref(a)
        for i in range(200):
            a.process_text_input(f"u{i}", use_tts=False)
        depths = collect_assistant_queue_depths(a)
        samples.append(
            SoakSample("work", sample_resource_snapshot(queue_depths=depths))
        )
        a.shutdown()
        samples.append(
            SoakSample(
                "closed",
                sample_resource_snapshot(queue_depths=collect_assistant_queue_depths(a)),
            )
        )
        assert_stable_upper_bounds(samples, max_thread_growth=64, max_child_growth=8)
        del a
        gc.collect()
        self.assertIsNone(ref())


class Finding5PerfPathTests(unittest.TestCase):
    def test_production_request_emits_perf_without_body(self) -> None:
        records: List[str] = []

        class _Log(logging.Logger):
            def info(self, msg, *args, **kwargs):  # type: ignore[override]
                records.append(msg % args if args else str(msg))

        class _R:
            def dispatch_outcome(self, text: str):
                return DispatchOutcome(
                    text="SECRET_REPLY_BODY", allow_tts=False, allow_character=False
                )

        assistant = MikuAssistant.from_dependencies(
            config=_Cfg(), router=_R()  # type: ignore[arg-type]
        )
        assistant.logger = _Log("perf-test")  # type: ignore[assignment]
        assistant.process_text_input("SECRET_USER_PROMPT", use_tts=False)
        joined = "\n".join(records)
        self.assertIn("[Perf]", joined)
        self.assertIn("stage=request", joined)
        self.assertIn("cold=", joined)
        self.assertNotIn("SECRET_USER_PROMPT", joined)
        self.assertNotIn("SECRET_REPLY_BODY", joined)
        # 第二次为热
        assistant.process_text_input("again", use_tts=False)
        cold_flags = [ln for ln in records if "stage=request" in ln]
        self.assertIn("cold=True", cold_flags[0])
        self.assertIn("cold=False", cold_flags[1])

    def test_session_timer_hard_cap_over_capacity(self) -> None:
        cap = 8
        timer = SessionTimer(max_staged_records=cap)
        for i in range(10_000):
            tid = timer.new_turn_id()
            timer.record_stage("request", 1.0, turn_id=tid, counts_as_request=True)
        self.assertLessEqual(len(timer.stages()), cap)
        self.assertEqual(timer._turn_seq, 10_000)
        self.assertFalse(timer.cold_start)
        with self.assertRaises(ValueError):
            SessionTimer(max_staged_records=0)
        with self.assertRaises(ValueError):
            SessionTimer(max_staged_records=True)  # type: ignore[arg-type]
        self.assertEqual(DEFAULT_MAX_STAGED_RECORDS, 64)


class Finding6FiniteTimeoutTests(unittest.TestCase):
    def test_rejects_non_finite(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf"), True, "1", 0, -1):
            cfg = _Cfg()
            cfg.set("resource.warmup.timeout", bad)
            with self.assertRaises(ValueError):
                parse_resource_settings(cfg)

    def test_accepts_finite(self) -> None:
        cfg = _Cfg()
        cfg.set("resource.warmup.timeout", 12)
        self.assertEqual(parse_resource_settings(cfg).warmup_timeout, 12.0)
        cfg.set("resource.warmup.timeout", 3.5)
        self.assertTrue(math.isfinite(parse_resource_settings(cfg).warmup_timeout))


class Finding7UnloadSemanticsTests(unittest.TestCase):
    def test_never_touched_skips_http(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        posted: List[Any] = []

        class _Resp:
            def raise_for_status(self) -> None:
                return None

        client._http_post = lambda *a, **k: posted.append(k) or _Resp()  # type: ignore[attr-defined]
        self.assertFalse(client.release_owned_model())
        self.assertEqual(posted, [])

    def test_touched_sends_keep_alive_zero(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        client._mark_model_touched()
        posted: List[Any] = []

        class _Resp:
            def raise_for_status(self) -> None:
                return None

        def fake_post(url, json=None, timeout=0, **kwargs):
            self.assertIs(kwargs.get("allow_redirects"), False)
            posted.append(json)
            return _Resp()

        client._http_post = fake_post  # type: ignore[attr-defined]
        self.assertTrue(client.release_owned_model())
        self.assertEqual(posted[0]["keep_alive"], 0)

    def test_low_vram_does_not_imply_unload(self) -> None:
        cfg = _Cfg()
        cfg.set("resource.profile", "low_vram")
        # omit unload_on_exit
        settings = parse_resource_settings(cfg)
        self.assertFalse(settings.ollama_unload_on_exit)


class FinalF1AShutdownPrewarmTests(unittest.TestCase):
    """F1-A：shutdown 竞态须回收已预热的本轮唯一 TTS/RVC Worker。"""

    def test_shutdown_during_prewarm_stops_worker(self) -> None:
        TrackingTTS.created = 0
        worker = {"alive": False}
        enter = threading.Event()
        leave = threading.Event()
        tts_objs: List[TrackingTTS] = []

        class PrewarmTTS(TrackingTTS):
            def prepare_rvc(self) -> bool:
                worker["alive"] = True
                self.prepare_calls += 1
                enter.set()
                leave.wait(timeout=5.0)
                return True

            def shutdown(self, *, stop_rvc: bool = True) -> None:
                self.shutdown_calls += 1
                self.stop_rvc_flags.append(stop_rvc)
                if stop_rvc:
                    worker["alive"] = False

        def tts_cls(**kwargs: Any) -> PrewarmTTS:
            obj = PrewarmTTS(**kwargs)
            tts_objs.append(obj)
            return obj

        assistant = _make_autoload_assistant(tts_cls=tts_cls)
        box: Dict[str, Any] = {}

        def loader() -> None:
            box["r"] = assistant.ensure_tts_for_playback()

        t = threading.Thread(target=loader)
        t.start()
        self.assertTrue(enter.wait(timeout=5.0))
        self.assertTrue(worker["alive"])
        assistant.shutdown()
        leave.set()
        t.join(timeout=5.0)
        self.assertFalse(box["r"].ok)
        self.assertIsNone(assistant.tts_engine)
        self.assertEqual(len(tts_objs), 1)
        self.assertEqual(tts_objs[0].shutdown_calls, 1)
        self.assertEqual(tts_objs[0].stop_rvc_flags, [True])
        self.assertFalse(worker["alive"])


class FinalF1BTransactionalVoiceTests(unittest.TestCase):
    """F1-B：两者皆缺时事务化；已有 TTS 时 STT 失败不得关掉正式 TTS。"""

    def test_both_missing_stt_failure_rolls_back_tts(self) -> None:
        TrackingTTS.created = 0
        tts_holder: List[TrackingTTS] = []

        def tts_cls(**kwargs: Any) -> TrackingTTS:
            obj = TrackingTTS(**kwargs)
            tts_holder.append(obj)
            return obj

        def boom_stt(**kwargs: Any) -> Any:
            raise RuntimeError("stt explode")

        assistant = _make_autoload_assistant(tts_cls=tts_cls, stt_cls=boom_stt)
        result = assistant.ensure_voice_stack()
        self.assertFalse(result.ok)
        self.assertIsNone(assistant.tts_engine)
        self.assertIsNone(assistant.stt_engine)
        self.assertEqual(len(tts_holder), 1)
        self.assertEqual(tts_holder[0].shutdown_calls, 1)
        self.assertEqual(tts_holder[0].stop_rvc_flags, [True])

    def test_existing_tts_stt_failure_keeps_published_tts(self) -> None:
        TrackingTTS.created = 0
        assistant = _make_autoload_assistant()
        self.assertTrue(assistant.ensure_tts_for_playback().ok)
        bound = assistant.tts_engine
        self.assertIsNotNone(bound)
        self.assertEqual(bound.shutdown_calls, 0)  # type: ignore[union-attr]

        def boom_stt(**kwargs: Any) -> Any:
            raise RuntimeError("stt explode")

        assistant._stt_engine_cls = boom_stt
        result = assistant.ensure_voice_stack()
        self.assertFalse(result.ok)
        self.assertIs(assistant.tts_engine, bound)
        self.assertEqual(bound.shutdown_calls, 0)  # type: ignore[union-attr]
        self.assertEqual(TrackingTTS.created, 1)


class FinalF1CWaiterWakeTests(unittest.TestCase):
    """F1-C：shutdown 后等待者须被唤醒并在短界限内返回。"""

    def test_waiter_returns_promptly_after_shutdown(self) -> None:
        TrackingTTS.created = 0
        enter = threading.Event()
        leave = threading.Event()

        def slow_tts(**kwargs: Any) -> TrackingTTS:
            enter.set()
            leave.wait(timeout=10.0)
            return TrackingTTS(**kwargs)

        assistant = _make_autoload_assistant(tts_cls=slow_tts)
        first_box: Dict[str, Any] = {}
        second_box: Dict[str, Any] = {}

        def first_loader() -> None:
            first_box["r"] = assistant.ensure_tts_for_playback()

        def second_loader() -> None:
            second_box["r"] = assistant.ensure_tts_for_playback()

        t1 = threading.Thread(target=first_loader)
        t1.start()
        self.assertTrue(enter.wait(timeout=5.0))
        t2 = threading.Thread(target=second_loader)
        t2.start()
        # 给等待者进入 CV.wait 的时间
        time.sleep(0.05)
        assistant.shutdown()
        t2.join(timeout=0.3)
        self.assertFalse(t2.is_alive())
        self.assertIsNotNone(second_box.get("r"))
        self.assertFalse(second_box["r"].ok)
        self.assertTrue(any("关闭" in e for e in second_box["r"].errors))
        leave.set()
        t1.join(timeout=5.0)


class FinalF1DShutdownExactlyOnceTests(unittest.TestCase):
    """F1-D：内部 TypeError 不得导致第二次 shutdown 调用。"""

    def test_internal_typeerror_shutdown_exactly_once(self) -> None:
        class PartialBoom:
            def __init__(self) -> None:
                self.calls = 0

            def shutdown(self, *, stop_rvc: bool = True) -> None:
                self.calls += 1
                # 部分清理后抛内部 TypeError（签名本身兼容 stop_rvc）
                raise TypeError("internal after partial cleanup")

        engine = PartialBoom()
        _shutdown_engine(engine, stop_rvc=True)
        self.assertEqual(engine.calls, 1)

        class LegacyNoKw:
            def __init__(self) -> None:
                self.calls = 0

            def shutdown(self) -> None:
                self.calls += 1

        legacy = LegacyNoKw()
        _shutdown_engine(legacy, stop_rvc=False)
        self.assertEqual(legacy.calls, 1)


if __name__ == "__main__":
    unittest.main()
