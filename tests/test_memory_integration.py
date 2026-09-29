"""长期记忆产品接线与无历史提取适配器的纯离线测试。"""
from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, Iterator, List, Optional, Sequence

from src.core.assembly import assemble_default
from src.core.assistant import MikuAssistant
from src.core.handlers.llm_handler import LLMIntentHandler
from src.core.router import IntentRouter
from src.core.safety import SafetyPolicy
from src.llm.conversation_history import ConversationHistory
from src.llm.generate_result import GenerateResult, GenerateStatus
from src.llm.stream_events import StreamEvent
from src.memory import (
    ExtractedMemory,
    MemoryAwareLLMClient,
    MemoryCategory,
    MemoryContext,
    MemoryExtractionResponseError,
    MemoryExtractionTransportError,
    MemoryIngestionService,
    MemoryQueryService,
    MemoryRuntime,
    MemorySettings,
    MemoryTurnEvent,
    MemoryTurnOutcome,
    MemoryValidationError,
    NewMemory,
    OllamaMemoryExtractor,
    SqliteMemoryStore,
    parse_memory_settings,
)
from src.memory.service_models import empty_memory_context


class MemConfig:
    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self.data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur if cur is not None else default

    def resolve_path(self, path: Optional[str]) -> Optional[str]:
        return path

    def get_rvc_config(self) -> Optional[dict]:
        return None


