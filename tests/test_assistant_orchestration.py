"""助手编排离线测试：注入路径 + 默认可替换装配（无真实设备）。"""
from __future__ import annotations

import logging
import threading
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, call, patch

from src.core.assembly import assemble_default
from src.core.assistant import MikuAssistant
from src.core.dispatch_outcome import DispatchOutcome
from src.core.owned_thread import OwnedThreadTask
from src.core.router import IntentRouter
from src.core.speech_queue import PlaybackStatus, TurnSpeechPlayer
from src.llm.ollama_client import OllamaClient
from src.voice.cosyvoice.client import CosyVoiceShutdownStatus
from src.voice.stt import STTEngine
from src.core.shutdown_status import (
    TtsShutdownStatus,
    engine_declares_aggregate_shutdown_status,
    engine_declares_shutdown_api,
)
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


def _wait_player_turn(
    assistant: MikuAssistant, *, timeout: float = 3.0
) -> PlaybackStatus:
    player = assistant._speech_player  # noqa: SLF001
    turn = assistant._active_turn  # noqa: SLF001
    if player is None or turn is None or not turn.speech_turn_id:
        raise AssertionError("expected active speech turn")
    return player.wait_for_turn(turn.speech_turn_id, timeout=timeout)


def _require_player_quiet(player: TurnSpeechPlayer, *, timeout: float = 3.0) -> None:
    if not player.shutdown(timeout=timeout):
        raise AssertionError("TurnSpeechPlayer did not shut down quietly")
    if not player._owner.snapshot().quiet:  # noqa: SLF001
        raise AssertionError("OwnedThreadTask not quiet after player shutdown")


