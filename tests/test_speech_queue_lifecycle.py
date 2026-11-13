"""TurnSpeechPlayer 生命周期与有界状态（纯 fake TTS，无模型/设备）。"""
from __future__ import annotations

import logging
import math
import threading
import unittest
import unittest.mock
from typing import Callable, List, Optional
from unittest.mock import patch

import src.core.speech_queue as queue_module
from src.core.owned_thread import OwnedThreadTask
from src.core.speech_queue import PlaybackStatus, TurnSpeechPlayer

_REAL_THREAD = threading.Thread


def _wait(
    event: threading.Event,
    *,
    timeout: float = 5.0,
    label: str = "gate",
) -> None:
    if not event.wait(timeout):
        raise AssertionError(f"{label} timeout")


def _join_dead(thread: threading.Thread, timeout: float = 3.0) -> None:
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise AssertionError("thread still alive after join")


def _launch(fn: Callable[[], object]) -> tuple[
    threading.Thread, list[object], list[BaseException], threading.Event
]:
    results: list[object] = []
    errors: list[BaseException] = []
    done = threading.Event()

    def body() -> None:
        try:
            results.append(fn())
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = _REAL_THREAD(target=body)
    worker.start()
    return worker, results, errors, done


def _require_quiet_shutdown(player: TurnSpeechPlayer, timeout: float = 2.0) -> None:
    if not player.shutdown(timeout=timeout):
        raise AssertionError("expected quiet shutdown")


def _resolve_deferred_threads(threads: List[object]) -> None:
    for thread in threads:
        if isinstance(thread, _DeferredThread) and thread.actual is None:
            thread.resolve()


class _DeferredThread:
    def __init__(
        self,
        target,
        *,
        block: tuple[threading.Event, threading.Event] | None = None,
        outer: tuple[threading.Event, threading.Event] | None = None,
        error: BaseException | None = None,
        **kwargs,
    ) -> None:
        self.target = target
        self.block = block
        self.outer = outer
        self.error = error
        self.actual: threading.Thread | None = None
        self._kwargs = kwargs

    @property
    def ident(self):
        return None if self.actual is None else self.actual.ident

    def is_alive(self) -> bool:
        return self.actual is not None and self.actual.is_alive()

    def resolve(self) -> None:
        inner = self.target
        if self.outer is not None:
            reached, release = self.outer

            def outer_target() -> None:
                inner()
                reached.set()
                _wait(release, label="outer exit")

            run_target = outer_target
        else:
            run_target = inner
        self.actual = _REAL_THREAD(target=run_target, **self._kwargs)
        self.actual.start()

    def start(self) -> None:
        if self.block is not None:
            entered, release = self.block
            entered.set()
            _wait(release, label="start release")
        if self.error is not None:
            raise self.error
        self.resolve()

    def join(self, timeout: float | None = None) -> None:
        if self.actual is None:
            raise RuntimeError("unstarted join")
        self.actual.join(timeout=timeout)


class _OwnerPatch:
    def __init__(self, factory: Callable[..., threading.Thread]) -> None:
        self.owners: List[OwnedThreadTask] = []
        self._factory = factory

    def make(self, target, **kwargs) -> OwnedThreadTask:
        kwargs["thread_factory"] = self._factory
        owner = OwnedThreadTask(target, **kwargs)
        self.owners.append(owner)
        return owner

    def __enter__(self) -> _OwnerPatch:
        self._patch = patch.object(queue_module, "OwnedThreadTask", side_effect=self.make)
        self._patch.__enter__()
        return self

    def __exit__(self, *args) -> None:
        self._patch.__exit__(*args)


class FakeTTS:
    def speak(self, text: str) -> None:
        pass


