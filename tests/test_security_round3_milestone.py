# -*- coding: utf-8 -*-
"""Round 3 里程碑返修：前台预热短上限与持久日志异常隐私（Finding 1–2）。"""
from __future__ import annotations

import logging
import math
import threading
import unittest
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

from src.core.owned_thread import OwnedThreadTask
from src.core.log_privacy import exception_log_label
from src.core.resource_coord import (
    WarmupWaitResult,
    _wait_ollama_warmup,
    prepare_rvc_if_needed,
    release_owned_ollama_model,
    run_coordinated_warmup,
    start_ollama_warmup,
)
from src.core.resource_settings import (
    DEFAULT_FOREGROUND_WARMUP_WAIT_SEC,
    MAX_FOREGROUND_WARMUP_WAIT_SEC,
    MAX_WARMUP_TIMEOUT_SEC,
    ResourceSettings,
    parse_resource_settings,
)
from src.core.safety import SafetyPolicy
from src.llm.ollama_client import OllamaClient
from src.system import confirm_dialog as confirm_mod
from src.system.windows_control import WindowsController


POISON_MSG = (
    "SECRET_URL=https://evil.example/leak?token=abc\n"
    r"C:\Users\uitra\secret\path\model.wav"
)


class PoisonError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(POISON_MSG)


class _Cfg:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {
            "resource.profile": "balanced",
            "resource.ollama.warmup_on_start": True,
            "resource.ollama.unload_on_exit": False,
            "resource.rvc.warmup_on_voice_start": True,
            "resource.warmup.serialize_gpu": True,
            "resource.warmup.order": "ollama_then_rvc",
            "resource.warmup.timeout": 120,
            "resource.warmup.foreground_wait_sec": 5,
            "ollama.warmup_on_start": True,
            "ollama.warmup_timeout": 120,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class _ListLog:
    def __init__(self) -> None:
        self.records: List[str] = []

    def _fmt(self, msg: str, *args: Any) -> str:
        return msg % args if args else msg

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.records.append(self._fmt(msg, *args))

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.records.append(self._fmt(msg, *args))

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.records.append(self._fmt(msg, *args))

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.records.append(self._fmt(msg, *args))

    def joined(self) -> str:
        return "\n".join(self.records)


class TrackingTTS:
    def __init__(self) -> None:
        self.prepare_calls = 0

    def prepare_rvc(self) -> bool:
        self.prepare_calls += 1
        return True


class ForegroundWarmupWaitTests(unittest.TestCase):
    def test_defaults_and_hard_caps(self) -> None:
        settings = parse_resource_settings(_Cfg())
        self.assertEqual(
            settings.foreground_warmup_wait_sec, DEFAULT_FOREGROUND_WARMUP_WAIT_SEC
        )
        self.assertEqual(settings.warmup_timeout, 120.0)
        self.assertLessEqual(
            settings.foreground_warmup_wait_sec, MAX_FOREGROUND_WARMUP_WAIT_SEC
        )
        self.assertLessEqual(settings.warmup_timeout, MAX_WARMUP_TIMEOUT_SEC)

    def test_foreground_wait_rejects_over_hard_max(self) -> None:
        for bad in (
            MAX_FOREGROUND_WARMUP_WAIT_SEC + 0.01,
            11,
            120,
            float("nan"),
            float("inf"),
            True,
            0,
            -1,
            "5",
        ):
            cfg = _Cfg()
            cfg.set("resource.warmup.foreground_wait_sec", bad)
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_resource_settings(cfg)

    def test_resource_timeout_rejects_over_hard_max(self) -> None:
        for bad in (
            MAX_WARMUP_TIMEOUT_SEC + 1,
            10_000,
            float("nan"),
            float("inf"),
            True,
            0,
            -1,
        ):
            cfg = _Cfg()
            cfg.set("resource.warmup.timeout", bad)
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_resource_settings(cfg)

    def test_accepts_boundary_maxima(self) -> None:
        cfg = _Cfg()
        cfg.set("resource.warmup.foreground_wait_sec", MAX_FOREGROUND_WARMUP_WAIT_SEC)
        cfg.set("resource.warmup.timeout", MAX_WARMUP_TIMEOUT_SEC)
        settings = parse_resource_settings(cfg)
        self.assertEqual(
            settings.foreground_warmup_wait_sec, float(MAX_FOREGROUND_WARMUP_WAIT_SEC)
        )
        self.assertEqual(settings.warmup_timeout, float(MAX_WARMUP_TIMEOUT_SEC))
        self.assertTrue(math.isfinite(settings.foreground_warmup_wait_sec))

