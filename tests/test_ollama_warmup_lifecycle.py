"""Ollama 后台预热 OwnedThreadTask 生命周期与装配/shutdown 所有权（纯 fake，无真实模型/DB）。"""
from __future__ import annotations

import threading
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

from src.core.assembly import assemble_default
from src.core.assistant import MikuAssistant
from src.core.owned_thread import OwnedThreadTask
from src.llm.ollama_client import OllamaClient

REAL_THREAD = threading.Thread
_GATE_SEC = 3.0


class _Cfg:
    def __init__(self, tmp: str, *, memory: bool = False) -> None:
        self.data: Dict[str, Any] = {
            "ollama": {
                "base_url": "http://127.0.0.1:9",
                "model": "m",
                "timeout": 1,
                "keep_alive": "30m",
                "warmup_on_start": True,
                "warmup_timeout": 3,
            },
            "memory": {
                "enabled": memory,
                "store": {"path": f"{tmp}/mem.sqlite"},
            },
            "voice": {
                "tts": {
                    "engine": "edge-tts",
                    "language": "zh-CN",
                    "rate": 150,
                    "volume": 0.8,
                    "voice": "v",
                    "fallback_enabled": True,
                    "fallback_engine": "pyttsx3",
                    "pipeline_enabled": True,
                },
                "stt": {
                    "engine": "speech_recognition",
                    "language": "zh-CN",
                    "energy_threshold": 1,
                },
            },
            "system": {"windows_control_enabled": False},
            "search": {"auto_open": False},
            "extensions": {"character_model": {"enabled": False}},
            "resource": {
                "profile": "balanced",
                "ollama": {"unload_on_exit": False, "warmup_on_start": True},
                "warmup": {"serialize_gpu": True, "order": "ollama_then_rvc"},
            },
        }

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur if cur is not None else default

    def get_rvc_config(self) -> Optional[dict]:
        return None

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        cur = self.data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value


def _forbidden_http(*_a: Any, **_k: Any) -> None:
    raise AssertionError("real HTTP forbidden")


def _strict_close_warmup(client: OllamaClient) -> None:
    if client.close_warmup(timeout=2.0) is not True:
        raise AssertionError("close_warmup must return strict True")


def _run_capture_errors(
    label: str, fn: Callable[[], None], errors: List[BaseException]
) -> threading.Thread:
    def _wrapper() -> None:
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_wrapper, name=label, daemon=True)
    thread.start()
    return thread