def _shutdown_assistant_with_player(
    assistant: MikuAssistant,
    *,
    release: Optional[threading.Event] = None,
    player_timeout: float = 3.0,
) -> None:
    if release is not None:
        release.set()
    player = assistant._speech_player  # noqa: SLF001
    if player is not None:
        _require_player_quiet(player, timeout=player_timeout)
    assistant.shutdown()
    if not assistant._shutdown_flow_done:  # noqa: SLF001
        raise AssertionError("assistant shutdown_flow not complete")


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
        assistant = MikuAssistant.from_dependencies(config, **kwargs)
        self.addCleanup(_shutdown_assistant_with_player, assistant)
        return assistant

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

        assistant.process_text_input("x", use_tts=False)
        self.assertEqual(tts.speak_calls, [])
        self.assertIsNone(assistant._speech_player)  # noqa: SLF001

        config.data["assistant"]["auto_tts"] = False
        assistant.process_text_input("y", use_tts=True)
        self.assertEqual(_wait_player_turn(assistant), PlaybackStatus.COMPLETED)
        self.assertEqual(tts.speak_calls, ["hello"])

    def test_use_tts_none_reads_config(self) -> None:
        tts = FakeTTS()
        router = FakeRouter("plain")
        config = MemoryConfig({"assistant": {"auto_tts": False}})
        assistant = self._make(config=config, router=router, tts_engine=tts)  # type: ignore[arg-type]
        assistant.process_text_input("z", use_tts=None)
        self.assertEqual(tts.speak_calls, [])

        config.data["assistant"]["auto_tts"] = True
        assistant.process_text_input("z2", use_tts=None)
        self.assertEqual(_wait_player_turn(assistant), PlaybackStatus.COMPLETED)
        self.assertEqual(tts.speak_calls, ["plain"])

    def test_legacy_playback_uses_player_not_per_reply_thread(self) -> None:
        tts = FakeTTS()
        router = FakeRouter("Miku: queued")
        assistant = self._make(router=router, tts_engine=tts)  # type: ignore[arg-type]
        assistant.process_text_input("a", use_tts=True)
        self.assertIsNotNone(assistant._speech_player)  # noqa: SLF001
        self.assertEqual(_wait_player_turn(assistant), PlaybackStatus.COMPLETED)
        self.assertEqual(tts.speak_calls, ["queued"])

    def test_character_forward(self) -> None:
        router = FakeRouter("char-msg")
        character = FakeCharacter(initialized=True)
        assistant = self._make(router=router, character_interface=character)  # type: ignore[arg-type]
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

    def test_cosy_incomplete_shutdown_allows_tts_retry_before_tfm(self) -> None:
        class CosyTTS:
            def __init__(self) -> None:
                self.calls = 0
                self.done = False

            def shutdown(self) -> None:
                self.calls += 1

            def local_cosyvoice_shutdown_status(self):
                return CosyVoiceShutdownStatus(
                    complete=self.done,
                    worker_alive=not self.done,
                    reader_alive=False,
                    writer_alive=False,
                )

        tts = CosyTTS()
        logger = MagicMock()
        assistant = self._assistant(logger=logger, tts_engine=tts)  # type: ignore[arg-type]
        assistant.shutdown()
        self.assertEqual(tts.calls, 1)
        self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001
        tts.done = True
        assistant.shutdown()
        self.assertEqual(tts.calls, 2)
        self.assertTrue(assistant._tts_shutdown_done)  # noqa: SLF001

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

    def test_multiple_failures_recorded_retry_incomplete_only(self) -> None:
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
        self.assertFalse(assistant._shutdown_flow_done)  # noqa: SLF001

        # 未完成项可有界重试；仍失败则再次保持 incomplete
        assistant.shutdown()
        self.assertEqual(tts.shutdown.call_count, 2)
        self.assertEqual(cleaner.stop_auto_cleanup.call_count, 2)
        self.assertEqual(character.close.call_count, 2)
        cleaner.cleanup_now.assert_not_called()
        self.assertFalse(assistant._shutdown_flow_done)  # noqa: SLF001

        # 此后成功：exactly-once 完成后不再重复
        tts.shutdown = MagicMock()
        cleaner.stop_auto_cleanup = MagicMock()
        character.close = MagicMock()
        assistant.tts_engine.shutdown = tts.shutdown
        assistant.file_cleaner.stop_auto_cleanup = cleaner.stop_auto_cleanup
        assistant.character_interface.close = character.close
        assistant.shutdown()
        self.assertEqual(tts.shutdown.call_count, 1)
        self.assertEqual(cleaner.stop_auto_cleanup.call_count, 1)
        self.assertEqual(character.close.call_count, 1)
        self.assertTrue(assistant._shutdown_flow_done)  # noqa: SLF001
        assistant.shutdown()
        self.assertEqual(tts.shutdown.call_count, 1)
        self.assertEqual(cleaner.stop_auto_cleanup.call_count, 1)
        self.assertEqual(character.close.call_count, 1)


def _assistant_tts_shutdown_shell() -> TTSEngine:
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
    engine.logger = logging.getLogger("test.assistant.tts_shutdown")
    engine._interrupt_event = threading.Event()  # noqa: SLF001
    engine._interrupt_epoch_lock = threading.Lock()  # noqa: SLF001
    engine._interrupt_epoch = 0  # noqa: SLF001
    engine._turn_stream_session = None  # noqa: SLF001
    engine._turn_stream_session_lock = threading.Lock()  # noqa: SLF001
    engine._pyttsx3 = None  # noqa: SLF001
    engine._tts_shutdown_status = None  # noqa: SLF001
    return engine