    def test_start_warmup_does_not_inflate_foreground_wait(self) -> None:
        client = OllamaClient(
            base_url="http://127.0.0.1:9",
            model="m",
            timeout=60,
            foreground_warmup_wait_sec=5.0,
        )
        client._resource_warmup_wait_timeout = 125.0  # noqa: SLF001
        entered, release = threading.Event(), threading.Event()
        joined: List[float] = []

        def blocked_warmup(*, timeout: float) -> bool:
            entered.set()
            if not release.wait(3.0):
                raise AssertionError("warm gate not released")
            return True

        real_join = OwnedThreadTask.join

        def capture_join(self, timeout: float = 2.0) -> bool:
            joined.append(float(timeout))
            return real_join(self, timeout=0)

        with patch.object(client, "warm_up", side_effect=blocked_warmup):
            try:
                self.assertTrue(client.start_warmup(timeout=120))
                self.assertTrue(entered.wait(1))
                self.assertEqual(client._foreground_warmup_wait_sec, 5.0)
                with patch.object(OwnedThreadTask, "join", capture_join):
                    client._wait_for_warmup()  # noqa: SLF001
                self.assertEqual(joined, [5.0])
                self.assertNotIn(125.0, joined)
                self.assertNotIn(120.0, joined)
            finally:
                release.set()
                self.assertTrue(client.wait_for_warmup(timeout=2))
                self.assertIs(client.close_warmup(timeout=2.0), True)

    def test_foreground_wait_never_uses_resource_timeout_unset_event(self) -> None:
        """未 quiet 的 owner：前台只传短上限 join，禁止 resource 长 timeout。"""
        client = OllamaClient(
            base_url="http://127.0.0.1:9",
            model="m",
            timeout=60,
            foreground_warmup_wait_sec=3.0,
        )
        client._resource_warmup_wait_timeout = 125.0  # noqa: SLF001
        entered, release = threading.Event(), threading.Event()
        joined: List[float] = []

        def blocked(*, timeout: float) -> bool:
            entered.set()
            if not release.wait(3.0):
                raise AssertionError("warm gate not released")
            return True

        real_join = OwnedThreadTask.join

        def capture_join(self, timeout: float = 2.0) -> bool:
            joined.append(float(timeout))
            return real_join(self, timeout=0)

        with patch.object(client, "warm_up", side_effect=blocked), patch.object(
            OwnedThreadTask, "join", capture_join
        ):
            try:
                self.assertTrue(client.start_warmup(timeout=1))
                self.assertTrue(entered.wait(1))
                client._wait_for_warmup()  # noqa: SLF001
                self.assertEqual(joined, [3.0])
                self.assertLessEqual(joined[0], MAX_FOREGROUND_WARMUP_WAIT_SEC)
            finally:
                release.set()
                self.assertTrue(client.wait_for_warmup(timeout=2))
                self.assertIs(client.close_warmup(timeout=2.0), True)

    def test_client_rejects_invalid_foreground_kwarg(self) -> None:
        with self.assertRaises(ValueError):
            OllamaClient(
                base_url="http://127.0.0.1:9",
                model="m",
                foreground_warmup_wait_sec=11,
            )


class SerializeGpuContractTests(unittest.TestCase):
    def test_incomplete_warmup_skips_rvc(self) -> None:
        class LLM:
            def start_warmup(self, *, timeout=None):
                return True

            def wait_for_warmup(self, *, timeout=None):
                self.seen_timeout = timeout
                return False

        llm = LLM()
        tts = TrackingTTS()
        events: List[str] = []
        run_coordinated_warmup(
            llm_client=llm,
            tts_engine=tts,
            settings=ResourceSettings(
                serialize_gpu_warmup=True,
                warmup_timeout=42.0,
                foreground_warmup_wait_sec=5.0,
            ),
            events=events,
        )
        self.assertEqual(llm.seen_timeout, 42.0)
        self.assertIn("ollama_wait_timeout", events)
        self.assertIn("rvc_skipped_serialize", events)
        self.assertEqual(tts.prepare_calls, 0)

    def test_wait_failure_detail_is_stable_label_not_str_exc(self) -> None:
        class LLM:
            def start_warmup(self, *, timeout=None):
                return True

            def wait_for_warmup(self, *, timeout=None):
                raise PoisonError()

        result = _wait_ollama_warmup(LLM(), timeout=1.0)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.detail, "PoisonError")
        self.assertNotIn("SECRET_URL", result.detail)
        self.assertNotIn("Users", result.detail)
        self.assertNotIn("\n", result.detail)


