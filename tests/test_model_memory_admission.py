"""Local model memory admission (fake host/metadata/HTTP only)."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.core.host_memory import HostMemorySnapshot
from src.core.model_memory_admission import (
    HINT_INSUFFICIENT,
    LocalModelMemoryAdmission,
    ModelMemoryAdmissionError,
    REASON_METADATA_FAILED,
    REASON_RESOURCE_BUSY,
    normalize_model_name,
    observable_host_memory_snapshot,
    payload_exempt_from_model_admission,
    _cold_required_commit_mb,
    _cold_required_physical_mb,
)
from src.core.network_endpoint import authorize_model_endpoint
from src.core.safety import Operation, SafetyPolicy
from src.llm.generate_result import GenerateStatus
from src.llm.ollama_client import OllamaClient
from src.memory.ollama_extractor import RequestsOllamaChatTransport
from src.memory.errors import MemoryExtractionTransportError

from tests.support.host_memory_fixtures import SUFFICIENT_HOST_MEMORY

_REMOTE_POLICY = SafetyPolicy(
    {"permissions": {Operation.REMOTE_MODEL_ENDPOINT.value: True}}
)

_CONTROLLED_WORKER_REASONS = frozenset(
    {
        "closing",
        "model_metadata_failed",
        "model_metadata_timeout",
        "model_memory_insufficient",
        "host_memory_unobservable",
        "resource_busy",
        "model_unknown",
    }
)


def _wait_release_checked(release: threading.Event, timeout: float = 3.0) -> None:
    if timeout > 3.0:
        raise ValueError("release wait timeout must be <= 3 seconds")
    if not release.wait(timeout):
        raise AssertionError(f"release.wait({timeout}) returned False")


def _assert_worker_joined_clean(
    thread: threading.Thread,
    *,
    allow_controlled: bool = False,
) -> None:
    thread.join(timeout=5.0)
    if thread.is_alive():
        raise AssertionError("worker thread still alive after join")
    errors: list[BaseException] = getattr(thread, "_admission_errors", [])
    if not errors:
        return
    if len(errors) != 1:
        raise AssertionError(f"expected at most one worker error, got {errors!r}")
    exc = errors[0]
    if not isinstance(exc, Exception):
        raise exc
    if allow_controlled and isinstance(exc, ModelMemoryAdmissionError):
        if exc.reason in _CONTROLLED_WORKER_REASONS:
            return
    raise AssertionError(f"unexpected worker exception: {exc!r}") from exc


def _loopback_guard(**kwargs) -> LocalModelMemoryAdmission:
    sealed = authorize_model_endpoint(None, "http://127.0.0.1:11434")
    return LocalModelMemoryAdmission(sealed, **kwargs)


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, body: bytes = b"{}") -> None:
        self.status_code = status_code
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size: int = 4096):
        offset = 0
        body = self._body
        while offset < len(body):
            yield body[offset : offset + chunk_size]
            offset += chunk_size

    @property
    def content(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


def _start_require(
    guard: LocalModelMemoryAdmission,
    *,
    call_timeout: float = 5.0,
    sealed_endpoint=None,
) -> threading.Thread:
    box: list[BaseException] = []

    def _run() -> None:
        try:
            guard.require_model_post(
                "m:latest",
                call_timeout=call_timeout,
                sealed_endpoint=sealed_endpoint,
            )
        except BaseException as exc:
            box.append(exc)

    thread = threading.Thread(target=_run, daemon=False)
    thread.start()
    thread._admission_errors = box  # type: ignore[attr-defined]
    return thread


class ModelMemoryAdmissionUnitTests(unittest.TestCase):
    def test_normalize_model_adds_latest_tag(self) -> None:
        self.assertEqual(normalize_model_name("Miku"), "Miku:latest")
        self.assertEqual(normalize_model_name("a/B"), "a/B:latest")
        self.assertEqual(normalize_model_name("x:7b"), "x:7b")

    def test_unload_payload_exempt(self) -> None:
        self.assertTrue(
            payload_exempt_from_model_admission(
                {"model": "m", "messages": [], "keep_alive": 0}
            )
        )
        self.assertFalse(
            payload_exempt_from_model_admission(
                {"model": "m", "messages": [], "keep_alive": False}
            )
        )

    def test_cold_requirement_uses_ceil_and_floor(self) -> None:
        one_gib = 1024 * 1024 * 1024
        self.assertEqual(_cold_required_physical_mb(one_gib), 1536 + 2048)
        self.assertEqual(_cold_required_commit_mb(one_gib), 2048 + 2048)

    def test_observable_snapshot_rejects_non_int_and_nan(self) -> None:
        ok = HostMemorySnapshot(8192, 8192)
        self.assertIs(observable_host_memory_snapshot(ok), ok)
        self.assertIsNone(
            observable_host_memory_snapshot(
                HostMemorySnapshot(
                    physical_available_mb=float("inf"),  # type: ignore[arg-type]
                    commit_available_mb=8192,
                )
            )
        )
        self.assertIsNone(
            observable_host_memory_snapshot(
                HostMemorySnapshot(
                    physical_available_mb=True,  # type: ignore[arg-type]
                    commit_available_mb=8192,
                )
            )
        )

    def test_remote_endpoint_skips_admission(self) -> None:
        sealed = authorize_model_endpoint(
            _REMOTE_POLICY, "https://example.com", context="test_remote"
        )
        guard = LocalModelMemoryAdmission(sealed, safety_policy=_REMOTE_POLICY)
        guard.require_model_post("m:latest", call_timeout=5.0)
        self.assertIs(guard.close(timeout=2.0), True)
        with self.assertRaises(ModelMemoryAdmissionError) as ctx:
            guard.require_model_post("m:latest", call_timeout=5.0)
        self.assertEqual(ctx.exception.reason, "closing")

    def test_ps_missing_or_null_does_not_fallback_to_tags(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        seen: list[str] = []
        tags = {
            "models": [{"name": "m:latest", "size": 2 * 1024 * 1024 * 1024}]
        }

        def fake_get(url: str, **kwargs):
            seen.append(url)
            if url.endswith("/api/ps"):
                return _FakeResponse(body=json.dumps({"models": None}).encode())
            return _FakeResponse(body=json.dumps(tags).encode())

        guard = _loopback_guard(http_get=fake_get)
        snap = HostMemorySnapshot(8192, 8192)
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=snap,
        ):
            try:
                with self.assertRaises(ModelMemoryAdmissionError) as ctx:
                    guard.require_model_post("m:latest", call_timeout=0.5)
                self.assertEqual(ctx.exception.reason, REASON_METADATA_FAILED)
                self.assertFalse(any("/api/tags" in u for u in seen))
            finally:
                self.assertIs(guard.close(timeout=5.0), True)

    def test_memory_override_metadata_uses_passed_endpoint(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        main = authorize_model_endpoint(None, "http://127.0.0.1:11434")
        override = authorize_model_endpoint(None, "http://127.0.0.1:11555")
        seen: list[str] = []

        def fake_get(url: str, **kwargs):
            seen.append(url)
            if url.endswith("/api/ps"):
                return _FakeResponse(
                    body=json.dumps(
                        {"models": [{"model": "m:latest", "size": 1}]}
                    ).encode()
                )
            return _FakeResponse(body=json.dumps({"models": []}).encode())

        guard = LocalModelMemoryAdmission(main, http_get=fake_get)
        snap = HostMemorySnapshot(8192, 8192)
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=snap,
        ):
            try:
                guard.require_model_post(
                    "m:latest",
                    call_timeout=5.0,
                    sealed_endpoint=override,
                )
                self.assertTrue(all("11555" in u for u in seen))
                self.assertFalse(any("11434" in u for u in seen))
            finally:
                self.assertIs(guard.close(timeout=5.0), True)

    def test_low_host_fast_reject_without_http(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        guard = _loopback_guard()
        low = HostMemorySnapshot(
            physical_available_mb=100,
            commit_available_mb=SUFFICIENT_HOST_MEMORY.commit_available_mb,
        )
        posts: list[str] = []

        def fake_get(url: str, **kwargs):
            posts.append(url)
            return _FakeResponse(body=b"{}")

        guard._http_get = fake_get  # type: ignore[method-assign]
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=low,
        ):
            try:
                with self.assertRaises(ModelMemoryAdmissionError) as ctx:
                    guard.require_model_post("m:latest", call_timeout=0.5)
                self.assertEqual(ctx.exception.reason, "model_memory_insufficient")
                self.assertEqual(posts, [])
            finally:
                self.assertIs(guard.close(timeout=5.0), True)

    def test_warm_resident_allows_when_margins_ok(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")

        def fake_get(url: str, **kwargs):
            if url.endswith("/api/ps"):
                return _FakeResponse(
                    body=json.dumps(
                        {"models": [{"model": "m:latest", "size": 8_000_000_000}]}
                    ).encode()
                )
            return _FakeResponse(body=json.dumps({"models": []}).encode())

        guard = _loopback_guard(http_get=fake_get)
        snap = HostMemorySnapshot(
            physical_available_mb=SUFFICIENT_HOST_MEMORY.physical_available_mb,
            commit_available_mb=SUFFICIENT_HOST_MEMORY.commit_available_mb,
        )
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=snap,
        ):
            try:
                guard.require_model_post("m:latest", call_timeout=0.5)
            finally:
                self.assertIs(guard.close(timeout=5.0), True)

    def test_cold_tags_only_when_ps_models_empty_list(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        ps = {"models": []}
        tags = {
            "models": [{"name": "m:latest", "size": 2 * 1024 * 1024 * 1024}]
        }

        def fake_get(url: str, **kwargs):
            if url.endswith("/api/ps"):
                return _FakeResponse(body=json.dumps(ps).encode())
            return _FakeResponse(body=json.dumps(tags).encode())

        guard = _loopback_guard(http_get=fake_get)
        req_phys = _cold_required_physical_mb(2 * 1024 * 1024 * 1024)
        snap = HostMemorySnapshot(
            physical_available_mb=req_phys - 1,
            commit_available_mb=SUFFICIENT_HOST_MEMORY.commit_available_mb,
        )
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=snap,
        ):
            try:
                with self.assertRaises(ModelMemoryAdmissionError) as ctx:
                    guard.require_model_post("m:latest", call_timeout=0.5)
                self.assertEqual(ctx.exception.user_message, HINT_INSUFFICIENT)
            finally:
                self.assertIs(guard.close(timeout=5.0), True)

    def test_metadata_timeout_keeps_owner_busy(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        release = threading.Event()
        entered = threading.Event()

        def fake_get(url: str, **kwargs):
            entered.set()
            _wait_release_checked(release, 3.0)
            return _FakeResponse(body=json.dumps({"models": []}).encode())

        guard = _loopback_guard(http_get=fake_get)
        ok = HostMemorySnapshot(8192, 8192)
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=ok,
        ):
            try:
                with self.assertRaises(ModelMemoryAdmissionError) as ctx:
                    guard.require_model_post("m:latest", call_timeout=0.05)
                self.assertEqual(ctx.exception.reason, "model_metadata_timeout")
                self.assertTrue(entered.wait(1.0))
                with self.assertRaises(ModelMemoryAdmissionError) as busy:
                    guard.require_model_post("m:latest", call_timeout=0.05)
                self.assertEqual(busy.exception.reason, REASON_RESOURCE_BUSY)
            finally:
                release.set()
                self.assertIs(guard.close(timeout=5.0), True)

    def test_ps_size_vram_does_not_grant_resident(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        ps = {"models": [{"model": "m:latest", "size_vram": 9_000_000_000}]}

        def fake_get(url: str, **kwargs):
            return _FakeResponse(body=json.dumps(ps).encode())

        guard = _loopback_guard(http_get=fake_get)
        ok = HostMemorySnapshot(8192, 8192)
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=ok,
        ):
            try:
                with self.assertRaises(ModelMemoryAdmissionError):
                    guard.require_model_post("m:latest", call_timeout=0.5)
            finally:
                self.assertIs(guard.close(timeout=5.0), True)

    def test_ps_model_name_conflict_rejects(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        ps = {
            "models": [
                {"model": "m:latest", "name": "other:latest", "size": 100}
            ]
        }

        def fake_get(url: str, **kwargs):
            return _FakeResponse(body=json.dumps(ps).encode())

        guard = _loopback_guard(http_get=fake_get)
        ok = HostMemorySnapshot(8192, 8192)
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=ok,
        ):
            try:
                with self.assertRaises(ModelMemoryAdmissionError):
                    guard.require_model_post("m:latest", call_timeout=0.5)
            finally:
                self.assertIs(guard.close(timeout=5.0), True)

    def test_body_over_64k_rejected(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        huge = b"x" * (65537)

        def fake_get(url: str, **kwargs):
            return _FakeResponse(body=huge)

        guard = _loopback_guard(http_get=fake_get)
        ok = HostMemorySnapshot(8192, 8192)
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=ok,
        ):
            try:
                with self.assertRaises(ModelMemoryAdmissionError):
                    guard.require_model_post("m:latest", call_timeout=0.5)
            finally:
                self.assertIs(guard.close(timeout=5.0), True)

    def test_require_single_flight_blocks_overlap(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        release = threading.Event()
        entered = threading.Event()

        def blocking_get(url: str, **kwargs):
            entered.set()
            _wait_release_checked(release, 3.0)
            return _FakeResponse(
                body=json.dumps(
                    {"models": [{"model": "m:latest", "size": 1}]}
                ).encode()
            )

        guard = _loopback_guard(http_get=blocking_get)
        ok = HostMemorySnapshot(8192, 8192)
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=ok,
        ):
            worker = _start_require(guard, call_timeout=5.0)
            try:
                self.assertTrue(entered.wait(1.0))
                with self.assertRaises(ModelMemoryAdmissionError) as ctx:
                    guard.require_model_post("m:latest", call_timeout=0.5)
                self.assertEqual(ctx.exception.reason, REASON_RESOURCE_BUSY)
            finally:
                release.set()
                _assert_worker_joined_clean(worker, allow_controlled=False)
                self.assertIs(guard.close(timeout=5.0), True)

    def test_close_requires_actual_quiet(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        release = threading.Event()
        entered = threading.Event()

        def hang_get(url: str, **kwargs):
            entered.set()
            _wait_release_checked(release, 3.0)
            return _FakeResponse(body=b"{}")

        guard = _loopback_guard(http_get=hang_get)
        ok = HostMemorySnapshot(8192, 8192)
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=ok,
        ):
            worker = _start_require(guard, call_timeout=5.0)
            try:
                self.assertTrue(entered.wait(1.0))
                self.assertIs(guard.close(timeout=0.05), False)
            finally:
                release.set()
                _assert_worker_joined_clean(worker, allow_controlled=True)
                self.assertIs(guard.close(timeout=5.0), True)

    def test_illegal_pre_snapshot_zero_get(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only admission path")
        seen: list[str] = []

        def fake_get(url: str, **kwargs):
            seen.append(url)
            return _FakeResponse(body=b"{}")

        guard = _loopback_guard(http_get=fake_get)
        bad = HostMemorySnapshot(
            physical_available_mb=float("nan"),  # type: ignore[arg-type]
            commit_available_mb=8192,
        )
        with patch(
            "src.core.model_memory_admission.read_host_memory_snapshot",
            return_value=bad,
        ):
            try:
                with self.assertRaises(ModelMemoryAdmissionError) as ctx:
                    guard.require_model_post("m:latest", call_timeout=0.5)
                self.assertEqual(ctx.exception.reason, "host_memory_unobservable")
                self.assertEqual(seen, [])
            finally:
                self.assertIs(guard.close(timeout=5.0), True)


class OllamaClientAdmissionWiringTests(unittest.TestCase):
    def test_resource_error_not_connection_error(self) -> None:
        sealed = authorize_model_endpoint(None, "http://127.0.0.1:11434")

        class DenyGuard(LocalModelMemoryAdmission):
            def require_model_post(  # type: ignore[no-untyped-def]
                self, model, *, call_timeout, sealed_endpoint=None
            ):
                raise ModelMemoryAdmissionError(
                    "model_memory_insufficient", HINT_INSUFFICIENT
                )

        client = OllamaClient(
            sealed_endpoint=sealed,
            model_memory_guard=DenyGuard(sealed),
        )
        client._http_post = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("must not POST")
        )
        result = client.generate_result("hi")
        self.assertEqual(result.status, GenerateStatus.RESOURCE_ERROR)

    def test_request_post_is_single_admission_gate(self) -> None:
        sealed = authorize_model_endpoint(None, "http://127.0.0.1:11434")
        calls: list[int] = []

        class CountGuard(LocalModelMemoryAdmission):
            def require_model_post(  # type: ignore[no-untyped-def]
                self, model, *, call_timeout, sealed_endpoint=None
            ):
                calls.append(1)
                raise ModelMemoryAdmissionError(
                    "model_memory_insufficient", HINT_INSUFFICIENT
                )

        client = OllamaClient(
            sealed_endpoint=sealed,
            model_memory_guard=CountGuard(sealed),
        )
        client._http_post = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("must not POST")
        )
        client.generate_result("hi")
        self.assertEqual(calls, [1])

    def test_unload_bypasses_guard(self) -> None:
        sealed = authorize_model_endpoint(None, "http://127.0.0.1:11434")

        class DenyGuard(LocalModelMemoryAdmission):
            def require_model_post(  # type: ignore[no-untyped-def]
                self, model, *, call_timeout, sealed_endpoint=None
            ):
                raise ModelMemoryAdmissionError("x", HINT_INSUFFICIENT)

        client = OllamaClient(
            sealed_endpoint=sealed,
            model_memory_guard=DenyGuard(sealed),
        )
        client._model_touch_succeeded = True
        called = {"n": 0}

        def fake_post(url, **kwargs):
            called["n"] += 1
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"done": True},
                status_code=200,
            )

        client._http_post = fake_post  # type: ignore[method-assign]
        self.assertTrue(client.release_owned_model())
        self.assertEqual(called["n"], 1)


class MemoryTransportAdmissionTests(unittest.TestCase):
    def test_post_chat_maps_resource(self) -> None:
        sealed = authorize_model_endpoint(None, "http://127.0.0.1:11434")

        class DenyGuard(LocalModelMemoryAdmission):
            def require_model_post(  # type: ignore[no-untyped-def]
                self, model, *, call_timeout, sealed_endpoint=None
            ):
                raise ModelMemoryAdmissionError(
                    "model_memory_insufficient", HINT_INSUFFICIENT
                )

        transport = RequestsOllamaChatTransport(
            sealed.base_url,
            sealed_endpoint=sealed,
            model_memory_guard=DenyGuard(sealed),
        )
        transport._session = SimpleNamespace(  # type: ignore[method-assign]
            post=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not POST")
            )
        )
        with self.assertRaises(MemoryExtractionTransportError) as ctx:
            transport.post_chat(
                {
                    "model": "m:latest",
                    "messages": [{"role": "user", "content": "x"}],
                }
            )
        self.assertEqual(ctx.exception.error_kind, "resource")


if __name__ == "__main__":
    unittest.main()
