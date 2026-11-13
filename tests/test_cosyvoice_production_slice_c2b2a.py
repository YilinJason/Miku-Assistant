"""CosyVoice C2b2a：独立三模型串行准备协调器（纯离线 fake）。"""
from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

from src.core.local_voice_warmup import (
    STAGE_BUSY,
    STAGE_CANCELLED,
    STAGE_COSYVOICE,
    STAGE_OLLAMA,
    STAGE_RVC,
    STAGE_UNSUPPORTED,
    LocalVoiceWarmupResult,
    prepare_local_voice,
)
from src.core.resource_settings import ResourceSettings
from src.core.safety import SafetyPolicy
from src.llm.ollama_client import OllamaClient, OllamaWarmupSnapshot
from src.memory.aware_client import MemoryAwareLLMClient
from src.voice.tts.engine import TTSEngine

from tests.support.host_memory_fixtures import install_module_sufficient_host_memory

_SUPPORTED = ResourceSettings(
    serialize_gpu_warmup=True,
    warmup_order="ollama_then_rvc",
    ollama_warmup_on_start=True,
    rvc_warmup_on_voice_start=True,
    warmup_timeout=5.0,
)


def setUpModule() -> None:
    install_module_sufficient_host_memory()


class _TrackingCosyEngine:
    engine_type = "cosyvoice"
    events: List[str]

    def __init__(self) -> None:
        self.events = []
        self.prepare_rvc_calls = 0
        self.prepare_local_calls = 0
        self._rvc_result = True
        self._cosy_result = True

    def prepare_rvc(self) -> bool:
        self.prepare_rvc_calls += 1
        self.events.append("rvc")
        return self._rvc_result

    def prepare_local_tts(self, *, cancel_event=None) -> bool:
        self.prepare_local_calls += 1
        self.events.append("cosy")
        if cancel_event is not None and cancel_event.is_set():
            return False
        return self._cosy_result


class _FakeMemoryServices:
    def recall_for_prompt(self, *a, **k):
        return []

    def submit_turn(self, *a, **k):
        return None


