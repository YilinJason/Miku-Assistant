"""助手编排离线测试：注入路径 + 默认可替换装配（无真实设备）。"""
from __future__ import annotations

import logging
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, call, patch

from src.core.assembly import assemble_default
from src.core.assistant import MikuAssistant
from src.core.router import IntentRouter
from src.llm.ollama_client import OllamaClient
from src.voice.stt import STTEngine
from src.voice.tts.engine import TTSEngine


class MemoryConfig:
    """内存配置替身，不读磁盘。"""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self.data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur if cur is not None else default

    def get_rvc_config(self) -> Optional[dict]:
        rvc = self.get("voice.tts.rvc") or {}
        return rvc if rvc.get("model_path") else None


class FakeRouter:
    def __init__(self, response: str = "routed"):
        self.response = response
        self.calls: List[str] = []

    def dispatch(self, text: str) -> str:
        self.calls.append(text)
        return self.response


class FakeTTS:
    def __init__(self):
        self.speak_calls: List[str] = []
        self.shutdown_calls = 0

    def speak(self, text: str) -> None:
        self.speak_calls.append(text)

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class FakeSTT:
    def __init__(self, result: str = "heard", error: Optional[BaseException] = None):
        self.result = result
        self.error = error
        self.listen_kwargs: List[dict] = []

    def listen(self, continuous: bool = False) -> str:
        self.listen_kwargs.append({"continuous": continuous})
        if self.error:
            raise self.error
        return self.result


class FakeCharacter:
    def __init__(self, initialized: bool = True):
        self._initialized = initialized
        self.messages: List[str] = []
        self.close_calls = 0

    def send_message(self, message: str, audio_file: Optional[str] = None) -> bool:
        self.messages.append(message)
        return True

    def close(self) -> None:
        self.close_calls += 1
        self._initialized = False


class FakeCleaner:
    def __init__(self):
        self.stop_calls = 0
        self.cleanup_calls = 0
        self.start_calls = 0

    def start_auto_cleanup(self) -> None:
        self.start_calls += 1

    def stop_auto_cleanup(self) -> None:
        self.stop_calls += 1

    def cleanup_now(self) -> None:
        self.cleanup_calls += 1


def _immediate_thread_factory(*, fail_start: bool = False):
    """可控线程替身：构造成功；start 同步执行 target，或按需抛错。"""

    created: List[Any] = []

    def factory(*args: Any, **kwargs: Any) -> MagicMock:
        inst = MagicMock(name="ImmediateThread")
        target = kwargs.get("target")
        targs = kwargs.get("args") or ()
        tkwargs = kwargs.get("kwargs") or {}

        def start() -> None:
            if fail_start:
                raise RuntimeError("start failed")
            if target is not None:
                target(*targs, **tkwargs)

        inst.start = MagicMock(side_effect=start)
        inst._target = target
        inst._args = targs
        created.append(inst)
        return inst

    factory.created = created  # type: ignore[attr-defined]
    return factory


class InjectionSideEffectGuards(unittest.TestCase):
    """注入构造不得触发默认装配副作用（patch 实际查找位置）。"""

    def test_from_dependencies_skips_file_log_dirs_engines_cleaner(self) -> None:
        config = MemoryConfig({"assistant": {"auto_tts": False}})
        # 默认参数已绑定真实类对象，不能靠 patch 模块同名替换默认值；
        # 对类 __init__ 做边界断言，证明注入路径未实例化它们。
        with patch("src.core.assistant.assemble_default") as assemble:
            with patch("src.core.assembly.Config") as config_cls:
                with patch("src.core.assembly.setup_app_logging") as setup_log:
                    with patch("src.core.assembly.ensure_runtime_dirs") as ensure_dirs:
                        with patch("src.core.assembly.FileCleaner") as cleaner_cls:
                            with patch.object(OllamaClient, "__init__", return_value=None) as ollama_init:
                                with patch.object(TTSEngine, "__init__", return_value=None) as tts_init:
                                    with patch.object(STTEngine, "__init__", return_value=None) as stt_init:
                                        assistant = MikuAssistant.from_dependencies(
                                            config,
                                            router=FakeRouter("ok"),  # type: ignore[arg-type]
                                        )
                                        assemble.assert_not_called()
                                        config_cls.assert_not_called()
                                        setup_log.assert_not_called()
                                        ensure_dirs.assert_not_called()
                                        cleaner_cls.assert_not_called()
                                        cleaner_cls.from_config.assert_not_called()
                                        ollama_init.assert_not_called()
                                        tts_init.assert_not_called()
                                        stt_init.assert_not_called()
                                        self.assertIsNone(assistant.tts_engine)
                                        self.assertIsNone(assistant.stt_engine)
                                        self.assertIsNone(assistant.file_cleaner)
                                        self.assertIsNone(assistant.llm_client)
                                        self.assertEqual(
                                            assistant.process_text_input("hi"), "ok"
                                        )