class OllamaWarmupLifecycleTests(unittest.TestCase):
    def test_never_started_snapshot_finished_false(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m")
        snap = client.warmup_snapshot()
        self.assertFalse(snap.finished)
        self.assertFalse(snap.succeeded)
        self.assertTrue(client.wait_for_warmup(timeout=0.1))

    def test_core_shutdown_does_not_complete_while_warmup_thread_alive(self) -> None:
        entered, release = threading.Event(), threading.Event()
        threads: List[threading.Thread] = []

        def warm(self, *, timeout: Optional[float] = None) -> bool:
            threads.append(threading.current_thread())
            entered.set()
            if not release.wait(_GATE_SEC):
                raise AssertionError("warm gate not released")
            return True

        with TemporaryDirectory(prefix="warm-leak-") as tmp:
            cfg = _Cfg(tmp, memory=False)
            parts = None
            app: Optional[MikuAssistant] = None
            with patch.object(OllamaClient, "warm_up", new=warm), patch(
                "requests.get", new=_forbidden_http
            ), patch("requests.post", new=_forbidden_http):
                try:
                    parts = assemble_default(
                        startup_mode="text",
                        config_loader=lambda *_a, **_k: cfg,
                        logging_setup=lambda *_a, **_k: None,
                        dir_preparer=lambda: None,
                        windows_controller_cls=lambda **_k: SimpleNamespace(),
                        chrome_searcher_cls=lambda **_k: SimpleNamespace(),
                        file_cleaner_from_config=lambda *_a, **_k: None,
                        start_cleaner=False,
                    )
                    self.assertTrue(entered.wait(1))
                    app = MikuAssistant.__new__(MikuAssistant)
                    app._bind_parts(parts)  # noqa: SLF001
                    app.shutdown()
                    self.assertTrue(threads[0].is_alive())
                    self.assertFalse(
                        app._shutdown_flow_done,  # noqa: SLF001
                        "must not claim shutdown complete with live warmup",
                    )
                finally:
                    release.set()
                    for thread in threads:
                        thread.join(2)
                        self.assertFalse(thread.is_alive())
                    if app is not None:
                        app.shutdown()
                        self.assertTrue(app._shutdown_flow_done)  # noqa: SLF001
                    if parts is not None:
                        owner = parts.llm_warmup_owner
                        self.assertIsNotNone(owner)
                        _strict_close_warmup(owner)
                        self.assertIs(
                            parts.model_memory_admission.close(timeout=2.0), True
                        )
                        tfm = parts.temp_file_manager.shutdown(timeout=2.0)
                        self.assertTrue(tfm.complete)

    def test_published_before_start_blocks_overlap_and_close(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        gate = threading.Event()
        released = threading.Event()
        real_start = OwnedThreadTask.start

        def slow_start(self: OwnedThreadTask) -> bool:
            gate.set()
            if not released.wait(_GATE_SEC):
                raise AssertionError("start gate not released")
            return real_start(self)

        with patch.object(OwnedThreadTask, "start", slow_start):
            try:
                holder = threading.Thread(
                    target=lambda: client.begin_fresh_warmup(timeout=1),
                    name="fresh",
                    daemon=True,
                )
                holder.start()
                self.assertTrue(gate.wait(1))
                self.assertFalse(client.start_warmup(timeout=1))
                self.assertFalse(client.close_warmup(timeout=0.05))
            finally:
                released.set()
                holder.join(2)
                self.assertFalse(holder.is_alive())
                _strict_close_warmup(client)

    def test_unknown_start_after_lost_ack_does_not_spawn_second_owner(self) -> None:
        entered, release = threading.Event(), threading.Event()
        made: List[threading.Thread] = []
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m")

        def warm(*, timeout: Optional[float] = None) -> bool:
            entered.set()
            if not release.wait(_GATE_SEC):
                raise AssertionError("release timeout")
            return True

        class LostAck:
            def __init__(self, *a: Any, **k: Any) -> None:
                self.thread = REAL_THREAD(*a, **k)
                made.append(self.thread)

            def start(self) -> None:
                self.thread.start()
                if not entered.wait(1):
                    raise AssertionError("warm owner not entered")
                raise RuntimeError("fixture start acknowledgement lost")

            def is_alive(self) -> bool:
                return self.thread.is_alive()

            def join(self, timeout: Optional[float] = None) -> None:
                self.thread.join(timeout)

            @property
            def ident(self) -> Optional[int]:
                return self.thread.ident

        with patch.object(client, "warm_up", new=warm), patch(
            "src.llm.ollama_client.threading.Thread", side_effect=LostAck
        ):
            try:
                with self.assertRaises(RuntimeError):
                    client.start_warmup(timeout=0.5)
                self.assertFalse(
                    client.start_warmup(timeout=0.5),
                    "unknown original start must retain its owner",
                )
                self.assertEqual(len(made), 1)
            finally:
                release.set()
                for thread in made:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                _strict_close_warmup(client)

    def test_factory_failure_before_os_start_allows_retry(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)

        def boom(*_a: Any, **_k: Any) -> threading.Thread:
            raise RuntimeError("thread factory unavailable")

        with patch("src.llm.ollama_client.threading.Thread", side_effect=boom):
            with self.assertRaisesRegex(RuntimeError, "thread factory unavailable"):
                client.start_warmup(timeout=1)
        snap = client.warmup_snapshot()
        self.assertFalse(snap.finished)

        def forbid_join(*_a: Any, **_k: Any) -> bool:
            raise AssertionError("wait must not join quiet factory-failed owner")

        with patch.object(OwnedThreadTask, "join", side_effect=forbid_join):
            self.assertTrue(client.wait_for_warmup(timeout=0.2))

        with patch.object(client, "warm_up", return_value=True):
            try:
                self.assertTrue(client.start_warmup(timeout=1))
                self.assertTrue(client.wait_for_warmup(timeout=2))
                self.assertTrue(client.warmup_snapshot().succeeded)
            finally:
                _strict_close_warmup(client)

    def test_cancelled_slow_warm_up_cannot_succeed(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        entered, release = threading.Event(), threading.Event()

        def warm(*, timeout: Optional[float] = None) -> bool:
            entered.set()
            if not release.wait(_GATE_SEC):
                raise AssertionError("warm gate not released")
            return True

        with patch.object(client, "warm_up", side_effect=warm):
            try:
                self.assertTrue(client.start_warmup(timeout=1))
                self.assertTrue(entered.wait(1))
                self.assertFalse(client.close_warmup(timeout=0.05))
                release.set()
                self.assertTrue(client.wait_for_warmup(timeout=2))
                self.assertFalse(client.warmup_snapshot().succeeded)
            finally:
                release.set()
                _strict_close_warmup(client)

    def test_close_warmup_blocks_warm_up_post(self) -> None:
        posts: List[Any] = []
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)

        def post(*_a: Any, **_k: Any) -> SimpleNamespace:
            posts.append(1)
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"done": True},
            )

        with patch.object(client, "_http_post", side_effect=post, create=True):
            self.assertTrue(client.close_warmup(timeout=0.01))
            self.assertFalse(client.warm_up(timeout=1))
            self.assertFalse(client.start_warmup(timeout=1))
            self.assertEqual(client.begin_fresh_warmup(timeout=1), 0)
        self.assertEqual(posts, [])

    def test_release_owned_model_after_close_still_available(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        client._model_touch_succeeded = True  # noqa: SLF001
        calls: List[Any] = []

        def post(*_a: Any, **_k: Any) -> SimpleNamespace:
            calls.append(_k.get("json", {}))
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"done": True},
            )

        with patch.object(client, "_http_post", side_effect=post, create=True):
            self.assertTrue(client.close_warmup(timeout=0.01))
            self.assertTrue(client.release_owned_model())
        self.assertEqual(calls[-1].get("keep_alive"), 0)

    def test_fresh_generation_not_polluted_by_stale_generation(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        results: Dict[int, bool] = {}
        lock = threading.Lock()
        gen1_release = threading.Event()

        def warm(*, timeout: Optional[float] = None) -> bool:
            with lock:
                snap = client.warmup_snapshot()
                gen = snap.generation
            if gen == 1:
                if not gen1_release.wait(_GATE_SEC):
                    raise AssertionError("gen1 gate not released")
                results[1] = True
                return True
            results[gen] = True
            return True

        with patch.object(client, "warm_up", side_effect=warm):
            try:
                g1 = client.begin_fresh_warmup(timeout=1)
                self.assertEqual(g1, 1)
                g2 = client.begin_fresh_warmup(timeout=1)
                self.assertEqual(g2, 1)
                gen1_release.set()
                self.assertTrue(client.wait_for_warmup(timeout=2))
                g3 = client.begin_fresh_warmup(timeout=1)
                self.assertEqual(g3, 2)
                self.assertTrue(client.wait_for_warmup(timeout=2))
                self.assertTrue(results.get(1))
                self.assertTrue(results.get(2))
                self.assertNotIn(3, results)
            finally:
                gen1_release.set()
                _strict_close_warmup(client)

    def test_assembly_registers_llm_warmup_owner_before_memory_wrap(self) -> None:
        with TemporaryDirectory(prefix="warm-owner-") as tmp:
            cfg = _Cfg(tmp, memory=True)
            with patch(
                "src.memory.runtime.MemoryRuntime.create_from_settings"
            ) as create, patch("requests.get", new=_forbidden_http), patch(
                "requests.post", new=_forbidden_http
            ), patch.object(OllamaClient, "warm_up", return_value=True):
                runtime = MagicMock()
                runtime.wrap_llm.side_effect = lambda llm: SimpleNamespace(_inner=llm)
                runtime.start.return_value = None
                runtime.close.return_value = SimpleNamespace(
                    worker_alive=False,
                    timed_out=False,
                    drained=True,
                )
                create.return_value = runtime
                parts = None
                try:
                    parts = assemble_default(
                        startup_mode="text",
                        config_loader=lambda *_a, **_k: cfg,
                        logging_setup=lambda *_a, **_k: None,
                        dir_preparer=lambda: None,
                        windows_controller_cls=lambda **_k: SimpleNamespace(),
                        chrome_searcher_cls=lambda **_k: SimpleNamespace(),
                        file_cleaner_from_config=lambda *_a, **_k: None,
                        start_cleaner=False,
                    )
                    self.assertIsInstance(parts.llm_warmup_owner, OllamaClient)
                    self.assertIs(
                        parts.llm_warmup_owner, runtime.wrap_llm.call_args[0][0]
                    )
                finally:
                    if parts is not None:
                        _strict_close_warmup(parts.llm_warmup_owner)
                        self.assertIs(
                            parts.model_memory_admission.close(timeout=2.0), True
                        )
                        self.assertTrue(
                            parts.temp_file_manager.shutdown(timeout=2.0).complete
                        )

    def test_from_dependencies_does_not_close_injected_llm(self) -> None:
        class InjectedLLM:
            def __init__(self) -> None:
                self.close_warmup_calls = 0

            def close_warmup(self, timeout: float = 2.0) -> bool:
                self.close_warmup_calls += 1
                return True

        llm = InjectedLLM()
        with TemporaryDirectory(prefix="warm-di-") as tmp:
            app = MikuAssistant.from_dependencies(
                config=_Cfg(tmp, memory=False), llm_client=llm
            )
            app.shutdown()
        self.assertEqual(llm.close_warmup_calls, 0)

    def test_fatal_in_warmup_worker_preserves_original_exception(self) -> None:
        cases: List[BaseException] = [
            KeyboardInterrupt("fixture kbi"),
            SystemExit("fixture exit"),
            GeneratorExit(),
            BaseException("fixture base"),
        ]
        for fatal in cases:
            with self.subTest(type=type(fatal).__name__):
                client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)

                def warm(*, timeout: Optional[float] = None) -> bool:
                    raise fatal

                with patch.object(client, "warm_up", side_effect=warm):
                    try:
                        self.assertTrue(client.start_warmup(timeout=1))
                        self.assertTrue(client.wait_for_warmup(timeout=2))
                        owner = client._warmup_owner  # noqa: SLF001
                        self.assertIsNotNone(owner)
                        self.assertIs(owner.error, fatal)
                        self.assertFalse(client.warmup_snapshot().succeeded)
                    finally:
                        _strict_close_warmup(client)

    def test_close_join_timeout_retains_owner_for_retry(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        entered, release = threading.Event(), threading.Event()

        def warm(*, timeout: Optional[float] = None) -> bool:
            entered.set()
            if not release.wait(_GATE_SEC):
                raise AssertionError("warm gate not released")
            return True

        with patch.object(client, "warm_up", side_effect=warm):
            try:
                self.assertTrue(client.start_warmup(timeout=1))
                self.assertTrue(entered.wait(1))
                first = client._warmup_owner  # noqa: SLF001
                self.assertFalse(client.close_warmup(timeout=0.01))
                self.assertIs(client._warmup_owner, first)  # noqa: SLF001
            finally:
                release.set()
                self.assertTrue(client.wait_for_warmup(timeout=2))
                _strict_close_warmup(client)

    def test_concurrent_close_and_start_single_owner(self) -> None:
        client = OllamaClient(base_url="http://127.0.0.1:9", model="m", timeout=1)
        owners: List[OwnedThreadTask] = []
        errors: List[BaseException] = []
        real_prepare = client._prepare_new_warmup_generation_locked  # noqa: SLF001

        def track_prepare(
            effective_timeout: float,
        ) -> tuple[OwnedThreadTask, int]:
            owner, gen = real_prepare(effective_timeout)
            owners.append(owner)
            return owner, gen

        with patch.object(client, "warm_up", return_value=True), patch.object(
            client, "_prepare_new_warmup_generation_locked", side_effect=track_prepare
        ):
            barrier = threading.Barrier(2)

            def closer() -> None:
                barrier.wait(1)
                client.close_warmup(timeout=2.0)

            def starter() -> None:
                barrier.wait(1)
                client.start_warmup(timeout=1)

            try:
                t_close = _run_capture_errors("close", closer, errors)
                t_start = _run_capture_errors("start", starter, errors)
                t_close.join(3)
                t_start.join(3)
                self.assertFalse(errors, errors)
                self.assertFalse(t_close.is_alive())
                self.assertFalse(t_start.is_alive())
                self.assertLessEqual(len(owners), 1)
            finally:
                _strict_close_warmup(client)


if __name__ == "__main__":
    unittest.main()