class LocalVoiceWarmupCoordinatorTests(unittest.TestCase):
    def test_unsupported_profile_zero_side_effects(self) -> None:
        engine = _TrackingCosyEngine()
        settings = ResourceSettings(
            serialize_gpu_warmup=False,
            warmup_order="ollama_then_rvc",
            ollama_warmup_on_start=True,
            rvc_warmup_on_voice_start=True,
        )
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        with patch.object(llm, "start_warmup") as start:
            result = prepare_local_voice(llm, engine, settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.final_stage, STAGE_UNSUPPORTED)
        start.assert_not_called()
        self.assertEqual(engine.events, [])

    def test_strict_stage_order_on_success(self) -> None:
        engine = _TrackingCosyEngine()
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        with patch.object(llm, "warm_up", return_value=True):
            result = prepare_local_voice(llm, engine, _SUPPORTED)
        self.assertTrue(result.ok)
        self.assertEqual(engine.events, ["rvc", "cosy"])
        self.assertGreaterEqual(result.stage_elapsed_sec[STAGE_OLLAMA], 0.0)
        self.assertGreaterEqual(result.total_elapsed_sec, 0.0)

    def test_ollama_wait_done_but_failed_skips_rvc_and_cosy(self) -> None:
        engine = _TrackingCosyEngine()
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        with patch.object(llm, "warm_up", return_value=False):
            result = prepare_local_voice(llm, engine, _SUPPORTED)
        self.assertFalse(result.ok)
        self.assertEqual(result.final_stage, STAGE_OLLAMA)
        self.assertEqual(engine.events, [])

    def test_past_success_fresh_failure_skips_downstream(self) -> None:
        engine = _TrackingCosyEngine()
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        with patch.object(llm, "warm_up", return_value=True):
            self.assertTrue(llm.start_warmup(timeout=1))
            self.assertTrue(llm._warmup_done.wait(2))  # noqa: SLF001
        self.assertTrue(llm.warmup_snapshot().succeeded)
        llm._mark_model_touched()  # noqa: SLF001 — 历史触达，不得冒充本轮成功
        with patch.object(llm, "warm_up", return_value=False):
            result = prepare_local_voice(llm, engine, _SUPPORTED)
        self.assertFalse(result.ok)
        self.assertEqual(result.final_stage, STAGE_OLLAMA)
        self.assertEqual(engine.events, [])

    def test_rvc_false_blocks_cosy(self) -> None:
        engine = _TrackingCosyEngine()
        engine._rvc_result = False
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        with patch.object(llm, "warm_up", return_value=True):
            result = prepare_local_voice(llm, engine, _SUPPORTED)
        self.assertFalse(result.ok)
        self.assertEqual(result.final_stage, STAGE_RVC)
        self.assertEqual(engine.events, ["rvc"])

    def test_pure_cosy_skips_rvc(self) -> None:
        engine = _TrackingCosyEngine()
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        with patch.object(llm, "warm_up", return_value=True):
            result = prepare_local_voice(
                llm, engine, _SUPPORTED, require_rvc=False
            )
        self.assertTrue(result.ok)
        self.assertEqual(engine.events, ["cosy"])
        self.assertNotIn(STAGE_RVC, result.stage_elapsed_sec)

    def test_cancel_does_not_clear_external_event(self) -> None:
        engine = _TrackingCosyEngine()
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        external = threading.Event()
        external.set()
        result = prepare_local_voice(
            llm, engine, _SUPPORTED, cancel_event=external
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.final_stage, STAGE_CANCELLED)
        self.assertTrue(external.is_set())
        self.assertEqual(engine.events, [])

    def test_ollama_timeout_does_not_advance(self) -> None:
        engine = _TrackingCosyEngine()
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        release = threading.Event()
        entered = threading.Event()
        worker_error: List[BaseException] = []

        def blocked(*, timeout: float) -> bool:
            entered.set()
            release.wait(timeout=30.0)
            return True

        settings = ResourceSettings(
            serialize_gpu_warmup=True,
            warmup_order="ollama_then_rvc",
            ollama_warmup_on_start=True,
            rvc_warmup_on_voice_start=True,
            warmup_timeout=0.15,
        )

        timeout_results: List[Any] = []

        def run_prepare() -> None:
            try:
                with patch.object(llm, "warm_up", side_effect=blocked):
                    timeout_results.append(
                        prepare_local_voice(llm, engine, settings)
                    )
            except BaseException as exc:
                worker_error.append(exc)

        t0 = time.monotonic()
        worker = threading.Thread(target=run_prepare, name="WarmupTimeoutProbe")
        worker.start()
        self.assertTrue(entered.wait(2.0))
        worker.join(timeout=2.0)
        elapsed = time.monotonic() - t0
        release.set()
        worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker_error, [])
        self.assertEqual(len(timeout_results), 1)
        result = timeout_results[0]
        self.assertFalse(result.ok)
        self.assertEqual(result.final_stage, STAGE_OLLAMA)
        self.assertEqual(engine.events, [])
        self.assertLess(elapsed, 2.0)

    def test_concurrent_prepare_second_call_busy_zero_side_effects(self) -> None:
        engine = _TrackingCosyEngine()
        release_ollama = threading.Event()
        ollama_entered = threading.Event()
        gate = threading.Event()
        results: List[Any] = []

        class _SlowOllama:
            def begin_fresh_warmup(self, *, timeout: float) -> int:
                ollama_entered.set()
                gate.wait(timeout=5.0)
                return 1

            def warmup_snapshot(self) -> SimpleNamespace:
                finished = release_ollama.is_set()
                return SimpleNamespace(
                    generation=1,
                    finished=finished,
                    succeeded=finished,
                )

        llm = _SlowOllama()

        def first() -> None:
            results.append(prepare_local_voice(llm, engine, _SUPPORTED))

        def second() -> None:
            gate.wait(timeout=5.0)
            results.append(prepare_local_voice(llm, engine, _SUPPORTED))

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        self.assertTrue(ollama_entered.wait(2.0))
        gate.set()
        t2.start()
        t2.join(timeout=2.0)
        release_ollama.set()
        t1.join(timeout=5.0)
        self.assertEqual(len(results), 2)
        busy = [r for r in results if r.final_stage == STAGE_BUSY]
        success = [r for r in results if r.ok]
        self.assertEqual(len(busy), 1)
        self.assertEqual(len(success), 1)
        self.assertEqual(engine.events, ["rvc", "cosy"])

    def test_result_stage_mapping_is_immutable(self) -> None:
        engine = _TrackingCosyEngine()
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        with patch.object(llm, "warm_up", return_value=True):
            result = prepare_local_voice(llm, engine, _SUPPORTED)
        self.assertTrue(result.ok)
        with self.assertRaises((TypeError, AttributeError)):
            result.stage_elapsed_sec["fake"] = 1.0
        payload = result.to_report_dict()
        self.assertIn(STAGE_OLLAMA, payload["stage_elapsed_sec"])

    def test_invalid_warmup_timeout_rejected_before_actions(self) -> None:
        engine = _TrackingCosyEngine()
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        settings = ResourceSettings(
            serialize_gpu_warmup=True,
            warmup_order="ollama_then_rvc",
            ollama_warmup_on_start=True,
            rvc_warmup_on_voice_start=True,
            warmup_timeout=float("inf"),
        )
        with patch.object(llm, "begin_fresh_warmup") as begin:
            result = prepare_local_voice(llm, engine, settings)
        begin.assert_not_called()
        self.assertEqual(result.detail, "warmup_timeout_invalid")
        self.assertEqual(engine.events, [])

    def test_cosy_success_after_cancel_set_reports_cancelled(self) -> None:
        engine = _TrackingCosyEngine()
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        cancel = threading.Event()

        def cosy_sets_cancel(*, cancel_event=None) -> bool:
            cancel.set()
            return True

        with patch.object(llm, "warm_up", return_value=True):
            with patch.object(engine, "prepare_local_tts", side_effect=cosy_sets_cancel):
                result = prepare_local_voice(
                    llm, engine, _SUPPORTED, cancel_event=cancel
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.final_stage, STAGE_CANCELLED)

    def test_non_cosy_engine_zero_calls(self) -> None:
        engine = SimpleNamespace(engine_type="edge-tts")
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        with patch.object(llm, "start_warmup") as start:
            result = prepare_local_voice(llm, engine, _SUPPORTED)
        self.assertEqual(result.final_stage, STAGE_UNSUPPORTED)
        start.assert_not_called()

    def test_snapshot_property_error_returns_fixed_failure(self) -> None:
        engine = _TrackingCosyEngine()

        class _BadSnap:
            @property
            def generation(self) -> int:
                raise ValueError("private diagnostic")

        class _Llm:
            def begin_fresh_warmup(self, *, timeout: float) -> int:
                return 1

            def warmup_snapshot(self) -> _BadSnap:
                return _BadSnap()

        result = prepare_local_voice(_Llm(), engine, _SUPPORTED)
        self.assertFalse(result.ok)
        self.assertEqual(result.final_stage, STAGE_OLLAMA)
        self.assertEqual(result.detail, "ollama_warmup_snapshot_failed")
        self.assertEqual(engine.events, [])

    def test_begin_token_bool_coercion_rejected(self) -> None:
        engine = _TrackingCosyEngine()

        class _Llm:
            def begin_fresh_warmup(self, *, timeout: float) -> bool:
                return True

            def warmup_snapshot(self) -> SimpleNamespace:
                return SimpleNamespace(
                    generation=1, finished=True, succeeded=True
                )

        result = prepare_local_voice(_Llm(), engine, _SUPPORTED)
        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "ollama_warmup_not_started")
        self.assertEqual(engine.events, [])

    def test_snapshot_non_strict_bool_fields_not_success(self) -> None:
        engine = _TrackingCosyEngine()

        class _Llm:
            def begin_fresh_warmup(self, *, timeout: float) -> int:
                return 1

            def warmup_snapshot(self) -> SimpleNamespace:
                return SimpleNamespace(
                    generation=1,
                    finished="false",
                    succeeded="false",
                )

        result = prepare_local_voice(_Llm(), engine, _SUPPORTED)
        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "ollama_warmup_snapshot_failed")
        self.assertEqual(engine.events, [])

    def test_stage_elapsed_source_dict_alias_isolated(self) -> None:
        source = {STAGE_OLLAMA: 0.1}
        result = LocalVoiceWarmupResult(
            ok=True,
            final_stage="completed",
            total_elapsed_sec=0.2,
            stage_elapsed_sec=types.MappingProxyType(source),
        )
        source[STAGE_OLLAMA] = 99.0
        self.assertEqual(result.stage_elapsed_sec[STAGE_OLLAMA], 0.1)

    def test_memory_wrapper_without_begin_fresh_no_legacy_start(self) -> None:
        engine = _TrackingCosyEngine()

        class _LegacyInner:
            def __init__(self) -> None:
                self.calls = 0

            def start_warmup(self, **kw: Any) -> bool:
                self.calls += 1
                return True

            def warmup_snapshot(self) -> SimpleNamespace:
                return SimpleNamespace(
                    generation=9, finished=True, succeeded=True
                )

        inner = _LegacyInner()
        wrapped = MemoryAwareLLMClient(
            inner,
            _FakeMemoryServices(),  # type: ignore[arg-type]
            _FakeMemoryServices(),  # type: ignore[arg-type]
        )
        result = prepare_local_voice(wrapped, engine, _SUPPORTED)
        self.assertFalse(result.ok)
        self.assertEqual(result.final_stage, STAGE_OLLAMA)
        self.assertEqual(inner.calls, 0)
        self.assertEqual(engine.events, [])


