"""Turn 级 stream 预取：真实 TTSEngine + TurnSpeechPlayer，仅 mock prepare/play。"""
from __future__ import annotations

import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Iterator, List, Optional, Set, Tuple
from unittest.mock import patch

from src.core.safety import SafetyPolicy
from src.core.speech_queue import PlaybackStatus, TurnSpeechPlayer
from src.voice.audio.player import AudioPlayer
from src.voice.audio.temp_files import TempFileManager
from src.voice.tts.engine import TTSEngine


def _join(thread: threading.Thread, timeout: float = 3.0) -> None:
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise AssertionError("thread still alive")


def _require_quiet_player(player: TurnSpeechPlayer, timeout: float = 2.0) -> None:
    if not player.shutdown(timeout=timeout):
        raise AssertionError("TurnSpeechPlayer shutdown not quiet")


def _require_quiet_tfm(tfm: TempFileManager, timeout: float = 2.0) -> None:
    status = tfm.shutdown(timeout=timeout)
    if not getattr(status, "complete", False):
        raise AssertionError("TempFileManager shutdown incomplete")
    if tfm.delayed_worker_alive():
        raise AssertionError("TFM delayed worker still alive")
    if tfm.owned_count() != 0:
        raise AssertionError("TFM still owns paths after shutdown")


def _require_quiet_engine(engine: TTSEngine) -> None:
    engine.shutdown(stop_rvc=False)
    status = engine.get_shutdown_status()
    if not status.complete:
        raise AssertionError("engine aggregate shutdown not complete")
    if not status.synthesis_idle:
        raise AssertionError("engine synthesis not idle after shutdown")


class _EnginePlayerHarness:
    def __init__(
        self,
        stack: ExitStack,
        *,
        pipeline_enabled: bool = True,
    ) -> None:
        self.stack = stack
        self.tmp = stack.enter_context(TemporaryDirectory())
        self.policy = SafetyPolicy({"permissions": {"file_delete": True}})
        self.policy.register_file_delete_dir(self.tmp)
        self.tfm = TempFileManager(safety_policy=self.policy, default_dir=self.tmp)
        self.prepare_log: List[str] = []
        self.play_log: List[str] = []
        self.path_to_text: Dict[str, str] = {}
        self.prepare_active = 0
        self.prepare_peak = 0
        self.prepare_gates: Dict[str, threading.Event] = {}
        self.play_gate = threading.Event()
        self.play_gate.set()
        self.third_prepare_gate = threading.Event()
        self.third_prepare_gate.set()
        self._lock = threading.Lock()
        self._waiting_on_gate: Set[str] = set()

        stack.enter_context(
            patch.object(AudioPlayer, "_detect_backend", return_value="none")
        )
        self.engine = TTSEngine(
            engine="edge-tts",
            pipeline_enabled=pipeline_enabled,
            safety_policy=self.policy,
            temp_file_manager=self.tfm,
            warm_rvc_on_init=False,
        )
        self.player = TurnSpeechPlayer(self.engine, maxsize=8, poll_interval=0.02)
        self._shutdown_done = False
        self.stack.callback(self._shutdown_harness)

    def release_all_gates(self) -> None:
        self.play_gate.set()
        self.third_prepare_gate.set()
        for gate in self.prepare_gates.values():
            gate.set()

    def _shutdown_harness(self) -> None:
        if self._shutdown_done:
            return
        self.release_all_gates()
        _require_quiet_player(self.player)
        self.engine.shutdown(stop_rvc=False)
        status = self.engine.get_shutdown_status()
        if not status.complete:
            raise AssertionError("engine aggregate shutdown not complete")
        if not status.synthesis_idle:
            raise AssertionError("engine synthesis not idle after shutdown")
        _require_quiet_tfm(self.tfm)
        self._shutdown_done = True

    def arm_prepare_block(self, text: str) -> None:
        gate = threading.Event()
        gate.clear()
        self.prepare_gates[text] = gate

    def release_prepare(self, text: str) -> None:
        self.prepare_gates.get(text, threading.Event()).set()

    def arm_play_block(self) -> None:
        self.play_gate.clear()

    def release_play(self) -> None:
        self.play_gate.set()

    def block_third_prepare(self) -> None:
        self.third_prepare_gate.clear()

    def allow_third_prepare(self) -> None:
        self.third_prepare_gate.set()

    def _prepare_impl(
        self,
        text: str,
        timer=None,
        label: str = "",
    ) -> Tuple[Optional[str], Set[str]]:
        if text == "句三" and not self.third_prepare_gate.wait(timeout=0.25):
            raise AssertionError("third segment prepared too early")
        gate = self.prepare_gates.get(text)
        if gate is not None and not gate.is_set():
            with self._lock:
                self._waiting_on_gate.add(text)
        if gate is not None and not gate.wait(timeout=3.0):
            with self._lock:
                self._waiting_on_gate.discard(text)
            raise TimeoutError(f"prepare blocked on {text}")
        with self._lock:
            self._waiting_on_gate.discard(text)
        with self._lock:
            self.prepare_log.append(text)
            self.prepare_active += 1
            self.prepare_peak = max(self.prepare_peak, self.prepare_active)
        try:
            path = self.tfm.create(".wav", directory=self.tmp)
            Path(path).write_bytes(b"RIFF")
            self.path_to_text[path] = text
            return path, {path}
        finally:
            with self._lock:
                self.prepare_active -= 1

    def fake_play(self, path: str) -> None:
        if not self.play_gate.wait(timeout=3.0):
            raise TimeoutError("play blocked")
        text = self.path_to_text[path]
        self.play_log.append(text)

    def run_patched(self, fn) -> None:
        h = self

        def _fake_prepare(_engine, text, timer=None, label="", **kwargs):
            return h._prepare_impl(text, timer, label)

        real_play = self.engine._player.play
        self.engine._player.play = lambda p: self.fake_play(p)  # noqa: SLF001
        try:
            with patch.object(TTSEngine, "_cloud_tts_allowed", return_value=True):
                with patch.object(
                    TTSEngine, "_prepare_playable_audio", _fake_prepare
                ):
                    try:
                        fn()
                    finally:
                        self._shutdown_harness()
                        self.engine._player.play = real_play  # noqa: SLF001
        finally:
            if self.engine._player.play is not real_play:
                self.engine._player.play = real_play  # noqa: SLF001


