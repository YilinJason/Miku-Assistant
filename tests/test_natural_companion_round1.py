# -*- coding: utf-8 -*-
"""自然陪伴 Round 1：统一人格提示边界——确定性离线契约测试。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import requests

from src.core.assembly import assemble_default
from src.core.assistant import MikuAssistant
from src.core.companion import (
    COMPANION_DYNAMIC_SEPARATOR,
    MAX_BASE_SYSTEM_CHARS,
    CompanionSettings,
    build_companion_base_system,
    compose_system_prompt,
    normalize_base_system,
    parse_companion_settings,
)
from src.core.handlers.llm_handler import LLMIntentHandler
from src.core.dispatch_outcome import ReplyStage
from src.core.router import IntentRouter
from src.llm.ollama_client import OllamaClient
from src.memory import (
    MemoryAwareLLMClient,
    MemoryCategory,
    MemoryQueryService,
    MemorySource,
    NewMemory,
    SqliteMemoryStore,
)
from src.memory.service_models import MemoryTurnEvent


class _Cfg:
    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self._data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class _NonStreamResponse:
    def __init__(self, text: str = "ok", status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)

    def json(self) -> dict:
        return {"message": {"role": "assistant", "content": "ok"}}


class _StreamResponse:
    def __init__(self) -> None:
        self.status_code = 200
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self):
        yield json.dumps(
            {"message": {"role": "assistant", "content": "流"}, "done": False},
            ensure_ascii=False,
        ).encode("utf-8")
        yield json.dumps(
            {"message": {"role": "assistant", "content": ""}, "done": True},
            ensure_ascii=False,
        ).encode("utf-8")

    def close(self) -> None:
        self.closed = True


class _NoopIngestion:
    def submit(self, event: object) -> bool:
        return isinstance(event, MemoryTurnEvent)


class CompanionConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = parse_companion_settings(_Cfg())
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.profile, "warm_balanced")
        base = build_companion_base_system(settings)
        self.assertIsNotNone(base)
        assert base is not None
        self.assertLessEqual(len(base), MAX_BASE_SYSTEM_CHARS)

    def test_disabled_returns_none_base(self) -> None:
        cfg = _Cfg({"assistant.companion.enabled": False})
        settings = parse_companion_settings(cfg)
        self.assertFalse(settings.enabled)
        self.assertIsNone(build_companion_base_system(settings))

    def test_bool_coercion_rejected(self) -> None:
        for bad in (1, 0, "true", "false", None):
            if bad is None:
                # None → default True，合法
                continue
            cfg = _Cfg({"assistant.companion.enabled": bad})
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_companion_settings(cfg)

    def test_empty_and_unknown_profile_fail_closed(self) -> None:
        for bad in ("", "  ", "friendly", "WARM_BALANCED", None, 1):
            cfg = _Cfg(
                {
                    "assistant.companion.enabled": True,
                    "assistant.companion.profile": bad,
                }
            )
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_companion_settings(cfg)

    def test_parse_before_assembly_side_effects(self) -> None:
        side_effects: List[str] = []

        class BadCfg:
            def get(self, key: str, default: Any = None) -> Any:
                if key == "assistant.companion.profile":
                    return "not_a_real_profile"
                if key == "assistant.companion.enabled":
                    return True
                if key == "ollama.base_url":
                    return "http://127.0.0.1:9"
                if key == "ollama.model":
                    return "m"
                if key == "ollama.timeout":
                    return 1
                if key.startswith("memory."):
                    return default
                if key.startswith("resource."):
                    return default
                if key.startswith("ollama."):
                    return default
                if key.startswith("security."):
                    return default
                return default

            def get_rvc_config(self) -> dict:
                return {}

        def logging_setup(_cfg: Any) -> None:
            side_effects.append("logging")

        def dir_preparer() -> None:
            side_effects.append("dirs")

        def boom_ollama(**_kw: Any) -> Any:
            side_effects.append("ollama")
            raise AssertionError("must not construct LLM")

        with self.assertRaises(ValueError) as ctx:
            assemble_default(
                config_loader=lambda _p: BadCfg(),
                logging_setup=logging_setup,
                dir_preparer=dir_preparer,
                ollama_client_cls=boom_ollama,
                start_cleaner=False,
                file_cleaner_from_config=lambda *_a, **_k: None,
                router_builder=lambda **_k: IntentRouter([]),
                windows_controller_cls=lambda **_k: None,
                chrome_searcher_cls=lambda **_k: None,
                character_interface_cls=lambda **_k: None,
                tts_engine_cls=lambda **_k: None,
                stt_engine_cls=lambda **_k: None,
            )
        self.assertIn("profile", str(ctx.exception).lower())
        self.assertEqual(side_effects, [])


class SystemComposeMatrixTests(unittest.TestCase):
    def test_four_way_matrix(self) -> None:
        base = "BASE-PERSONA"
        dyn = "DYNAMIC-CTX"
        self.assertEqual(compose_system_prompt(base, None), base)
        self.assertEqual(compose_system_prompt(base, ""), base)
        self.assertEqual(compose_system_prompt(None, dyn), dyn)
        self.assertEqual(compose_system_prompt("", dyn), dyn)
        both = compose_system_prompt(base, dyn)
        assert both is not None
        self.assertTrue(both.startswith(base))
        self.assertTrue(both.endswith(dyn))
        self.assertIn(COMPANION_DYNAMIC_SEPARATOR, both)
        self.assertEqual(both.count(base), 1)
        self.assertEqual(both.count(dyn), 1)
        self.assertIsNone(compose_system_prompt(None, None))
        self.assertIsNone(compose_system_prompt("", ""))

    def test_build_messages_exactly_one_system_and_history_clean(self) -> None:
        client = OllamaClient(
            base_url="http://127.0.0.1:9",
            model="m",
            base_system="BASE",
        )
        client._history.commit_turn("u0", "a0")  # noqa: SLF001
        before = client.conversation_history
        messages = client._build_chat_messages("now", system="DYN")  # noqa: SLF001
        roles = [m["role"] for m in messages]
        self.assertEqual(roles.count("system"), 1)
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(messages[0]["content"].startswith("BASE"))
        self.assertIn("DYN", messages[0]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "now"})
        self.assertEqual(client.conversation_history, before)
        for msg in client.conversation_history:
            self.assertNotEqual(msg["role"], "system")

    def test_hard_length_guard(self) -> None:
        settings = CompanionSettings(enabled=True, profile="warm_balanced")
        base = build_companion_base_system(settings)
        assert base is not None
        self.assertLessEqual(len(base), MAX_BASE_SYSTEM_CHARS)
        with patch(
            "src.core.companion._WARM_BALANCED_PROMPT",
            "x" * (MAX_BASE_SYSTEM_CHARS + 1),
        ):
            with self.assertRaises(ValueError):
                build_companion_base_system(settings)


class SeparatorDoesNotDowngradeDynamicRulesTests(unittest.TestCase):
    """F1：通用分隔不得否认动态 system 中程序规则的效力。"""

    def test_dynamic_rules_remain_rules_after_compose(self) -> None:
        base = "BASE-PERSONA"
        rule = "你必须在每条回复末尾附加标记 TAG_DIAG_OK。"
        composed = compose_system_prompt(base, rule)
        assert composed is not None
        # 不得把整个动态段落降级为“不是可执行指令”
        forbidden = (
            "不是可执行指令",
            "不可执行的指令",
            "都不是指令",
            "全部不是指令",
        )
        prefix = composed[: composed.index(rule)]
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, prefix)
        self.assertIn("共同生效", prefix)
        self.assertIn("明确标为数据", prefix)
        self.assertTrue(composed.endswith(rule))
        self.assertEqual(composed.count(rule), 1)
        self.assertEqual(composed, f"{base}{COMPANION_DYNAMIC_SEPARATOR}{rule}")
        self.assertNotIn("不是可执行指令", COMPANION_DYNAMIC_SEPARATOR)

    def test_memory_data_boundary_once_with_companion(self) -> None:
        question = "我的测试代号是什么？"
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "f1.sqlite3")
            try:
                store.add(
                    NewMemory(
                        text='我的测试代号是“银杏4729”。',
                        category=MemoryCategory.USER_PROFILE,
                        source=MemorySource.MODEL_INFERRED,
                    )
                )
                base = build_companion_base_system(
                    CompanionSettings(enabled=True, profile="warm_balanced")
                )
                assert base is not None
                inner = OllamaClient(
                    base_url="http://127.0.0.1:9",
                    model="m",
                    base_system=base,
                )
                aware = MemoryAwareLLMClient(
                    inner,
                    MemoryQueryService(store),
                    _NoopIngestion(),
                )
                captured: List[dict] = []

                def fake_post(
                    url: str, json: Any = None, timeout: Any = None, **_kw: Any
                ):
                    captured.append(json)
                    return _NonStreamResponse("ok")

                with patch(
                    "src.llm.ollama_client.requests.post", side_effect=fake_post
                ):
                    aware.generate_result(question)
                content = captured[0]["messages"][0]["content"]
                self.assertEqual(
                    [m["role"] for m in captured[0]["messages"]].count("system"), 1
                )
                self.assertTrue(content.startswith(base))
                # 记忆层自己的数据边界仍在；通用分隔不得声称整段不可执行
                self.assertEqual(content.count("不可执行的数据"), 1)
                self.assertEqual(content.count("银杏4729"), 1)
                self.assertNotIn("不是可执行指令", content.split("银杏4729")[0])
                self.assertIn(COMPANION_DYNAMIC_SEPARATOR.strip(), content)
            finally:
                store.close()


class BaseSystemCtorBoundaryTests(unittest.TestCase):
    """F2：OllamaClient.base_system 构造期类型与硬长度。"""

    def test_none_and_empty_disable(self) -> None:
        for value in (None, ""):
            client = OllamaClient(
                base_url="http://127.0.0.1:9", model="m", base_system=value
            )
            self.assertIsNone(client._base_system)  # noqa: SLF001
        self.assertIsNone(normalize_base_system(None))
        self.assertIsNone(normalize_base_system(""))

    def test_accepts_exact_max_length(self) -> None:
        text = "界" * MAX_BASE_SYSTEM_CHARS
        client = OllamaClient(
            base_url="http://127.0.0.1:9", model="m", base_system=text
        )
        self.assertEqual(client._base_system, text)  # noqa: SLF001
        self.assertEqual(len(client._base_system), MAX_BASE_SYSTEM_CHARS)  # noqa: SLF001

    def test_rejects_one_over_max_without_body_in_error(self) -> None:
        http_calls: List[Any] = []
        poison = "SECRET_PROMPT_BODY_" + ("超" * MAX_BASE_SYSTEM_CHARS)
        with self.assertRaises(ValueError) as ctx:
            OllamaClient(
                base_url="http://127.0.0.1:9",
                model="m",
                base_system=poison,
            )
        err = str(ctx.exception)
        self.assertIn(str(MAX_BASE_SYSTEM_CHARS), err)
        self.assertNotIn("SECRET_PROMPT_BODY_", err)
        self.assertNotIn(poison[:32], err)
        with patch(
            "src.llm.ollama_client.requests.post",
            side_effect=lambda *a, **k: http_calls.append(1),
        ):
            pass
        self.assertEqual(http_calls, [])

    def test_rejects_non_string_before_http(self) -> None:
        http_calls: List[Any] = []

        def boom(*_a: Any, **_k: Any) -> Any:
            http_calls.append(1)
            raise AssertionError("HTTP must not run")

        for bad in (True, False, b"bytes", ["list"], {"k": "v"}, 123):
            with self.subTest(bad=type(bad).__name__):
                with patch(
                    "src.llm.ollama_client.requests.post", side_effect=boom
                ):
                    with self.assertRaises(ValueError) as ctx:
                        OllamaClient(
                            base_url="http://127.0.0.1:9",
                            model="m",
                            base_system=bad,  # type: ignore[arg-type]
                        )
                self.assertIn(type(bad).__name__, str(ctx.exception))
        self.assertEqual(http_calls, [])

    def test_per_request_dynamic_system_unchanged(self) -> None:
        """不约束既有每请求动态 system 兼容契约。"""
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m")
        messages = client._build_chat_messages(  # noqa: SLF001
            "q", system="你必须遵守诊断规则 X。"
        )
        self.assertEqual(messages[0]["content"], "你必须遵守诊断规则 X。")


class HttpPayloadContractTests(unittest.TestCase):
    def test_stream_and_non_stream_identical_system(self) -> None:
        base = "人格BASE"
        dyn = "动态DYN"
        non_stream: List[dict] = []
        stream_payloads: List[dict] = []

        def capture_ns(url: str, json: Any = None, timeout: Any = None, **_kw: Any):
            non_stream.append(json)
            return _NonStreamResponse()

        def capture_s(
            url: str, json: Any = None, timeout: Any = None, stream: bool = False, **_kw: Any
        ):
            stream_payloads.append(json)
            self.assertTrue(stream)
            return _StreamResponse()

        c1 = OllamaClient(
            base_url="http://127.0.0.1:9", model="m", base_system=base
        )
        c1._history.commit_turn("hu", "ha")  # noqa: SLF001
        with patch("src.llm.ollama_client.requests.post", side_effect=capture_ns):
            c1.generate_result("q", system=dyn)

        c2 = OllamaClient(
            base_url="http://127.0.0.1:9", model="m", base_system=base
        )
        c2._history.commit_turn("hu", "ha")  # noqa: SLF001
        with patch("src.llm.ollama_client.requests.post", side_effect=capture_s):
            list(c2.generate_stream("q", system=dyn))

        self.assertEqual(len(non_stream), 1)
        self.assertEqual(len(stream_payloads), 1)
        for payload in (non_stream[0], stream_payloads[0]):
            self.assertNotIn("system", payload)
            self.assertEqual(payload["messages"][0]["role"], "system")
            content = payload["messages"][0]["content"]
            self.assertTrue(content.startswith(base))
            self.assertIn(dyn, content)
            self.assertEqual(
                [m["role"] for m in payload["messages"]].count("system"), 1
            )
        self.assertEqual(
            non_stream[0]["messages"][0]["content"],
            stream_payloads[0]["messages"][0]["content"],
        )
        self.assertEqual(
            [dict(m) for m in non_stream[0]["messages"]],
            [dict(m) for m in stream_payloads[0]["messages"]],
        )


class MemoryCompanionIntegrationTests(unittest.TestCase):
    def test_companion_plus_memory_preserves_json_once(self) -> None:
        question = "我的测试代号和最喜欢的饮品是什么？"
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")
            try:
                store.add(
                    NewMemory(
                        text='用户的测试代号是“银杏4729”',
                        category=MemoryCategory.USER_PROFILE,
                        source=MemorySource.MODEL_INFERRED,
                    )
                )
                store.add(
                    NewMemory(
                        text="用户最喜欢的饮品是桂花乌龙",
                        category=MemoryCategory.PREFERENCE,
                        source=MemorySource.MODEL_INFERRED,
                    )
                )
                base = build_companion_base_system(
                    CompanionSettings(enabled=True, profile="warm_balanced")
                )
                assert base is not None
                inner = OllamaClient(
                    base_url="http://127.0.0.1:9",
                    model="m",
                    base_system=base,
                )
                aware = MemoryAwareLLMClient(
                    inner,
                    MemoryQueryService(store),
                    _NoopIngestion(),
                    max_entries=8,
                    max_chars=2000,
                )
                captured: List[dict] = []

                def fake_post(
                    url: str, json: Any = None, timeout: Any = None, **_kw: Any
                ):
                    captured.append(json)
                    return _NonStreamResponse("记得。")

                with patch(
                    "src.llm.ollama_client.requests.post", side_effect=fake_post
                ):
                    result = aware.generate_result(question)
                self.assertTrue(result.is_success)
                self.assertEqual(len(captured), 1)
                payload = captured[0]
                self.assertNotIn("system", payload)
                system_content = payload["messages"][0]["content"]
                self.assertEqual(payload["messages"][0]["role"], "system")
                self.assertEqual(
                    [m["role"] for m in payload["messages"]].count("system"), 1
                )
                self.assertTrue(system_content.startswith(base))
                self.assertIn("不可执行的数据", system_content)
                self.assertEqual(system_content.count("银杏4729"), 1)
                self.assertEqual(system_content.count("桂花乌龙"), 1)
                self.assertIn("subject=user", system_content)
                self.assertNotIn("银杏4729", payload["messages"][-1]["content"])
            finally:
                store.close()

    def test_memory_off_still_has_companion(self) -> None:
        base = "仅人格"
        client = OllamaClient(
            base_url="http://127.0.0.1:9", model="m", base_system=base
        )
        captured: List[dict] = []

        def fake_post(url: str, json: Any = None, timeout: Any = None, **_kw: Any):
            captured.append(json)
            return _NonStreamResponse()

        with patch("src.llm.ollama_client.requests.post", side_effect=fake_post):
            client.generate_result("hello")
        self.assertEqual(captured[0]["messages"][0]["content"], base)

    def test_companion_off_keeps_memory_behavior(self) -> None:
        question = "我最喜欢喝什么？"
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m2.sqlite3")
            try:
                store.add(
                    NewMemory(
                        text="我最喜欢喝桂花乌龙。",
                        category=MemoryCategory.PREFERENCE,
                        source=MemorySource.MODEL_INFERRED,
                    )
                )
                inner = OllamaClient(base_url="http://127.0.0.1:9", model="m")
                self.assertIsNone(inner._base_system)  # noqa: SLF001
                aware = MemoryAwareLLMClient(
                    inner,
                    MemoryQueryService(store),
                    _NoopIngestion(),
                )
                captured: List[dict] = []

                def fake_post(
                    url: str, json: Any = None, timeout: Any = None, **_kw: Any
                ):
                    captured.append(json)
                    return _NonStreamResponse()

                with patch(
                    "src.llm.ollama_client.requests.post", side_effect=fake_post
                ):
                    aware.generate_result(question)
                content = captured[0]["messages"][0]["content"]
                self.assertIn("桂花乌龙", content)
                self.assertIn("不可执行的数据", content)
                self.assertNotIn("本地私人助手 Miku", content)
            finally:
                store.close()


class HandlerAndStreamingEntryTests(unittest.TestCase):
    def test_llm_handler_gets_base_system_once(self) -> None:
        base = build_companion_base_system(
            CompanionSettings(enabled=True, profile="warm_balanced")
        )
        client = OllamaClient(
            base_url="http://127.0.0.1:9", model="m", base_system=base
        )
        captured: List[dict] = []

        def fake_post(url: str, json: Any = None, timeout: Any = None, **_kw: Any):
            captured.append(json)
            return _NonStreamResponse("hi")

        handler = LLMIntentHandler(client)
        with patch("src.llm.ollama_client.requests.post", side_effect=fake_post):
            outcome = handler.handle_outcome("你好呀")
        self.assertTrue(outcome.allow_tts)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["messages"][0]["content"], base)

    def test_process_text_input_streaming_same_base_system(self) -> None:
        base = build_companion_base_system(
            CompanionSettings(enabled=True, profile="warm_balanced")
        )
        client = OllamaClient(
            base_url="http://127.0.0.1:9", model="m", base_system=base
        )
        captured: List[dict] = []

        def fake_post(
            url: str, json: Any = None, timeout: Any = None, stream: bool = False, **_kw: Any
        ):
            captured.append(json)
            return _StreamResponse()

        assistant = MikuAssistant.from_dependencies(
            config=_Cfg(
                {
                    "assistant.auto_tts": False,
                    "assistant.stream_reply": True,
                }
            ),
            llm_client=client,
            router=IntentRouter([LLMIntentHandler(client)]),
        )
        with patch("src.llm.ollama_client.requests.post", side_effect=fake_post):
            outcome = assistant.process_text_input_streaming("今天怎么样", use_tts=False)
        self.assertEqual(outcome.stage, ReplyStage.OK)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["messages"][0]["content"], base)
        self.assertEqual(captured[0]["stream"], True)


class PromptBoundaryContentTests(unittest.TestCase):
    def test_warm_balanced_contains_key_boundaries(self) -> None:
        text = build_companion_base_system(
            CompanionSettings(enabled=True, profile="warm_balanced")
        )
        assert text is not None
        must_have = [
            "本地私人助手 Miku",
            "不要每轮都提问",
            "长期记忆",
            "不知道",
            "紧急服务",
            "真实执行结果",
            "安全策略",
        ]
        for needle in must_have:
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        # 不得写成可配置任意 custom_prompt 入口
        self.assertNotIn("custom_prompt", text)


class AssembleCompanionWireTests(unittest.TestCase):
    def test_assemble_passes_base_system_when_enabled(self) -> None:
        seen: Dict[str, Any] = {}

        class Cfg:
            def get(self, key: str, default: Any = None) -> Any:
                mapping = {
                    "assistant.companion.enabled": True,
                    "assistant.companion.profile": "warm_balanced",
                    "ollama.base_url": "http://127.0.0.1:9",
                    "ollama.model": "m",
                    "ollama.timeout": 1,
                    "ollama.keep_alive": "30m",
                    "system.windows_control_enabled": False,
                    "search.chrome_path": None,
                    "search.search_engine": "google",
                    "search.auto_open": False,
                    "extensions.character_model.enabled": False,
                    "memory.enabled": False,
                }
                return mapping.get(key, default)

            def get_rvc_config(self) -> dict:
                return {}

        class CapturingClient(OllamaClient):
            def __init__(self, **kwargs: Any) -> None:
                seen.update(kwargs)
                super().__init__(**kwargs)

        parts = assemble_default(
            config_loader=lambda _p: Cfg(),
            logging_setup=lambda _c: None,
            dir_preparer=lambda: None,
            ollama_client_cls=CapturingClient,
            start_cleaner=False,
            file_cleaner_from_config=lambda *_a, **_k: None,
            router_builder=lambda **_k: IntentRouter([]),
            windows_controller_cls=lambda **_k: None,
            chrome_searcher_cls=lambda **_k: None,
            character_interface_cls=lambda **_k: None,
            tts_engine_cls=lambda **_k: None,
            stt_engine_cls=lambda **_k: None,
            startup_mode="text",
        )
        self.assertTrue(parts.companion_settings.enabled)
        expected = build_companion_base_system(parts.companion_settings)
        self.assertEqual(seen.get("base_system"), expected)
        self.assertEqual(parts.llm_client._base_system, expected)  # noqa: SLF001

    def test_assemble_omits_base_when_disabled(self) -> None:
        seen: Dict[str, Any] = {}

        class Cfg:
            def get(self, key: str, default: Any = None) -> Any:
                mapping = {
                    "assistant.companion.enabled": False,
                    "assistant.companion.profile": "warm_balanced",
                    "ollama.base_url": "http://127.0.0.1:9",
                    "ollama.model": "m",
                    "ollama.timeout": 1,
                    "system.windows_control_enabled": False,
                    "search.search_engine": "google",
                    "search.auto_open": False,
                    "extensions.character_model.enabled": False,
                    "memory.enabled": False,
                }
                return mapping.get(key, default)

            def get_rvc_config(self) -> dict:
                return {}

        class CapturingClient(OllamaClient):
            def __init__(self, **kwargs: Any) -> None:
                seen.update(kwargs)
                super().__init__(**kwargs)

        parts = assemble_default(
            config_loader=lambda _p: Cfg(),
            logging_setup=lambda _c: None,
            dir_preparer=lambda: None,
            ollama_client_cls=CapturingClient,
            start_cleaner=False,
            file_cleaner_from_config=lambda *_a, **_k: None,
            router_builder=lambda **_k: IntentRouter([]),
            windows_controller_cls=lambda **_k: None,
            chrome_searcher_cls=lambda **_k: None,
            character_interface_cls=lambda **_k: None,
            tts_engine_cls=lambda **_k: None,
            stt_engine_cls=lambda **_k: None,
            startup_mode="text",
        )
        self.assertNotIn("base_system", seen)
        self.assertIsNone(parts.llm_client._base_system)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