class OllamaFreshWarmupTests(unittest.TestCase):
    def test_default_restart_false_keeps_idempotent(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        with patch.object(client, "warm_up", return_value=True):
            self.assertTrue(client.start_warmup(timeout=1))
            self.assertTrue(client._warmup_done.wait(2))  # noqa: SLF001
            self.assertFalse(client.start_warmup(timeout=1))
            snap = client.warmup_snapshot()
            self.assertTrue(snap.finished and snap.succeeded)

    def test_restart_after_complete_starts_new_generation(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        calls = {"n": 0}

        def warm(*, timeout: float) -> bool:
            calls["n"] += 1
            return calls["n"] == 1

        with patch.object(client, "warm_up", side_effect=warm):
            try:
                self.assertTrue(client.start_warmup(timeout=1))
                self.assertTrue(client._warmup_done.wait(2))  # noqa: SLF001
                self.assertTrue(client.warmup_snapshot().succeeded)
                self.assertTrue(client.start_warmup(timeout=1, restart=True))
                self.assertTrue(client._warmup_done.wait(2))  # noqa: SLF001
                self.assertEqual(calls["n"], 2)
                self.assertFalse(client.warmup_snapshot().succeeded)
            finally:
                self.assertIs(client.close_warmup(timeout=2.0), True)

    def test_in_progress_restart_does_not_spawn_second_thread(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        release = threading.Event()
        entered = threading.Event()

        def blocked(*, timeout: float) -> bool:
            entered.set()
            if not release.wait(2.0):
                raise AssertionError("warm gate not released")
            return True

        with patch.object(client, "warm_up", side_effect=blocked):
            try:
                self.assertTrue(client.start_warmup(timeout=1))
                self.assertTrue(entered.wait(1))
                first_owner = client._warmup_owner  # noqa: SLF001
                self.assertFalse(client.start_warmup(timeout=1, restart=True))
                self.assertIs(client._warmup_owner, first_owner)  # noqa: SLF001
                release.set()
                self.assertTrue(client.wait_for_warmup(timeout=2))
            finally:
                release.set()
                self.assertIs(client.close_warmup(timeout=2.0), True)

    def test_thread_start_failure_restores_generation(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)

        def factory_fail(*_a: Any, **_k: Any) -> threading.Thread:
            raise RuntimeError("thread unavailable")

        with patch(
            "src.llm.ollama_client.threading.Thread", side_effect=factory_fail
        ):
            try:
                with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                    client.start_warmup(timeout=1, restart=True)
                self.assertEqual(client.warmup_snapshot().generation, 1)
                self.assertFalse(client.warmup_snapshot().finished)
            finally:
                self.assertIs(client.close_warmup(timeout=2.0), True)

    def test_memory_wrapper_delegates_snapshot_and_restart(self) -> None:
        inner = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        wrapped = MemoryAwareLLMClient(
            inner,
            _FakeMemoryServices(),  # type: ignore[arg-type]
            _FakeMemoryServices(),  # type: ignore[arg-type]
        )
        with patch.object(inner, "warm_up", return_value=True):
            try:
                self.assertTrue(wrapped.start_warmup(timeout=1, restart=True))
                self.assertTrue(inner._warmup_done.wait(2))  # noqa: SLF001
                snap = wrapped.warmup_snapshot()
                self.assertIsInstance(snap, OllamaWarmupSnapshot)
                self.assertTrue(snap.finished and snap.succeeded)
            finally:
                self.assertIs(inner.close_warmup(timeout=2.0), True)

    def test_memory_wrapper_default_start_warmup_legacy_inner(self) -> None:
        class _LegacyInner:
            def start_warmup(self, *, timeout: Optional[float] = None) -> bool:
                return True

        wrapped = MemoryAwareLLMClient(
            _LegacyInner(),
            _FakeMemoryServices(),  # type: ignore[arg-type]
            _FakeMemoryServices(),  # type: ignore[arg-type]
        )
        self.assertTrue(wrapped.start_warmup(timeout=1))

    def test_memory_wrapper_begin_fresh_only_delegates_atomic_api(self) -> None:
        inner = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        wrapped = MemoryAwareLLMClient(
            inner,
            _FakeMemoryServices(),  # type: ignore[arg-type]
            _FakeMemoryServices(),  # type: ignore[arg-type]
        )
        with patch.object(inner, "begin_fresh_warmup", return_value=3) as begin:
            self.assertEqual(wrapped.begin_fresh_warmup(timeout=1), 3)
        begin.assert_called_once_with(timeout=1)


class RvcPrepareStartupResultTests(unittest.TestCase):
    def test_health_ok_worker_warm_false_not_ready(self) -> None:
        from src.voice.rvc.health_check import RVCHealthReport

        report = RVCHealthReport(
            ok=True,
            model_path="voice.pth",
            index_path="voice.index",
            python_path=sys.executable,
            errors=[],
        )
        engine = TTSEngine.__new__(TTSEngine)
        engine.logger = MagicMock()
        engine._rvc = SimpleNamespace(enabled=True)
        engine._rvc_config = {
            "prewarm_audio_processing": False,
            "worker_enabled": True,
        }
        engine._rvc_prepare_done = False
        engine._rvc_ready = False
        engine.safety_policy = SafetyPolicy()
        engine._temp_files = None
        with patch(
            "src.voice.rvc.health_check.check_rvc_health",
            return_value=report,
        ) as health_mock, patch(
            "src.voice.rvc.infer.warm_rvc_worker",
            return_value=False,
        ):
            self.assertFalse(engine.prepare_rvc())
        health_mock.assert_called_once()
        _args, kwargs = health_mock.call_args
        self.assertFalse(kwargs.get("probe_import", True))
        self.assertFalse(engine._rvc_ready)  # noqa: SLF001


class _TtsEngineRvcWarmContractAdapter:
    """将真实 TTSEngine.prepare_rvc 接入协调器 fake，穿透 RVC 契约到 Cosy 门禁。"""

    engine_type = "cosyvoice"

    def __init__(self) -> None:
        from src.voice.rvc.health_check import RVCHealthReport

        self.prepare_local_calls = 0
        self._report = RVCHealthReport(
            ok=True,
            model_path="voice.pth",
            index_path="voice.index",
            python_path=sys.executable,
            errors=[],
        )
        engine = TTSEngine.__new__(TTSEngine)
        engine.logger = MagicMock()
        engine._rvc = SimpleNamespace(enabled=True)
        engine._rvc_config = {
            "prewarm_audio_processing": False,
            "worker_enabled": True,
            "worker_warmup_inference": True,
        }
        engine._rvc_prepare_done = False
        engine._rvc_ready = False
        engine.safety_policy = SafetyPolicy()
        engine._temp_files = None
        self._engine = engine

    def prepare_rvc(self) -> bool:
        with patch(
            "src.voice.rvc.health_check.check_rvc_health",
            return_value=self._report,
        ), patch("src.voice.rvc.infer.get_rvc_worker") as get_worker:
            worker = MagicMock()
            worker.start.return_value = True
            worker.warm_up.return_value = False
            worker.ready = True
            get_worker.return_value = worker
            return self._engine.prepare_rvc()

    def prepare_local_tts(self, *, cancel_event=None) -> bool:
        self.prepare_local_calls += 1
        return True


class CoordinatorRvcWarmContractTests(unittest.TestCase):
    def test_warm_contract_failure_skips_cosy_via_real_prepare_rvc(self) -> None:
        engine = _TtsEngineRvcWarmContractAdapter()
        llm = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        with patch.object(llm, "warm_up", return_value=True):
            result = prepare_local_voice(llm, engine, _SUPPORTED)
        self.assertFalse(result.ok)
        self.assertEqual(result.final_stage, STAGE_RVC)
        self.assertEqual(engine.prepare_local_calls, 0)
        self.assertFalse(engine._engine._rvc_ready)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main(verbosity=2)
