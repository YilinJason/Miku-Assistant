"""前台有界 pending、Job owner 与 CLI 路由（fake，不经真实模型）。"""
from __future__ import annotations

import io
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from src.cli.text_session import run_interactive_session
from src.cli.voice_session import run_voice_session
from src.core.assistant import MikuAssistant, VoiceStackLoadResult
from src.core.resource_settings import ResourceSettings
from src.core.safety import SafetyPolicy, TrustedRvcProfile
from src.core.voice_foreground import (
    _ForegroundLoadJob,
    _reap_finished_job,
    app_tts_playback_ready,
    app_voice_stack_ready,
    configured_cosyvoice,
    configured_edge_rvc_foreground,
    cosy_foreground_path,
    foreground_voice_load_blocks_shutdown,
    join_foreground_loader_bounded,
    notify_foreground_shutdown,
    refresh_app_tts_playback_ready,
    request_tts_for_playback,
    request_voice_stack,
    tts_foreground_bound_path,
)


_SUPPORTED = ResourceSettings(
    serialize_gpu_warmup=True,
    warmup_order="ollama_then_rvc",
    ollama_warmup_on_start=True,
    rvc_warmup_on_voice_start=True,
    warmup_timeout=5.0,
    foreground_warmup_wait_sec=5.0,
)


class _CosyTts:
    engine_type = "cosyvoice"

    def __init__(self, *, worker_ready: bool = True) -> None:
        self._worker_ready = worker_ready

    def local_cosyvoice_worker_playback_ready(self) -> bool:
        return self._worker_ready

    def shutdown(self, **kwargs) -> None:
        pass


def _policy(tmp: Path) -> SafetyPolicy:
    py = tmp / "python.exe"
    model = tmp / "m.pth"
    py.write_bytes(b"")
    model.write_bytes(b"x")
    return SafetyPolicy(
        {"permissions": {"rvc_worker": True, "local_tts_worker": True}},
        trusted_rvc_profile=TrustedRvcProfile(python_path=py, model_path=model),
    )


def _load_pending(load: object) -> bool:
    return getattr(load, "pending", False) is True


