"""CosyVoice 生产切片 C2a：装配接线与助手共享 TFM 关闭状态（纯离线 fake）。"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from src.core.assembly import (
    assemble_default,
    build_assembly_safety_policy,
    build_tts_engine,
    validate_assembly_voice_tts_config,
)
from src.core.assistant import MikuAssistant
from src.core.safety import SafetyPolicy, TempFileShutdownStatus
from src.voice.audio.temp_files import TempFileManager
from src.voice.cosyvoice.client import CosyVoiceShutdownStatus
from tests.test_cosyvoice_production_slice_a import _seed_repo


class _MemConfig:
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
            "ollama": {
                "base_url": "http://127.0.0.1:9",
                "model": "m",
                "timeout": 1,
                "keep_alive": "30m",
            },
            "voice": {"tts": tts, "stt": {"engine": "speech_recognition"}},
            "security": {"permissions": {"local_tts_worker": False, "file_delete": True}},
            "system": {"windows_control_enabled": False},
            "search": {"auto_open": False},
            "extensions": {"character_model": {"enabled": False}},
            "resource": {
                "profile": "balanced",
                "ollama": {"unload_on_exit": False},
                "warmup": {"serialize_gpu": True, "order": "ollama_then_rvc"},
            },
        }
    )


class SpyTTS:
    last_kwargs: Dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        SpyTTS.last_kwargs = dict(kwargs)
        self.safety_policy = kwargs.get("safety_policy")

    def shutdown(self, **kwargs: Any) -> None:
        pass


class BuildTtsEngineWiringTests(unittest.TestCase):
    def test_edge_kwargs_omit_cosyvoice_config(self) -> None:
        SpyTTS.last_kwargs = {}
        policy = SafetyPolicy({"permissions": {"cloud_tts": True, "file_delete": True}})
        with patch("src.voice.tts.engine.AudioPlayer"), patch(
            "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
        ):
            build_tts_engine(
                _base_cfg(),
                policy,
                tts_engine_cls=SpyTTS,
                warm_rvc_on_init=False,
            )
        self.assertNotIn("cosyvoice_config", SpyTTS.last_kwargs)
        self.assertEqual(SpyTTS.last_kwargs.get("engine"), "edge-tts")

    def test_cosyvoice_passes_inner_config_and_shared_refs(self) -> None:
        SpyTTS.last_kwargs = {}
        with TemporaryDirectory() as tmp:
            policy, _ = _policy_sealed(tmp)
            mgr = TempFileManager(safety_policy=policy, default_dir=tmp)
            self.addCleanup(mgr.shutdown)
            cfg = _base_cfg(
                engine="cosyvoice",
                cosyvoice={"max_text_chars": 100},
            )
            with patch("src.voice.tts.engine.AudioPlayer"), patch(
                "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
            ):
                engine = build_tts_engine(
                    cfg,
                    policy,
                    tts_engine_cls=SpyTTS,
                    warm_rvc_on_init=False,
                    temp_file_manager=mgr,
                )
            self.assertEqual(SpyTTS.last_kwargs.get("engine"), "cosyvoice")
            self.assertEqual(
                SpyTTS.last_kwargs.get("cosyvoice_config"), {"max_text_chars": 100}
            )
            self.assertIs(SpyTTS.last_kwargs.get("safety_policy"), policy)
            self.assertIs(SpyTTS.last_kwargs.get("temp_file_manager"), mgr)
            self.assertIs(engine.safety_policy, policy)


def _policy_sealed(tmp: str):
    from tests.test_cosyvoice_production_slice_a import _policy_with_local_tts

    return _policy_with_local_tts(tmp)


class ValidateAssemblyConfigTests(unittest.TestCase):
    def test_rejects_invalid_cosyvoice_before_side_effects(self) -> None:
        cfg = _base_cfg(engine="cosyvoice", cosyvoice={"cpu_threads": True})
        with self.assertRaises(ValueError):
            validate_assembly_voice_tts_config(cfg)

    def test_rejects_non_bool_local_tts_permission_when_cosy(self) -> None:
        cfg = _base_cfg(engine="cosyvoice")
        cfg.data["security"] = {"permissions": {"local_tts_worker": "yes"}}
        with self.assertRaises(ValueError):
            validate_assembly_voice_tts_config(cfg)

    def test_assemble_skips_logging_when_validation_fails(self) -> None:
        cfg = _base_cfg(engine="cosyvoice", cosyvoice={"startup_timeout_sec": 61})
        logging_called = False

        def _logging(_c: Any) -> None:
            nonlocal logging_called
            logging_called = True

        with self.assertRaises(ValueError):
            assemble_default(
                startup_mode="text",
                config_loader=lambda _p: cfg,
                logging_setup=_logging,
                dir_preparer=lambda: None,
            )
        self.assertFalse(logging_called)


class AssemblySafetyPolicyTests(unittest.TestCase):
    def test_false_local_perm_does_not_seal(self) -> None:
        policy = build_assembly_safety_policy(_base_cfg(engine="cosyvoice"))
        self.assertFalse(policy.local_tts_profile_sealed())

    def test_cosy_true_perm_seals_when_assets_present(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_repo(root)
            cfg = _base_cfg(engine="cosyvoice")
            cfg.data["security"] = {"permissions": {"local_tts_worker": True}}
            with patch("src.core.assembly.PROJECT_ROOT", root):
                policy = build_assembly_safety_policy(cfg)
            self.assertTrue(policy.local_tts_profile_sealed())

    def test_seal_failure_falls_back_unsealed(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = _base_cfg(engine="cosyvoice")
            cfg.data["security"] = {"permissions": {"local_tts_worker": True}}
            with patch("src.core.assembly.PROJECT_ROOT", Path(tmp)):
                policy = build_assembly_safety_policy(cfg)
            self.assertFalse(policy.local_tts_profile_sealed())

    def test_non_local_permission_error_not_swallowed(self) -> None:
        cfg = _base_cfg()
        cfg.data["security"] = {"permissions": {"local_tts_worker": "bad"}}
        with self.assertRaises(ValueError):
            build_assembly_safety_policy(cfg)


class InteractiveModeAssemblyTests(unittest.TestCase):
    def test_text_mode_cosy_unsealed_still_assembles_without_tts(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = _base_cfg(engine="cosyvoice")
            cfg.data["security"] = {"permissions": {"local_tts_worker": True}}
            with patch("src.core.assembly.PROJECT_ROOT", Path(tmp)):
                parts = assemble_default(
                    startup_mode="interactive",
                    config_loader=lambda _p: cfg,
                    logging_setup=lambda _c: None,
                    dir_preparer=lambda: None,
                    ollama_client_cls=lambda **kw: MagicMock(name="llm"),
                    tts_engine_cls=SpyTTS,
                    stt_engine_cls=lambda **kw: MagicMock(name="stt"),
                    file_cleaner_from_config=lambda cfg, safety_policy=None: None,
                    start_cleaner=False,
                )
            self.assertIsNone(parts.tts_engine)
            self.assertFalse(parts.safety_policy.local_tts_profile_sealed())


class AssistantCosyShutdownTests(unittest.TestCase):
    def test_incomplete_local_shutdown_skips_shared_tfm_until_retry(self) -> None:
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        with TemporaryDirectory() as tmp:
            mgr = TempFileManager(safety_policy=policy, default_dir=tmp)
            self.addCleanup(mgr.shutdown)
            tfm_calls: List[str] = []

            def _mgr_shutdown(timeout: float = 2.0):
                tfm_calls.append("shutdown")
                return TempFileShutdownStatus(
                    complete=True,
                    owned=0,
                    pending=0,
                    inflight=0,
                    worker_alive=False, active_io_leases=0,
                )

            mgr.shutdown = _mgr_shutdown  # type: ignore[method-assign]

            class CosyTTS:
                def __init__(self) -> None:
                    self.shutdown_calls = 0
                    self._complete = False

                def shutdown(self) -> None:
                    self.shutdown_calls += 1

                def local_cosyvoice_shutdown_status(self):
                    return CosyVoiceShutdownStatus(
                        complete=self._complete,
                        worker_alive=not self._complete,
                        reader_alive=False,
                        writer_alive=False,
                    )

                def mark_complete(self) -> None:
                    self._complete = True

            tts = CosyTTS()
            logger = MagicMock()
            assistant = MikuAssistant.from_dependencies(
                _base_cfg(),
                logger=logger,
                safety_policy=policy,
                tts_engine=tts,
            )
            assistant._temp_file_manager = mgr  # noqa: SLF001

            assistant.shutdown()
            self.assertEqual(tts.shutdown_calls, 1)
            self.assertEqual(tfm_calls, [])
            self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001
            self.assertFalse(assistant._temp_shutdown_done)  # noqa: SLF001

            tts.mark_complete()
            assistant.shutdown()
            self.assertEqual(tts.shutdown_calls, 2)
            self.assertEqual(tfm_calls, ["shutdown"])

    def test_edge_fake_tts_keeps_legacy_shutdown_semantics(self) -> None:
        order: List[str] = []
        tts = MagicMock()
        tts.shutdown = MagicMock(side_effect=lambda: order.append("tts"))
        del tts.local_cosyvoice_shutdown_status

        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        with TemporaryDirectory() as tmp:
            mgr = TempFileManager(safety_policy=policy, default_dir=tmp)
            self.addCleanup(mgr.shutdown)
            mgr.shutdown = MagicMock(  # type: ignore[method-assign]
                side_effect=lambda timeout=2.0: (
                    order.append("tfm"),
                    TempFileShutdownStatus(
                        complete=True,
                        owned=0,
                        pending=0,
                        inflight=0,
                        worker_alive=False, active_io_leases=0,
                    ),
                )[1]
            )
            assistant = MikuAssistant.from_dependencies(
                _base_cfg(),
                logger=MagicMock(),
                safety_policy=policy,
                tts_engine=tts,
            )
            assistant._temp_file_manager = mgr  # noqa: SLF001
            assistant.shutdown()
            self.assertEqual(order, ["tts", "tfm"])

    def test_cosy_none_status_skips_shared_tfm(self) -> None:
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        with TemporaryDirectory() as tmp:
            mgr = TempFileManager(safety_policy=policy, default_dir=tmp)
            self.addCleanup(mgr.shutdown)
            tfm_calls: List[str] = []

            def _mgr_shutdown(timeout: float = 2.0):
                tfm_calls.append("shutdown")
                return TempFileShutdownStatus(
                    complete=True,
                    owned=0,
                    pending=0,
                    inflight=0,
                    worker_alive=False, active_io_leases=0,
                )

            mgr.shutdown = _mgr_shutdown  # type: ignore[method-assign]

            class CosyTTS:
                engine_type = "cosyvoice"

                def shutdown(self) -> None:
                    pass

                def local_cosyvoice_shutdown_status(self):
                    return None

            assistant = MikuAssistant.from_dependencies(
                _base_cfg(engine="cosyvoice"),
                logger=MagicMock(),
                safety_policy=policy,
                tts_engine=CosyTTS(),
            )
            assistant._temp_file_manager = mgr  # noqa: SLF001
            assistant.shutdown()
            self.assertEqual(tfm_calls, [])
            self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001

    def test_cosy_noncallable_status_skips_shared_tfm(self) -> None:
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        with TemporaryDirectory() as tmp:
            mgr = TempFileManager(safety_policy=policy, default_dir=tmp)
            self.addCleanup(mgr.shutdown)
            tfm_calls: List[str] = []

            def _mgr_shutdown(timeout: float = 2.0):
                tfm_calls.append("shutdown")
                return TempFileShutdownStatus(
                    complete=True,
                    owned=0,
                    pending=0,
                    inflight=0,
                    worker_alive=False, active_io_leases=0,
                )

            mgr.shutdown = _mgr_shutdown  # type: ignore[method-assign]

            class CosyTTS:
                engine_type = "cosyvoice"

                def shutdown(self) -> None:
                    pass

                local_cosyvoice_shutdown_status = None  # type: ignore[misc]

            assistant = MikuAssistant.from_dependencies(
                _base_cfg(engine="cosyvoice"),
                logger=MagicMock(),
                safety_policy=policy,
                tts_engine=CosyTTS(),
            )
            assistant._temp_file_manager = mgr  # noqa: SLF001
            assistant.shutdown()
            self.assertEqual(tfm_calls, [])
            self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001

    def test_status_reader_failure_does_not_block_independent_cleanup(self) -> None:
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        cleaner = MagicMock()
        character = MagicMock()

        class CosyTTS:
            engine_type = "cosyvoice"

            def shutdown(self) -> None:
                pass

            def local_cosyvoice_shutdown_status(self):
                raise RuntimeError("status_unavailable")

        assistant = MikuAssistant.from_dependencies(
            _base_cfg(engine="cosyvoice"),
            logger=MagicMock(),
            safety_policy=policy,
            tts_engine=CosyTTS(),
            file_cleaner=cleaner,
            character_interface=character,
        )
        assistant.shutdown()
        self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001
        cleaner.stop_auto_cleanup.assert_called_once()
        cleaner.cleanup_now.assert_called_once()
        character.close.assert_called_once()

    def test_cosy_status_recovery_three_step_retry(self) -> None:
        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        with TemporaryDirectory() as tmp:
            mgr = TempFileManager(safety_policy=policy, default_dir=tmp)
            self.addCleanup(mgr.shutdown)
            tfm_calls: List[str] = []

            def _mgr_shutdown(timeout: float = 2.0):
                tfm_calls.append("shutdown")
                return TempFileShutdownStatus(
                    complete=True,
                    owned=0,
                    pending=0,
                    inflight=0,
                    worker_alive=False, active_io_leases=0,
                )

            mgr.shutdown = _mgr_shutdown  # type: ignore[method-assign]

            class CosyTTS:
                engine_type = "cosyvoice"

                def __init__(self) -> None:
                    self.shutdown_calls = 0
                    self._phase = 0

                def shutdown(self) -> None:
                    self.shutdown_calls += 1

                def local_cosyvoice_shutdown_status(self):
                    if self._phase == 0:
                        return None
                    if self._phase == 1:
                        raise RuntimeError("status_unavailable")
                    return CosyVoiceShutdownStatus(
                        complete=True,
                        worker_alive=False,
                        reader_alive=False,
                        writer_alive=False,
                    )

                def advance(self) -> None:
                    self._phase += 1

            tts = CosyTTS()
            assistant = MikuAssistant.from_dependencies(
                _base_cfg(engine="cosyvoice"),
                logger=MagicMock(),
                safety_policy=policy,
                tts_engine=tts,
            )
            assistant._temp_file_manager = mgr  # noqa: SLF001

            assistant.shutdown()
            self.assertEqual(tts.shutdown_calls, 1)
            self.assertEqual(tfm_calls, [])
            self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001

            tts.advance()
            assistant.shutdown()
            self.assertEqual(tts.shutdown_calls, 2)
            self.assertEqual(tfm_calls, [])
            self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001

            tts.advance()
            assistant.shutdown()
            self.assertEqual(tts.shutdown_calls, 3)
            self.assertEqual(tfm_calls, ["shutdown"])
            self.assertTrue(assistant._tts_shutdown_done)  # noqa: SLF001

    def test_edge_declared_status_unknown_defers_tfm_until_complete(self) -> None:
        """声明 local_cosyvoice_shutdown_status 的适配器不得用 None 声称完成（ADR 决策 3）。"""
        order: List[str] = []

        class EdgeTTS:
            engine_type = "edge-tts"

            def __init__(self) -> None:
                self._complete = False

            def shutdown(self) -> None:
                order.append("tts")

            def local_cosyvoice_shutdown_status(self):
                if not self._complete:
                    return None
                return CosyVoiceShutdownStatus(
                    complete=True,
                    worker_alive=False,
                    reader_alive=False,
                    writer_alive=False,
                )

            def mark_complete(self) -> None:
                self._complete = True

        policy = SafetyPolicy({"permissions": {"file_delete": True}})
        with TemporaryDirectory() as tmp:
            mgr = TempFileManager(safety_policy=policy, default_dir=tmp)
            self.addCleanup(mgr.shutdown)
            mgr.shutdown = MagicMock(  # type: ignore[method-assign]
                side_effect=lambda timeout=2.0: (
                    order.append("tfm"),
                    TempFileShutdownStatus(
                        complete=True,
                        owned=0,
                        pending=0,
                        inflight=0,
                        worker_alive=False,
                        active_io_leases=0,
                    ),
                )[1]
            )
            tts = EdgeTTS()
            assistant = MikuAssistant.from_dependencies(
                _base_cfg(),
                logger=MagicMock(),
                safety_policy=policy,
                tts_engine=tts,
            )
            assistant._temp_file_manager = mgr  # noqa: SLF001

            assistant.shutdown()
            self.assertEqual(order, ["tts"])
            self.assertFalse(assistant._tts_shutdown_done)  # noqa: SLF001
            mgr.shutdown.assert_not_called()

            tts.mark_complete()
            assistant.shutdown()
            self.assertEqual(order, ["tts", "tts", "tfm"])
            self.assertTrue(assistant._tts_shutdown_done)  # noqa: SLF001
            mgr.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