class LoggerInjectionTests(unittest.TestCase):
    def test_injected_logger_identity_and_error_forwarding(self) -> None:
        injected = MagicMock(name="injected_logger")
        stt = FakeSTT(error=RuntimeError("mic down"))
        assistant = MikuAssistant.from_dependencies(
            MemoryConfig(),
            stt_engine=stt,
            router=FakeRouter(),  # type: ignore[arg-type]
            logger=injected,
        )
        self.assertIs(assistant.logger, injected)
        self.assertEqual(assistant.process_voice_input(), "")
        injected.error.assert_called()
        self.assertIn("语音识别失败", injected.error.call_args[0][0])

    def test_default_logger_when_not_injected(self) -> None:
        assistant = MikuAssistant.from_dependencies(
            MemoryConfig(),
            router=FakeRouter(),  # type: ignore[arg-type]
        )
        self.assertIs(assistant.logger, logging.getLogger("src.core.assistant"))


class ProcessTextInputTests(unittest.TestCase):
    def _make(self, **kwargs) -> MikuAssistant:
        config = kwargs.pop("config", MemoryConfig({"assistant": {"auto_tts": True}}))
        return MikuAssistant.from_dependencies(config, **kwargs)

    def test_dispatch_input_and_return(self) -> None:
        router = FakeRouter("answer")
        assistant = self._make(router=router)  # type: ignore[arg-type]
        self.assertEqual(assistant.process_text_input("question"), "answer")
        self.assertEqual(router.calls, ["question"])

    def test_use_tts_explicit_overrides_config(self) -> None:
        tts = FakeTTS()
        router = FakeRouter("Miku: hello")
        config = MemoryConfig({"assistant": {"auto_tts": True}})
        assistant = self._make(config=config, router=router, tts_engine=tts)  # type: ignore[arg-type]

        with patch("src.core.assistant.threading.Thread") as thread_cls:
            thread_cls.side_effect = _immediate_thread_factory()
            assistant.process_text_input("x", use_tts=False)
            thread_cls.assert_not_called()
            self.assertEqual(tts.speak_calls, [])

        factory = _immediate_thread_factory()
        with patch("src.core.assistant.threading.Thread", side_effect=factory) as thread_cls:
            config.data["assistant"]["auto_tts"] = False
            assistant.process_text_input("y", use_tts=True)
            thread_cls.assert_called_once()
            self.assertEqual(len(factory.created), 1)
            factory.created[0].start.assert_called_once()
            self.assertEqual(tts.speak_calls, ["hello"])

    def test_use_tts_none_reads_config(self) -> None:
        tts = FakeTTS()
        router = FakeRouter("plain")
        config = MemoryConfig({"assistant": {"auto_tts": False}})
        assistant = self._make(config=config, router=router, tts_engine=tts)  # type: ignore[arg-type]
        with patch("src.core.assistant.threading.Thread") as thread_cls:
            assistant.process_text_input("z", use_tts=None)
            thread_cls.assert_not_called()

        factory = _immediate_thread_factory()
        with patch("src.core.assistant.threading.Thread", side_effect=factory):
            config.data["assistant"]["auto_tts"] = True
            assistant.process_text_input("z2", use_tts=None)
            factory.created[0].start.assert_called_once()
            self.assertEqual(tts.speak_calls, ["plain"])

    def test_thread_start_failure_falls_back_to_sync_speak(self) -> None:
        tts = FakeTTS()
        router = FakeRouter("Miku: sync")
        logger = MagicMock()
        assistant = self._make(
            router=router, tts_engine=tts, logger=logger  # type: ignore[arg-type]
        )
        factory = _immediate_thread_factory(fail_start=True)
        with patch("src.core.assistant.threading.Thread", side_effect=factory):
            assistant.process_text_input("a", use_tts=True)
            factory.created[0].start.assert_called_once()
            self.assertEqual(tts.speak_calls, ["sync"])
            logger.error.assert_called()
            self.assertIn("启动音频播放线程失败", logger.error.call_args[0][0])

    def test_character_forward(self) -> None:
        router = FakeRouter("char-msg")
        character = FakeCharacter(initialized=True)
        assistant = self._make(router=router, character_interface=character)  # type: ignore[arg-type]
        with patch("src.core.assistant.threading.Thread"):
            self.assertEqual(assistant.process_text_input("q", use_tts=False), "char-msg")
        self.assertEqual(character.messages, ["char-msg"])

        character2 = FakeCharacter(initialized=False)
        assistant2 = self._make(router=router, character_interface=character2)  # type: ignore[arg-type]
        assistant2.process_text_input("q2", use_tts=False)
        self.assertEqual(character2.messages, [])


