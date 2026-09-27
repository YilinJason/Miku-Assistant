"""STT 结构化结果、provider 注入与 VoiceSession 策略的纯离线测试。"""
from __future__ import annotations

import io
import logging
import sys
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

from src.cli.voice_session import VoiceSession
from src.core.assistant import MikuAssistant
from src.voice.recognition import RecognitionResult, RecognitionStatus
from src.voice.stt import STTEngine
from tests.test_assistant_orchestration import FakeRouter, MemoryConfig


class FakeProvider:
    def __init__(self, results: List[Any], *, ready: bool = True) -> None:
        self.results = list(results)
        self.ready = ready
        self.calls: List[dict] = []

    def recognize(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def is_ready(self) -> bool:
        return self.ready

    def check_microphone(self) -> tuple[bool, str]:
        return self.ready, "" if self.ready else "not ready"


class RecognitionValueTests(unittest.TestCase):
    def test_result_invariants(self) -> None:
        self.assertEqual(RecognitionResult.success(" 你好 ").text, "你好")
        with self.assertRaises(ValueError):
            RecognitionResult(status=RecognitionStatus.SUCCESS)
        with self.assertRaises(ValueError):
            RecognitionResult(
                status=RecognitionStatus.SERVICE_ERROR,
                text="不得成为用户输入",
            )
        with self.assertRaises(ValueError):
            RecognitionResult.failure(RecognitionStatus.SUCCESS)
        with self.assertRaises(ValueError):
            RecognitionResult.failure(RecognitionStatus.CANCELLED)
        with self.assertRaises(TypeError):
            RecognitionResult.success(123)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            RecognitionResult(status="success", text="x")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            RecognitionResult(
                status=RecognitionStatus.SUCCESS,
                text="ok",
                error_detail="must not coexist",
            )

    def test_coerce_rejects_bypassed_invariants(self) -> None:
        empty_success = object.__new__(RecognitionResult)
        object.__setattr__(empty_success, "status", RecognitionStatus.SUCCESS)
        object.__setattr__(empty_success, "text", "")
        object.__setattr__(empty_success, "error_detail", None)
        coerced = RecognitionResult.coerce(empty_success)
        self.assertEqual(coerced.status, RecognitionStatus.INTERNAL_ERROR)
        self.assertEqual(coerced.text, "")

        fake_user = object.__new__(RecognitionResult)
        object.__setattr__(fake_user, "status", RecognitionStatus.UNRECOGNIZED)
        object.__setattr__(fake_user, "text", "不得进入 LLM")
        object.__setattr__(fake_user, "error_detail", None)
        coerced_fail = RecognitionResult.coerce(fake_user)
        self.assertEqual(coerced_fail.status, RecognitionStatus.INTERNAL_ERROR)
        self.assertEqual(coerced_fail.text, "")

        success_with_error = object.__new__(RecognitionResult)
        object.__setattr__(success_with_error, "status", RecognitionStatus.SUCCESS)
        object.__setattr__(success_with_error, "text", "看似有效的用户输入")
        object.__setattr__(success_with_error, "error_detail", "不得与成功并存")
        coerced_success_with_error = RecognitionResult.coerce(success_with_error)
        self.assertEqual(
            coerced_success_with_error.status,
            RecognitionStatus.INTERNAL_ERROR,
        )
        self.assertEqual(coerced_success_with_error.text, "")

        missing_fields = object.__new__(RecognitionResult)
        coerced_missing = RecognitionResult.coerce(missing_fields)
        self.assertEqual(coerced_missing.status, RecognitionStatus.INTERNAL_ERROR)
        self.assertEqual(coerced_missing.text, "")

        self.assertEqual(
            RecognitionResult.coerce("bad").status,
            RecognitionStatus.INTERNAL_ERROR,
        )
        ok = RecognitionResult.success("合法")
        self.assertEqual(RecognitionResult.coerce(ok), ok)


class ProviderInjectionTests(unittest.TestCase):
    def test_injected_provider_skips_default_initialization_and_forwards_arguments(self) -> None:
        provider = FakeProvider([RecognitionResult.success("ok")])
        with patch.object(STTEngine, "_initialize_engine") as initialize:
            engine = STTEngine(provider=provider)
        initialize.assert_not_called()

        cancel = threading.Event()
        result = engine.recognize(
            timeout=1.5,
            phrase_time_limit=2.5,
            continuous=True,
            cancel_event=cancel,
        )
        self.assertEqual(result, RecognitionResult.success("ok"))
        self.assertEqual(
            provider.calls,
            [
                {
                    "timeout": 1.5,
                    "phrase_time_limit": 2.5,
                    "continuous": True,
                    "cancel_event": cancel,
                }
            ],
        )
        self.assertTrue(engine.is_ready())
        self.assertEqual(engine.check_microphone(), (True, ""))

    def test_provider_invalid_result_exception_and_keyboard_interrupt(self) -> None:
        invalid = STTEngine(provider=FakeProvider(["bad"]))  # type: ignore[list-item]
        self.assertEqual(
            invalid.recognize().status,
            RecognitionStatus.INTERNAL_ERROR,
        )

        failed = STTEngine(provider=FakeProvider([RuntimeError("boom")]))
        result = failed.recognize()
        self.assertEqual(result.status, RecognitionStatus.INTERNAL_ERROR)
        self.assertIn("boom", result.error_detail or "")

        interrupted = STTEngine(provider=FakeProvider([KeyboardInterrupt()]))
        with self.assertRaises(KeyboardInterrupt):
            interrupted.recognize()

        exited = STTEngine(provider=FakeProvider([SystemExit()]))
        with self.assertRaises(SystemExit):
            exited.recognize()

        broken = object.__new__(RecognitionResult)
        object.__setattr__(broken, "status", RecognitionStatus.SUCCESS)
        object.__setattr__(broken, "text", "")
        object.__setattr__(broken, "error_detail", None)
        engine = STTEngine(provider=FakeProvider([broken]))
        out = engine.recognize()
        self.assertEqual(out.status, RecognitionStatus.INTERNAL_ERROR)
        self.assertEqual(out.text, "")

    def test_listen_compatibility_and_legacy_prompt(self) -> None:
        failures = [
            RecognitionStatus.SILENCE_TIMEOUT,
            RecognitionStatus.UNRECOGNIZED,
            RecognitionStatus.SERVICE_ERROR,
            RecognitionStatus.DEVICE_ERROR,
            RecognitionStatus.NOT_READY,
            RecognitionStatus.CANCELLED,
            RecognitionStatus.INTERNAL_ERROR,
        ]
        results = [RecognitionResult.success("heard")]
        for status in failures:
            if status is RecognitionStatus.CANCELLED:
                results.append(RecognitionResult.cancelled())
            else:
                results.append(RecognitionResult.failure(status, "detail"))
        engine = STTEngine(provider=FakeProvider(results))
        self.assertEqual(engine.listen(), "heard")
        for _ in failures:
            self.assertEqual(engine.listen(), "")

        prompt_engine = STTEngine(
            provider=FakeProvider(
                [RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED)]
            )
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(prompt_engine.listen(continuous=True), "")
        self.assertEqual(output.getvalue().count("没听清"), 1)

    def test_pre_cancel_does_not_call_provider(self) -> None:
        provider = FakeProvider([RecognitionResult.success("should-not-run")])
        engine = STTEngine(provider=provider)
        cancel = threading.Event()
        cancel.set()
        result = engine.recognize(cancel_event=cancel)
        self.assertEqual(result.status, RecognitionStatus.CANCELLED)
        self.assertEqual(provider.calls, [])

    def test_structured_entry_does_not_print_legacy_prompt(self) -> None:
        engine = STTEngine(
            provider=FakeProvider(
                [RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED)]
            )
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result = engine.recognize(continuous=True)
        self.assertEqual(result.status, RecognitionStatus.UNRECOGNIZED)
        self.assertEqual(output.getvalue(), "")


class _WaitTimeoutError(Exception):
    pass


class _UnknownValueError(Exception):
    pass


class _RequestError(Exception):
    pass


class _Microphone:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


class _Audio:
    def __init__(self, duration: float = 0.5) -> None:
        self.sample_rate = 1000
        self.sample_width = 1
        self.frame_data = b"x" * int(duration * self.sample_rate)

    def get_wav_data(self) -> bytes:
        return b"fake-wav"


class _Recognizer:
    def __init__(self, *, listen_result: Any = None, google_result: Any = "ok") -> None:
        self.listen_result = listen_result if listen_result is not None else _Audio()
        self.google_result = google_result

    def listen(self, source, **kwargs):
        if isinstance(self.listen_result, BaseException):
            raise self.listen_result
        return self.listen_result

    def recognize_google(self, audio, language):
        if isinstance(self.google_result, BaseException):
            raise self.google_result
        return self.google_result


class DefaultAdapterClassificationTests(unittest.TestCase):
    def _engine(self, recognizer: Any, *, engine_type: str = "speech_recognition") -> STTEngine:
        with patch.object(STTEngine, "_initialize_engine"):
            engine = STTEngine(engine=engine_type)
        engine._recognizer = recognizer  # noqa: SLF001
        engine._noise_adjusted = True  # noqa: SLF001
        return engine

    def _sr_module(self, microphone: Any = _Microphone) -> Any:
        return SimpleNamespace(
            Microphone=microphone,
            WaitTimeoutError=_WaitTimeoutError,
            UnknownValueError=_UnknownValueError,
            RequestError=_RequestError,
        )

    def test_google_status_mapping_without_devices(self) -> None:
        cases = [
            (
                _Recognizer(listen_result=_WaitTimeoutError("quiet")),
                RecognitionStatus.SILENCE_TIMEOUT,
            ),
            (
                _Recognizer(google_result=_UnknownValueError("no words")),
                RecognitionStatus.UNRECOGNIZED,
            ),
            (
                _Recognizer(google_result=_RequestError("offline")),
                RecognitionStatus.SERVICE_ERROR,
            ),
            (
                _Recognizer(listen_result=_Audio(0.1)),
                RecognitionStatus.UNRECOGNIZED,
            ),
            (
                _Recognizer(google_result=RuntimeError("unexpected")),
                RecognitionStatus.INTERNAL_ERROR,
            ),
        ]
        with patch.dict(sys.modules, {"speech_recognition": self._sr_module()}):
            for recognizer, expected in cases:
                with self.subTest(expected=expected):
                    result = self._engine(recognizer).recognize(continuous=True)
                    self.assertEqual(result.status, expected)

    def test_device_error_and_success(self) -> None:
        def bad_microphone():
            raise OSError("no mic")

        with patch.dict(
            sys.modules,
            {"speech_recognition": self._sr_module(microphone=bad_microphone)},
        ):
            result = self._engine(_Recognizer()).recognize(continuous=True)
        self.assertEqual(result.status, RecognitionStatus.DEVICE_ERROR)

        with patch.dict(sys.modules, {"speech_recognition": self._sr_module()}):
            success = self._engine(_Recognizer(google_result=" 你好 ")).recognize()
        self.assertEqual(success, RecognitionResult.success("你好"))

    def test_not_ready_and_cancelled_before_device_access(self) -> None:
        engine = self._engine(None)
        self.assertEqual(engine.recognize().status, RecognitionStatus.NOT_READY)
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(
            engine.recognize(cancel_event=cancel).status,
            RecognitionStatus.CANCELLED,
        )

    def test_cancel_after_capture_skips_transcription(self) -> None:
        cancel = threading.Event()

        class CancelAfterListen(_Recognizer):
            def listen(self, source, **kwargs):
                cancel.set()
                return _Audio()

            def recognize_google(self, audio, language):
                raise AssertionError("transcription must not run after cancellation")

        with patch.dict(sys.modules, {"speech_recognition": self._sr_module()}):
            result = self._engine(CancelAfterListen()).recognize(
                continuous=True,
                cancel_event=cancel,
            )
        self.assertEqual(result.status, RecognitionStatus.CANCELLED)

    def test_whisper_result_mapping_and_temp_cleanup(self) -> None:
        engine = self._engine(_Recognizer(), engine_type="whisper")
        engine._model = None  # noqa: SLF001
        self.assertEqual(
            engine._recognize_whisper_result(_Audio()).status,  # noqa: SLF001
            RecognitionStatus.NOT_READY,
        )
        # 缺模型时不得打开麦克风
        def microphone_must_not_open():
            raise AssertionError("missing Whisper model must return before device access")

        with patch.dict(
            sys.modules,
            {"speech_recognition": self._sr_module(microphone=microphone_must_not_open)},
        ):
            early = engine.recognize(continuous=True)
        self.assertEqual(early.status, RecognitionStatus.NOT_READY)

        model = MagicMock()
        model.transcribe.return_value = {"text": "  "}
        engine._model = model  # noqa: SLF001
        self.assertEqual(
            engine._recognize_whisper_result(_Audio()).status,  # noqa: SLF001
            RecognitionStatus.UNRECOGNIZED,
        )

        model.transcribe.side_effect = RuntimeError("decode")
        self.assertEqual(
            engine._recognize_whisper_result(_Audio()).status,  # noqa: SLF001
            RecognitionStatus.INTERNAL_ERROR,
        )

        model.transcribe.side_effect = None
        model.transcribe.return_value = {"text": "清理"}
        created: List[str] = []
        real_mkstemp = __import__("tempfile").mkstemp

        def tracking_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            created.append(path)
            return fd, path

        with patch("src.voice.stt.tempfile.mkstemp", side_effect=tracking_mkstemp):
            result = engine._recognize_whisper_result(_Audio())  # noqa: SLF001
        self.assertEqual(result, RecognitionResult.success("清理"))
        self.assertTrue(created)
        self.assertFalse(__import__("os").path.isfile(created[0]))


class AssistantRecognitionTests(unittest.TestCase):
    def _assistant(self, stt: Any) -> MikuAssistant:
        return MikuAssistant.from_dependencies(
            MemoryConfig(),
            stt_engine=stt,
            router=FakeRouter(),  # type: ignore[arg-type]
            logger=logging.getLogger("test.stt.assistant"),
        )

    def test_structured_and_legacy_entries(self) -> None:
        structured = FakeProvider([RecognitionResult.success("结构化")])
        assistant = self._assistant(structured)
        self.assertEqual(
            assistant.recognize_voice_input(continuous=True),
            RecognitionResult.success("结构化"),
        )

        class Legacy:
            def __init__(self, value: Any) -> None:
                self.value = value

            def listen(self, continuous=False):
                return self.value

        self.assertEqual(
            self._assistant(Legacy("旧入口")).recognize_voice_input().status,
            RecognitionStatus.SUCCESS,
        )
        self.assertEqual(
            self._assistant(Legacy("")).recognize_voice_input().status,
            RecognitionStatus.UNRECOGNIZED,
        )

    def test_structured_legacy_provider_is_not_given_new_none_keywords(self) -> None:
        class OldStructured:
            def recognize(self, continuous=False, cancel_event=None):
                return RecognitionResult.success("兼容")

        result = self._assistant(OldStructured()).recognize_voice_input(
            continuous=True
        )
        self.assertEqual(result, RecognitionResult.success("兼容"))

    def test_invalid_exception_and_keyboard_interrupt(self) -> None:
        invalid = self._assistant(FakeProvider(["bad"]))  # type: ignore[list-item]
        self.assertEqual(
            invalid.recognize_voice_input().status,
            RecognitionStatus.INTERNAL_ERROR,
        )

        failed = self._assistant(FakeProvider([RuntimeError("boom")]))
        self.assertEqual(
            failed.recognize_voice_input().status,
            RecognitionStatus.INTERNAL_ERROR,
        )

        interrupted = self._assistant(FakeProvider([KeyboardInterrupt()]))
        with self.assertRaises(KeyboardInterrupt):
            interrupted.recognize_voice_input()

        exited = self._assistant(FakeProvider([SystemExit()]))
        with self.assertRaises(SystemExit):
            exited.recognize_voice_input()

        broken = object.__new__(RecognitionResult)
        object.__setattr__(broken, "status", RecognitionStatus.UNRECOGNIZED)
        object.__setattr__(broken, "text", "伪造用户话")
        object.__setattr__(broken, "error_detail", None)
        coerced = self._assistant(FakeProvider([broken])).recognize_voice_input()
        self.assertEqual(coerced.status, RecognitionStatus.INTERNAL_ERROR)
        self.assertEqual(coerced.text, "")
        self.assertEqual(
            self._assistant(FakeProvider([broken])).process_voice_input(),
            "",
        )

    def test_process_voice_input_keeps_string_contract(self) -> None:
        ok = self._assistant(FakeProvider([RecognitionResult.success("听到了")]))
        self.assertEqual(ok.process_voice_input(), "听到了")
        silent = self._assistant(
            FakeProvider(
                [RecognitionResult.failure(RecognitionStatus.SILENCE_TIMEOUT)]
            )
        )
        self.assertEqual(silent.process_voice_input(), "")
        missing = self._assistant(None)
        self.assertEqual(missing.process_voice_input(), "STT 模块未初始化")

    def test_pre_cancel_skips_legacy_stt(self) -> None:
        legacy = MagicMock()
        cancel = threading.Event()
        cancel.set()
        result = self._assistant(legacy).recognize_voice_input(cancel_event=cancel)
        self.assertEqual(result.status, RecognitionStatus.CANCELLED)
        legacy.listen.assert_not_called()


class _SessionAssistant:
    def __init__(self, results: List[RecognitionResult]) -> None:
        self.results = list(results)
        self.recognition_calls = 0
        self.text_calls: List[str] = []
        self.config = MemoryConfig({"assistant": {"stream_reply": False}})

    def recognize_voice_input(self, continuous=False):
        self.recognition_calls += 1
        return self.results.pop(0)

    @staticmethod
    def check_mode_switch(text: str) -> Optional[str]:
        return None

    def process_text_input(self, text: str, use_tts=True) -> str:
        self.text_calls.append(text)
        return "reply"


class VoiceSessionRecognitionPolicyTests(unittest.TestCase):
    def test_retryable_results_continue_without_dispatch(self) -> None:
        assistant = _SessionAssistant(
            [
                RecognitionResult.failure(RecognitionStatus.SILENCE_TIMEOUT),
                RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED),
                RecognitionResult.success("你好"),
                RecognitionResult.success("退出"),
            ]
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result = VoiceSession(
                assistant,  # type: ignore[arg-type]
                use_tts=False,
                stream_reply=False,
            ).run()
        self.assertEqual(result, "exit")
        self.assertEqual(assistant.recognition_calls, 4)
        self.assertEqual(assistant.text_calls, ["你好"])
        self.assertEqual(output.getvalue().count("没听清"), 1)

    def test_repeated_unrecognized_prompts_are_throttled(self) -> None:
        assistant = _SessionAssistant(
            [
                RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED),
                RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED),
                RecognitionResult.success("退出"),
            ]
        )
        output = io.StringIO()
        with patch(
            "src.cli.voice_session.time.monotonic",
            side_effect=[10.0, 10.5],
        ), redirect_stdout(output):
            result = VoiceSession(
                assistant,  # type: ignore[arg-type]
                use_tts=False,
                stream_reply=False,
            ).run()
        self.assertEqual(result, "exit")
        self.assertEqual(assistant.recognition_calls, 3)
        self.assertEqual(output.getvalue().count("没听清"), 1)

    def test_cancelled_exits_without_dispatch(self) -> None:
        assistant = _SessionAssistant([RecognitionResult.cancelled()])
        with redirect_stdout(io.StringIO()):
            result = VoiceSession(
                assistant,  # type: ignore[arg-type]
                stream_reply=False,
            ).run()
        self.assertEqual(result, "exit")
        self.assertEqual(assistant.recognition_calls, 1)
        self.assertEqual(assistant.text_calls, [])

    def test_fatal_statuses_pause_once_without_busy_loop_or_dispatch(self) -> None:
        statuses = (
            RecognitionStatus.SERVICE_ERROR,
            RecognitionStatus.DEVICE_ERROR,
            RecognitionStatus.NOT_READY,
            RecognitionStatus.INTERNAL_ERROR,
        )
        for status in statuses:
            with self.subTest(status=status):
                assistant = _SessionAssistant([RecognitionResult.failure(status, "x")])
                with redirect_stdout(io.StringIO()):
                    result = VoiceSession(
                        assistant,  # type: ignore[arg-type]
                        stream_reply=False,
                    ).run()
                self.assertEqual(result, "stt_error")
                self.assertEqual(assistant.recognition_calls, 1)
                self.assertEqual(assistant.text_calls, [])

    def test_legacy_assistant_not_ready_prompt_is_not_dispatched(self) -> None:
        class LegacyAssistant:
            config = MemoryConfig({"assistant": {"stream_reply": False}})

            def __init__(self) -> None:
                self.text_calls: List[str] = []

            @staticmethod
            def process_voice_input(continuous=False):
                return "STT 模块未初始化"

            @staticmethod
            def check_mode_switch(text: str) -> Optional[str]:
                return None

            def process_text_input(self, text: str, use_tts=True):
                self.text_calls.append(text)
                return "reply"

        assistant = LegacyAssistant()
        with redirect_stdout(io.StringIO()):
            result = VoiceSession(
                assistant,  # type: ignore[arg-type]
                stream_reply=False,
            ).run()
        self.assertEqual(result, "stt_error")
        self.assertEqual(assistant.text_calls, [])

    def test_broken_success_result_does_not_dispatch_or_mix_with_unsafe_pause(self) -> None:
        broken = object.__new__(RecognitionResult)
        object.__setattr__(broken, "status", RecognitionStatus.SUCCESS)
        object.__setattr__(broken, "text", "")
        object.__setattr__(broken, "error_detail", None)

        class BrokenAssistant:
            config = MemoryConfig({"assistant": {"stream_reply": False}})

            def __init__(self) -> None:
                self.text_calls: List[str] = []
                self.recognition_calls = 0

            def recognize_voice_input(self, continuous=False):
                self.recognition_calls += 1
                return broken

            @staticmethod
            def check_mode_switch(text: str) -> Optional[str]:
                return None

            def process_text_input(self, text: str, use_tts=True):
                self.text_calls.append(text)
                return "reply"

        assistant = BrokenAssistant()
        with redirect_stdout(io.StringIO()):
            result = VoiceSession(
                assistant,  # type: ignore[arg-type]
                stream_reply=False,
            ).run()
        self.assertEqual(result, "stt_error")
        self.assertNotEqual(result, "unsafe_pause")
        self.assertEqual(assistant.recognition_calls, 1)
        self.assertEqual(assistant.text_calls, [])


if __name__ == "__main__":
    unittest.main()
