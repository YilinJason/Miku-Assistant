"""OwnedThreadTask 生命周期契约（纯 stdlib，无模型/设备）。"""
from __future__ import annotations

import sys
import threading
import unittest

from src.core.owned_thread import OwnedThreadTask, OwnedThreadStatus

_REAL_THREAD = threading.Thread


def _wait(event: threading.Event, timeout: float = 5.0, label: str = "gate") -> None:
    if not event.wait(timeout):
        raise AssertionError(f"{label} timeout")


def _join_dead(thread: threading.Thread, timeout: float = 3.0) -> None:
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise AssertionError("thread still alive after join")


def _require_quiet(task: OwnedThreadTask, timeout: float = 2.0) -> None:
    if not task.join(timeout=timeout):
        raise AssertionError("expected quiet join")


def _run_on_thread(
    fn,
) -> tuple[list[object], list[BaseException], threading.Thread]:
    results: list[object] = []
    errors: list[BaseException] = []

    def body() -> None:
        try:
            results.append(fn())
        except BaseException as exc:
            errors.append(exc)

    thread = _REAL_THREAD(target=body)
    thread.start()
    return results, errors, thread


class _DeferredThread:
    """仅注入受测 task 的 thread_factory；外层 creator 一律 _REAL_THREAD。"""

    def __init__(
        self,
        target,
        *,
        error: BaseException | None = None,
        block: tuple[threading.Event, threading.Event] | None = None,
        start_barrier: tuple[threading.Event, threading.Event] | None = None,
        after_real_start: bool = False,
        **kwargs,
    ) -> None:
        self.target = target
        self.error = error
        self.block = block
        self.start_barrier = start_barrier
        self.after_real_start = after_real_start
        self.actual: threading.Thread | None = None
        self._kwargs = kwargs

    @property
    def ident(self):
        return None if self.actual is None else self.actual.ident

    def is_alive(self) -> bool:
        return self.actual is not None and self.actual.is_alive()

    def resolve(self) -> None:
        self.actual = _REAL_THREAD(target=self.target, **self._kwargs)
        self.actual.start()

    def start(self) -> None:
        if self.start_barrier is not None:
            entered, release = self.start_barrier
            entered.set()
            _wait(release, label="start barrier")
        if self.block is not None:
            entered, release = self.block
            entered.set()
            _wait(release, label="deferred start gate")
        if self.error is None or self.after_real_start:
            self.resolve()
        if self.error is not None:
            raise self.error

    def join(self, timeout: float | None = None) -> None:
        if self.actual is None:
            raise RuntimeError("unstarted join")
        self.actual.join(timeout=timeout)


