"""CosyVoice plan filter for punctuation-only segments (offline fake regression)."""
from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from src.voice.tts.cosyvoice_backend import (
    CosyVoiceSpeechLimitError,
    MAX_TOTAL_SPEECH_CHARS,
    plan_cosyvoice_segments,
)
from src.voice.tts.engine import TTSEngine
from tests.test_cosyvoice_production_slice_c1 import (
    _make_engine,
    _settings,
    install_module_sufficient_host_memory,
)


def setUpModule() -> None:
    install_module_sufficient_host_memory()


def _assert_backend_stream_quiet(test: unittest.TestCase, engine: TTSEngine) -> None:
    backend = engine._cosy_backend
    test.assertIsNotNone(backend)
    reconcile = getattr(backend, "reconcile_released_outputs", None)
    if callable(reconcile):
        reconcile()
    test.assertEqual(backend.inflight_calls(), 0)
    test.assertEqual(backend.retained_output_count(), 0)
    with engine._turn_stream_session_lock:
        session = engine._turn_stream_session
    if session is not None:
        _, _, _, tfm_pending = engine._turn_stream_io_bindings()
        snap = session.snapshot()
        test.assertTrue(snap.quiet)
        test.assertTrue(session.paths_released(tfm_pending))


def _assert_tfm_shutdown_zero(test: unittest.TestCase, engine: TTSEngine) -> None:
    tfm_status = engine._temp_files.shutdown_status()
    test.assertTrue(tfm_status.complete)
    test.assertEqual(tfm_status.owned, 0)
    test.assertEqual(tfm_status.pending_cleanup, 0)
    test.assertEqual(tfm_status.active_io_leases, 0)


def _assert_runtime_quiet(test: unittest.TestCase, engine: TTSEngine) -> None:
    """Success path before shutdown: no in-flight work or retained failure outputs."""
    backend = engine._cosy_backend
    test.assertIsNotNone(backend)
    reconcile = getattr(backend, "reconcile_released_outputs", None)
    if callable(reconcile):
        reconcile()
    test.assertEqual(backend.inflight_calls(), 0)
    test.assertEqual(backend.retained_output_count(), 0)
    tfm = engine._temp_files
    test.assertEqual(tfm.owned_count(), 0)
    test.assertEqual(tfm.pending_cleanup_count(), 0)
    test.assertEqual(tfm.active_io_lease_count(), 0)
    with engine._turn_stream_session_lock:
        session = engine._turn_stream_session
    if session is not None:
        _, _, _, tfm_pending = engine._turn_stream_io_bindings()
        snap = session.snapshot()
        test.assertTrue(snap.quiet)
        test.assertTrue(session.paths_released(tfm_pending))


def _assert_runtime_after_failed_speak(test: unittest.TestCase, engine: TTSEngine) -> None:
    """Failed synthesis retains one owned WAV until worker retirement at shutdown."""
    backend = engine._cosy_backend
    test.assertIsNotNone(backend)
    reconcile = getattr(backend, "reconcile_released_outputs", None)
    if callable(reconcile):
        reconcile()
    test.assertEqual(backend.inflight_calls(), 0)
    test.assertEqual(backend.retained_output_count(), 1)


def _shutdown_and_assert_quiet(test: unittest.TestCase, engine: TTSEngine) -> None:
    engine.shutdown(stop_rvc=False)
    status = engine.get_shutdown_status()
    test.assertTrue(status.complete)
    test.assertTrue(status.temp_files_complete)
    test.assertTrue(status.synthesis_idle)
    _assert_tfm_shutdown_zero(test, engine)
    _assert_backend_stream_quiet(test, engine)


class CosyVoicePunctuationSegmentPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = _settings()

    def test_isolated_full_stop_plans_empty(self) -> None:
        self.assertEqual(plan_cosyvoice_segments("。", self.settings), [])

    def test_whitespace_and_symbol_only_segments_dropped(self) -> None:
        self.assertEqual(plan_cosyvoice_segments("   ", self.settings), [])
        self.assertEqual(plan_cosyvoice_segments("「」", self.settings), [])
        self.assertEqual(plan_cosyvoice_segments("🔥", self.settings), [])

    def test_mixed_text_segments_preserved(self) -> None:
        text = "你好。再见！"
        segments = plan_cosyvoice_segments(text, self.settings)
        self.assertTrue(segments)
        self.assertEqual("".join(segments), text)

    def test_single_letter_digit_and_scripts_preserved(self) -> None:
        self.assertEqual(plan_cosyvoice_segments("好", self.settings), ["好"])
        self.assertEqual(plan_cosyvoice_segments("7", self.settings), ["7"])
        self.assertEqual(plan_cosyvoice_segments("Hi.", self.settings), ["Hi."])
        self.assertEqual(plan_cosyvoice_segments("あ。", self.settings), ["あ。"])
        self.assertEqual(plan_cosyvoice_segments("مرحبا.", self.settings), ["مرحبا."])

    def test_rejects_over_3200_chars_before_filter(self) -> None:
        text = "x" * (MAX_TOTAL_SPEECH_CHARS + 1)
        with self.assertRaises(CosyVoiceSpeechLimitError):
            plan_cosyvoice_segments(text, self.settings)

    def test_rejects_over_32_segments_before_filter(self) -> None:
        settings = _settings(max_text_chars=10)
        text = "。".join(["一二三四五六七八九十"] * 40)
        with self.assertRaises(CosyVoiceSpeechLimitError):
            plan_cosyvoice_segments(text, settings)


class CosyVoicePunctuationSpeakStreamTests(unittest.TestCase):
    def test_speak_stream_skips_punctuation_only_chunks(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp, rvc_model=False)
            try:
                assert fake is not None
                synth_texts: list[str] = []
                plays: list[str] = []
                original = fake.synthesize

                def _track_synth(text, *, style_id, output_path, cancel_event=None):
                    synth_texts.append(text)
                    return original(
                        text,
                        style_id=style_id,
                        output_path=output_path,
                        cancel_event=cancel_event,
                    )

                fake.synthesize = _track_synth  # type: ignore[method-assign]
                engine._player.play = lambda path: plays.append(path)  # type: ignore[method-assign]
                chunk_done = {"n": 0}
                engine.speak_stream(
                    iter(["你好。", "。", "再见。"]),
                    on_chunk_done=lambda: chunk_done.__setitem__(
                        "n", chunk_done["n"] + 1
                    ),
                )
                self.assertEqual(chunk_done["n"], 3)
                self.assertEqual(synth_texts, ["你好。", "再见。"])
                self.assertEqual(len(plays), 2)
                _assert_runtime_quiet(self, engine)
            finally:
                _shutdown_and_assert_quiet(self, engine)

    def test_real_synth_failure_still_propagates(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp, rvc_model=False)
            try:
                assert fake is not None
                fake.synthesize = lambda *a, **k: False  # type: ignore[method-assign]
                with self.assertRaises(RuntimeError):
                    engine.speak_stream(iter(["合成失败句。"]))
                _assert_runtime_after_failed_speak(self, engine)
            finally:
                _shutdown_and_assert_quiet(self, engine)

    def test_synth_keyboard_interrupt_identity_propagates(self) -> None:
        with TemporaryDirectory() as tmp:
            engine, fake, _ = _make_engine(tmp, rvc_model=False)
            try:
                assert fake is not None
                interrupt = KeyboardInterrupt()

                def _raise_interrupt(*_args, **_kwargs):
                    raise interrupt

                fake.synthesize = _raise_interrupt  # type: ignore[method-assign]
                with self.assertRaises(KeyboardInterrupt) as caught:
                    engine.speak_stream(iter(["中断句。"]))
                self.assertIs(caught.exception, interrupt)
                _assert_runtime_after_failed_speak(self, engine)
            finally:
                _shutdown_and_assert_quiet(self, engine)


if __name__ == "__main__":
    unittest.main()