@unittest.skipUnless(__import__("sys").platform == "win32", "voice foreground bound")
class VoiceForegroundBoundTests(unittest.TestCase):
    def test_cosy_path_without_autoload_when_configured(self) -> None:
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg)
        assistant._voice_autoload_enabled = False
        self.assertTrue(configured_cosyvoice(assistant))
        self.assertTrue(cosy_foreground_path(assistant))

    def test_config_protocol_edge_rvc_uses_foreground_before_tts_bound(self) -> None:
        class _ConfigProtocol:
            def __init__(self) -> None:
                self.data = {
                    "voice": {
                        "tts": {
                            "engine": "edge-tts",
                            "rvc": {"model_path": "fake.pth"},
                        }
                    },
                    "memory": {"enabled": False},
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
                return dict(value) if value.get("model_path") else None

        assistant = MikuAssistant.from_dependencies(_ConfigProtocol())
        assistant.resource_settings = _SUPPORTED
        assistant._voice_autoload_enabled = True
        self.assertTrue(configured_edge_rvc_foreground(assistant))
        self.assertTrue(tts_foreground_bound_path(assistant))
        self.assertIsNone(assistant.tts_engine)

    def test_injection_guard_fails_fast_without_autoload(self) -> None:
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg)
        assistant._voice_autoload_enabled = False
        assistant.resource_settings = _SUPPORTED
        out = request_voice_stack(assistant, wait=False)
        self.assertFalse(out.ok)
        self.assertFalse(_load_pending(out))
        self.assertIn("禁止自动创建", "; ".join(out.errors))

    def test_edge_without_rvc_uses_sync_ensure_no_fg_job(self) -> None:
        from types import SimpleNamespace

        cfg = {"voice": {"tts": {"engine": "edge-tts"}}}
        edge = MagicMock(engine_type="edge-tts")
        edge._rvc = None
        assistant = MikuAssistant.from_dependencies(cfg, tts_engine=edge)
        assistant._voice_autoload_enabled = True
        assistant.resource_settings = _SUPPORTED
        result = request_tts_for_playback(assistant)
        self.assertTrue(result.ok)
        self.assertFalse(_load_pending(result))
        self.assertFalse(foreground_voice_load_blocks_shutdown(assistant))

    def test_edge_rvc_bound_not_ready_until_prepare_completed(self) -> None:
        from types import SimpleNamespace

        from src.voice.tts.engine import RvcPrepareState

        class _EdgeRvcTts:
            engine_type = "edge-tts"

            def __init__(self) -> None:
                self._rvc = SimpleNamespace(enabled=True)
                self._state = RvcPrepareState(
                    enabled=True,
                    inflight=False,
                    attempt_completed=False,
                    ready=False,
                )

            def get_rvc_prepare_state(self) -> RvcPrepareState:
                return self._state

        cfg = {
            "voice": {
                "tts": {"engine": "edge-tts", "rvc": {"model_path": "m.pth"}},
            }
        }
        edge = _EdgeRvcTts()
        assistant = MikuAssistant.from_dependencies(cfg, tts_engine=edge)
        assistant.resource_settings = _SUPPORTED
        assistant._voice_autoload_enabled = True
        self.assertFalse(app_tts_playback_ready(assistant))
        gate = threading.Event()

        def slow_ensure(**kwargs):
            gate.set()
            time.sleep(0.5)
            edge._state = RvcPrepareState(
                enabled=True,
                inflight=False,
                attempt_completed=True,
                ready=True,
            )
            return VoiceStackLoadResult(ok=True)

        with patch.object(assistant, "ensure_tts_for_playback", side_effect=slow_ensure):
            first = request_tts_for_playback(assistant, wait=False, wait_sec=0.05)
            self.assertTrue(gate.wait(1.0))
        self.assertTrue(_load_pending(first))
        self.assertFalse(first.ok)
        self.assertTrue(join_foreground_loader_bounded(assistant, 2.0))
        self.assertTrue(app_tts_playback_ready(assistant))

    def test_readiness_getter_fail_closed(self) -> None:
        tts = MagicMock(engine_type="cosyvoice")
        tts.local_cosyvoice_worker_playback_ready.side_effect = RuntimeError("x")
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg, tts_engine=tts)
        assistant._local_voice_prepare_ok = True
        refresh_app_tts_playback_ready(assistant)
        self.assertFalse(app_tts_playback_ready(assistant))

    def test_idle_worker_invalidates_prepare_flag(self) -> None:
        tts = _CosyTts(worker_ready=False)
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg, tts_engine=tts)
        assistant._local_voice_prepare_ok = True
        refresh_app_tts_playback_ready(assistant)
        self.assertFalse(assistant._local_voice_prepare_ok)
        self.assertFalse(app_tts_playback_ready(assistant))

    def test_blocked_prepare_returns_pending_without_long_wait(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
            assistant = MikuAssistant.from_dependencies(
                cfg, safety_policy=policy, stt_engine=MagicMock()
            )
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            gate = threading.Event()

            def slow_prepare(*args, **kwargs):
                gate.set()
                time.sleep(2.0)
                return MagicMock(ok=True, final_stage="completed")

            with patch(
                "src.core.assistant.build_tts_engine",
                return_value=_CosyTts(),
            ), patch(
                "src.core.voice_activation.prepare_local_voice",
                side_effect=slow_prepare,
            ):
                t0 = time.monotonic()
                first = request_tts_for_playback(assistant, wait=False)
                self.assertTrue(gate.wait(1.0))
                second = request_tts_for_playback(assistant, wait=False)
                elapsed = time.monotonic() - t0
            self.assertTrue(_load_pending(first))
            self.assertTrue(_load_pending(second))
            self.assertLess(elapsed, 0.5)
            self.assertTrue(join_foreground_loader_bounded(assistant, 5.0))

    def test_sticky_failure_does_not_respawn_or_sync_ensure(self) -> None:
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg)
        assistant.resource_settings = _SUPPORTED
        assistant._voice_autoload_enabled = True
        assistant._tts_load_failed = True
        assistant._fg_sticky_failure = VoiceStackLoadResult(
            ok=False, errors=["TTS 先前加载失败；请输入 tts 显式重试"]
        )
        with patch.object(assistant, "ensure_tts_for_playback") as ensure_spy:
            out = request_tts_for_playback(assistant, wait=False)
        self.assertFalse(out.ok)
        self.assertFalse(_load_pending(out))
        ensure_spy.assert_not_called()
        with assistant._fg_lock:
            self.assertIsNone(assistant._fg_active_job)

    def test_upgrade_to_voice_stack_requires_stt_ready(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
            assistant = MikuAssistant.from_dependencies(
                cfg, safety_policy=policy, stt_engine=None
            )
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            gate = threading.Event()
            stack_calls: list[int] = []

            def slow_tts(*args, **kwargs):
                gate.set()
                time.sleep(0.3)
                assistant.tts_engine = _CosyTts()
                assistant._local_voice_prepare_ok = True
                return VoiceStackLoadResult(ok=True)

            def stack(*args, **kwargs):
                stack_calls.append(1)
                assistant.stt_engine = MagicMock()
                return VoiceStackLoadResult(ok=True)

            with patch.object(
                assistant, "ensure_tts_for_playback", side_effect=slow_tts
            ), patch.object(assistant, "ensure_voice_stack", side_effect=stack):
                threading.Thread(
                    target=lambda: request_tts_for_playback(assistant, wait=True),
                    daemon=True,
                ).start()
                self.assertTrue(gate.wait(1.0))
                request_voice_stack(assistant, wait=True)
                self.assertTrue(join_foreground_loader_bounded(assistant, 5.0))
            self.assertGreaterEqual(len(stack_calls), 1)
            self.assertTrue(app_voice_stack_ready(assistant))

    def test_active_job_blocks_shutdown_until_creator_done_and_join(self) -> None:
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg)
        job = _ForegroundLoadJob(generation=1, kind="tts", allow_retry=False)
        assistant._fg_active_job = job
        self.assertTrue(foreground_voice_load_blocks_shutdown(assistant))
        job.creator_done = True
        job.terminal = True
        job.thread = threading.Thread(target=lambda: None)
        job.thread.start()
        job.thread.join()
        job.start_ack = True
        self.assertTrue(join_foreground_loader_bounded(assistant, 1.0))
        self.assertFalse(foreground_voice_load_blocks_shutdown(assistant))

    def test_pre_start_thread_ref_not_reaped_before_creator_done(self) -> None:
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg)
        job = _ForegroundLoadJob(generation=1, kind="tts", allow_retry=False)
        dead = threading.Thread(target=lambda: None)
        job.thread = dead
        job.start_ack = False
        job.creator_done = False
        assistant._fg_active_job = job
        _reap_finished_job(assistant)
        self.assertIs(assistant._fg_active_job, job)
        job.creator_done = True
        job.terminal = True
        job.start_ack = True
        _reap_finished_job(assistant)
        self.assertIsNone(assistant._fg_active_job)

    def test_wait_outcome_returns_failure_not_pending_after_reap(self) -> None:
        from src.core.voice_foreground import _fg_wait_outcome

        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg)
        fail = VoiceStackLoadResult(ok=False, errors=["boom"])
        job = _ForegroundLoadJob(
            generation=1,
            kind="tts",
            allow_retry=False,
            creator_done=True,
            terminal=True,
            result=fail,
        )
        job.done.set()
        assistant._fg_active_job = job
        out = _fg_wait_outcome(
            assistant,
            wait=False,
            wait_sec=None,
            label="TTS",
            ready_fn=lambda: False,
        )
        self.assertFalse(out.ok)
        self.assertFalse(_load_pending(out))
        self.assertIn("boom", "; ".join(out.errors))

    def test_shutdown_interrupts_owner_candidate_tts(self) -> None:
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        published = MagicMock()
        candidate = MagicMock()
        assistant = MikuAssistant.from_dependencies(cfg, tts_engine=published)
        from src.core.voice_activation import UnpublishedVoice

        assistant._voice_owner_slot = UnpublishedVoice(tts=candidate)
        notify_foreground_shutdown(assistant)
        published.interrupt.assert_called_once()
        candidate.interrupt.assert_called_once()

    def test_explicit_retry_does_not_clear_tts_load_failed_before_admit(self) -> None:
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg)
        assistant._voice_autoload_enabled = True
        assistant.resource_settings = _SUPPORTED
        assistant._tts_load_failed = True
        in_flight = _ForegroundLoadJob(generation=1, kind="tts", allow_retry=False)
        in_flight.creator_done = False
        assistant._fg_active_job = in_flight
        request_tts_for_playback(assistant, allow_retry=True, wait=False)
        self.assertTrue(assistant._tts_load_failed)

    def test_standalone_pending_routes_to_text_not_hard_fail(self) -> None:
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg, stt_engine=MagicMock())
        assistant._voice_autoload_enabled = True
        assistant.resource_settings = _SUPPORTED
        pending = VoiceStackLoadResult(
            ok=False,
            pending=True,
            warnings=["准备中"],
        )
        with patch(
            "src.cli.voice_session.check_voice_preflight",
            return_value=MagicMock(
                ok=False,
                pending=True,
                errors=[],
                warnings=["准备中"],
                summary=lambda: "pending",
            ),
        ), patch(
            "src.cli.text_session.run_text_session"
        ) as text_run, patch(
            "src.cli.voice_session.VoiceSession"
        ) as voice_cls:
            run_voice_session(assistant, standalone=True)
        text_run.assert_called_once()
        voice_cls.assert_not_called()

    def test_legacy_result_without_pending_attr(self) -> None:
        class _LegacyResult:
            ok = False
            errors = ["x"]
            warnings = []

        self.assertFalse(_load_pending(_LegacyResult()))

    def test_creator_constructor_keyboard_interrupt_releases_ownership(self) -> None:
        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        assistant = MikuAssistant.from_dependencies(cfg)
        assistant._voice_autoload_enabled = True
        assistant.resource_settings = _SUPPORTED

        class _KbiThread(threading.Thread):
            def __init__(self, *args, **kwargs):
                raise KeyboardInterrupt

        with patch("src.core.voice_foreground.threading.Thread", _KbiThread):
            with self.assertRaises(KeyboardInterrupt):
                request_tts_for_playback(assistant, wait=False)
        self.assertFalse(foreground_voice_load_blocks_shutdown(assistant))

    def test_creator_start_worker_blocked_until_release(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
            cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
            assistant = MikuAssistant.from_dependencies(
                cfg, safety_policy=policy, stt_engine=MagicMock()
            )
            assistant.resource_settings = _SUPPORTED
            assistant._voice_autoload_enabled = True
            entered = threading.Event()
            release = threading.Event()
            request_errors: list[BaseException] = []
            import src.core.voice_foreground as vfg

            real_vfg_start = vfg.threading.Thread.start

            def blocked_vfg_start(self, *args, **kwargs):
                if self.name.startswith("miku-voice-fg-"):
                    entered.set()
                    if not release.wait(2.0):
                        raise RuntimeError("gate timeout")
                return real_vfg_start(self, *args, **kwargs)

            def _request() -> None:
                try:
                    request_tts_for_playback(assistant, wait=False)
                except BaseException as exc:
                    request_errors.append(exc)

            requester: Optional[threading.Thread] = None
            try:
                with patch.object(
                    vfg.threading.Thread, "start", blocked_vfg_start
                ), patch(
                    "src.core.assistant.build_tts_engine",
                    return_value=_CosyTts(),
                ), patch(
                    "src.core.voice_activation.prepare_local_voice",
                    return_value=MagicMock(ok=True, final_stage="completed"),
                ):
                    requester = threading.Thread(target=_request, daemon=False)
                    requester.start()
                    self.assertTrue(entered.wait(1.0))
                    self.assertTrue(foreground_voice_load_blocks_shutdown(assistant))
            finally:
                release.set()
                assistant._voice_load_cancel.set()
                if requester is not None:
                    requester.join(10.0)
                    if requester.is_alive():
                        self.fail("requester thread leaked")
            if request_errors:
                raise request_errors[0]
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if not foreground_voice_load_blocks_shutdown(assistant):
                    break
                time.sleep(0.05)
            else:
                self.fail("foreground loader still blocking after release")
            _reap_finished_job(assistant)

    def test_creator_os_bootstrap_interrupt_retains_thread_ref(self) -> None:
        import src.core.voice_foreground as vfg

        cfg = {"voice": {"tts": {"engine": "cosyvoice"}}}
        app = MikuAssistant.from_dependencies(cfg)
        app._voice_autoload_enabled = True
        app.resource_settings = _SUPPORTED
        entered, release = threading.Event(), threading.Event()
        actual: list[threading.Thread] = []
        real_thread = threading.Thread

        class DelayedBootstrap(real_thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                actual.append(self)

                def interrupted_start_wait(*a, **k):
                    if not entered.wait(1):
                        raise RuntimeError("bootstrap did not enter")
                    raise KeyboardInterrupt

                self._started.wait = interrupted_start_wait

            def _bootstrap(self):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("bootstrap gate not released")
                super()._bootstrap()

        try:
            with patch.object(vfg.threading, "Thread", DelayedBootstrap):
                with self.assertRaises(KeyboardInterrupt):
                    vfg._creator_start_job(app, kind="tts", allow_retry=False)
            self.assertTrue(entered.is_set())
            self.assertTrue(foreground_voice_load_blocks_shutdown(app))
            self.assertIs(app._fg_active_job.thread, actual[0])
        finally:
            release.set()
            for t in actual:
                t._started.wait = threading.Event.wait.__get__(t._started)
                t._started.wait(2)
                t.join(2)
                self.assertFalse(t.is_alive())
            _reap_finished_job(app)

    def test_run_interactive_initial_auto_tts_legacy_fail_path(self) -> None:
        assistant = MikuAssistant.from_dependencies({})

        def _cfg_get(key: str, default=None):
            if key == "assistant.auto_tts":
                return True
            if key == "assistant.stream_reply":
                return False
            return default

        assistant.config = MagicMock()
        assistant.config.get.side_effect = _cfg_get
        assistant.config.set = MagicMock()

        class _LegacyLoad:
            ok = False
            errors = ["legacy-no-pending"]

        buf = io.StringIO()
        with patch(
            "src.cli.text_session._request_tts", return_value=_LegacyLoad()
        ), patch("builtins.input", side_effect=["quit"]), redirect_stdout(buf):
            run_interactive_session(assistant, stream_reply=False)
        out = buf.getvalue()
        self.assertIn("legacy-no-pending", out)
        self.assertNotIn("准备中", out)

    def test_run_interactive_explicit_tts_retry_legacy_fail_path(self) -> None:
        assistant = MikuAssistant.from_dependencies({})
        assistant.config = MagicMock()
        assistant.config.get.side_effect = lambda k, d=None: (
            False if k == "assistant.auto_tts" else d
        )
        assistant.config.set = MagicMock()

        class _LegacyLoad:
            ok = False
            errors = ["retry-fail"]

        buf = io.StringIO()
        with patch(
            "src.cli.text_session._request_tts", return_value=_LegacyLoad()
        ), patch("builtins.input", side_effect=["tts", "quit"]), redirect_stdout(buf):
            run_interactive_session(assistant, stream_reply=False)
        out = buf.getvalue()
        self.assertIn("retry-fail", out)


if __name__ == "__main__":
    unittest.main()