class StartBasicsTests(unittest.TestCase):
    def test_construct_does_not_create_thread(self) -> None:
        created: list[threading.Thread] = []

        def factory(**kwargs):
            t = _REAL_THREAD(**kwargs)
            created.append(t)
            return t

        task = OwnedThreadTask(lambda _c: None, name="t", thread_factory=factory)
        st = task.snapshot()
        self.assertEqual(created, [])
        self.assertTrue(st.quiet)
        self.assertFalse(st.alive)
        self.assertIsNone(task.error)

    def test_single_start_ack_not_success(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def target(cancel: threading.Event) -> None:
            entered.set()
            _wait(release, label="target release")

        task = OwnedThreadTask(target, name="ok")
        try:
            self.assertTrue(task.start())
            _wait(entered, timeout=3.0, label="target enter")
            self.assertIsNone(task.error)
            task.cancel()
            release.set()
            _require_quiet(task)
        finally:
            release.set()
            task.cancel()
            _require_quiet(task)

    def test_repeat_and_concurrent_start_second_false(self) -> None:
        gate = threading.Event()
        release = threading.Event()
        results: list[bool] = []
        racer_errors: list[BaseException] = []
        lock = threading.Lock()

        def target(cancel: threading.Event) -> None:
            gate.set()
            _wait(release, label="target release")

        def racer() -> None:
            try:
                value = task.start()
            except BaseException as exc:
                with lock:
                    racer_errors.append(exc)
                return
            with lock:
                results.append(value)

        task = OwnedThreadTask(target, name="once")
        racers: list[threading.Thread] = []
        try:
            self.assertTrue(task.start())
            _wait(gate, timeout=3.0, label="target gate")
            self.assertFalse(task.start())
            for _ in range(4):
                t = _REAL_THREAD(target=racer)
                racers.append(t)
                t.start()
            for t in racers:
                _join_dead(t)
            self.assertEqual(len(results), 4)
            self.assertEqual(racer_errors, [])
            self.assertTrue(all(r is False for r in results))
        finally:
            release.set()
            task.cancel()
            _require_quiet(task)

    def test_cancel_before_start_false_zero_thread(self) -> None:
        factory_calls: list[str] = []

        def factory(**kwargs):
            factory_calls.append(kwargs["name"])
            return _REAL_THREAD(**kwargs)

        task = OwnedThreadTask(lambda _c: None, name="nope", thread_factory=factory)
        task.cancel()
        self.assertFalse(task.start())
        self.assertEqual(factory_calls, [])
        self.assertIsNone(task.error)
        self.assertTrue(task.snapshot().quiet)
        self.assertTrue(task.join(timeout=0.5))

    def test_target_ignores_cancel_join_short_budget_false(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def target(_cancel: threading.Event) -> None:
            entered.set()
            _wait(release, label="target release")

        task = OwnedThreadTask(target, name="ignore")
        try:
            self.assertTrue(task.start())
            _wait(entered, timeout=3.0, label="target enter")
            self.assertFalse(task.join(timeout=0.05))
            st = task.snapshot()
            self.assertFalse(st.quiet)
            self.assertTrue(st.alive)
            self.assertIsNone(task.error)
        finally:
            release.set()
            _require_quiet(task)


class ConstructorAndStartFailureTests(unittest.TestCase):
    def test_factory_blocks_snapshot_join_cancel_fast(self) -> None:
        ctor_gate = threading.Event()
        release_ctor = threading.Event()

        def blocking_factory(**kwargs):
            ctor_gate.set()
            _wait(release_ctor, label="constructor release")
            return _REAL_THREAD(**kwargs)

        task = OwnedThreadTask(lambda _c: None, name="block", thread_factory=blocking_factory)
        results, errors, creator = _run_on_thread(task.start)
        try:
            _wait(ctor_gate, timeout=3.0, label="constructor gate")
            self.assertFalse(task.start())
            self.assertTrue(task.snapshot().creator_pending)
            self.assertFalse(task.snapshot().quiet)
            self.assertFalse(task.join(timeout=0.05))
            task.cancel()
        finally:
            release_ctor.set()
            _join_dead(creator)
            self.assertEqual(errors, [])
            task.cancel()
            _require_quiet(task)

    def test_factory_exception_preserves_object(self) -> None:
        for failure in (
            MemoryError("no thread"),
            KeyboardInterrupt("ctor"),
            SystemExit(2),
        ):
            with self.subTest(kind=type(failure).__name__):

                def factory(**_kwargs):
                    raise failure

                task = OwnedThreadTask(lambda _c: None, name="boom", thread_factory=factory)
                caught: BaseException | None = None
                try:
                    task.start()
                except BaseException as exc:
                    caught = exc
                self.assertIs(caught, failure)
                self.assertIs(task.error, failure)
                self.assertTrue(task.join(timeout=0.0))
                self.assertTrue(task.snapshot().quiet)
                self.assertFalse(task.start())

    def test_start_blocks_unacknowledged(self) -> None:
        start_gate = threading.Event()
        release_start = threading.Event()
        created: list[_DeferredThread] = []

        def factory(**kwargs):
            thread = _DeferredThread(block=(start_gate, release_start), **kwargs)
            created.append(thread)
            return thread

        task = OwnedThreadTask(lambda _c: None, name="slow", thread_factory=factory)
        results, errors, creator = _run_on_thread(task.start)
        try:
            _wait(start_gate, timeout=3.0, label="start gate")
            self.assertTrue(task.snapshot().creator_pending)
            self.assertFalse(task.join(timeout=0.05))
        finally:
            release_start.set()
            _join_dead(creator)
            self.assertEqual(errors, [])
            for deferred in created:
                if deferred.actual is not None:
                    deferred.join(timeout=2.0)
                    self.assertFalse(deferred.is_alive())
            _require_quiet(task)
        self.assertEqual(results, [True])

    def test_start_unknown_needs_late_entry_terminal_and_exit(self) -> None:
        for failure in (
            RuntimeError("start failed"),
            KeyboardInterrupt("bootstrap"),
            SystemExit(9),
        ):
            with self.subTest(kind=type(failure).__name__):
                created: list[_DeferredThread] = []

                def factory(**kwargs):
                    thread = _DeferredThread(error=failure, **kwargs)
                    created.append(thread)
                    return thread

                task = OwnedThreadTask(lambda _c: None, name="unk", thread_factory=factory)
                caught: BaseException | None = None
                try:
                    try:
                        task.start()
                    except BaseException as exc:
                        caught = exc
                    self.assertIs(caught, failure)
                    self.assertIs(task.error, failure)
                    self.assertTrue(task.snapshot().start_unknown)
                    self.assertFalse(task.snapshot().quiet)
                    self.assertFalse(task.join(timeout=0.01))
                    self.assertFalse(task.start())
                    task.cancel()
                    created[0].resolve()
                    created[0].join(timeout=2.0)
                    self.assertFalse(created[0].is_alive())
                    _require_quiet(task)
                    self.assertFalse(task.snapshot().start_unknown)
                    self.assertTrue(task.snapshot().quiet)
                finally:
                    task.cancel()
                    for deferred in created:
                        if deferred.actual is not None:
                            deferred.join(timeout=2.0)
                            self.assertFalse(deferred.is_alive())

    def test_start_raises_after_thread_already_bootstrap(self) -> None:
        err = OSError("late raise")
        boot = threading.Event()
        created: list[_DeferredThread] = []
        calls: list[str] = []

        def target(_c: threading.Event) -> None:
            calls.append("ran")
            boot.set()

        def factory(**kwargs):
            thread = _DeferredThread(error=err, after_real_start=True, **kwargs)
            created.append(thread)
            return thread

        task = OwnedThreadTask(target, name="late", thread_factory=factory)
        caught: BaseException | None = None
        try:
            try:
                task.start()
            except BaseException as exc:
                caught = exc
            self.assertIs(caught, err)
            self.assertIs(task.error, err)
            _wait(boot, timeout=3.0, label="bootstrap")
            _require_quiet(task)
            self.assertEqual(calls, ["ran"])
            self.assertFalse(task.snapshot().start_unknown)
        finally:
            task.cancel()
            for deferred in created:
                if deferred.actual is not None:
                    deferred.join(timeout=2.0)
                    self.assertFalse(deferred.is_alive())


class PostStartInterruptTests(unittest.TestCase):
    def test_interrupt_after_os_start_return_records_error_not_quiet(self) -> None:
        returned = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        created: list[threading.Thread] = []
        interruption = KeyboardInterrupt("after os start return")
        fired = False

        class _ReturnMarkerThread(_REAL_THREAD):
            def start(self) -> None:
                super().start()
                returned.set()

        def factory(**kwargs):
            thread = _ReturnMarkerThread(**kwargs)
            created.append(thread)
            return thread

        def target(_cancel: threading.Event) -> None:
            entered.set()
            _wait(release, label="post-start release")

        def trace(frame, event, arg):
            nonlocal fired
            if (
                frame.f_code is OwnedThreadTask.start.__code__
                and event == "line"
                and returned.is_set()
                and not fired
            ):
                fired = True
                raise interruption
            return trace

        task = OwnedThreadTask(target, name="post-start-ki", thread_factory=factory)
        caught: list[BaseException] = []
        prior = sys.gettrace()
        try:
            sys.settrace(trace)
            try:
                task.start()
            except BaseException as exc:
                caught.append(exc)
            finally:
                sys.settrace(prior)
            self.assertTrue(fired)
            _wait(entered, timeout=3.0, label="target enter")
            self.assertEqual(len(caught), 1)
            self.assertIs(caught[0], interruption)
            self.assertIs(task.error, interruption)
            self.assertFalse(task.snapshot().quiet)
            self.assertFalse(task.join(timeout=0.0))
        finally:
            sys.settrace(prior)
            task.cancel()
            release.set()
            for thread in created:
                if thread.ident is not None:
                    _join_dead(thread)
            _require_quiet(task)


class UnknownLateCancelTests(unittest.TestCase):
    def test_unknown_late_start_cancel_zero_target(self) -> None:
        start_entered = threading.Event()
        start_release = threading.Event()
        calls: list[str] = []
        created: list[_DeferredThread] = []

        def target(_c: threading.Event) -> None:
            calls.append("target")

        def factory(**kwargs):
            thread = _DeferredThread(
                start_barrier=(start_entered, start_release),
                **kwargs,
            )
            created.append(thread)
            return thread

        task = OwnedThreadTask(target, name="late-cancel", thread_factory=factory)
        results, errors, creator = _run_on_thread(task.start)
        try:
            _wait(start_entered, timeout=3.0, label="start barrier")
            task.cancel()
            start_release.set()
            _join_dead(creator)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0])
            _require_quiet(task)
            self.assertEqual(calls, [])
            self.assertTrue(task.snapshot().quiet)
        finally:
            task.cancel()
            start_release.set()
            if creator.is_alive():
                _join_dead(creator)
            for deferred in created:
                if deferred.actual is not None:
                    deferred.join(timeout=2.0)
                    self.assertFalse(deferred.is_alive())
            _require_quiet(task)