def _cfg(*, memory_enabled: bool = False, **memory_overrides: Any) -> MemConfig:
    memory = {
        "enabled": memory_enabled,
        "db_path": None,
        "recall": {"max_entries": 4, "max_chars": 400, "candidate_limit": 20},
        "ingestion": {
            "queue_capacity": 4,
            "max_extractions_per_turn": 4,
            "close_drain": True,
            "close_timeout": 2.0,
        },
        "extractor": {
            "base_url": None,
            "model": None,
            "timeout": None,
            "keep_alive": None,
        },
    }
    memory.update(memory_overrides)
    return MemConfig(
        {
            "ollama": {
                "base_url": "http://x",
                "model": "m",
                "timeout": 1,
                "keep_alive": "30m",
                "warmup_on_start": False,
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
            "system": {"windows_control_enabled": False, "require_confirmation": True},
            "search": {
                "chrome_path": None,
                "search_engine": "google",
                "auto_open": False,
            },
            "extensions": {"character_model": {"enabled": False}},
            "memory": memory,
        }
    )


class FakeTransport:
    def __init__(self, response: Any = None, *, error: Optional[BaseException] = None):
        self.response = response
        self.error = error
        self.payloads: List[dict] = []

    def post_chat(self, payload: dict[str, Any]) -> Any:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return self.response


class FakeLLM:
    def __init__(self) -> None:
        self.history = ConversationHistory()
        self.calls: List[Dict[str, Any]] = []
        self.stream_events: List[StreamEvent] = [
            StreamEvent.text_delta("你好"),
            StreamEvent.completed("你好世界"),
        ]
        self.result = GenerateResult.success("助手回复")
        self.raise_on_result: Optional[BaseException] = None
        self.model = "fake-model"
        self.connection_calls = 0
        self.model_available_calls = 0
        self.list_models_calls = 0

    @property
    def conversation_history(self):
        return self.history.snapshot()

    def clear_history(self) -> None:
        self.history.clear()

    def generate_result(
        self, prompt: str, stream: bool = False, system: Optional[str] = None
    ) -> GenerateResult:
        self.calls.append({"prompt": prompt, "system": system, "stream": stream})
        if self.raise_on_result is not None:
            raise self.raise_on_result
        if self.result.is_success:
            self.history.commit_turn(prompt, self.result.text or "")
        return self.result

    def generate(
        self, prompt: str, stream: bool = False, system: Optional[str] = None
    ) -> str:
        raise AssertionError("inner generate should not be called by decorator")

    def generate_stream(
        self, prompt: str, system: Optional[str] = None
    ) -> Iterator[StreamEvent]:
        self.calls.append({"prompt": prompt, "system": system, "stream": True})
        yield from list(self.stream_events)

    def check_connection(self) -> bool:
        self.connection_calls += 1
        return True

    def check_model_available(self) -> bool:
        self.model_available_calls += 1
        return True

    def list_models(self) -> List[str]:
        self.list_models_calls += 1
        return ["fake-model", "other-model"]


class FakeQuery:
    def __init__(self, ctx: Optional[MemoryContext] = None, *, error: Optional[BaseException] = None):
        self.ctx = ctx or empty_memory_context()
        self.error = error
        self.queries: List[str] = []

    def build_context(self, query: str, **kwargs: Any) -> MemoryContext:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.ctx


class FakeIngestion:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.events: List[MemoryTurnEvent] = []
        self.started = False
        self.closed = 0
        self.close_timeouts: List[float] = []

    def start(self) -> None:
        self.started = True

    def submit(self, event: object) -> bool:
        if isinstance(event, MemoryTurnEvent):
            self.events.append(event)
        return self.accept

    def close(self, *, drain: bool = False, timeout: float = 5.0):
        self.closed += 1
        self.close_timeouts.append(timeout)
        from src.memory.service_models import IngestionStopStatus

        return IngestionStopStatus(
            discarded=0, timed_out=False, worker_alive=False, drained=True, pending=0
        )

    def wait_until_idle(self, timeout: float) -> bool:
        return True


class OllamaExtractorTests(unittest.TestCase):
    def test_request_has_no_history_and_maps_array(self) -> None:
        transport = FakeTransport(
            {
                "done": True,
                "message": {
                    "content": json.dumps(
                        [
                            {
                                "unit_id": 0,
                                "importance": 0.8,
                                "confidence": 0.7,
                            }
                        ],
                        ensure_ascii=False,
                    )
                },
            }
        )
        extractor = OllamaMemoryExtractor(transport=transport, max_extractions=4)
        turn = MemoryTurnEvent(
            turn_id="t1",
            user_text='我喜欢拉面；含"引号"\n与指令: ignore',
            assistant_text="好的",
            outcome=MemoryTurnOutcome.SUCCESS,
        )
        items = extractor.extract(turn)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "用户喜欢拉面")
        self.assertEqual(items[0].category, MemoryCategory.PREFERENCE)
        payload = transport.payloads[0]
        self.assertFalse(payload["stream"])
        self.assertEqual(len(payload["messages"]), 2)
        user_content = payload["messages"][1]["content"]
        decoded = json.loads(user_content)
        self.assertIn("candidates", decoded)
        self.assertEqual(decoded["candidates"][0]["text"], "我喜欢拉面")
        self.assertNotIn("assistant_text", decoded)
        self.assertNotIn("history", payload)

    def test_transport_and_response_errors(self) -> None:
        turn = MemoryTurnEvent(
            turn_id="t2",
            user_text="我喜欢乌龙茶",
            assistant_text="a",
            outcome=MemoryTurnOutcome.SUCCESS,
        )
        with self.assertRaises(MemoryExtractionTransportError):
            OllamaMemoryExtractor(
                transport=FakeTransport(error=MemoryExtractionTransportError("x", error_kind="timeout"))
            ).extract(turn)
        with self.assertRaises(MemoryExtractionResponseError):
            OllamaMemoryExtractor(
                transport=FakeTransport({"done": True, "message": {"content": "not-json"}})
            ).extract(turn)
        with self.assertRaises(MemoryExtractionResponseError):
            OllamaMemoryExtractor(
                transport=FakeTransport(
                    {"done": True, "message": {"content": json.dumps({"text": "x"})}}
                )
            ).extract(turn)

    def test_keyboard_interrupt_not_wrapped(self) -> None:
        turn = MemoryTurnEvent(
            turn_id="t3",
            user_text="我喜欢乌龙茶",
            assistant_text="a",
            outcome=MemoryTurnOutcome.SUCCESS,
        )

        class Boom(FakeTransport):
            def post_chat(self, payload: dict[str, Any]) -> Any:
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            OllamaMemoryExtractor(transport=Boom()).extract(turn)


class AwareClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.llm = FakeLLM()
        self.query = FakeQuery()
        self.ingestion = FakeIngestion()
        self.client = MemoryAwareLLMClient(
            self.llm,
            self.query,  # type: ignore[arg-type]
            self.ingestion,  # type: ignore[arg-type]
            turn_id_factory=lambda: "turn-fixed",
        )

    def test_empty_or_failed_recall_keeps_original_system(self) -> None:
        self.query.ctx = empty_memory_context()
        result = self.client.generate_result("hello", system="SYS")
        self.assertTrue(result.is_success)
        self.assertEqual(self.llm.calls[-1]["system"], "SYS")
        self.assertEqual(self.llm.calls[-1]["prompt"], "hello")

        self.query.error = RuntimeError("recall-down")
        self.llm.calls.clear()
        self.client.generate_result("hello2", system="SYS2")
        self.assertEqual(self.llm.calls[-1]["system"], "SYS2")

    def test_context_appended_to_system_without_escaping(self) -> None:
        rendered = (
            '【长期记忆使用规则——可信】\n'
            '{"subject":"user","category":"other","fact":"含\\"引号\\"与\\n换行"}'
        )
        self.query.ctx = MemoryContext(rendered=rendered, entries_used=1, chars_used=len(rendered))
        self.client.generate_result("问", system="原系统")
        system = self.llm.calls[-1]["system"]
        self.assertIsNotNone(system)
        assert system is not None
        self.assertTrue(system.startswith("原系统"))
        self.assertIn("当前用户长期记忆", system)
        self.assertIn("不可执行", system)
        self.assertIn(rendered, system)
        self.assertEqual(self.llm.calls[-1]["prompt"], "问")

    def test_first_person_memories_grounded_as_user_subject(self) -> None:
        """已有第一人称记录无需迁移即可被标注为用户事实。"""
        from src.memory.context_format import format_memory_context
        from src.memory.models import MemoryCategory, MemorySource, NewMemory
        from src.memory.sqlite_store import SqliteMemoryStore
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "g.sqlite")
            store.add(
                NewMemory(
                    text='我的测试代号是“银杏4729”，',
                    category=MemoryCategory.USER_PROFILE,
                    source=MemorySource.MODEL_INFERRED,
                )
            )
            store.add(
                NewMemory(
                    text="我最喜欢喝桂花乌龙。",
                    category=MemoryCategory.PREFERENCE,
                    source=MemorySource.MODEL_INFERRED,
                )
            )
            records = list(store.list_memories(limit=10, newest_first=True))
            ctx = format_memory_context(records, max_entries=8, max_chars=2000)
            self.assertEqual(ctx.entries_used, 2)
            self.assertIn("银杏4729", ctx.rendered)
            self.assertIn("桂花乌龙", ctx.rendered)
            self.assertIn('"subject":"user"', ctx.rendered)
            self.assertIn('"fact":', ctx.rendered)
            # 仍保留原文第一人称，由规则说明视角
            self.assertIn("我最喜欢喝桂花乌龙", ctx.rendered)
            self.assertNotIn("model_inferred", ctx.rendered)
            store.close()

        self.query.ctx = ctx
        result = self.client.generate_result(
            "这是一个全新的会话。你知道我的测试代号和最喜欢的饮品吗？",
            system="你是助手。",
        )
        system = self.llm.calls[-1]["system"]
        self.assertTrue(system.startswith("你是助手。"))
        self.assertIn("当前用户长期记忆", system)
        self.assertIn("银杏4729", system)
        self.assertIn("桂花乌龙", system)
        self.assertTrue(result.is_success)

    def test_non_stream_success_submits_once(self) -> None:
        self.client.generate_result("用户", system=None)
        self.assertEqual(len(self.ingestion.events), 1)
        self.assertEqual(self.ingestion.events[0].turn_id, "turn-fixed")
        self.assertEqual(self.ingestion.events[0].user_text, "用户")
        self.assertEqual(self.ingestion.events[0].assistant_text, "助手回复")

        self.llm.result = GenerateResult.empty()
        self.client.generate_result("空")
        self.assertEqual(len(self.ingestion.events), 1)

        self.llm.result = GenerateResult.failure(GenerateStatus.HTTP_ERROR, "boom")
        self.client.generate_result("失败")
        self.assertEqual(len(self.ingestion.events), 1)

    def test_stream_completed_submits_once_failures_do_not(self) -> None:
        list(self.client.generate_stream("流式"))
        self.assertEqual(len(self.ingestion.events), 1)
        self.assertEqual(self.ingestion.events[0].assistant_text, "你好世界")

        self.ingestion.events.clear()
        self.llm.stream_events = [
            StreamEvent.text_delta("半"),
            StreamEvent.failed(GenerateStatus.RESPONSE_ERROR, "bad"),
        ]
        list(self.client.generate_stream("失败流"))
        self.assertEqual(self.ingestion.events, [])

        self.llm.stream_events = [StreamEvent.text_delta("只有增量")]
        list(self.client.generate_stream("缺终结"))
        self.assertEqual(self.ingestion.events, [])

    def test_generate_compat_single_submit_preserves_history(self) -> None:
        text = self.client.generate("一轮")
        self.assertEqual(text, "助手回复")
        self.assertEqual(len(self.ingestion.events), 1)
        self.assertEqual(len(self.llm.history), 2)
        self.client.clear_history()
        self.assertEqual(len(self.llm.history), 0)

    def test_turn_id_factory_error_does_not_break_success(self) -> None:
        def boom_id() -> str:
            raise RuntimeError("id-boom")

        client = MemoryAwareLLMClient(
            self.llm,
            self.query,  # type: ignore[arg-type]
            self.ingestion,  # type: ignore[arg-type]
            turn_id_factory=boom_id,
        )
        result = client.generate_result("ok-prompt")
        self.assertTrue(result.is_success)
        self.assertEqual(result.text, "助手回复")
        self.assertEqual(self.ingestion.events, [])

        self.llm.stream_events = [
            StreamEvent.text_delta("a"),
            StreamEvent.completed("完整成功"),
        ]
        events = list(client.generate_stream("stream-ok"))
        self.assertEqual(events[-1].kind.name, "COMPLETED")
        self.assertEqual(events[-1].text, "完整成功")
        self.assertEqual(self.ingestion.events, [])

    def test_stream_early_close_closes_inner_once_without_submit(self) -> None:
        class CloseAwareStream:
            def __init__(self, events: List[StreamEvent]) -> None:
                self._events = list(events)
                self.close_calls = 0
                self._i = 0

            def __iter__(self):
                return self

            def __next__(self) -> StreamEvent:
                if self._i >= len(self._events):
                    raise StopIteration
                item = self._events[self._i]
                self._i += 1
                return item

            def close(self) -> None:
                self.close_calls += 1

        inner_stream = CloseAwareStream(
            [StreamEvent.text_delta("半"), StreamEvent.completed("不应提交")]
        )

        class StreamLLM(FakeLLM):
            def generate_stream(self, prompt: str, system: Optional[str] = None):
                self.calls.append({"prompt": prompt, "system": system, "stream": True})
                return inner_stream

        llm = StreamLLM()
        client = MemoryAwareLLMClient(
            llm,
            self.query,  # type: ignore[arg-type]
            self.ingestion,  # type: ignore[arg-type]
        )
        gen = client.generate_stream("early")
        self.assertEqual(next(gen).delta, "半")
        gen.close()
        self.assertEqual(inner_stream.close_calls, 1)
        self.assertEqual(self.ingestion.events, [])
        # 再 close 幂等：外层再次 close 不应重复触发多次底层（生成器已关）
        gen.close()
        self.assertEqual(inner_stream.close_calls, 1)


