"""默认装配失败回滚离线测试（fake 构造器，无真实设备）。"""
from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from src.core.assembly import assemble_default
from src.core.router import IntentRouter


class MemoryConfig:
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
        return None


def _base_config(*, character: bool = False) -> MemoryConfig:
    return MemoryConfig(
        {
            "ollama": {"base_url": "http://x", "model": "m", "timeout": 1},
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
            "search": {"chrome_path": None, "search_engine": "google", "auto_open": False},
            "extensions": {
                "character_model": {
                    "enabled": character,
                    "type": "unity",
                    "config": {},
                }
            },
        }
    )


class TrackingTTS:
    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs
        self.shutdown_calls = 0
        self.shutdown_error: Optional[BaseException] = None

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_error:
            raise self.shutdown_error


class TrackingCharacter:
    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs
        self.close_calls = 0
        self.close_error: Optional[BaseException] = None

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise self.close_error


class TrackingCleaner:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0
        self.cleanup_calls = 0
        self.start_error: Optional[BaseException] = None
        self.stop_error: Optional[BaseException] = None

    def start_auto_cleanup(self) -> None:
        self.start_calls += 1
        if self.start_error:
            raise self.start_error

    def stop_auto_cleanup(self) -> None:
        self.stop_calls += 1
        if self.stop_error:
            raise self.stop_error

    def cleanup_now(self) -> None:
        self.cleanup_calls += 1