class TargetExceptionTests(unittest.TestCase):
    def test_target_errors_preserved_quiet_not_success(self) -> None:
        for failure in (
            ValueError("boom"),
            KeyboardInterrupt("in target"),
            SystemExit(3),
        ):
            with self.subTest(kind=type(failure).__name__):

                def target(_c: threading.Event) -> None:
                    raise failure

                task = OwnedThreadTask(target, name="err")
                try:
                    self.assertTrue(task.start())
                    _require_quiet(task)
                    self.assertIs(task.error, failure)
                    st = task.snapshot()
                    self.assertTrue(st.quiet)
                    self.assertTrue(st.terminal)
                finally:
                    task.cancel()
                    _require_quiet(task)


class TerminalAliveTests(unittest.TestCase):
    def test_terminal_while_thread_still_alive_not_quiet(self) -> None:
        post_terminal = threading.Event()
        exit_gate = threading.Event()
        created: list[threading.Thread] = []

        def factory(**kwargs):
            wrapped = kwargs.pop("target")

            def run() -> None:
                wrapped()
                post_terminal.set()
                _wait(exit_gate, label="outer exit")

            thread = _REAL_THREAD(target=run, **kwargs)
            created.append(thread)
            return thread

        task = OwnedThreadTask(lambda _c: None, name="hold", thread_factory=factory)
        try:
            self.assertTrue(task.start())
            _wait(post_terminal, timeout=3.0, label="terminal gate")
            st = task.snapshot()
            self.assertTrue(st.terminal)
            self.assertTrue(st.alive)
            self.assertFalse(st.quiet)
            self.assertIsNone(task.error)
            self.assertFalse(task.join(timeout=0.05))
        finally:
            exit_gate.set()
            _require_quiet(task)
            for thread in created:
                _join_dead(thread)