class CliCompatAndCloseTimeoutTests(unittest.TestCase):
    def test_aware_client_proxies_cli_preflight_surface(self) -> None:
        llm = FakeLLM()
        client = MemoryAwareLLMClient(
            llm,
            FakeQuery(),  # type: ignore[arg-type]
            FakeIngestion(),  # type: ignore[arg-type]
        )
        self.assertEqual(client.model, "fake-model")
        self.assertTrue(client.check_connection())
        self.assertTrue(client.check_model_available())
        self.assertEqual(client.list_models(), ["fake-model", "other-model"])
        self.assertEqual(llm.connection_calls, 1)
        self.assertEqual(llm.model_available_calls, 1)
        self.assertEqual(llm.list_models_calls, 1)

    def test_voice_preflight_works_with_wrapped_client(self) -> None:
        from src.cli.voice_preflight import check_voice_preflight

        class ReadySTT:
            def is_ready(self) -> bool:
                return True

            def check_microphone(self):
                return True, "ok"

        llm = FakeLLM()
        wrapped = MemoryAwareLLMClient(
            llm,
            FakeQuery(),  # type: ignore[arg-type]
            FakeIngestion(),  # type: ignore[arg-type]
        )
        assistant = MikuAssistant.from_dependencies(
            _cfg(memory_enabled=False),
            llm_client=wrapped,
            stt_engine=ReadySTT(),
            router=IntentRouter([]),
        )
        report = check_voice_preflight(assistant)
        self.assertTrue(report.ok)
        self.assertEqual(report.errors, [])
        self.assertEqual(llm.connection_calls, 1)
        self.assertEqual(llm.model_available_calls, 1)
        self.assertEqual(wrapped.model, "fake-model")

    def test_runtime_close_timeout_rejects_invalid_without_side_effects(self) -> None:
        store_closed = {"n": 0}

        class TrackingStore:
            def close(self) -> None:
                store_closed["n"] += 1

        ingestion = FakeIngestion()
        settings = parse_memory_settings(_cfg(memory_enabled=True))
        runtime = MemoryRuntime(
            TrackingStore(),  # type: ignore[arg-type]
            FakeQuery(),  # type: ignore[arg-type]
            ingestion,  # type: ignore[arg-type]
            settings=settings,
            owns_store=True,
        )
        for bad in (True, "1.0", float("nan"), float("inf"), -1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(MemoryValidationError):
                    runtime.close(timeout=bad)  # type: ignore[arg-type]
                self.assertEqual(ingestion.closed, 0)
                self.assertEqual(store_closed["n"], 0)
                self.assertFalse(runtime.is_closed)
                self.assertTrue(runtime.owns_store)

        status = runtime.close(timeout=1.5)
        self.assertTrue(status.drained)
        self.assertEqual(ingestion.close_timeouts, [1.5])
        self.assertEqual(store_closed["n"], 1)
        self.assertTrue(runtime.is_closed)

        ingestion_zero = FakeIngestion()
        store_zero = {"n": 0}

        class TrackingStoreZero:
            def close(self) -> None:
                store_zero["n"] += 1

        runtime_zero = MemoryRuntime(
            TrackingStoreZero(),  # type: ignore[arg-type]
            FakeQuery(),  # type: ignore[arg-type]
            ingestion_zero,  # type: ignore[arg-type]
            settings=settings,
            owns_store=True,
        )
        status_zero = runtime_zero.close(timeout=0.0)
        self.assertTrue(status_zero.drained)
        self.assertEqual(ingestion_zero.close_timeouts, [0.0])
        self.assertEqual(store_zero["n"], 1)
        self.assertTrue(runtime_zero.is_closed)


class AssemblyAndShutdownTests(unittest.TestCase):
    def test_disabled_memory_skips_factory(self) -> None:
        calls = {"factory": 0}

        def factory(*args: Any, **kwargs: Any) -> Any:
            calls["factory"] += 1
            raise AssertionError("factory should not run")

        class TrackingTTS:
            def __init__(self, **kwargs: Any):
                pass

            def shutdown(self) -> None:
                pass

        class TrackingSTT:
            def __init__(self, **kwargs: Any):
                pass

        parts = assemble_default(
            config_loader=lambda _p: _cfg(memory_enabled=False),
            logging_setup=lambda _c: None,
            dir_preparer=lambda: None,
            ollama_client_cls=lambda **kwargs: FakeLLM(),
            tts_engine_cls=TrackingTTS,
            stt_engine_cls=TrackingSTT,
            windows_controller_cls=lambda **kwargs: None,
            chrome_searcher_cls=lambda **kwargs: object(),
            character_interface_cls=lambda **kwargs: None,
            file_cleaner_from_config=lambda *a, **k: None,
            router_builder=lambda **kwargs: IntentRouter([]),
            start_cleaner=False,
            memory_runtime_factory=factory,
        )
        self.assertIsNone(parts.memory_runtime)
        self.assertEqual(calls["factory"], 0)
        self.assertNotIsInstance(parts.llm_client, MemoryAwareLLMClient)

    def test_enabled_path_wraps_and_invalid_config_before_devices(self) -> None:
        device_calls: List[str] = []

        class BoomTTS:
            def __init__(self, **kwargs: Any):
                device_calls.append("tts")

            def shutdown(self) -> None:
                device_calls.append("tts-shutdown")

        class TrackingSTT:
            def __init__(self, **kwargs: Any):
                device_calls.append("stt")

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "m.sqlite3"
            cfg = _cfg(memory_enabled=True)
            cfg.data["memory"]["db_path"] = str(db)

            class TrackingRuntime:
                def __init__(self) -> None:
                    self.started = False
                    self.closed = 0
                    self.store = object()
                    self.settings = parse_memory_settings(cfg)

                def wrap_llm(self, llm: Any) -> MemoryAwareLLMClient:
                    return MemoryAwareLLMClient(
                        llm,
                        FakeQuery(),  # type: ignore[arg-type]
                        FakeIngestion(),  # type: ignore[arg-type]
                    )

                def start(self) -> None:
                    self.started = True

                def close(self, **kwargs: Any) -> Any:
                    self.closed += 1
                    from src.memory.service_models import IngestionStopStatus

                    return IngestionStopStatus(
                        discarded=0,
                        timed_out=False,
                        worker_alive=False,
                        drained=True,
                        pending=0,
                    )

            runtime = TrackingRuntime()

            def factory(settings: MemorySettings, **kwargs: Any) -> Any:
                return runtime

            parts = assemble_default(
                config_loader=lambda _p: cfg,
                logging_setup=lambda _c: None,
                dir_preparer=lambda: None,
                ollama_client_cls=lambda **kwargs: FakeLLM(),
                tts_engine_cls=BoomTTS,
                stt_engine_cls=TrackingSTT,
                windows_controller_cls=lambda **kwargs: None,
                chrome_searcher_cls=lambda **kwargs: object(),
                character_interface_cls=lambda **kwargs: None,
                file_cleaner_from_config=lambda *a, **k: None,
                router_builder=lambda **kwargs: IntentRouter([]),
                start_cleaner=False,
                memory_runtime_factory=factory,
            )
            self.assertTrue(runtime.started)
            self.assertIsInstance(parts.llm_client, MemoryAwareLLMClient)
            self.assertIn("tts", device_calls)

            bad = _cfg(memory_enabled=True)
            bad.data["memory"]["recall"] = {"max_entries": 0, "max_chars": 10, "candidate_limit": 1}
            device_calls.clear()
            with self.assertRaises(MemoryValidationError):
                assemble_default(
                    config_loader=lambda _p: bad,
                    logging_setup=lambda _c: None,
                    dir_preparer=lambda: None,
                    ollama_client_cls=lambda **kwargs: FakeLLM(),
                    tts_engine_cls=BoomTTS,
                    stt_engine_cls=TrackingSTT,
                    windows_controller_cls=lambda **kwargs: None,
                    chrome_searcher_cls=lambda **kwargs: object(),
                    character_interface_cls=lambda **kwargs: None,
                    file_cleaner_from_config=lambda *a, **k: None,
                    router_builder=lambda **kwargs: IntentRouter([]),
                    start_cleaner=False,
                    memory_runtime_factory=factory,
                )
            self.assertEqual(device_calls, [])

    def test_rollback_closes_memory_and_shutdown_continues(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "m.sqlite3"
            cfg = _cfg(memory_enabled=True)
            cfg.data["memory"]["db_path"] = str(db)

            class RT:
                def __init__(self) -> None:
                    self.close_calls = 0
                    self.started = False

                def wrap_llm(self, llm: Any) -> Any:
                    return llm

                def start(self) -> None:
                    self.started = True

                def close(self, **kwargs: Any) -> Any:
                    self.close_calls += 1
                    from src.memory.service_models import IngestionStopStatus

                    return IngestionStopStatus(
                        discarded=0,
                        timed_out=False,
                        worker_alive=False,
                        drained=True,
                        pending=0,
                    )

            runtime = RT()

            class FailTTS:
                def __init__(self, **kwargs: Any):
                    raise RuntimeError("tts-fail")

                def shutdown(self) -> None:
                    pass

            with self.assertRaises(RuntimeError):
                assemble_default(
                    config_loader=lambda _p: cfg,
                    logging_setup=lambda _c: None,
                    dir_preparer=lambda: None,
                    ollama_client_cls=lambda **kwargs: FakeLLM(),
                    tts_engine_cls=FailTTS,
                    stt_engine_cls=lambda **kwargs: None,
                    windows_controller_cls=lambda **kwargs: None,
                    chrome_searcher_cls=lambda **kwargs: object(),
                    character_interface_cls=lambda **kwargs: None,
                    file_cleaner_from_config=lambda *a, **k: None,
                    router_builder=lambda **kwargs: IntentRouter([]),
                    start_cleaner=False,
                    memory_runtime_factory=lambda settings, **k: runtime,
                )
            self.assertEqual(runtime.close_calls, 1)

            # shutdown: memory 失败不阻止后续
            class BadRuntime(RT):
                def close(self, **kwargs: Any) -> Any:
                    self.close_calls += 1
                    raise RuntimeError("memory-close-fail")

            bad_rt = BadRuntime()
            tts_shutdown = {"n": 0}

            class TTS:
                def shutdown(self) -> None:
                    tts_shutdown["n"] += 1

            assistant = MikuAssistant.from_dependencies(
                cfg,
                llm_client=FakeLLM(),
                tts_engine=TTS(),
                memory_runtime=bad_rt,
                router=IntentRouter([]),
            )
            assistant.shutdown()
            assistant.shutdown()
            # 首次失败后允许仅重试 memory close；TTS 只关一次
            self.assertEqual(bad_rt.close_calls, 2)
            self.assertEqual(tts_shutdown["n"], 1)
            self.assertFalse(assistant._shutdown_flow_done)  # noqa: SLF001


class IntentPathTests(unittest.TestCase):
    def test_system_intent_does_not_submit_memory_llm_does(self) -> None:
        llm = FakeLLM()
        ingestion = FakeIngestion()
        client = MemoryAwareLLMClient(
            llm,
            FakeQuery(),  # type: ignore[arg-type]
            ingestion,  # type: ignore[arg-type]
            turn_id_factory=lambda: "x",
        )
        policy = SafetyPolicy.from_app_config(MemConfig({}))

        class FakeSystemHandler:
            def handle_outcome(self, text: str):
                from src.core.dispatch_outcome import DispatchOutcome

                if "关机" in text:
                    return DispatchOutcome.from_legacy_handler_text("已处理系统指令")
                return None

            def handle(self, text: str):
                outcome = self.handle_outcome(text)
                return None if outcome is None else outcome.text

        router = IntentRouter(
            [
                FakeSystemHandler(),  # type: ignore[list-item]
                LLMIntentHandler(client, safety_policy=policy),
            ]
        )
        router.dispatch("请关机")
        self.assertEqual(ingestion.events, [])
        router.dispatch("随便聊聊")
        self.assertEqual(len(ingestion.events), 1)

        ingestion.events.clear()
        list(client.generate_stream("流式聊"))
        self.assertEqual(len(ingestion.events), 1)


class EndToEndTempStoreTests(unittest.TestCase):
    def test_extractor_errors_fail_open_through_ingestion(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = SqliteMemoryStore(Path(tmp) / "t.sqlite3")
            transport = FakeTransport(
                {"done": True, "message": {"content": "not-json"}}
            )
            extractor = OllamaMemoryExtractor(transport=transport)
            ingestion = MemoryIngestionService(store, extractor, queue_capacity=2)
            try:
                ingestion.start()
                self.assertTrue(
                    ingestion.submit(
                        MemoryTurnEvent(
                            turn_id="e1",
                            user_text="u",
                            assistant_text="a",
                            outcome=MemoryTurnOutcome.SUCCESS,
                        )
                    )
                )
                done = threading.Event()

                def wait_idle() -> None:
                    # 等待 worker 处理完（失败开放后队列空）
                    while not done.wait(0.01):
                        if ingestion._pending_count() == 0 and not ingestion._in_flight:  # noqa: SLF001
                            done.set()
                            return

                t = threading.Thread(target=wait_idle)
                t.start()
                try:
                    self.assertTrue(done.wait(timeout=2.0))
                finally:
                    t.join(timeout=1.0)
                self.assertEqual(list(store.list_memories()), [])
                # 下一轮合法响应仍可写入
                transport.response = {
                    "done": True,
                    "message": {
                        "content": json.dumps(
                            [
                                {"unit_id": 0, "importance": 0.9, "confidence": 0.9}
                            ],
                            ensure_ascii=False,
                        )
                    },
                }
                transport.error = None
                self.assertTrue(
                    ingestion.submit(
                        MemoryTurnEvent(
                            turn_id="e2",
                            user_text="我喜欢记住这个习惯",
                            assistant_text="a2",
                            outcome=MemoryTurnOutcome.SUCCESS,
                        )
                    )
                )
                done2 = threading.Event()

                def wait2() -> None:
                    while not done2.wait(0.01):
                        if len(store.list_memories()) >= 1:
                            done2.set()
                            return

                t2 = threading.Thread(target=wait2)
                t2.start()
                try:
                    self.assertTrue(done2.wait(timeout=2.0))
                finally:
                    t2.join(timeout=1.0)
            finally:
                ingestion.close(drain=True, timeout=2.0)
                store.close()


class ConfigAndRuntimeFaultTests(unittest.TestCase):
    def test_disabled_ignores_corrupt_subconfig(self) -> None:
        cfg = _cfg(memory_enabled=False)
        cfg.data["memory"]["recall"] = {"max_entries": "not-an-int"}
        settings = parse_memory_settings(cfg)
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.max_entries, 8)

    def test_enabled_rejects_bool_float_and_numeric_string(self) -> None:
        for bad in (True, 1.5, "8"):
            cfg = _cfg(memory_enabled=True)
            cfg.data["memory"]["recall"]["max_entries"] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(MemoryValidationError):
                    parse_memory_settings(cfg)

    def test_transport_rejects_non_finite_timeout(self) -> None:
        from src.memory.ollama_extractor import RequestsOllamaChatTransport

        for bad in (float("nan"), float("inf"), True):
            with self.subTest(bad=bad):
                with self.assertRaises(MemoryValidationError):
                    RequestsOllamaChatTransport("http://x", timeout=bad)  # type: ignore[arg-type]

    def test_enabled_invalid_config_fails_before_device_factory(self) -> None:
        device_calls: List[str] = []

        class BoomTTS:
            def __init__(self, **kwargs: Any):
                device_calls.append("tts")

        bad = _cfg(memory_enabled=True)
        bad.data["memory"]["recall"]["max_entries"] = True
        with self.assertRaises(MemoryValidationError):
            assemble_default(
                config_loader=lambda _p: bad,
                logging_setup=lambda _c: None,
                dir_preparer=lambda: None,
                ollama_client_cls=lambda **kwargs: FakeLLM(),
                tts_engine_cls=BoomTTS,
                stt_engine_cls=lambda **kwargs: device_calls.append("stt"),
                windows_controller_cls=lambda **kwargs: None,
                chrome_searcher_cls=lambda **kwargs: object(),
                character_interface_cls=lambda **kwargs: None,
                file_cleaner_from_config=lambda *a, **k: None,
                router_builder=lambda **kwargs: IntentRouter([]),
                start_cleaner=False,
                memory_runtime_factory=lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("factory")
                ),
            )
        self.assertEqual(device_calls, [])

    def test_create_from_settings_closes_store_on_extractor_failure(self) -> None:
        class TrackingStore:
            def __init__(self, *args: Any, **kwargs: Any):
                self.closed = False

            def close(self) -> None:
                self.closed = True

        store_box: List[TrackingStore] = []

        def store_factory(*args: Any, **kwargs: Any) -> TrackingStore:
            store = TrackingStore()
            store_box.append(store)
            return store

        def extractor_factory(**kwargs: Any) -> Any:
            raise RuntimeError("extractor-boom")

        settings = parse_memory_settings(_cfg(memory_enabled=True))
        with self.assertRaises(RuntimeError):
            MemoryRuntime.create_from_settings(
                settings,
                store_factory=store_factory,
                extractor_factory=extractor_factory,
            )
        self.assertEqual(len(store_box), 1)
        self.assertTrue(store_box[0].closed)

    def test_store_close_failure_keeps_ownership_for_retry(self) -> None:
        class FlakyStore:
            def __init__(self) -> None:
                self.close_calls = 0
                self.fail_once = True

            def close(self) -> None:
                self.close_calls += 1
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("store-close-fail")

        store = FlakyStore()
        settings = parse_memory_settings(_cfg(memory_enabled=True))
        ingestion = FakeIngestion()
        runtime = MemoryRuntime(
            store,  # type: ignore[arg-type]
            FakeQuery(),  # type: ignore[arg-type]
            ingestion,  # type: ignore[arg-type]
            settings=settings,
            owns_store=True,
        )
        with self.assertRaises(RuntimeError):
            runtime.close()
        self.assertFalse(runtime.is_closed)
        self.assertTrue(runtime.owns_store)
        self.assertEqual(store.close_calls, 1)
        status = runtime.close()
        self.assertTrue(runtime.is_closed)
        self.assertFalse(runtime.owns_store)
        self.assertEqual(store.close_calls, 2)
        self.assertTrue(status.drained)

    def test_assistant_retries_memory_close_after_worker_timeout(self) -> None:
        gate = threading.Event()
        started = threading.Event()

        class BlockingExtractor:
            def extract(self, turn: MemoryTurnEvent):
                started.set()
                gate.wait(timeout=5.0)
                return [ExtractedMemory(text="saved-late")]

        cfg = _cfg(memory_enabled=True)
        cfg.data["memory"]["ingestion"]["close_timeout"] = 0.0
        settings = parse_memory_settings(cfg)
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "retry.sqlite3"
            store = SqliteMemoryStore(path)
            extractor = BlockingExtractor()
            ingestion = MemoryIngestionService(
                store, extractor, queue_capacity=2, owns_store=False
            )
            runtime = MemoryRuntime(
                store,
                MemoryQueryService(store),
                ingestion,
                settings=settings,
                owns_store=True,
            )
            runtime.start()
            self.assertTrue(
                ingestion.submit(
                    MemoryTurnEvent(
                        turn_id="late",
                        user_text="u",
                        assistant_text="a",
                        outcome=MemoryTurnOutcome.SUCCESS,
                    )
                )
            )
            self.assertTrue(started.wait(timeout=2.0))

            tts_shutdown = {"n": 0}

            class TTS:
                def shutdown(self) -> None:
                    tts_shutdown["n"] += 1

            assistant = MikuAssistant.from_dependencies(
                cfg,
                llm_client=FakeLLM(),
                tts_engine=TTS(),
                memory_runtime=runtime,
                router=IntentRouter([]),
            )
            try:
                assistant.shutdown()
                self.assertEqual(tts_shutdown["n"], 1)
                self.assertFalse(assistant._shutdown_flow_done)  # noqa: SLF001
                self.assertTrue(assistant._non_memory_shutdown_done)  # noqa: SLF001
                self.assertFalse(runtime.is_closed)
                gate.set()
                dead = threading.Event()

                def wait_dead() -> None:
                    worker = ingestion._worker  # noqa: SLF001
                    while worker is not None and worker.is_alive():
                        dead.wait(0.01)
                    dead.set()

                waiter = threading.Thread(target=wait_dead)
                waiter.start()
                try:
                    self.assertTrue(dead.wait(timeout=2.0))
                finally:
                    waiter.join(timeout=1.0)
                assistant.shutdown()
                self.assertTrue(assistant._shutdown_flow_done)  # noqa: SLF001
                self.assertTrue(runtime.is_closed)
                self.assertEqual(tts_shutdown["n"], 1)
                reopened = SqliteMemoryStore(path)
                try:
                    texts = {m.text for m in reopened.list_memories()}
                    self.assertIn("saved-late", texts)
                finally:
                    reopened.close()
            finally:
                gate.set()
                try:
                    assistant.shutdown()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
