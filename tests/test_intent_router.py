"""意图路由离线行为测试（标准库 unittest，无真实客户端/音频/模型）。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

from src.core.handlers import (
    IntentHandler,
    LLMIntentHandler,
    SearchIntentHandler,
    SystemIntentHandler,
)
from src.core.config import Config
from src.core.router import IntentRouter
from src.core.router_factory import build_intent_router
from src.core.safety import Operation, SafetyPolicy
from src.system import windows_control as wc_mod
from src.system.windows_control import WindowsController

# 隔离宿主环境中的 MIKU_DRY_RUN，避免污染权限 / dry_run 断言
_CLEAR_MIKU_DRY_RUN = patch.dict(os.environ, {"MIKU_DRY_RUN": ""}, clear=False)

_POWER_PHRASES = (
    ("关机", "shutdown"),
    ("重启", "restart"),
    ("锁屏", "lock"),
)


class SecurityDefaultTests(unittest.TestCase):
    """缺省配置必须保持最小权限；显式开启另由各能力契约测试覆盖。"""

    def test_policy_denies_device_affecting_defaults(self) -> None:
        with _CLEAR_MIKU_DRY_RUN:
            policy = SafetyPolicy()
        for operation in (
            Operation.REGISTRY_READ,
            Operation.REGISTRY_WRITE,
            Operation.SYSTEM_POWER,
            Operation.OS_SHELL,
            Operation.BROWSER_LAUNCH,
            Operation.ENV_MODIFY,
        ):
            with self.subTest(operation=operation):
                self.assertFalse(policy.is_allowed(operation))

    def test_generated_config_does_not_enable_system_or_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(str(Path(tmp) / "config.yaml"))
            self.assertFalse(config.get("system.windows_control_enabled"))
            self.assertFalse(config.get("security.permissions.system_power"))
            self.assertFalse(config.get("security.permissions.browser_launch"))
            self.assertFalse(config.get("security.permissions.env_modify"))
            self.assertFalse(config.get("search.auto_open"))


class RecordingHandler(IntentHandler):
    """可配置返回值并记录调用次数的测试用处理器。"""

    def __init__(self, name: str, result: Optional[str]):
        super().__init__(None)
        self.name = name
        self.result = result
        self.calls: List[str] = []

    def handle(self, text: str) -> Optional[str]:
        self.calls.append(text)
        return self.result


class FakeController:
    def __init__(self, result: Optional[str] = "system-ok"):
        self.result = result
        self.calls: List[str] = []

    def check_and_execute(self, text: str) -> Optional[str]:
        self.calls.append(text)
        return self.result


class FakeSearcher:
    def __init__(
        self,
        query: Optional[str] = "kw",
        search_result: str = "search-ok",
    ):
        self.query = query
        self.search_result = search_result
        self.extract_calls: List[str] = []
        self.search_calls: List[Optional[str]] = []

    def extract_search_query(self, text: str) -> Optional[str]:
        self.extract_calls.append(text)
        return self.query

    def search(self, query: Optional[str]) -> str:
        self.search_calls.append(query)
        return self.search_result


class FakeLLM:
    def __init__(self, reply: str = "llm-ok"):
        self.reply = reply
        self.calls: List[str] = []

    def generate(self, text: str) -> str:
        self.calls.append(text)
        return self.reply


class IntentRouterContractTests(unittest.TestCase):
    def test_system_hit_skips_search_and_llm(self) -> None:
        controller = FakeController("sys")
        searcher = FakeSearcher()
        llm = FakeLLM()
        router = IntentRouter(
            [
                SystemIntentHandler(controller),  # type: ignore[arg-type]
                SearchIntentHandler(searcher),  # type: ignore[arg-type]
                LLMIntentHandler(llm),  # type: ignore[arg-type]
            ]
        )
        self.assertEqual(router.dispatch("关机"), "sys")
        self.assertEqual(controller.calls, ["关机"])
        self.assertEqual(searcher.extract_calls, [])
        self.assertEqual(llm.calls, [])

    def test_search_hit_skips_llm(self) -> None:
        controller = FakeController(None)
        searcher = FakeSearcher(query="python", search_result="opened")
        llm = FakeLLM()
        router = IntentRouter(
            [
                SystemIntentHandler(controller),  # type: ignore[arg-type]
                SearchIntentHandler(searcher),  # type: ignore[arg-type]
                LLMIntentHandler(llm),  # type: ignore[arg-type]
            ]
        )
        self.assertEqual(router.dispatch("搜索 python"), "opened")
        self.assertEqual(controller.calls, ["搜索 python"])
        self.assertEqual(searcher.extract_calls, ["搜索 python"])
        self.assertEqual(searcher.search_calls, ["python"])
        self.assertEqual(llm.calls, [])

    def test_falls_through_to_llm(self) -> None:
        controller = FakeController(None)
        searcher = FakeSearcher(query=None)
        llm = FakeLLM("hello")
        router = IntentRouter(
            [
                SystemIntentHandler(controller),  # type: ignore[arg-type]
                SearchIntentHandler(searcher),  # type: ignore[arg-type]
                LLMIntentHandler(llm),  # type: ignore[arg-type]
            ]
        )
        self.assertEqual(router.dispatch("你好"), "hello")
        self.assertEqual(llm.calls, ["你好"])

    def test_none_continues_empty_string_stops(self) -> None:
        first = RecordingHandler("a", None)
        second = RecordingHandler("b", "")
        third = RecordingHandler("c", "should-not-run")
        router = IntentRouter([first, second, third])
        self.assertEqual(router.dispatch("x"), "")
        self.assertEqual(first.calls, ["x"])
        self.assertEqual(second.calls, ["x"])
        self.assertEqual(third.calls, [])

    def test_all_miss_returns_none(self) -> None:
        router = IntentRouter(
            [
                RecordingHandler("a", None),
                RecordingHandler("b", None),
            ]
        )
        self.assertIsNone(router.dispatch("anything"))

    def test_optional_deps_missing_behavior(self) -> None:
        router = IntentRouter(
            [
                SystemIntentHandler(None),
                SearchIntentHandler(None),
                LLMIntentHandler(None),
            ]
        )
        self.assertEqual(router.dispatch("随便聊聊"), "LLM 模块未初始化")

        thin = IntentRouter(
            [
                SystemIntentHandler(None),
                SearchIntentHandler(None),
            ]
        )
        self.assertIsNone(thin.dispatch("你好"))

    def test_factory_router_matches_contracts(self) -> None:
        controller = FakeController("locked")
        searcher = FakeSearcher(
            query="原神攻略",
            search_result="已打开搜索: 原神攻略",
        )
        llm = FakeLLM()
        router = build_intent_router(
            windows_controller=controller,  # type: ignore[arg-type]
            chrome_searcher=searcher,  # type: ignore[arg-type]
            llm_client=llm,  # type: ignore[arg-type]
            safety_policy=None,
        )

        # 系统命中
        self.assertEqual(router.dispatch("锁屏"), "locked")
        self.assertEqual(llm.calls, [])
        self.assertEqual(searcher.extract_calls, [])

        # 搜索命中：系统未处理 → 搜索收到正确 query，LLM 不调用
        controller.result = None
        llm.calls.clear()
        searcher.extract_calls.clear()
        searcher.search_calls.clear()
        user_text = "搜索 原神攻略"
        self.assertEqual(router.dispatch(user_text), "已打开搜索: 原神攻略")
        self.assertEqual(searcher.extract_calls, [user_text])
        self.assertEqual(searcher.search_calls, ["原神攻略"])
        self.assertEqual(llm.calls, [])

        # LLM 兜底
        searcher.query = None
        self.assertEqual(router.dispatch("天气怎样"), "llm-ok")
        self.assertEqual(llm.calls, ["天气怎样"])

        missing = build_intent_router()
        self.assertEqual(missing.dispatch("hi"), "LLM 模块未初始化")


class WindowsControlSafetyTests(unittest.TestCase):
    """在确认框与命令执行边界 mock，验证 dry-run / 权限拒绝不触发真实操作。"""

    def test_permission_denied_skips_confirm_and_subprocess(self) -> None:
        confirm = MagicMock(return_value=True)
        subprocess_run = MagicMock()

        with _CLEAR_MIKU_DRY_RUN:
            with patch.object(wc_mod, "confirm_system_action", confirm):
                with patch.object(wc_mod, "safe_system_power_run", subprocess_run):
                    policy = SafetyPolicy({"permissions": {"system_power": False}})
                    self.assertFalse(policy.is_allowed(Operation.SYSTEM_POWER))
                    controller = WindowsController(
                        safety_policy=policy,
                        require_confirmation=True,
                    )
                    self.assertTrue(controller.dry_run)
                    for phrase, _cmd in _POWER_PHRASES:
                        with self.subTest(phrase=phrase):
                            confirm.reset_mock()
                            subprocess_run.reset_mock()
                            result = controller.check_and_execute(phrase)
                            self.assertIn("拦截", result or "")
                            confirm.assert_not_called()
                            subprocess_run.assert_not_called()

    def test_explicit_dry_run_skips_confirm_and_subprocess(self) -> None:
        confirm = MagicMock(return_value=True)
        subprocess_run = MagicMock()

        with _CLEAR_MIKU_DRY_RUN:
            with patch.object(wc_mod, "confirm_system_action", confirm):
                with patch.object(wc_mod, "safe_system_power_run", subprocess_run):
                    policy = SafetyPolicy({"permissions": {"system_power": True}})
                    self.assertTrue(
                        policy.is_allowed(Operation.SYSTEM_POWER),
                        "显式 dry-run 用例要求策略本身允许 SYSTEM_POWER",
                    )
                    controller = WindowsController(
                        safety_policy=policy,
                        dry_run=True,
                        require_confirmation=True,
                    )
                    self.assertTrue(controller.dry_run)
                    for phrase, _cmd in _POWER_PHRASES:
                        with self.subTest(phrase=phrase):
                            confirm.reset_mock()
                            subprocess_run.reset_mock()
                            result = controller.check_and_execute(phrase)
                            self.assertIn("拦截", result or "")
                            confirm.assert_not_called()
                            subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