class TtsShutdownPropagationTests(unittest.TestCase):
    def _assistant(self, **kwargs) -> MikuAssistant:
        logger = kwargs.pop("logger", MagicMock(name="logger"))
        return MikuAssistant.from_dependencies(
            MemoryConfig(),
            logger=logger,
            router=kwargs.pop("router", FakeRouter()),  # type: ignore[arg-type]
            **kwargs,
        )

    def test_rvc_incomplete_keeps_tts_and_runs_cleaner(self) -> None:
        engine = _assistant_tts_shutdown_shell()
        outcomes = iter([False, True])
        cleaner = FakeCleaner()
        order: list[str] = []

        def _stop() -> bool:
            return next(outcomes)

        with patch("src.voice.rvc.infer.stop_rvc_worker", side_effect=_stop):
            cleaner.stop_auto_cleanup = MagicMock(
                side_effect=lambda: order.append("stop")
            )
            cleaner.cleanup_now = MagicMock(
                side_effect=lambda: order.append("cleanup")
            )
            assistant = self._assistant(tts_engine=engine, file_cleaner=cleaner)
            assistant.shutdown()
            self.assertIs(assistant.tts_engine, engine)
            self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001
            self.assertEqual(order, ["stop", "cleanup"])
            assistant.shutdown()
        self.assertTrue(assistant._tts_shutdown_done)  # noqa: SLF001
        self.assertTrue(engine.get_shutdown_status().complete)

    def test_voice_loading_blocks_shared_tfm_shutdown(self) -> None:
        engine = _assistant_tts_shutdown_shell()
        mgr = MagicMock()
        mgr.shutdown = MagicMock(return_value=MagicMock(complete=True))
        with patch("src.voice.rvc.infer.stop_rvc_worker", return_value=True):
            assistant = self._assistant(tts_engine=engine)
            assistant._temp_file_manager = mgr  # noqa: SLF001
            assistant.temp_file_manager = mgr
            assistant._voice_loading = True  # noqa: SLF001
            assistant.shutdown()
        self.assertTrue(assistant._tts_shutdown_done)  # noqa: SLF001
        self.assertFalse(assistant._temp_shutdown_done)  # noqa: SLF001
        mgr.shutdown.assert_not_called()

    def test_magicmock_tts_does_not_require_aggregate_status(self) -> None:
        tts = MagicMock()
        tts.shutdown = MagicMock()
        assistant = self._assistant(tts_engine=tts)
        assistant.shutdown()
        self.assertFalse(engine_declares_aggregate_shutdown_status(tts))
        self.assertTrue(assistant._tts_shutdown_done)  # noqa: SLF001

    def test_declared_adapter_incomplete_not_marked_done(self) -> None:
        class Adapter:
            engine_type = "edge-tts"

            def shutdown(self) -> None:
                pass

            def get_shutdown_status(self):
                return TtsShutdownStatus(
                    complete=False,
                    local_tts_complete=False,
                    persistent_rvc_complete=False,
                    rvc_subprocess_complete=False,
                    synthesis_idle=False,
                    temp_files_complete=False,
                )

            def local_cosyvoice_shutdown_status(self):
                return None

        assistant = self._assistant(tts_engine=Adapter())
        assistant.shutdown()
        self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001

    def test_non_boolean_aggregate_status_rejected(self) -> None:
        engine = _assistant_tts_shutdown_shell()
        with patch.object(engine, "shutdown"), patch.object(
            engine,
            "get_shutdown_status",
            return_value=TtsShutdownStatus(  # type: ignore[arg-type]
                complete=1,  # noqa: SLF001 — intentional malformed probe parity
                local_tts_complete=True,
                persistent_rvc_complete=True,
                rvc_subprocess_complete=True,
                synthesis_idle=True,
                temp_files_complete=True,
            ),
        ):
            assistant = self._assistant(tts_engine=engine)
            assistant.shutdown()
        self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001