class VoiceAndSpeakTests(unittest.TestCase):
    def test_stt_pass_through_and_errors(self) -> None:
        stt = FakeSTT(result="你好")
        assistant = MikuAssistant.from_dependencies(
            MemoryConfig(),
            stt_engine=stt,
            router=FakeRouter(),  # type: ignore[arg-type]
        )
        self.assertEqual(assistant.process_voice_input(continuous=True), "你好")
        self.assertEqual(stt.listen_kwargs, [{"continuous": True}])

        none_stt = MikuAssistant.from_dependencies(
            MemoryConfig(),
            router=FakeRouter(),  # type: ignore[arg-type]
        )
        self.assertEqual(none_stt.process_voice_input(), "STT 模块未初始化")

        bad = FakeSTT(error=RuntimeError("mic"))
        assistant_bad = MikuAssistant.from_dependencies(
            MemoryConfig(),
            stt_engine=bad,
            router=FakeRouter(),  # type: ignore[arg-type]
        )
        self.assertEqual(assistant_bad.process_voice_input(), "")

    def test_speak_strips_prefix(self) -> None:
        tts = FakeTTS()
        assistant = MikuAssistant.from_dependencies(
            MemoryConfig(),
            tts_engine=tts,
            router=FakeRouter(),  # type: ignore[arg-type]
        )
        assistant.speak("Miku: hi there")
        self.assertEqual(tts.speak_calls, ["hi there"])