class TurnAudioPrefetchWiringTests(unittest.TestCase):
    def test_first_chunk_before_production_finished(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack)
        started = threading.Event()

        def fake_prepare(_engine, text, timer=None, label="", **kwargs):
            started.set()
            if text == "首句":
                threading.Event().wait(0.2)
            path = h.tfm.create(".wav", directory=h.tmp)
            Path(path).write_bytes(b"RIFF")
            h.path_to_text[path] = text
            return path, {path}

        def body() -> None:
            h.engine._player.play = lambda _p: None  # noqa: SLF001
            tid = h.player.begin_turn()
            self.assertTrue(h.player.enqueue(tid, "首句"))
            self.assertTrue(started.wait(timeout=2.0))
            self.assertTrue(h.player.enqueue(tid, "后句"))
            h.player.mark_production_finished(tid)
            self.assertEqual(
                h.player.wait_for_turn(tid, timeout=5.0),
                PlaybackStatus.COMPLETED,
            )

        real_play = h.engine._player.play
        try:
            with patch.object(TTSEngine, "_cloud_tts_allowed", return_value=True):
                with patch.object(TTSEngine, "_prepare_playable_audio", fake_prepare):
                    try:
                        body()
                    finally:
                        h._shutdown_harness()
                        h.engine._player.play = real_play  # noqa: SLF001
        finally:
            if h.engine._player.play is not real_play:
                h.engine._player.play = real_play  # noqa: SLF001

    def test_real_prefetch_second_while_first_play_blocked(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)
        h.arm_prepare_block("句一")
        h.arm_play_block()
        h.block_third_prepare()
        second_ready = threading.Event()
        errors: List[BaseException] = []

        def body() -> None:
            tid = h.player.begin_turn()
            h.arm_play_block()
            self.assertTrue(h.player.enqueue(tid, "句一"))
            h.release_prepare("句一")

            def wait_second() -> None:
                try:
                    for _ in range(400):
                        if "句二" in h.prepare_log:
                            second_ready.set()
                            return
                        threading.Event().wait(0.01)
                    raise AssertionError("句二 not prepared during 句一 play")
                except BaseException as exc:
                    errors.append(exc)

            w = threading.Thread(target=wait_second, daemon=True)
            w.start()
            self.assertTrue(h.player.enqueue(tid, "句二"))
            self.assertTrue(second_ready.wait(timeout=3.0))
            _join(w)
            if errors:
                raise errors[0]
            self.assertNotIn("句三", h.prepare_log)
            self.assertTrue(h.player.enqueue(tid, "句三"))
            h.release_play()
            h.allow_third_prepare()
            h.player.mark_production_finished(tid)
            self.assertEqual(
                h.player.wait_for_turn(tid, timeout=8.0),
                PlaybackStatus.COMPLETED,
            )
            self.assertEqual(h.play_log, ["句一", "句二", "句三"])

        h.run_patched(body)
        self.assertLessEqual(h.prepare_peak, 1)

    def test_serial_pipeline_no_parallel_prepare(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=False)

        def body() -> None:
            tid = h.player.begin_turn()
            self.assertTrue(h.player.enqueue(tid, "a"))
            self.assertTrue(h.player.enqueue(tid, "b"))
            h.player.mark_production_finished(tid)
            self.assertEqual(
                h.player.wait_for_turn(tid, timeout=3.0),
                PlaybackStatus.COMPLETED,
            )

        h.run_patched(body)
        self.assertEqual(h.prepare_log, ["a", "b"])
        self.assertLessEqual(h.prepare_peak, 1)

    def test_play_failure_still_attempts_cleanup(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)
        remove_calls: List[str] = []

        def boom_play(_path: str) -> None:
            raise RuntimeError("play-boom")

        real_remove = h.tfm.remove

        def track_remove(path: str, *args, **kwargs) -> None:
            remove_calls.append(path)
            real_remove(path, *args, **kwargs)

        outcome: List[PlaybackStatus] = []

        def body() -> None:
            tid = h.player.begin_turn()
            self.assertTrue(h.player.enqueue(tid, "boom"))
            h.player.mark_production_finished(tid)
            outcome.append(h.player.wait_for_turn(tid, timeout=3.0))

        real_play = h.engine._player.play
        h.engine._player.play = boom_play  # noqa: SLF001
        try:
            with patch.object(TTSEngine, "_cloud_tts_allowed", return_value=True):
                with patch.object(
                    TTSEngine,
                    "_prepare_playable_audio",
                    lambda _e, text, timer=None, label="", **kwargs: h._prepare_impl(
                        text, timer, label
                    ),
                ):
                    with patch.object(h.tfm, "remove", track_remove):
                        try:
                            body()
                        finally:
                            h._shutdown_harness()
                            h.engine._player.play = real_play  # noqa: SLF001
        finally:
            if h.engine._player.play is not real_play:
                h.engine._player.play = real_play  # noqa: SLF001
        self.assertTrue(remove_calls)
        self.assertEqual(outcome[0], PlaybackStatus.FAILED)

    def test_prepare_entry_raises_when_prior_session_retained(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)
        epoch = h.engine._interrupt_epoch_snapshot()
        busy = RuntimeError(
            "上一轮 turn stream 预取未 quiet 或路径未释放，拒绝新 session"
        )
        with patch.object(
            h.engine,
            "_require_recycled_turn_stream_session",
            side_effect=busy,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                h.engine._prepare_turn_stream_entry(epoch, raise_on_busy=True)
        self.assertIn("quiet", str(ctx.exception).lower())
        _require_quiet_engine(h.engine)

    def test_tfm_pending_blocks_prepare_entry(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)
        epoch = h.engine._interrupt_epoch_snapshot()
        with patch.object(h.tfm, "pending_cleanup_count", return_value=1):
            with self.assertRaises(RuntimeError) as ctx:
                h.engine._prepare_turn_stream_entry(epoch, raise_on_busy=True)
        self.assertIn("pending", str(ctx.exception).lower())
        _require_quiet_engine(h.engine)

    def test_speak_stream_raises_when_turn_stream_busy(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)
        busy = RuntimeError(
            "上一轮 turn stream 预取未 quiet 或路径未释放，拒绝新 session"
        )
        with patch.object(
            h.engine,
            "_require_recycled_turn_stream_session",
            side_effect=busy,
        ):
            with patch.object(TTSEngine, "_cloud_tts_allowed", return_value=True):
                with self.assertRaises(RuntimeError):
                    h.engine.speak_stream(iter(["intruder"]))
        _require_quiet_engine(h.engine)

    def test_chunk_iterator_failure_propagates(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)

        def bad_chunks() -> Iterator[str]:
            yield "first"
            raise RuntimeError("chunk-iter-boom")

        seen: List[str] = []

        def fake_prepare(_engine, text, timer=None, label="", **kwargs):
            seen.append(text)
            path = h.tfm.create(".wav", directory=h.tmp)
            Path(path).write_bytes(b"RIFF")
            return path, {path}

        real_play = h.engine._player.play
        h.engine._player.play = lambda _p: None  # noqa: SLF001
        try:
            with patch.object(TTSEngine, "_cloud_tts_allowed", return_value=True):
                with patch.object(TTSEngine, "_prepare_playable_audio", fake_prepare):
                    try:
                        with self.assertRaises(RuntimeError) as ctx:
                            h.engine.speak_stream(bad_chunks())
                    finally:
                        h._shutdown_harness()
                        h.engine._player.play = real_play  # noqa: SLF001
        finally:
            if h.engine._player.play is not real_play:
                h.engine._player.play = real_play  # noqa: SLF001
        self.assertEqual(ctx.exception.args[0], "chunk-iter-boom")
        self.assertEqual(seen, ["first"])

    def test_tfm_pending_blocks_speak_stream(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)

        with patch.object(
            h.tfm, "pending_cleanup_count", return_value=1
        ):
            with patch.object(TTSEngine, "_cloud_tts_allowed", return_value=True):
                with self.assertRaises(RuntimeError) as ctx:
                    h.engine.speak_stream(iter(["x"]))
        self.assertIn("pending", str(ctx.exception).lower())
        _require_quiet_engine(h.engine)

    def test_interrupt_after_admission_blocks_late_prepare(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)
        h.arm_prepare_block("late")

        def body() -> None:
            tid = h.player.begin_turn()
            self.assertTrue(h.player.enqueue(tid, "first"))
            self.assertTrue(h.player.enqueue(tid, "late"))
            for _ in range(400):
                if h.prepare_log == ["first"]:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(h.prepare_log, ["first"])
            for _ in range(400):
                if "late" in h._waiting_on_gate:
                    break
                threading.Event().wait(0.01)
            self.assertIn("late", h._waiting_on_gate)
            self.assertTrue(h.player.compare_and_invalidate(tid))
            h.engine.interrupt()
            self.assertEqual(
                h.player.wait_for_turn(tid, timeout=8.0),
                PlaybackStatus.INVALIDATED,
            )
            self.assertNotIn("late", h.prepare_log)

        h.run_patched(body)

    def test_new_turn_event_stops_old_stream_prepare(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)
        h.arm_prepare_block("stale")

        def body() -> None:
            t1 = h.player.begin_turn()
            self.assertTrue(h.player.enqueue(t1, "stale"))
            self.assertTrue(h.player.enqueue(t1, "never"))
            for _ in range(400):
                if "stale" in h._waiting_on_gate:
                    break
                threading.Event().wait(0.01)
            self.assertIn("stale", h._waiting_on_gate)
            t2 = h.player.begin_turn()
            self.assertEqual(
                h.player.wait_for_turn(t1, timeout=2.0),
                PlaybackStatus.INVALIDATED,
            )
            self.assertTrue(h.player.enqueue(t2, "fresh"))
            h.release_prepare("stale")
            h.player.mark_production_finished(t2)
            self.assertEqual(
                h.player.wait_for_turn(t2, timeout=5.0),
                PlaybackStatus.COMPLETED,
            )

        h.run_patched(body)
        self.assertEqual(h.prepare_log.count("never"), 0)
        self.assertIn("fresh", h.prepare_log)

    def test_play_fail_aborts_late_edge_mp3_to_wav_stage(self) -> None:
        """播放失败后 session 永久 cancel 应阻断晚到 MP3 进入 MP3→WAV。"""
        import asyncio

        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)
        mp3_to_wav_log: List[str] = []
        seg_two_cloud = threading.Event()
        play_fail_gate = threading.Event()

        def track_mp3_to_wav(mp3_path: str, rvc_config) -> str:
            mp3_to_wav_log.append(mp3_path)
            wav = h.tfm.create(".wav", directory=h.tmp)
            Path(wav).write_bytes(b"RIFF")
            return wav

        async def fake_edge_tts(self, text: str) -> str:
            if text == "seg-two":
                seg_two_cloud.set()
                if not play_fail_gate.wait(timeout=3.0):
                    raise TimeoutError("play failure not signaled before seg-two save")
            path = h.tfm.create(".mp3", directory=h.tmp)
            Path(path).write_bytes(b"MP3")
            return path

        primary = RuntimeError("play-boom")

        def boom_play(_path: str) -> None:
            if not seg_two_cloud.wait(timeout=3.0):
                raise TimeoutError("seg-two cloud save not started before play")
            play_fail_gate.set()
            raise primary

        real_play = h.engine._player.play
        h.engine._player.play = boom_play  # noqa: SLF001
        try:
            with patch.object(TTSEngine, "_cloud_tts_allowed", return_value=True):
                with patch.object(TTSEngine, "_edge_tts_to_mp3", fake_edge_tts):
                    with patch.object(h.engine, "_mp3_to_wav", track_mp3_to_wav):
                        try:
                            with self.assertRaises(RuntimeError) as ctx:
                                h.engine.speak_stream(
                                    iter(["seg-one", "seg-two"])
                                )
                        finally:
                            h._shutdown_harness()
                            h.engine._player.play = real_play  # noqa: SLF001
        finally:
            if h.engine._player.play is not real_play:
                h.engine._player.play = real_play  # noqa: SLF001
        self.assertIs(ctx.exception, primary)
        self.assertEqual(len(mp3_to_wav_log), 1)

    def test_pre_cancelled_speak_stream_preserves_engine_interrupt(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)
        h.engine._signal_interrupt()
        self.assertTrue(h.engine._interrupt_event.is_set())
        cancel = threading.Event()
        cancel.set()
        prepare_calls: List[str] = []

        def track_prepare(_engine, text, timer=None, label="", **kwargs):
            prepare_calls.append(text)
            return h._prepare_impl(text, timer, label)

        real_play = h.engine._player.play
        h.engine._player.play = lambda _p: None  # noqa: SLF001
        try:
            with patch.object(TTSEngine, "_cloud_tts_allowed", return_value=True):
                with patch.object(
                    TTSEngine, "_prepare_playable_audio", track_prepare
                ):
                    try:
                        h.engine.speak_stream(iter(["skip"]), cancel_event=cancel)
                    finally:
                        h._shutdown_harness()
                        h.engine._player.play = real_play  # noqa: SLF001
        finally:
            if h.engine._player.play is not real_play:
                h.engine._player.play = real_play  # noqa: SLF001
        self.assertEqual(prepare_calls, [])
        self.assertTrue(h.engine._interrupt_event.is_set())

    def test_legacy_speak_silent_when_tfm_pending(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        h = _EnginePlayerHarness(stack, pipeline_enabled=True)
        spoken: List[str] = []

        def track_prepare(_engine, text, timer=None, label="", **kwargs):
            spoken.append("prepare")
            return h._prepare_impl(text, timer, label)

        real_play = h.engine._player.play
        h.engine._player.play = lambda _p: None  # noqa: SLF001
        try:
            with patch.object(h.tfm, "pending_cleanup_count", return_value=1):
                with patch.object(TTSEngine, "_cloud_tts_allowed", return_value=True):
                    with patch.object(
                        TTSEngine, "_prepare_playable_audio", track_prepare
                    ):
                        try:
                            h.engine.speak("hello")
                        finally:
                            h._shutdown_harness()
                            h.engine._player.play = real_play  # noqa: SLF001
        finally:
            if h.engine._player.play is not real_play:
                h.engine._player.play = real_play  # noqa: SLF001
        self.assertEqual(spoken, [])

    def test_legacy_fake_uses_speak_not_stream(self) -> None:
        spoken: List[str] = []

        class Legacy:
            def speak(self, text: str) -> None:
                spoken.append(text)

        player = TurnSpeechPlayer(Legacy(), poll_interval=0.02)
        try:
            tid = player.begin_turn()
            self.assertTrue(player.enqueue(tid, "a"))
            self.assertTrue(player.enqueue(tid, "b"))
            player.mark_production_finished(tid)
            self.assertEqual(
                player.wait_for_turn(tid, timeout=2.0),
                PlaybackStatus.COMPLETED,
            )
            self.assertEqual(spoken, ["a", "b"])
        finally:
            _require_quiet_player(player)


if __name__ == "__main__":
    unittest.main()