class PersistentLoggerScrubTests(unittest.TestCase):
    def _assert_scrubbed(self, text: str) -> None:
        self.assertNotIn("SECRET_URL", text)
        self.assertNotIn("evil.example", text)
        self.assertNotIn(r"C:\Users\uitra", text)
        self.assertNotIn("secret\\path", text)
        self.assertNotIn("model.wav", text)
        self.assertNotIn("token=abc", text)

    def test_resource_coord_start_warmup_logs_label(self) -> None:
        class LLM:
            def start_warmup(self, *, timeout=None):
                raise PoisonError()

        log = _ListLog()
        start_ollama_warmup(
            LLM(),
            settings=ResourceSettings(ollama_warmup_on_start=True),
            log=log,  # type: ignore[arg-type]
        )
        joined = log.joined()
        self.assertIn("PoisonError", joined)
        self._assert_scrubbed(joined)

    def test_resource_coord_rvc_and_release_logs_label(self) -> None:
        class TTS:
            def prepare_rvc(self) -> bool:
                raise PoisonError()

        class LLM:
            def model_touch_succeeded(self) -> bool:
                return True

            def release_owned_model(self) -> bool:
                raise PoisonError()

        log = _ListLog()
        prepare_rvc_if_needed(
            TTS(),
            settings=ResourceSettings(rvc_warmup_on_voice_start=True),
            log=log,  # type: ignore[arg-type]
        )
        release_owned_ollama_model(
            LLM(),
            settings=ResourceSettings(ollama_unload_on_exit=True),
            log=log,  # type: ignore[arg-type]
        )
        joined = log.joined()
        self.assertIn("PoisonError", joined)
        self._assert_scrubbed(joined)

    def test_wait_failure_log_via_coord_omits_poison(self) -> None:
        class LLM:
            def start_warmup(self, *, timeout=None):
                return True

            def wait_for_warmup(self, *, timeout=None):
                raise PoisonError()

        log = _ListLog()
        events: List[str] = []
        run_coordinated_warmup(
            llm_client=LLM(),
            tts_engine=TrackingTTS(),
            settings=ResourceSettings(serialize_gpu_warmup=True),
            log=log,  # type: ignore[arg-type]
            events=events,
        )
        joined = log.joined()
        self.assertIn("ollama_wait_failed", events)
        self.assertIn("PoisonError", joined)
        self._assert_scrubbed(joined)

    def test_windows_control_logger_scrubs_exception(self) -> None:
        records: List[str] = []

        class CapHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        policy = SafetyPolicy({"permissions": {"system_power": True}})
        policy.bind_process_runners(
            run_fn=MagicMock(side_effect=PoisonError())
        )
        controller = WindowsController(
            safety_policy=policy, dry_run=False, require_confirmation=False
        )
        handler = CapHandler()
        controller.logger.addHandler(handler)
        try:
            with patch.dict("os.environ", {"MIKU_DRY_RUN": ""}, clear=False):
                out = controller._execute_command("lock")  # noqa: SLF001
        finally:
            controller.logger.removeHandler(handler)

        self.assertIn("SECRET_URL", out)  # 用户返回可保留
        joined = "\n".join(records)
        self.assertIn("action=lock", joined)
        self.assertIn("PoisonError", joined)
        self.assertNotIn("SECRET_URL", joined)
        self.assertNotIn("evil.example", joined)
        self.assertNotIn(r"C:\Users\uitra", joined)

    def test_confirm_dialog_logger_scrubs_exception(self) -> None:
        records: List[str] = []

        class CapHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        handler = CapHandler()
        confirm_mod.logger.addHandler(handler)
        try:
            with patch.object(
                confirm_mod,
                "_show_win32_message_box",
                side_effect=PoisonError(),
            ):
                with patch.object(confirm_mod.platform, "system", return_value="Windows"):
                    ok = confirm_mod.confirm_system_action("shutdown")
        finally:
            confirm_mod.logger.removeHandler(handler)

        self.assertFalse(ok)
        joined = "\n".join(records)
        self.assertIn("action=shutdown", joined)
        self.assertIn("PoisonError", joined)
        self.assertNotIn("SECRET_URL", joined)
        self.assertNotIn("evil.example", joined)
        self.assertNotIn(r"C:\Users\uitra", joined)

    def test_exception_log_label_counterexample(self) -> None:
        label = exception_log_label(PoisonError())
        self.assertEqual(label, "PoisonError")
        self.assertNotIn("SECRET_URL", label)
        self.assertNotIn("\n", label)


class WarmupWaitResultContractTests(unittest.TestCase):
    def test_detail_must_not_carry_str_exc(self) -> None:
        result = WarmupWaitResult("failed", exception_log_label(PoisonError()))
        self.assertEqual(result.detail, "PoisonError")
        self.assertNotIn(POISON_MSG, result.detail)


if __name__ == "__main__":
    unittest.main()