class ShutdownLifecycleTests(unittest.TestCase):
    def _assistant(self, **kwargs) -> MikuAssistant:
        logger = kwargs.pop("logger", MagicMock(name="logger"))
        return MikuAssistant.from_dependencies(
            MemoryConfig(),
            logger=logger,
            router=kwargs.pop("router", FakeRouter()),  # type: ignore[arg-type]
            **kwargs,
        )

    def test_shutdown_order_repeat_and_caller_managed_untouched(self) -> None:
        tts = FakeTTS()
        cleaner = FakeCleaner()
        character = FakeCharacter()
        order: List[str] = []
        logger = MagicMock()

        tts.shutdown = MagicMock(side_effect=lambda: order.append("tts"))
        cleaner.stop_auto_cleanup = MagicMock(side_effect=lambda: order.append("stop"))
        cleaner.cleanup_now = MagicMock(side_effect=lambda: order.append("cleanup"))
        character.close = MagicMock(side_effect=lambda: order.append("char"))

        llm = MagicMock(name="llm")
        stt = MagicMock(name="stt")
        windows = MagicMock(name="windows")
        searcher = MagicMock(name="searcher")

        assistant = self._assistant(
            logger=logger,
            llm_client=llm,
            tts_engine=tts,
            stt_engine=stt,
            windows_controller=windows,
            chrome_searcher=searcher,
            file_cleaner=cleaner,
            character_interface=character,
        )
        assistant.shutdown()
        self.assertEqual(order, ["tts", "stop", "cleanup", "char"])
        logger.info.assert_any_call("助手已关闭")

        # 重复关闭：不重试任何步骤
        order.clear()
        assistant.shutdown()
        self.assertEqual(order, [])
        self.assertEqual(tts.shutdown.call_count, 1)
        self.assertEqual(cleaner.stop_auto_cleanup.call_count, 1)
        self.assertEqual(cleaner.cleanup_now.call_count, 1)
        self.assertEqual(character.close.call_count, 1)

        for obj in (llm, stt, windows, searcher):
            self.assertFalse(obj.close.called)
            self.assertFalse(obj.shutdown.called)

    def test_optional_components_missing_still_succeeds(self) -> None:
        logger = MagicMock()
        assistant = self._assistant(logger=logger)
        assistant.shutdown()
        logger.info.assert_any_call("助手已关闭")
        assistant.shutdown()
        # 第二次不重复成功日志要求不严格；至少不再错误
        self.assertFalse(
            any(
                "部分资源释放失败" in str(c)
                for c in logger.error.call_args_list
            )
        )

    def test_tts_shutdown_failure_continues(self) -> None:
        order: List[str] = []
        tts = FakeTTS()
        cleaner = FakeCleaner()
        character = FakeCharacter()
        logger = MagicMock()
        tts.shutdown = MagicMock(side_effect=RuntimeError("tts boom"))
        cleaner.stop_auto_cleanup = MagicMock(side_effect=lambda: order.append("stop"))
        cleaner.cleanup_now = MagicMock(side_effect=lambda: order.append("cleanup"))
        character.close = MagicMock(side_effect=lambda: order.append("char"))

        assistant = self._assistant(
            logger=logger,
            tts_engine=tts,
            file_cleaner=cleaner,
            character_interface=character,
        )
        assistant.shutdown()
        self.assertEqual(order, ["stop", "cleanup", "char"])
        self.assertTrue(
            any("tts_engine" in str(c) and "shutdown" in str(c) for c in logger.error.call_args_list)
        )
        self.assertTrue(
            any("部分资源释放失败" in str(c) for c in logger.error.call_args_list)
        )
        success_msgs = [c for c in logger.info.call_args_list if c == call("助手已关闭")]
        self.assertEqual(success_msgs, [])

    def test_cleaner_stop_failure_skips_cleanup_still_closes_character(self) -> None:
        order: List[str] = []
        logger = MagicMock()
        tts = FakeTTS()
        cleaner = FakeCleaner()
        character = FakeCharacter()
        tts.shutdown = MagicMock(side_effect=lambda: order.append("tts"))
        cleaner.stop_auto_cleanup = MagicMock(side_effect=RuntimeError("stop boom"))
        cleaner.cleanup_now = MagicMock(side_effect=lambda: order.append("cleanup"))
        character.close = MagicMock(side_effect=lambda: order.append("char"))

        assistant = self._assistant(
            logger=logger,
            tts_engine=tts,
            file_cleaner=cleaner,
            character_interface=character,
        )
        assistant.shutdown()
        self.assertEqual(order, ["tts", "char"])
        cleaner.cleanup_now.assert_not_called()
        self.assertTrue(
            any("stop_auto_cleanup" in str(c) for c in logger.error.call_args_list)
        )
        self.assertTrue(
            any("跳过 cleanup_now" in str(c) for c in logger.warning.call_args_list)
        )

    def test_cleanup_now_failure_still_closes_character(self) -> None:
        order: List[str] = []
        logger = MagicMock()
        tts = FakeTTS()
        cleaner = FakeCleaner()
        character = FakeCharacter()
        tts.shutdown = MagicMock(side_effect=lambda: order.append("tts"))
        cleaner.stop_auto_cleanup = MagicMock(side_effect=lambda: order.append("stop"))
        cleaner.cleanup_now = MagicMock(side_effect=RuntimeError("cleanup boom"))
        character.close = MagicMock(side_effect=lambda: order.append("char"))

        assistant = self._assistant(
            logger=logger,
            tts_engine=tts,
            file_cleaner=cleaner,
            character_interface=character,
        )
        assistant.shutdown()
        self.assertEqual(order, ["tts", "stop", "char"])
        self.assertTrue(
            any("cleanup_now" in str(c) for c in logger.error.call_args_list)
        )

    def test_character_close_failure_recorded(self) -> None:
        logger = MagicMock()
        character = FakeCharacter()
        character.close = MagicMock(side_effect=RuntimeError("char boom"))
        assistant = self._assistant(logger=logger, character_interface=character)
        assistant.shutdown()
        self.assertTrue(
            any("character_interface" in str(c) and "close" in str(c) for c in logger.error.call_args_list)
        )
        self.assertTrue(
            any("部分资源释放失败" in str(c) for c in logger.error.call_args_list)
        )

    def test_multiple_failures_recorded_no_retry_on_repeat(self) -> None:
        logger = MagicMock()
        tts = FakeTTS()
        cleaner = FakeCleaner()
        character = FakeCharacter()
        tts.shutdown = MagicMock(side_effect=RuntimeError("t"))
        cleaner.stop_auto_cleanup = MagicMock(side_effect=RuntimeError("s"))
        cleaner.cleanup_now = MagicMock()
        character.close = MagicMock(side_effect=RuntimeError("c"))

        assistant = self._assistant(
            logger=logger,
            tts_engine=tts,
            file_cleaner=cleaner,
            character_interface=character,
        )
        assistant.shutdown()
        self.assertEqual(tts.shutdown.call_count, 1)
        self.assertEqual(cleaner.stop_auto_cleanup.call_count, 1)
        cleaner.cleanup_now.assert_not_called()
        self.assertEqual(character.close.call_count, 1)
        err_text = " ".join(str(c) for c in logger.error.call_args_list)
        self.assertIn("tts_engine", err_text)
        self.assertIn("stop_auto_cleanup", err_text)
        self.assertIn("character_interface", err_text)
        self.assertIn("部分资源释放失败", err_text)

        assistant.shutdown()
        self.assertEqual(tts.shutdown.call_count, 1)
        self.assertEqual(cleaner.stop_auto_cleanup.call_count, 1)
        self.assertEqual(character.close.call_count, 1)
        cleaner.cleanup_now.assert_not_called()