class ShutdownStatusProtocolTests(unittest.TestCase):
    def test_class_declared_aggregate_api_detected(self) -> None:
        class Adapter:
            def get_shutdown_status(self):
                return TtsShutdownStatus(
                    True, True, True, True, True, True
                )

        self.assertTrue(engine_declares_aggregate_shutdown_status(Adapter()))

    def test_instance_declared_aggregate_api_detected(self) -> None:
        class Legacy:
            engine_type = "edge-tts"

            def shutdown(self) -> None:
                pass

        engine = Legacy()
        engine.get_shutdown_status = lambda: TtsShutdownStatus(  # type: ignore[attr-defined]
            False, False, False, False, False, False
        )
        self.assertTrue(engine_declares_aggregate_shutdown_status(engine))

    def test_undeclared_dynamic_mock_has_no_aggregate_api(self) -> None:
        tts = MagicMock()
        self.assertFalse(engine_declares_aggregate_shutdown_status(tts))
        self.assertFalse(
            engine_declares_shutdown_api(tts, "local_cosyvoice_shutdown_status")
        )

    def test_noncallable_declared_aggregate_fails_shutdown(self) -> None:
        class Engine:
            engine_type = "edge-tts"
            get_shutdown_status = None

            def shutdown(self) -> None:
                pass

        assistant = TtsShutdownPropagationTests()._assistant(tts_engine=Engine())
        assistant.shutdown()
        self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001

    def test_explicit_instance_mock_getter_respected(self) -> None:
        tts = MagicMock()
        tts.shutdown = MagicMock()
        tts.get_shutdown_status = lambda: TtsShutdownStatus(
            False, False, False, False, False, False
        )
        assistant = TtsShutdownPropagationTests()._assistant(tts_engine=tts)
        assistant.shutdown()
        self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001

    def test_legacy_without_declared_getters_still_succeeds(self) -> None:
        class Legacy:
            engine_type = "edge-tts"

            def shutdown(self) -> None:
                pass

        assistant = TtsShutdownPropagationTests()._assistant(tts_engine=Legacy())
        assistant.shutdown()
        self.assertTrue(assistant._tts_shutdown_done)  # noqa: SLF001


