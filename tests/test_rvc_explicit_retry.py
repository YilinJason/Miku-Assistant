"""失败 RVC 显式重试与 warm 构造迟返 cancel（fake，穿透 TTSEngine/App）。"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.assistant import MikuAssistant
from src.core.resource_settings import ResourceSettings
from src.core.rvc_prepare_metadata import (
    RvcPrepareState,
    rvc_prepare_failed_sticky,
)
from src.core.safety import SafetyPolicy, TrustedRvcProfile
from src.core.shutdown_status import TtsShutdownStatus
from src.core.voice_activation import bound_rvc_explicit_retry_needed, run_bound_rvc_explicit_retry
from src.core.voice_foreground import app_tts_playback_ready
from src.core.voice_load_lane import ensure_voice_transaction
from src.core.host_memory import ModelStartupMemoryDecision
from src.voice.rvc import infer as infer_mod
from src.voice.rvc import worker as wm
from src.voice.rvc.infer import RvcWorkerUnavailableError
from src.voice.rvc.infer import (
    get_rvc_worker,
    retire_rvc_worker_for_retry,
    stop_rvc_worker,
    warm_rvc_worker,
)
from src.voice.rvc.worker import RVCWorkerClient
from src.voice.tts.engine import TTSEngine

from tests.support.host_memory_fixtures import install_module_sufficient_host_memory


_SUPPORTED = ResourceSettings(
    serialize_gpu_warmup=True,
    warmup_order="ollama_then_rvc",
    ollama_warmup_on_start=True,
    rvc_warmup_on_voice_start=True,
    warmup_timeout=5.0,
    foreground_warmup_wait_sec=0.05,
)


def setUpModule() -> None:
    install_module_sufficient_host_memory()


def _policy(tmp: Path) -> SafetyPolicy:
    py = tmp / "python.exe"
    model = tmp / "m.pth"
    py.write_bytes(b"")
    model.write_bytes(b"x")
    return SafetyPolicy(
        {"permissions": {"rvc_worker": True, "rvc_infer": True}},
        trusted_rvc_profile=TrustedRvcProfile(python_path=py, model_path=model),
    )


def _failed_edge_engine(policy: SafetyPolicy, model: Path) -> TTSEngine:
    engine = TTSEngine(
        engine="edge-tts",
        rvc_config={
            "model_path": str(model),
            "worker_enabled": True,
            "prewarm_audio_processing": False,
            "worker_warmup_inference": False,
        },
        safety_policy=policy,
        warm_rvc_on_init=False,
        fallback_enabled=False,
    )
    with patch(
        "src.voice.rvc.health_check.check_rvc_health",
        return_value=MagicMock(ok=True),
    ), patch("src.voice.rvc.infer.warm_rvc_worker", return_value=False):
        engine.prepare_rvc()
    state = engine.get_rvc_prepare_state()
    assert state.attempt_completed and not state.ready
    return engine


_RVC_WORKER_CFG = {
    "model_path": "fake.pth",
    "device": "cpu",
    "worker_enabled": True,
    "worker_warmup_inference": False,
    "prewarm_audio_processing": False,
}


class _MinimalVoiceConfig:
    """自含配置协议，不依赖 tmp Root fixture。"""

    def __init__(self, *, engine: str = "edge-tts", model_path: str = "fake.pth") -> None:
        tts: dict = {"engine": engine}
        if engine == "edge-tts":
            tts["rvc"] = {"model_path": model_path}
        self.data = {
            "voice": {"tts": tts},
            "memory": {"enabled": False},
            "system": {"windows_control_enabled": False},
        }

    def get(self, key: str, default=None):
        value = self.data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def get_rvc_config(self):
        value = self.get("voice.tts.rvc", {})
        return value if isinstance(value, dict) else {}


class _StickyFailedTts:
    """RVC sticky 失败 TTS stub（Edge），显式 retry 可置 ready。"""

    engine_type = "edge-tts"

    def __init__(self) -> None:
        self._rvc = SimpleNamespace(enabled=True)
        self.ready = False
        self.retry_calls = 0

    def get_rvc_prepare_state(self) -> RvcPrepareState:
        return RvcPrepareState(
            enabled=True,
            inflight=False,
            attempt_completed=True,
            ready=self.ready,
        )

    def prepare_rvc(self, **kwargs) -> bool:
        return self.ready

    def retry_rvc_preparation(self, **kwargs) -> bool:
        self.retry_calls += 1
        self.ready = True
        return True

    def shutdown(self, **kwargs) -> None:
        return None

    def get_shutdown_status(self) -> TtsShutdownStatus:
        return TtsShutdownStatus(True, True, True, True, True, True)


class _CosyStickyFailedTts(_StickyFailedTts):
    engine_type = "cosyvoice"

    def prepare_local_tts(self, **kwargs) -> bool:
        return True

    def local_cosyvoice_worker_playback_ready(self) -> bool:
        return self.ready


def _real_rvc_worker_client(
    policy: SafetyPolicy | None = None,
) -> tuple[RVCWorkerClient, ModelStartupMemoryDecision]:
    enough = ModelStartupMemoryDecision(True, "", 65536, 65536, 4096, 14336)
    pol = policy or SafetyPolicy({"permissions": {"rvc_worker": True, "rvc_infer": True}})
    with patch.object(wm, "check_model_startup_memory", return_value=enough):
        client = wm.RVCWorkerClient(_RVC_WORKER_CFG, safety_policy=pol)
    return client, enough


def _bare_failed_retry_engine(policy: SafetyPolicy) -> TTSEngine:
    engine = TTSEngine.__new__(TTSEngine)
    engine._rvc = SimpleNamespace(enabled=True)
    engine._rvc_config = dict(_RVC_WORKER_CFG)
    engine.safety_policy = policy
    engine._rvc_ready = engine._rvc_prepare_inflight = False
    engine._rvc_prepare_done = engine._rvc_prepare_attempt_completed = True
    engine._shutdown_event = threading.Event()
    engine._interrupt_event = threading.Event()
    engine._interrupt_epoch = 0
    engine._interrupt_epoch_lock = threading.Lock()
    engine._speak_lock = threading.Lock()
    return engine


def _register_app_shutdown(case: unittest.TestCase, app: MikuAssistant) -> None:
    case.addCleanup(app.shutdown)


def _register_engine_shutdown(case: unittest.TestCase, engine: TTSEngine) -> None:
    case.addCleanup(engine.shutdown)


def _register_engine_stop(case: unittest.TestCase) -> None:
    case.addCleanup(stop_rvc_worker)

@unittest.skipUnless(sys.platform == "win32", "RVC explicit retry")
class RvcExplicitRetryEngineTests(unittest.TestCase):
    def tearDown(self) -> None:
        stop_rvc_worker()

    def test_prepare_failed_sticky_helper(self) -> None:
        failed = RvcPrepareState(
            enabled=True, inflight=False, attempt_completed=True, ready=False
        )
        self.assertTrue(rvc_prepare_failed_sticky(failed))
        self.assertFalse(
            rvc_prepare_failed_sticky(
                RvcPrepareState(
                    enabled=True,
                    inflight=True,
                    attempt_completed=True,
                    ready=False,
                )
            )
        )

    def test_retry_retires_once_then_prepares(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            model = Path(tmp) / "m.pth"
            engine = _failed_edge_engine(policy, model)
            _register_engine_shutdown(self, engine)
            retire_calls = {"n": 0}
            warm_calls = {"n": 0}

            def fake_retire(cfg, pol):
                retire_calls["n"] += 1
                return True

            def fake_warm(*a, **k):
                warm_calls["n"] += 1
                return True

            with patch(
                "src.voice.rvc.infer.retire_rvc_worker_for_retry",
                side_effect=fake_retire,
            ), patch(
                "src.voice.rvc.infer.warm_rvc_worker",
                side_effect=fake_warm,
            ):
                self.assertTrue(engine.retry_rvc_preparation())
            self.assertEqual(retire_calls["n"], 1)
            self.assertEqual(warm_calls["n"], 1)
            self.assertTrue(engine.get_rvc_prepare_state().ready)

    def test_retry_refuses_when_already_ready(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            model = Path(tmp) / "m.pth"
            engine = _failed_edge_engine(policy, model)
            _register_engine_shutdown(self, engine)
            engine._rvc_ready = True  # noqa: SLF001
            with patch("src.voice.rvc.infer.retire_rvc_worker_for_retry") as retire:
                self.assertFalse(engine.retry_rvc_preparation())
                retire.assert_not_called()

    def test_late_get_worker_cancel_skips_start_and_warm(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"device": "cpu", "model_path": str(Path(tmp) / "m.pth")}
            gate = threading.Event()
            entered = threading.Event()
            real_client_init = RVCWorkerClient.__init__
            thread_error: list[BaseException] = []

            def slow_init(self, *args, **kwargs):
                entered.set()
                if not gate.wait(2.0):
                    raise AssertionError("gate wait timed out")
                real_client_init(self, *args, **kwargs)

            cancel = threading.Event()

            def run_warm() -> None:
                try:
                    warm_rvc_worker(
                        cfg,
                        safety_policy=policy,
                        cancel_event=cancel,
                    )
                except BaseException as exc:
                    thread_error.append(exc)

            start_calls: list[int] = []

            def track_start(self):
                start_calls.append(1)
                return True

            worker = threading.Thread(target=run_warm, name="late-warm-cancel")
            started_worker = False
            with patch.object(RVCWorkerClient, "__init__", slow_init), patch.object(
                RVCWorkerClient, "start", track_start
            ), patch.object(RVCWorkerClient, "warm_up", return_value=True):
                try:
                    worker.start()
                    started_worker = True
                    self.assertTrue(entered.wait(2.0))
                    cancel.set()
                    gate.set()
                    worker.join(5.0)
                    self.assertFalse(worker.is_alive())
                    if thread_error:
                        raise thread_error[0]
                    self.assertEqual(start_calls, [])
                finally:
                    gate.set()
                    if started_worker:
                        if worker.is_alive():
                            worker.join(5.0)
                        self.assertFalse(worker.is_alive())
                    stop_rvc_worker()


@unittest.skipUnless(sys.platform == "win32", "RVC explicit retry App")
class RvcExplicitRetryAppTests(unittest.TestCase):
    def tearDown(self) -> None:
        stop_rvc_worker()

    def test_normal_ensure_does_not_retry_failed_rvc(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            model = Path(tmp) / "m.pth"
            engine = _failed_edge_engine(policy, model)
            _register_engine_shutdown(self, engine)
            cfg = {"voice": {"tts": {"engine": "edge-tts", "rvc": {"model_path": str(model)}}}}
            assistant = MikuAssistant.from_dependencies(
                cfg,
                safety_policy=policy,
                tts_engine=engine,
                stt_engine=MagicMock(),
            )
            _register_app_shutdown(self, assistant)
            assistant.resource_settings = _SUPPORTED
            warm_calls = {"n": 0}

            with patch(
                "src.voice.rvc.infer.warm_rvc_worker",
                side_effect=lambda *a, **k: warm_calls.__setitem__("n", warm_calls["n"] + 1) or True,
            ):
                first = assistant.ensure_tts_for_playback()
                second = assistant.ensure_tts_for_playback()
            self.assertTrue(first.ok)
            self.assertTrue(second.ok)
            self.assertEqual(warm_calls["n"], 0)

    def test_explicit_retry_after_memory_fail_then_success(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            model = Path(tmp) / "m.pth"
            engine = _failed_edge_engine(policy, model)
            _register_engine_shutdown(self, engine)
            cfg = {"voice": {"tts": {"engine": "edge-tts", "rvc": {"model_path": str(model)}}}}
            assistant = MikuAssistant.from_dependencies(
                cfg,
                safety_policy=policy,
                tts_engine=engine,
                stt_engine=MagicMock(),
            )
            _register_app_shutdown(self, assistant)
            assistant.resource_settings = _SUPPORTED
            self.assertTrue(
                bound_rvc_explicit_retry_needed(engine, _SUPPORTED)
            )
            retire_calls = {"n": 0}
            warm_calls = {"n": 0}

            def fake_retire(cfg, pol):
                retire_calls["n"] += 1
                return True

            def fake_warm(*a, **k):
                warm_calls["n"] += 1
                return True

            with patch(
                "src.voice.rvc.infer.retire_rvc_worker_for_retry",
                side_effect=fake_retire,
            ), patch(
                "src.voice.rvc.infer.warm_rvc_worker",
                side_effect=fake_warm,
            ):
                out = assistant.ensure_tts_for_playback(allow_retry=True)
            self.assertTrue(out.ok)
            self.assertEqual(retire_calls["n"], 1)
            self.assertEqual(warm_calls["n"], 1)
            self.assertTrue(engine.get_rvc_prepare_state().ready)

    def test_explicit_false_retry_reports_failure_on_app(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            model = Path(tmp) / "m.pth"
            engine = _failed_edge_engine(policy, model)
            _register_engine_shutdown(self, engine)
            cfg = {"voice": {"tts": {"engine": "edge-tts", "rvc": {"model_path": str(model)}}}}
            assistant = MikuAssistant.from_dependencies(
                cfg,
                safety_policy=policy,
                tts_engine=engine,
                stt_engine=MagicMock(),
            )
            _register_app_shutdown(self, assistant)
            assistant.resource_settings = _SUPPORTED
            with patch.object(TTSEngine, "retry_rvc_preparation", return_value=False):
                out = assistant.ensure_tts_for_playback(allow_retry=True)
            self.assertFalse(out.ok)

    def test_foreground_explicit_retry_short_budget(self) -> None:
        from src.core.voice_foreground import request_tts_for_playback

        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            model = Path(tmp) / "m.pth"
            engine = _failed_edge_engine(policy, model)
            _register_engine_shutdown(self, engine)
            cfg = {
                "voice": {
                    "tts": {
                        "engine": "edge-tts",
                        "rvc": {"model_path": str(model)},
                    }
                }
            }
            assistant = MikuAssistant.from_dependencies(
                cfg,
                safety_policy=policy,
                tts_engine=engine,
                stt_engine=MagicMock(),
            )
            _register_app_shutdown(self, assistant)
            assistant.resource_settings = _SUPPORTED
            entered = threading.Event()
            release = threading.Event()
            retry_error: list[BaseException] = []
            job = None
            fg_thread: threading.Thread | None = None

            def slow_retry(*a, **k):
                entered.set()
                try:
                    if not release.wait(5.0):
                        raise AssertionError("slow_retry release wait timed out")
                    engine._rvc_ready = True  # noqa: SLF001
                    return True
                except BaseException as exc:
                    retry_error.append(exc)
                    raise

            with patch.object(
                TTSEngine, "retry_rvc_preparation", side_effect=slow_retry
            ):
                try:
                    out = request_tts_for_playback(
                        assistant,
                        allow_retry=True,
                        wait=True,
                        wait_sec=0.05,
                    )
                    job = assistant._fg_active_job
                    self.assertIsNotNone(job)
                    fg_thread = job.thread
                    self.assertTrue(entered.wait(2.0))
                    self.assertFalse(out.ok)
                    self.assertTrue(getattr(out, "pending", False))
                    if retry_error:
                        raise retry_error[0]
                    release.set()
                    self.assertTrue(job.done.wait(5.0), "foreground load job did not finish")
                    if fg_thread is not None:
                        if fg_thread.is_alive():
                            fg_thread.join(5.0)
                        self.assertFalse(fg_thread.is_alive())
                    self.assertIsNotNone(job.result)
                    self.assertTrue(job.result.ok)
                    self.assertFalse(getattr(job.result, "pending", False))
                finally:
                    release.set()
                    target_job = job
                    active = assistant._fg_active_job
                    if target_job is None and active is not None:
                        target_job = active
                    elif (
                        target_job is not None
                        and active is not None
                        and active is not target_job
                    ):
                        target_job = job
                    thread = fg_thread
                    if target_job is not None and thread is None:
                        thread = target_job.thread
                    if thread is not None and thread.ident is not None:
                        if thread.is_alive():
                            thread.join(5.0)
                        self.assertFalse(thread.is_alive())
            assistant.shutdown()


class RetireRvcWorkerForRetryTests(unittest.TestCase):
    def tearDown(self) -> None:
        stop_rvc_worker()

    def test_retire_refuses_busy_client(self) -> None:
        import src.voice.rvc.infer as infer_mod

        policy = SafetyPolicy({"permissions": {"rvc_worker": True}})
        cfg = {"model_path": "m.pth", "device": "cpu"}
        lock = threading.Lock()
        lock.acquire()

        class _Busy:
            safety_policy = policy
            _lock = lock

            def try_acquire_work_lease(self):
                return None

        client = _Busy()
        try:
            infer_mod._worker_client = client
            infer_mod._worker_config_key = infer_mod._config_key(cfg)
            self.assertFalse(retire_rvc_worker_for_retry(cfg, policy))
        finally:
            if lock.locked():
                lock.release()
            infer_mod._worker_client = None
            infer_mod._worker_config_key = None


class RvcExplicitRetryAtomicBehaviorTests(unittest.TestCase):
    def test_stop_rvc_worker_marks_fence_before_stop(self) -> None:
        import src.voice.rvc.infer as infer_mod

        policy = SafetyPolicy({"permissions": {"rvc_worker": True}})
        client = MagicMock()
        client.safety_policy = policy
        client._globally_retired = False
        observed: list[bool] = []

        def mark():
            client._globally_retired = True

        client.mark_globally_retired = mark

        def stop():
            observed.append(client._globally_retired)
            return False

        client.stop = stop
        infer_mod._worker_client = client
        infer_mod._worker_config_key = "k"
        try:
            self.assertFalse(infer_mod.stop_rvc_worker())
            self.assertEqual(observed, [True])
        finally:
            infer_mod._worker_client = None
            infer_mod._worker_config_key = None

    def test_warm_cancel_after_warm_up_quiesces(self) -> None:
        cancel = threading.Event()
        client = MagicMock()
        client.safety_policy = SafetyPolicy()

        def warm(**kwargs):
            cancel.set()
            return True

        client.warm_up = warm
        client.stop = MagicMock(return_value=True)
        cfg = {
            "model_path": "m.pth",
            "device": "cpu",
            "worker_enabled": True,
            "worker_warmup_inference": True,
        }
        with patch(
            "src.voice.rvc.infer.get_rvc_worker", return_value=client
        ), patch("src.voice.rvc.infer._worker_client", client), patch(
            "src.voice.rvc.infer._worker_config_key", "k"
        ), patch.object(client, "start", return_value=True):
            self.assertFalse(
                warm_rvc_worker(
                    cfg,
                    client.safety_policy,
                    cancel_event=cancel,
                )
            )
            client.stop.assert_called()

    def test_ollama_finished_not_succeeded_blocks_explicit_retry(self) -> None:
        from types import SimpleNamespace

        from src.core.resource_settings import ResourceSettings
        from src.core.rvc_prepare_metadata import RvcPrepareState
        from src.core.voice_activation import run_bound_rvc_explicit_retry

        class Llm:
            calls: list[str] = []

            def begin_fresh_warmup(self, **kwargs):
                Llm.calls.append("fresh")
                return 1

            def warmup_snapshot(self):
                return SimpleNamespace(generation=1, finished=True, succeeded=False)

        class _FailedEdge:
            engine_type = "edge-tts"

            def __init__(self) -> None:
                self._rvc = SimpleNamespace(enabled=True)
                self.retry_calls = 0

            def get_rvc_prepare_state(self) -> RvcPrepareState:
                return RvcPrepareState(
                    enabled=True,
                    inflight=False,
                    attempt_completed=True,
                    ready=False,
                )

            def retry_rvc_preparation(self, **kwargs):
                self.retry_calls += 1
                return True

        Llm.calls = []
        tts = _FailedEdge()
        result = run_bound_rvc_explicit_retry(
            llm_client=Llm(),
            tts_engine=tts,
            settings=ResourceSettings(),
            cancel_event=threading.Event(),
            log=MagicMock(),
            local_prepare_ok=False,
        )
        self.assertFalse(result.ok)
        self.assertEqual(tts.retry_calls, 0)
        self.assertEqual(Llm.calls, ["fresh"])

    def test_cancel_during_cosy_local_prepare_never_publishes_ready(self) -> None:
        from src.core.shutdown_status import TtsShutdownStatus
        from src.core.voice_activation import run_bound_rvc_explicit_retry

        cancel = threading.Event()

        class _CosyLocalCancel:
            engine_type = "cosyvoice"
            ready = False

            def __init__(self) -> None:
                self._rvc = SimpleNamespace(enabled=True)
                self.retry_calls = 0

            def get_rvc_prepare_state(self) -> RvcPrepareState:
                return RvcPrepareState(
                    enabled=True,
                    inflight=False,
                    attempt_completed=True,
                    ready=self.ready,
                )

            def retry_rvc_preparation(self, **kwargs):
                self.retry_calls += 1
                self.ready = True
                return True

            def prepare_local_tts(self, **kwargs):
                cancel.set()
                return True

            def local_cosyvoice_worker_playback_ready(self) -> bool:
                return True

            def shutdown(self, **kwargs):
                return None

            def get_shutdown_status(self):
                return TtsShutdownStatus(True, True, True, True, True, True)

        tts = _CosyLocalCancel()
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        app = MikuAssistant.from_dependencies(
            cfg,
            safety_policy=SafetyPolicy(),
            tts_engine=tts,
        )
        _register_app_shutdown(self, app)
        result = run_bound_rvc_explicit_retry(
            llm_client=None,
            tts_engine=tts,
            settings=ResourceSettings(),
            cancel_event=cancel,
            log=MagicMock(),
            local_prepare_ok=False,
            app=app,
        )
        self.assertFalse(result.ok)
        self.assertFalse(app._local_voice_prepare_ok)
        self.assertEqual(tts.retry_calls, 1)

    def test_explicit_retry_true_without_verified_ready_is_failure(self) -> None:
        from src.core.voice_activation import run_bound_rvc_explicit_retry

        class _UnfinishedRetry:
            engine_type = "edge-tts"

            def __init__(self) -> None:
                self._rvc = SimpleNamespace(enabled=True)
                self.retry_calls = 0

            def get_rvc_prepare_state(self) -> RvcPrepareState:
                if self.retry_calls:
                    return RvcPrepareState(
                        enabled=True,
                        inflight=False,
                        attempt_completed=True,
                        ready=False,
                    )
                return RvcPrepareState(
                    enabled=True,
                    inflight=False,
                    attempt_completed=True,
                    ready=False,
                )

            def retry_rvc_preparation(self, **kwargs):
                self.retry_calls += 1
                return True

        tts = _UnfinishedRetry()
        result = run_bound_rvc_explicit_retry(
            llm_client=None,
            tts_engine=tts,
            settings=ResourceSettings(),
            cancel_event=threading.Event(),
            log=MagicMock(),
            local_prepare_ok=False,
        )
        self.assertEqual(tts.retry_calls, 1)
        self.assertFalse(result.ok)


@unittest.skipUnless(sys.platform == "win32", "RVC explicit retry persistent regression")
class RvcExplicitRetryPersistentRegressionTests(unittest.TestCase):
    """atomic13 / completion 已证实场景的自含仓库回归（不 import tmp Root）。"""

    def setUp(self) -> None:
        _register_engine_stop(self)

    def test_incomplete_global_stop_still_fences_before_stop(self) -> None:
        client, _ = _real_rvc_worker_client()
        observed: list[bool] = []

        def incomplete_stop() -> bool:
            observed.append(client._globally_retired)
            return False

        with patch.object(infer_mod, "_worker_client", client), patch.object(
            infer_mod, "_worker_config_key", infer_mod._config_key(_RVC_WORKER_CFG)
        ), patch.object(client, "stop", side_effect=incomplete_stop):
            self.assertFalse(infer_mod.stop_rvc_worker())
            self.assertIs(infer_mod._worker_client, client)
            self.assertEqual(observed, [True])

    def test_globally_retired_client_cannot_spawn_after_slow_resolve(self) -> None:
        client, enough = _real_rvc_worker_client()
        python = MagicMock()
        python.is_file.return_value = True
        spawn = MagicMock(side_effect=AssertionError("retired startup reached spawn"))

        def resolve(*args):
            client.mark_globally_retired()
            return python

        with patch.object(wm, "check_model_startup_memory", return_value=enough), patch.object(
            wm, "resolve_rvc_python", side_effect=resolve
        ), patch.object(wm, "ensure_policy_rvc_python"), patch.object(
            client.safety_policy, "rvc_model", return_value=Path("fake.pth")
        ), patch.object(
            client.safety_policy, "spawn_rvc_worker", spawn
        ):
            self.assertFalse(client.start())
        self.assertEqual(spawn.call_count, 0)

    def test_retire_phase_publishes_inflight_then_clears(self) -> None:
        engine = _bare_failed_retry_engine(SafetyPolicy())
        seen: list[bool] = []

        def retire(*args, **kwargs):
            seen.append(engine.get_rvc_prepare_state().inflight)
            return False

        with patch.object(infer_mod, "retire_rvc_worker_for_retry", side_effect=retire):
            self.assertFalse(engine.retry_rvc_preparation())
        self.assertEqual(seen, [True])
        self.assertFalse(engine.get_rvc_prepare_state().inflight)

    def test_cancel_during_retire_clears_inflight_keeps_sticky(self) -> None:
        engine = _bare_failed_retry_engine(SafetyPolicy())

        def retire(*args, **kwargs):
            engine._shutdown_event.set()
            return True

        with patch.object(infer_mod, "retire_rvc_worker_for_retry", side_effect=retire):
            self.assertFalse(engine.retry_rvc_preparation())
        state = engine.get_rvc_prepare_state()
        self.assertFalse(state.inflight)
        self.assertTrue(state.attempt_completed)

    def test_already_cancelled_bound_retry_skips_ollama_and_rvc_retry(self) -> None:
        calls: list[str] = []

        class Llm:
            def start_warmup(self, **kwargs):
                calls.append("start")
                return True

            def begin_fresh_warmup(self, **kwargs):
                calls.append("fresh")
                return 7

            def warmup_snapshot(self):
                return SimpleNamespace(generation=7, finished=True, succeeded=True)

        cancel = threading.Event()
        cancel.set()
        tts = _StickyFailedTts()
        result = run_bound_rvc_explicit_retry(
            llm_client=Llm(),
            tts_engine=tts,
            settings=ResourceSettings(),
            cancel_event=cancel,
            log=MagicMock(),
            local_prepare_ok=False,
        )
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])
        self.assertEqual(tts.retry_calls, 0)

    def test_fresh_warmup_is_only_ollama_entry_for_explicit_retry(self) -> None:
        calls: list[str] = []

        class Llm:
            def start_warmup(self, **kwargs):
                calls.append("start")
                return True

            def begin_fresh_warmup(self, **kwargs):
                calls.append("fresh")
                return 7

            def warmup_snapshot(self):
                return SimpleNamespace(generation=7, finished=True, succeeded=True)

        result = run_bound_rvc_explicit_retry(
            llm_client=Llm(),
            tts_engine=_StickyFailedTts(),
            settings=ResourceSettings(),
            cancel_event=threading.Event(),
            log=MagicMock(),
            local_prepare_ok=False,
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls, ["fresh"])

    def test_policy_replacement_fences_before_incomplete_stop(self) -> None:
        client, _ = _real_rvc_worker_client()
        observed: list[bool] = []

        def incomplete_stop() -> bool:
            observed.append(client._globally_retired)
            return False

        foreign = SafetyPolicy({"permissions": {"rvc_worker": True, "rvc_infer": True}})
        with patch.object(infer_mod, "_worker_client", client), patch.object(
            infer_mod, "_worker_config_key", infer_mod._config_key(_RVC_WORKER_CFG)
        ), patch.object(client, "stop", side_effect=incomplete_stop):
            with self.assertRaises(RvcWorkerUnavailableError):
                infer_mod.get_rvc_worker(_RVC_WORKER_CFG, foreign)
            self.assertEqual(observed, [True])
            self.assertIs(infer_mod._worker_client, client)

    def test_explicit_retry_foreign_worker_zero_stop(self) -> None:
        client, _ = _real_rvc_worker_client()
        stop_calls = {"n": 0}
        real_stop = client.stop

        def counting_stop(*args, **kwargs):
            stop_calls["n"] += 1
            return real_stop(*args, **kwargs)

        foreign = SafetyPolicy({"permissions": {"rvc_worker": True, "rvc_infer": True}})
        with patch.object(infer_mod, "_worker_client", client), patch.object(
            infer_mod, "_worker_config_key", infer_mod._config_key(_RVC_WORKER_CFG)
        ), patch.object(client, "stop", side_effect=counting_stop):
            with self.assertRaises(RvcWorkerUnavailableError):
                infer_mod.get_rvc_worker(
                    _RVC_WORKER_CFG,
                    foreign,
                    for_explicit_retry=True,
                )
        self.assertEqual(stop_calls["n"], 0)

    def test_cancelled_ctor_fences_before_quiet_disposal(self) -> None:
        for stop_quiet in (False, True):
            with self.subTest(stop_quiet=stop_quiet):
                client, _ = _real_rvc_worker_client()
                cancel = threading.Event()
                observed: list[bool] = []
                key = infer_mod._config_key(_RVC_WORKER_CFG)

                def ctor(*args, **kwargs):
                    cancel.set()
                    return client

                def cancel_stop() -> bool:
                    observed.append(client._globally_retired)
                    return stop_quiet

                with patch.object(infer_mod, "_worker_client", None), patch.object(
                    infer_mod, "_worker_config_key", None
                ), patch.object(wm, "RVCWorkerClient", side_effect=ctor), patch.object(
                    client, "stop", side_effect=cancel_stop
                ):
                    if stop_quiet:
                        with self.assertRaises(RvcWorkerUnavailableError):
                            infer_mod.get_rvc_worker(
                                _RVC_WORKER_CFG,
                                client.safety_policy,
                                cancel_event=cancel,
                            )
                        self.assertIsNone(infer_mod._worker_client)
                        self.assertIsNone(infer_mod._worker_config_key)
                    else:
                        returned = infer_mod.get_rvc_worker(
                            _RVC_WORKER_CFG,
                            client.safety_policy,
                            cancel_event=cancel,
                        )
                        self.assertIs(returned, client)
                        self.assertIs(infer_mod._worker_client, client)
                        self.assertEqual(infer_mod._worker_config_key, key)
                        self.assertEqual(observed, [True])
                        cleanup_observed: list[bool] = []
                        stop_hit: list[int] = []

                        def cleanup_stop() -> bool:
                            stop_hit.append(id(client))
                            cleanup_observed.append(client._globally_retired)
                            return True

                        with patch.object(client, "stop", side_effect=cleanup_stop):
                            self.assertTrue(infer_mod.stop_rvc_worker())
                        self.assertEqual(stop_hit, [id(client)])
                        self.assertEqual(cleanup_observed, [True])
                        self.assertIsNone(infer_mod._worker_client)
                        self.assertIsNone(infer_mod._worker_config_key)
                if stop_quiet:
                    self.assertEqual(observed, [True])

    def test_cosy_explicit_retry_publishes_app_playback_ready(self) -> None:
        tts = _CosyStickyFailedTts()
        app = MikuAssistant.from_dependencies(
            config=_MinimalVoiceConfig(engine="cosyvoice"),
            safety_policy=SafetyPolicy(),
            tts_engine=tts,
        )
        app.resource_settings = _SUPPORTED
        _register_app_shutdown(self, app)
        self.assertTrue(app.ensure_tts_for_playback(allow_retry=True).ok)
        self.assertTrue(app_tts_playback_ready(app))

    def test_voice_stack_retry_builds_missing_stt_after_bound_retry(self) -> None:
        tts = _StickyFailedTts()
        stt = SimpleNamespace(shutdown=lambda: None)
        app = MikuAssistant.from_dependencies(
            config=_MinimalVoiceConfig(),
            safety_policy=SafetyPolicy(),
            tts_engine=tts,
        )
        app.resource_settings = _SUPPORTED
        app._voice_autoload_enabled = True
        _register_app_shutdown(self, app)
        with patch("src.core.assistant.build_stt_engine", return_value=stt) as build:
            result = ensure_voice_transaction(
                app,
                want_tts=True,
                want_stt=True,
                allow_retry=True,
                lane_label="repo-regression",
            )
        self.assertTrue(result.ok)
        self.assertEqual(build.call_count, 1)
        self.assertIs(app.stt_engine, stt)

    def test_retire_rejects_busy_client_without_stop(self) -> None:
        policy = SafetyPolicy({"permissions": {"rvc_worker": True}})
        lock = threading.Lock()
        lock.acquire()

        class _BusyClient:
            safety_policy = policy

            def try_acquire_work_lease(self):
                return None

        busy = _BusyClient()
        stop_calls = {"n": 0}

        def stop():
            stop_calls["n"] += 1
            return True

        busy.stop = stop
        try:
            infer_mod._worker_client = busy
            infer_mod._worker_config_key = infer_mod._config_key(_RVC_WORKER_CFG)
            self.assertFalse(
                retire_rvc_worker_for_retry(_RVC_WORKER_CFG, policy)
            )
            self.assertEqual(stop_calls["n"], 0)
        finally:
            if lock.locked():
                lock.release()
            infer_mod._worker_client = None
            infer_mod._worker_config_key = None