class JoinTimeoutValidationTests(unittest.TestCase):
    def test_rejects_invalid_timeouts(self) -> None:
        task = OwnedThreadTask(lambda _c: None, name="v")
        for bad in (None, True, False, float("nan"), float("inf"), -1.0, 10.1):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    task.join(timeout=bad)  # type: ignore[arg-type]

    def test_never_started_join_is_quiet(self) -> None:
        task = OwnedThreadTask(lambda _c: None, name="fresh")
        self.assertTrue(task.join(timeout=0.0))


class SnapshotNonDeadlockTests(unittest.TestCase):
    def test_snapshot_while_creator_pending(self) -> None:
        gate = threading.Event()
        release = threading.Event()

        def factory(**kwargs):
            gate.set()
            _wait(release, label="constructor release")
            return _REAL_THREAD(**kwargs)

        task = OwnedThreadTask(lambda _c: None, name="snap", thread_factory=factory)
        results, errors, creator = _run_on_thread(task.start)
        try:
            _wait(gate, timeout=3.0, label="constructor gate")
            statuses: list[OwnedThreadStatus] = []
            for _ in range(5):
                statuses.append(task.snapshot())
            self.assertTrue(all(s.creator_pending for s in statuses))
        finally:
            release.set()
            _join_dead(creator)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            _require_quiet(task)


if __name__ == "__main__":
    unittest.main()