class SpeechQueueLifecycleTests(unittest.TestCase):
    def _owner_patch(self, factory: Callable[..., threading.Thread]) -> _OwnerPatch:
        return _OwnerPatch(factory)

    def test_inert_constructor_never_builds_thread_via_factory_patch(self) -> None:
        created: List[threading.Thread] = []

        def factory(**kwargs):
            thread = _REAL_THREAD(**kwargs)
            created.append(thread)
            return thread

        with self._owner_patch(factory) as ctx:
            player = TurnSpeechPlayer(None, poll_interval=0.02)
            try:
                self.assertEqual(created, [])
                tid = player.begin_turn()
                self.assertTrue(player.enqueue(tid, "   "))
                self.assertFalse(player.enqueue(tid + 1, "stale"))
                player.mark_production_finished(tid)
                self.assertEqual(
                    player.wait_for_turn(tid, timeout=1.0), PlaybackStatus.COMPLETED
                )
                self.assertEqual(created, [])
            finally:
                _require_quiet_shutdown(player, timeout=2.0)
            self.assertEqual(len(ctx.owners), 1)

    def test_cancellation_barrier_before_commit_rejects_without_start(self) -> None:
        for argument in ("cancel_event", "extra_cancel"):
            with self.subTest(argument=argument):
                checked = threading.Event()
                calls: List[str] = []
                created: List[threading.Thread] = []

                class ObservedCancel(threading.Event):
                    def is_set(self) -> bool:
                        value = super().is_set()
                        checked.set()
                        return value

                cancellation = ObservedCancel()

                class TTS:
                    def speak(self, text: str) -> None:
                        calls.append(text)

                def factory(**kwargs):
                    thread = _REAL_THREAD(**kwargs)
                    created.append(thread)
                    return thread

                creator: Optional[threading.Thread] = None
                with self._owner_patch(factory):
                    player = TurnSpeechPlayer(TTS(), poll_interval=0.02)
                    try:
                        tid = player.begin_turn()
                        with player._cond:  # noqa: SLF001
                            creator, results, errors, done = _launch(
                                lambda: player.enqueue(
                                    tid,
                                    "already cancelled",
                                    **{argument: cancellation},
                                )
                            )
                            _wait(checked, label="pre-admission cancellation check")
                            cancellation.set()
                        _join_dead(creator)
                        self.assertEqual(errors, [])
                        self.assertEqual(results, [False])
                        self.assertEqual(created, [])
                        self.assertEqual(calls, [])
                    finally:
                        cancellation.set()
                        if creator is not None and creator.is_alive():
                            _join_dead(creator)
                        _require_quiet_shutdown(player)
                        for thread in created:
                            _join_dead(thread)

    def test_speak_failure_logger_does_not_emit_traceback(self) -> None:
        entered = threading.Event()

        class SensitiveError(RuntimeError):
            def __str__(self) -> str:
                return "user-secret-path C:\\Users\\me\\voice.wav"

        class BoomTTS:
            def speak(self, text: str) -> None:
                entered.set()
                raise SensitiveError("boom")

        records: List[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        logger = logging.getLogger("speech_queue.lifecycle.test")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.ERROR)

        player = TurnSpeechPlayer(BoomTTS(), poll_interval=0.02, logger=logger)
        try:
            tid = player.begin_turn()
            self.assertTrue(player.enqueue(tid, "x"))
            _wait(entered)
            for _ in range(100):
                if player.consumer_failed:
                    break
                threading.Event().wait(0.01)
            self.assertTrue(player.consumer_failed)
            self.assertEqual(len(records), 1)
            msg = records[0].getMessage()
            self.assertIn("err=", msg)
            self.assertNotIn("voice.wav", msg)
            self.assertIsNone(records[0].exc_info)
        finally:
            logger.removeHandler(handler)
            _require_quiet_shutdown(player)

    def test_blocked_constructor_shutdown_bounded_and_late_start_skips_speech(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls: List[str] = []
        created: List[threading.Thread] = []

        class TTS:
            def speak(self, text: str) -> None:
                calls.append(text)

        def factory(**kwargs):
            entered.set()
            _wait(release, label="constructor release")
            thread = _REAL_THREAD(**kwargs)
            created.append(thread)
            return thread

        creator: Optional[threading.Thread] = None
        with self._owner_patch(factory) as ctx:
            player = TurnSpeechPlayer(TTS(), poll_interval=0.02)
            try:
                tid = player.begin_turn()
                creator, results, errors, done = _launch(
                    lambda: player.enqueue(tid, "never")
                )
                _wait(entered)
                self.assertTrue(ctx.owners[0].snapshot().creator_pending)
                self.assertFalse(player.shutdown(timeout=0.5))
                self.assertFalse(done.is_set())
            finally:
                release.set()
                if creator is not None:
                    _join_dead(creator)
                _require_quiet_shutdown(player)
                for thread in created:
                    if thread.ident is not None:
                        _join_dead(thread)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [False])
            self.assertEqual(calls, [])

    def test_unknown_start_failures_preserve_error_and_owner(self) -> None:
        for failure in (
            RuntimeError("start"),
            KeyboardInterrupt("start"),
            SystemExit(7),
        ):
            with self.subTest(kind=type(failure).__name__):
                created: List[_DeferredThread] = []
                calls: List[str] = []
                caught: List[BaseException] = []

                class TTS:
                    def speak(self, text: str) -> None:
                        calls.append(text)

                def factory(**kwargs):
                    thread = _DeferredThread(error=failure, **kwargs)
                    created.append(thread)
                    return thread

                with self._owner_patch(factory) as ctx:
                    player = TurnSpeechPlayer(TTS(), poll_interval=0.02)
                    try:
                        tid = player.begin_turn()
                        try:
                            player.enqueue(tid, "never")
                        except BaseException as exc:
                            caught.append(exc)
                        self.assertEqual(len(caught), 1)
                        self.assertIs(caught[0], failure)
                        self.assertIs(ctx.owners[0].error, failure)
                        self.assertEqual(
                            player.wait_for_turn(tid, timeout=0.5),
                            PlaybackStatus.FAILED,
                        )
                        self.assertFalse(player.shutdown(timeout=0.5))
                        fresh = player.begin_turn()
                        self.assertFalse(player.enqueue(fresh, "also never"))
                        self.assertEqual(len(created), 1)
                    finally:
                        _resolve_deferred_threads(created)
                        for thread in created:
                            if thread.actual is not None:
                                _join_dead(thread.actual)
                        _require_quiet_shutdown(player)
                    self.assertEqual(calls, [])

    def test_fatal_target_notifies_waiter_and_blocks_restart(self) -> None:
        for failure in (KeyboardInterrupt("target"), SystemExit(4)):
            with self.subTest(kind=type(failure).__name__):
                created: List[threading.Thread] = []

                class TTS:
                    def speak(self, text: str) -> None:
                        raise failure

                def factory(**kwargs):
                    thread = _REAL_THREAD(**kwargs)
                    created.append(thread)
                    return thread

                with self._owner_patch(factory) as ctx:
                    player = TurnSpeechPlayer(TTS(), poll_interval=0.02)
                    try:
                        tid = player.begin_turn()
                        self.assertTrue(player.enqueue(tid, "fatal"))
                        player.mark_production_finished(tid)
                        self.assertEqual(
                            player.wait_for_turn(tid, timeout=3.0),
                            PlaybackStatus.FAILED,
                        )
                        for thread in created:
                            _join_dead(thread)
                        self.assertIs(ctx.owners[0].error, failure)
                        fresh = player.begin_turn()
                        self.assertFalse(player.enqueue(fresh, "no restart"))
                        self.assertEqual(len(created), 1)
                    finally:
                        _require_quiet_shutdown(player)
                        for thread in created:
                            _join_dead(thread)

    def test_outer_thread_alive_after_terminal_shutdown_not_quiet(self) -> None:
        reached = threading.Event()
        release = threading.Event()
        created: List[_DeferredThread] = []

        class TTS:
            def speak(self, text: str) -> None:
                pass

        def factory(**kwargs):
            thread = _DeferredThread(outer=(reached, release), **kwargs)
            created.append(thread)
            return thread

        with self._owner_patch(factory) as ctx:
            player = TurnSpeechPlayer(TTS(), poll_interval=0.02)
            try:
                tid = player.begin_turn()
                self.assertTrue(player.enqueue(tid, "short"))
                player.mark_production_finished(tid)
                self.assertEqual(
                    player.wait_for_turn(tid, timeout=3.0), PlaybackStatus.COMPLETED
                )
                player.stop_accepting()
                _wait(reached)
                snap = ctx.owners[0].snapshot()
                self.assertTrue(snap.terminal)
                self.assertTrue(snap.alive)
                self.assertFalse(player.shutdown(timeout=0.5))
            finally:
                release.set()
                for thread in created:
                    if thread.actual is not None:
                        _join_dead(thread.actual)
                _require_quiet_shutdown(player)

    def test_ten_thousand_turns_bounded_and_old_waiter_invalidated(self) -> None:
        player = TurnSpeechPlayer(None, poll_interval=0.01)
        waiter: Optional[threading.Thread] = None
        waiter_entered = threading.Event()
        original_wait = player._cond.wait

        def observed_wait(timeout=None):
            waiter_entered.set()
            return original_wait(timeout)

        try:
            old_tid = player.begin_turn()
            player._cond.wait = observed_wait  # noqa: SLF001
            waiter, results, errors, done = _launch(
                lambda: player.wait_for_turn(old_tid, timeout=5.0)
            )
            _wait(waiter_entered)
            for _ in range(10_000):
                tid = player.begin_turn()
                player.mark_production_finished(tid)
                self.assertLessEqual(len(player._turns), 128)  # noqa: SLF001
            _join_dead(waiter)
            self.assertEqual(errors, [])
            self.assertEqual(results, [PlaybackStatus.INVALIDATED])
            self.assertNotIn(old_tid, player._turns)  # noqa: SLF001
            self.assertEqual(
                player.wait_for_turn(old_tid, timeout=0.5),
                PlaybackStatus.INVALIDATED,
            )
        finally:
            player._cond.wait = original_wait  # noqa: SLF001
            _require_quiet_shutdown(player)
            if waiter is not None and waiter.is_alive():
                _join_dead(waiter)

    def test_config_and_shutdown_budget_matrix(self) -> None:
        for option, values in (
            ("maxsize", (True, False, 0, 129, 1.5)),
            ("poll_interval", (True, 0.0, 1.1, float("nan"), float("inf"))),
        ):
            for value in values:
                with self.subTest(option=option, value=value):
                    with self.assertRaises(ValueError):
                        TurnSpeechPlayer(FakeTTS(), **{option: value})  # type: ignore[arg-type]
        player = TurnSpeechPlayer(FakeTTS(), poll_interval=0.02)
        try:
            for invalid in (None, True, -1.0, float("nan"), float("inf"), 11.0):
                with self.subTest(timeout=invalid):
                    with self.assertRaises(ValueError):
                        player.shutdown(timeout=invalid)  # type: ignore[arg-type]
                    self.assertFalse(player.stopped)
        finally:
            _require_quiet_shutdown(player)

    def test_recoverable_exception_clears_on_new_turn(self) -> None:
        entered = threading.Event()

        class BoomTTS:
            def speak(self, text: str) -> None:
                entered.set()
                raise RuntimeError("boom")

        player = TurnSpeechPlayer(BoomTTS(), poll_interval=0.02)
        ok = threading.Event()

        class OkTTS:
            def speak(self, text: str) -> None:
                ok.set()

        try:
            tid = player.begin_turn()
            self.assertTrue(player.enqueue(tid, "x"))
            _wait(entered)
            for _ in range(100):
                if player.consumer_failed:
                    break
                threading.Event().wait(0.01)
            self.assertTrue(player.consumer_failed)
            tid2 = player.begin_turn()
            self.assertFalse(player.consumer_failed)
            player._tts = OkTTS()  # noqa: SLF001
            self.assertTrue(player.enqueue(tid2, "ok"))
            _wait(ok)
        finally:
            _require_quiet_shutdown(player)

    def test_shutdown_idempotent_when_quiet(self) -> None:
        player = TurnSpeechPlayer(FakeTTS(), poll_interval=0.02)
        tid = player.begin_turn()
        self.assertTrue(player.enqueue(tid, "x"))
        player.mark_production_finished(tid)
        player.wait_for_turn(tid, timeout=2.0)
        _require_quiet_shutdown(player)
        self.assertTrue(player.shutdown(timeout=2.0))


if __name__ == "__main__":
    unittest.main()