class AssemblyRollbackTests(unittest.TestCase):
    def _common_kwargs(self, **overrides: Any) -> dict:
        cfg = overrides.pop("config", _base_config())
        kwargs = {
            "startup_mode": "voice",
            "config_loader": lambda path: cfg,
            "logging_setup": lambda c: None,
            "dir_preparer": lambda: None,
            "ollama_client_cls": lambda **kw: MagicMock(name="llm"),
            "tts_engine_cls": TrackingTTS,
            "stt_engine_cls": lambda **kw: MagicMock(name="stt"),
            "windows_controller_cls": lambda **kw: MagicMock(name="win"),
            "chrome_searcher_cls": lambda **kw: MagicMock(name="search"),
            "character_interface_cls": TrackingCharacter,
            "file_cleaner_from_config": lambda cfg, safety_policy=None: TrackingCleaner(),
            "start_cleaner": True,
        }
        kwargs.update(overrides)
        return kwargs

    def test_tts_reclaimed_when_later_component_fails(self) -> None:
        tts_holder: List[TrackingTTS] = []

        def tts_cls(**kwargs: Any) -> TrackingTTS:
            obj = TrackingTTS(**kwargs)
            tts_holder.append(obj)
            return obj

        def stt_cls(**kwargs: Any) -> Any:
            raise RuntimeError("stt explode")

        with self.assertRaises(RuntimeError) as ctx:
            assemble_default(**self._common_kwargs(tts_engine_cls=tts_cls, stt_engine_cls=stt_cls))
        self.assertEqual(str(ctx.exception), "stt explode")
        self.assertEqual(len(tts_holder), 1)
        self.assertEqual(tts_holder[0].shutdown_calls, 1)

    def test_fatal_assembly_failure_does_not_start_ollama_warmup(self) -> None:
        llm = MagicMock(name="llm")

        def stt_cls(**kwargs: Any) -> Any:
            raise RuntimeError("stt explode before ready")

        with self.assertRaisesRegex(RuntimeError, "stt explode before ready"):
            assemble_default(
                **self._common_kwargs(
                    ollama_client_cls=lambda **kw: llm,
                    stt_engine_cls=stt_cls,
                )
            )

        llm.start_warmup.assert_not_called()

    def test_character_and_cleaner_rollback_order_on_router_failure(self) -> None:
        order: List[str] = []
        cleaner = TrackingCleaner()

        def tts_cls(**kwargs: Any) -> TrackingTTS:
            obj = TrackingTTS(**kwargs)

            def _shutdown() -> None:
                order.append("tts")
                obj.shutdown_calls += 1

            obj.shutdown = _shutdown  # type: ignore[method-assign]
            return obj

        def char_cls(**kwargs: Any) -> TrackingCharacter:
            obj = TrackingCharacter(**kwargs)

            def _close() -> None:
                order.append("char")
                obj.close_calls += 1

            obj.close = _close  # type: ignore[method-assign]
            return obj

        def cleaner_factory(cfg: Any, safety_policy: Any = None) -> TrackingCleaner:
            def _stop() -> None:
                order.append("cleaner_stop")
                cleaner.stop_calls += 1

            cleaner.stop_auto_cleanup = _stop  # type: ignore[method-assign]
            return cleaner

        def router_builder(**kwargs: Any) -> IntentRouter:
            raise RuntimeError("router boom")

        with self.assertRaises(RuntimeError) as ctx:
            assemble_default(
                **self._common_kwargs(
                    config=_base_config(character=True),
                    tts_engine_cls=tts_cls,
                    character_interface_cls=char_cls,
                    file_cleaner_from_config=cleaner_factory,
                    router_builder=router_builder,
                )
            )
        self.assertIs(type(ctx.exception), RuntimeError)
        self.assertEqual(str(ctx.exception), "router boom")
        # 逆序：cleaner → character → tts
        self.assertEqual(order, ["cleaner_stop", "char", "tts"])
        self.assertEqual(cleaner.cleanup_calls, 0)

    def test_rollback_failure_continues_and_preserves_original_exception(self) -> None:
        original = RuntimeError("original assemble fail")

        def tts_cls(**kwargs: Any) -> TrackingTTS:
            obj = TrackingTTS(**kwargs)
            obj.shutdown_error = RuntimeError("tts rollback fail")
            return obj

        def stt_cls(**kwargs: Any) -> Any:
            raise original

        with self.assertRaises(RuntimeError) as ctx:
            assemble_default(**self._common_kwargs(tts_engine_cls=tts_cls, stt_engine_cls=stt_cls))
        self.assertIs(ctx.exception, original)

    def test_successful_assembly_does_not_reclaim_early(self) -> None:
        tts_holder: List[TrackingTTS] = []
        cleaner = TrackingCleaner()

        def tts_cls(**kwargs: Any) -> TrackingTTS:
            obj = TrackingTTS(**kwargs)
            tts_holder.append(obj)
            return obj

        parts = assemble_default(
            **self._common_kwargs(
                config=_base_config(character=True),
                tts_engine_cls=tts_cls,
                file_cleaner_from_config=lambda cfg, safety_policy=None: cleaner,
            )
        )
        self.assertIsInstance(parts.router, IntentRouter)
        self.assertEqual(tts_holder[0].shutdown_calls, 0)
        self.assertEqual(parts.character_interface.close_calls, 0)  # type: ignore[union-attr]
        self.assertEqual(cleaner.stop_calls, 0)
        self.assertEqual(cleaner.cleanup_calls, 0)
        self.assertEqual(cleaner.start_calls, 1)
        self.assertIs(parts.file_cleaner, cleaner)

    def test_cleaner_construct_failure_continues(self) -> None:
        def cleaner_factory(cfg: Any, safety_policy: Any = None) -> Any:
            raise RuntimeError("cleaner construct fail")

        parts = assemble_default(
            **self._common_kwargs(file_cleaner_from_config=cleaner_factory)
        )
        self.assertIsNone(parts.file_cleaner)
        self.assertIsNotNone(parts.tts_engine)
        self.assertIsInstance(parts.router, IntentRouter)

    def test_cleaner_start_failure_stop_ok_not_handed_to_assistant(self) -> None:
        cleaner = TrackingCleaner()
        cleaner.start_error = RuntimeError("start fail")

        parts = assemble_default(
            **self._common_kwargs(
                file_cleaner_from_config=lambda cfg, safety_policy=None: cleaner
            )
        )
        self.assertIsNone(parts.file_cleaner)
        self.assertEqual(cleaner.start_calls, 1)
        self.assertEqual(cleaner.stop_calls, 1)
        self.assertEqual(cleaner.cleanup_calls, 0)

    def test_cleaner_start_and_stop_failure_keeps_reference(self) -> None:
        cleaner = TrackingCleaner()
        cleaner.start_error = RuntimeError("start fail")
        cleaner.stop_error = RuntimeError("stop fail")

        parts = assemble_default(
            **self._common_kwargs(
                file_cleaner_from_config=lambda cfg, safety_policy=None: cleaner
            )
        )
        self.assertIs(parts.file_cleaner, cleaner)
        self.assertEqual(cleaner.start_calls, 1)
        self.assertEqual(cleaner.stop_calls, 1)
        self.assertEqual(cleaner.cleanup_calls, 0)

    def test_uncreated_components_not_reclaimed_no_cleanup_now(self) -> None:
        cleaner = TrackingCleaner()
        char_created = []

        def char_cls(**kwargs: Any) -> TrackingCharacter:
            char_created.append(True)
            return TrackingCharacter(**kwargs)

        def stt_cls(**kwargs: Any) -> Any:
            raise RuntimeError("fail before character")

        with self.assertRaises(RuntimeError):
            assemble_default(
                **self._common_kwargs(
                    config=_base_config(character=True),
                    stt_engine_cls=stt_cls,
                    character_interface_cls=char_cls,
                    file_cleaner_from_config=lambda cfg, safety_policy=None: cleaner,
                )
            )
        self.assertEqual(char_created, [])
        self.assertEqual(cleaner.start_calls, 0)
        self.assertEqual(cleaner.stop_calls, 0)
        self.assertEqual(cleaner.cleanup_calls, 0)

    def test_start_cleaner_false_still_registers_stop_on_router_failure(self) -> None:
        cleaner = TrackingCleaner()

        def router_builder(**kwargs: Any) -> IntentRouter:
            raise RuntimeError("router after idle cleaner")

        with self.assertRaises(RuntimeError) as ctx:
            assemble_default(
                **self._common_kwargs(
                    file_cleaner_from_config=lambda cfg, safety_policy=None: cleaner,
                    start_cleaner=False,
                    router_builder=router_builder,
                )
            )
        self.assertEqual(str(ctx.exception), "router after idle cleaner")
        self.assertEqual(cleaner.start_calls, 0)
        self.assertEqual(cleaner.stop_calls, 1)
        self.assertEqual(cleaner.cleanup_calls, 0)

    def test_multi_resource_rollback_failures_still_reclaim_tts(self) -> None:
        order: List[str] = []
        original = RuntimeError("router multi fail")
        cleaner = TrackingCleaner()

        def tts_cls(**kwargs: Any) -> TrackingTTS:
            obj = TrackingTTS(**kwargs)

            def _shutdown() -> None:
                order.append("tts")
                obj.shutdown_calls += 1

            obj.shutdown = _shutdown  # type: ignore[method-assign]
            return obj

        def char_cls(**kwargs: Any) -> TrackingCharacter:
            obj = TrackingCharacter(**kwargs)

            def _close() -> None:
                order.append("char")
                obj.close_calls += 1
                raise RuntimeError("char rollback fail")

            obj.close = _close  # type: ignore[method-assign]
            return obj

        def cleaner_factory(cfg: Any, safety_policy: Any = None) -> TrackingCleaner:
            def _stop() -> None:
                order.append("cleaner_stop")
                cleaner.stop_calls += 1
                raise RuntimeError("cleaner rollback fail")

            cleaner.stop_auto_cleanup = _stop  # type: ignore[method-assign]
            return cleaner

        def router_builder(**kwargs: Any) -> IntentRouter:
            raise original

        with self.assertLogs("src.core.assembly", level="ERROR") as logs:
            with self.assertRaises(RuntimeError) as ctx:
                assemble_default(
                    **self._common_kwargs(
                        config=_base_config(character=True),
                        tts_engine_cls=tts_cls,
                        character_interface_cls=char_cls,
                        file_cleaner_from_config=cleaner_factory,
                        router_builder=router_builder,
                    )
                )
        self.assertIs(ctx.exception, original)
        self.assertEqual(order, ["cleaner_stop", "char", "tts"])
        self.assertEqual(cleaner.stop_calls, 1)
        self.assertEqual(cleaner.cleanup_calls, 0)
        joined = "\n".join(logs.output)
        self.assertIn("file_cleaner.stop_auto_cleanup", joined)
        self.assertIn("character_interface.close", joined)
        self.assertIn("cleaner rollback fail", joined)
        self.assertIn("char rollback fail", joined)

    def test_start_fail_immediate_stop_fail_then_router_fail_two_phase_stop(self) -> None:
        """阶段一立即 stop 失败后，致命回滚阶段再尝试 stop 一次。"""
        original = RuntimeError("router after start/stop fail")
        cleaner = TrackingCleaner()
        cleaner.start_error = RuntimeError("start fail")
        cleaner.stop_error = RuntimeError("stop always fail")
        tts_holder: List[TrackingTTS] = []

        def tts_cls(**kwargs: Any) -> TrackingTTS:
            obj = TrackingTTS(**kwargs)
            tts_holder.append(obj)
            return obj

        def router_builder(**kwargs: Any) -> IntentRouter:
            raise original

        with self.assertLogs("src.core.assembly", level="ERROR") as logs:
            with self.assertRaises(RuntimeError) as ctx:
                assemble_default(
                    **self._common_kwargs(
                        tts_engine_cls=tts_cls,
                        file_cleaner_from_config=lambda cfg, safety_policy=None: cleaner,
                        router_builder=router_builder,
                    )
                )
        self.assertIs(ctx.exception, original)
        # 阶段一立即 stop 1 次 + 回滚阶段再 1 次
        self.assertEqual(cleaner.start_calls, 1)
        self.assertEqual(cleaner.stop_calls, 2)
        self.assertEqual(cleaner.cleanup_calls, 0)
        self.assertEqual(tts_holder[0].shutdown_calls, 1)
        joined = "\n".join(logs.output)
        self.assertIn("启动失败", joined)
        self.assertIn("装配回滚失败", joined)
        self.assertIn("file_cleaner.stop_auto_cleanup", joined)


if __name__ == "__main__":
    unittest.main()