class DefaultAssemblyWiringTests(unittest.TestCase):
    def test_assemble_default_passes_config_and_starts_cleaner(self) -> None:
        recorded: Dict[str, Any] = {}
        config = MemoryConfig(
            {
                "ollama": {
                    "base_url": "http://x",
                    "model": "m",
                    "timeout": 9,
                    "keep_alive": "15m",
                    "warmup_on_start": True,
                    "warmup_timeout": 42,
                },
                "voice": {
                    "tts": {
                        "engine": "edge-tts",
                        "language": "zh-CN",
                        "rate": 120,
                        "volume": 0.5,
                        "voice": "v1",
                        "fallback_enabled": False,
                        "fallback_engine": "pyttsx3",
                        "pipeline_enabled": False,
                    },
                    "stt": {
                        "engine": "speech_recognition",
                        "language": "zh-CN",
                        "energy_threshold": 1,
                    },
                },
                "system": {
                    "windows_control_enabled": True,
                    "require_confirmation": False,
                },
                "search": {
                    "chrome_path": None,
                    "search_engine": "bing",
                    "auto_open": False,
                },
                "extensions": {"character_model": {"enabled": False}},
            }
        )

        def load_config(path: Optional[str]) -> MemoryConfig:
            recorded["config_path"] = path
            return config

        def logging_setup(cfg: Any) -> str:
            recorded["logged"] = cfg
            return "log-path"

        def prepare_dirs() -> None:
            recorded["dirs"] = True

        def ollama_cls(**kwargs: Any) -> MagicMock:
            obj = MagicMock(name="llm")
            recorded["ollama"] = kwargs
            recorded["llm_obj"] = obj
            return obj

        def tts_cls(**kwargs: Any) -> MagicMock:
            obj = MagicMock(name="tts")
            recorded["tts"] = kwargs
            recorded["tts_obj"] = obj
            return obj

        def stt_cls(**kwargs: Any) -> MagicMock:
            obj = MagicMock(name="stt")
            recorded["stt"] = kwargs
            recorded["stt_obj"] = obj
            return obj

        def windows_cls(**kwargs: Any) -> MagicMock:
            obj = MagicMock(name="win")
            recorded["windows"] = kwargs
            recorded["win_obj"] = obj
            return obj

        def search_cls(**kwargs: Any) -> MagicMock:
            obj = MagicMock(name="search")
            recorded["search"] = kwargs
            recorded["search_obj"] = obj
            return obj

        cleaner = FakeCleaner()

        def cleaner_factory(cfg: Any, safety_policy: Any = None) -> FakeCleaner:
            recorded["cleaner_cfg"] = cfg
            recorded["cleaner_policy"] = safety_policy
            return cleaner

        parts = assemble_default(
            "cfg.yaml",
            config_loader=load_config,
            logging_setup=logging_setup,
            dir_preparer=prepare_dirs,
            ollama_client_cls=ollama_cls,
            tts_engine_cls=tts_cls,
            stt_engine_cls=stt_cls,
            windows_controller_cls=windows_cls,
            chrome_searcher_cls=search_cls,
            file_cleaner_from_config=cleaner_factory,
            start_cleaner=True,
        )

        self.assertEqual(recorded["config_path"], "cfg.yaml")
        self.assertTrue(recorded["dirs"])
        self.assertIs(recorded["logged"], config)
        self.assertEqual(recorded["ollama"]["base_url"], "http://x")
        self.assertEqual(recorded["ollama"]["model"], "m")
        self.assertEqual(recorded["ollama"]["keep_alive"], "15m")
        recorded["llm_obj"].start_warmup.assert_called_once_with(timeout=42.0)
        self.assertEqual(recorded["tts"]["voice"], "v1")
        self.assertEqual(recorded["tts"]["pipeline_enabled"], False)
        self.assertIs(recorded["tts"]["safety_policy"], parts.safety_policy)
        self.assertEqual(recorded["stt"]["energy_threshold"], 1)
        self.assertEqual(recorded["windows"]["require_confirmation"], False)
        self.assertEqual(recorded["search"]["search_engine"], "bing")
        self.assertEqual(cleaner.start_calls, 1)
        self.assertIsInstance(parts.router, IntentRouter)
        self.assertIs(parts.llm_client, recorded["llm_obj"])
        self.assertIs(parts.tts_engine, recorded["tts_obj"])
        self.assertIs(parts.windows_controller, recorded["win_obj"])
        self.assertIs(parts.chrome_searcher, recorded["search_obj"])
        handlers = parts.router.handlers
        self.assertIs(handlers[0].controller, parts.windows_controller)
        self.assertIs(handlers[1].searcher, parts.chrome_searcher)
        self.assertIs(handlers[2].llm_client, parts.llm_client)

    def test_miku_default_ctor_uses_assemble_default(self) -> None:
        parts = MagicMock()
        parts.config = MemoryConfig()
        parts.safety_policy = MagicMock()
        parts.log_file = None
        parts.llm_client = None
        parts.tts_engine = None
        parts.stt_engine = None
        parts.windows_controller = None
        parts.chrome_searcher = None
        parts.character_interface = None
        parts.file_cleaner = None
        parts.router = FakeRouter("from-default")
        parts.logger = None

        with patch("src.core.assistant.assemble_default", return_value=parts) as assemble:
            assistant = MikuAssistant("path.yaml")
            assemble.assert_called_once_with("path.yaml")
            self.assertEqual(
                assistant.process_text_input("n", use_tts=False), "from-default"
            )
            self.assertIs(assistant.logger, logging.getLogger("src.core.assistant"))


if __name__ == "__main__":
    unittest.main()