class LegacyBoundPlaybackTests(unittest.TestCase):
    """非流 ``process_text_input`` 复用 TurnSpeechPlayer，无逐回复 daemon 线程。"""

    def _assistant(
        self,
        *,
        router: Any,
        tts: Any,
        config: Optional[MemoryConfig] = None,
        character_interface: Optional[FakeCharacter] = None,
    ) -> MikuAssistant:
        cfg = config or MemoryConfig({"assistant": {"auto_tts": True}})
        assistant = MikuAssistant.from_dependencies(
            cfg,
            router=router,  # type: ignore[arg-type]
            tts_engine=tts,
            character_interface=character_interface,
        )
        self.addCleanup(_shutdown_assistant_with_player, assistant)
        return assistant

    def test_burst_returns_immediately_single_consumer_bounded_pending(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        spoken: List[str] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                entered.set()
                if not release.wait(timeout=5.0):
                    raise TimeoutError("release")

            def shutdown(self) -> None:
                return None

        router = FakeRouter("Miku: hold")
        assistant = self._assistant(router=router, tts=GatedTTS())
        self.addCleanup(_shutdown_assistant_with_player, assistant, release=release)
        self.assertEqual(assistant.process_text_input("first", use_tts=True), "Miku: hold")
        self.assertTrue(entered.wait(timeout=3.0), "consumer should enter speak")
        player = assistant._speech_player  # noqa: SLF001
        self.assertIsNotNone(player)

        for i in range(200):
            reply = assistant.process_text_input(f"burst-{i}", use_tts=True)
            self.assertEqual(reply, "Miku: hold")

        consumers = [
            t
            for t in threading.enumerate()
            if t.name == "TurnSpeechPlayer" and t.is_alive()
        ]
        self.assertEqual(len(consumers), 1)

        active = assistant._active_turn  # noqa: SLF001
        assert player is not None and active is not None and active.speech_turn_id
        release.set()
        status = player.wait_for_turn(active.speech_turn_id, timeout=5.0)
        self.assertIn(
            status,
            (PlaybackStatus.COMPLETED, PlaybackStatus.INVALIDATED),
        )
        self.assertIn("hold", spoken[-1] if spoken else "")

    def test_stale_turn_side_effect_does_not_claim_active(self) -> None:
        tts = FakeTTS()
        assistant = self._assistant(router=FakeRouter("ok"), tts=tts)
        stale = assistant._begin_generation_turn(external_cancel=None)  # noqa: SLF001
        assert stale is not None
        assistant._begin_generation_turn(external_cancel=None)  # noqa: SLF001
        outcome = DispatchOutcome(text="Miku: late", allow_tts=True, allow_character=False)
        assistant._apply_outcome_side_effects(  # noqa: SLF001
            outcome, use_tts=True, use_legacy_thread=True, turn=stale
        )
        self.assertEqual(tts.speak_calls, [])
        self.assertFalse(stale.speech_turn_id)

    def test_latest_turn_plays_after_rapid_inputs(self) -> None:
        release = threading.Event()
        spoken: List[str] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                if not release.wait(timeout=5.0):
                    raise TimeoutError("release")

            def shutdown(self) -> None:
                return None

        class CountRouter:
            def __init__(self) -> None:
                self.n = 0

            def dispatch(self, text: str) -> str:
                self.n += 1
                return f"Miku: line-{self.n}"

        router = CountRouter()
        assistant = self._assistant(router=router, tts=GatedTTS())
        release.set()
        for _ in range(5):
            assistant.process_text_input("x", use_tts=True)
        active = assistant._active_turn  # noqa: SLF001
        assert active is not None
        player = assistant._speech_player  # noqa: SLF001
        assert player is not None
        status = player.wait_for_turn(active.speech_turn_id, timeout=3.0)
        self.assertEqual(status, PlaybackStatus.COMPLETED)
        self.assertEqual(spoken[-1], "line-5")

    def test_allow_tts_false_skips_player(self) -> None:
        class SilentRouter:
            def dispatch_outcome(self, text: str) -> DispatchOutcome:
                return DispatchOutcome(
                    text="Miku: quiet",
                    allow_tts=False,
                    allow_character=False,
                )

        tts = FakeTTS()
        assistant = self._assistant(router=SilentRouter(), tts=tts)
        self.assertEqual(assistant.process_text_input("q", use_tts=True), "Miku: quiet")
        self.assertEqual(tts.speak_calls, [])
        self.assertIsNone(assistant._speech_player)  # noqa: SLF001

    def test_shutdown_waits_player_then_retries(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        spoken: List[str] = []

        class GatedTTS:
            def speak(self, text: str) -> None:
                spoken.append(text)
                entered.set()
                if not release.wait(timeout=5.0):
                    raise TimeoutError("release")

            def shutdown(self) -> None:
                return None

        assistant = self._assistant(router=FakeRouter("Miku: bye"), tts=GatedTTS())
        self.addCleanup(_shutdown_assistant_with_player, assistant, release=release)
        assistant.process_text_input("z", use_tts=True)
        self.assertTrue(entered.wait(timeout=3.0), "speak should start")
        player = assistant._speech_player  # noqa: SLF001
        assert player is not None
        self.assertFalse(player.shutdown(timeout=0.05))
        release.set()
        self.assertTrue(player.shutdown(timeout=3.0))

    def test_legacy_enqueue_bootstrap_runtimeerror_keeps_reply_and_character(
        self,
    ) -> None:
        character = FakeCharacter()
        router = FakeRouter("Miku: spoken")
        logger = MagicMock()
        cfg = MemoryConfig({"assistant": {"auto_tts": True}})
        assistant = MikuAssistant.from_dependencies(
            cfg,
            router=router,  # type: ignore[arg-type]
            tts_engine=FakeTTS(),
            character_interface=character,
            logger=logger,
        )
        self.addCleanup(_shutdown_assistant_with_player, assistant)
        boom = RuntimeError("owned start failed")

        def fail_start(_self: OwnedThreadTask) -> bool:
            raise boom

        with patch.object(OwnedThreadTask, "start", fail_start):
            text = assistant.process_text_input("q", use_tts=True)
        self.assertEqual(text, "Miku: spoken")
        self.assertEqual(character.messages, ["Miku: spoken"])
        active = assistant._active_turn  # noqa: SLF001
        assert active is not None
        self.assertFalse(active.speech_turn_id)
        logger.error.assert_called()
        self.assertIn("非流播报失败", logger.error.call_args[0][0])

    def test_legacy_enqueue_bootstrap_keyboard_interrupt_reraises_after_release(
        self,
    ) -> None:
        ki = KeyboardInterrupt("bootstrap")
        assistant = self._assistant(router=FakeRouter("x"), tts=FakeTTS())
        with patch.object(OwnedThreadTask, "start", side_effect=ki):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                assistant.process_text_input("q", use_tts=True)
            self.assertIs(ctx.exception, ki)
        active = assistant._active_turn  # noqa: SLF001
        assert active is not None
        self.assertFalse(active.speech_turn_id)

    def test_legacy_enqueue_bootstrap_system_exit_reraises_after_release(self) -> None:
        exit_obj = SystemExit(9)
        assistant = self._assistant(router=FakeRouter("x"), tts=FakeTTS())
        with patch.object(OwnedThreadTask, "start", side_effect=exit_obj):
            with self.assertRaises(SystemExit) as ctx:
                assistant.process_text_input("q", use_tts=True)
            self.assertIs(ctx.exception, exit_obj)
        active = assistant._active_turn  # noqa: SLF001
        assert active is not None
        self.assertFalse(active.speech_turn_id)

    def test_legacy_enqueue_bootstrap_generator_exit_reraises_after_release(
        self,
    ) -> None:
        assistant = self._assistant(router=FakeRouter("x"), tts=FakeTTS())

        def fail_start(_self: OwnedThreadTask) -> bool:
            raise GeneratorExit()

        with patch.object(OwnedThreadTask, "start", fail_start):
            with self.assertRaises(GeneratorExit):
                assistant.process_text_input("q", use_tts=True)
        active = assistant._active_turn  # noqa: SLF001
        assert active is not None
        self.assertFalse(active.speech_turn_id)

    def test_legacy_enqueue_bootstrap_custom_base_exception_reraises(self) -> None:
        class CustomFatal(BaseException):
            pass

        fatal = CustomFatal("bootstrap-fatal")
        assistant = self._assistant(router=FakeRouter("x"), tts=FakeTTS())
        with patch.object(OwnedThreadTask, "start", side_effect=fatal):
            with self.assertRaises(CustomFatal) as ctx:
                assistant.process_text_input("q", use_tts=True)
            self.assertIs(ctx.exception, fatal)
        active = assistant._active_turn  # noqa: SLF001
        assert active is not None
        self.assertFalse(active.speech_turn_id)


class DefaultAssemblyWiringTests(unittest.TestCase):
    def test_assemble_default_passes_config_and_starts_cleaner(self) -> None:
        recorded: Dict[str, Any] = {}
        config = MemoryConfig(
            {
                "ollama": {
                    "base_url": "http://127.0.0.1:9",
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
            startup_mode="voice",
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
        self.assertEqual(recorded["ollama"]["base_url"], "http://127.0.0.1:9")
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
        parts.memory_runtime = None
        parts.startup_mode = "interactive"
        parts.resource_settings = None
        parts.voice_autoload_enabled = False
        parts.tts_engine_cls = None
        parts.stt_engine_cls = None

        with patch("src.core.assistant.assemble_default", return_value=parts) as assemble:
            assistant = MikuAssistant("path.yaml")
            assemble.assert_called_once_with("path.yaml", startup_mode="interactive")
            self.assertEqual(
                assistant.process_text_input("n", use_tts=False), "from-default"
            )
            self.assertIs(assistant.logger, logging.getLogger("src.core.assistant"))


if __name__ == "__main__":
    unittest.main()
