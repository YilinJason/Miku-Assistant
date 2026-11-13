"""CosyVoice retained 被动对账：纯 fake ownership manager/client，无真实文件或 TFM。"""
from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from typing import Callable, Optional

from src.core.safety import SafetyPolicy
from src.voice.cosyvoice.client import CosyVoiceShutdownStatus
from src.voice.tts.cosyvoice_backend import CosyVoiceTtsBackend
from src.voice.tts.engine import TTSEngine
from tests.test_cosyvoice_production_slice_c1 import _settings


class _FakeOwnershipManager:
    """有限 owned/pending 状态；被动对账路径禁止 create/remove。"""

    def __init__(self, *, pending: int = 0) -> None:
        self._owned: set[str] = set()
        self._pending = int(pending)
        self.remove_calls = 0
        self.create_calls = 0
        self._is_owned_error: BaseException | None = None
        self._is_owned_hook: Optional[Callable[[str], bool]] = None

    def register_owned(self, path: str) -> None:
        self._owned.add(path)

    def drop_owned(self, path: str) -> None:
        self._owned.discard(path)

    def is_owned(self, path: str) -> bool:
        if self._is_owned_error is not None:
            raise self._is_owned_error
        if self._is_owned_hook is not None:
            return bool(self._is_owned_hook(path))
        return path in self._owned

    def remove(self, *args, **kwargs) -> None:
        self.remove_calls += 1
        raise AssertionError("passive reconcile must not call TempFileManager.remove")

    def create(self, *args, **kwargs) -> str:
        self.create_calls += 1
        raise AssertionError("passive reconcile must not call TempFileManager.create")

    def pending_cleanup_count(self) -> int:
        return self._pending


class _FakePassiveCosyClient:
    def __init__(self, *, stop_failed: bool = False) -> None:
        self._stop_failed = stop_failed
        self.retire_calls = 0
        self.prepare_calls = 0

    @property
    def stop_failed(self) -> bool:
        return self._stop_failed

    def prepare(self, **kwargs) -> bool:
        self.prepare_calls += 1
        raise AssertionError("passive reconcile must not call client.prepare")

    def retire_worker(self, timeout: float = 2.0) -> CosyVoiceShutdownStatus:
        self.retire_calls += 1
        return CosyVoiceShutdownStatus(
            complete=True,
            worker_alive=False,
            reader_alive=False,
            writer_alive=False,
        )


def _make_backend(
    *,
    client: _FakePassiveCosyClient | None = None,
    mgr: _FakeOwnershipManager | None = None,
) -> tuple[CosyVoiceTtsBackend, _FakeOwnershipManager, _FakePassiveCosyClient]:
    policy = SafetyPolicy(
        {"permissions": {"local_tts_worker": True, "file_delete": True}}
    )
    mgr = mgr or _FakeOwnershipManager()
    client = client or _FakePassiveCosyClient()
    backend = CosyVoiceTtsBackend(
        _settings(),
        policy,
        mgr,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
    )
    return backend, mgr, client


def _minimal_admission_engine(
    mgr: _FakeOwnershipManager,
    backend: CosyVoiceTtsBackend | SimpleNamespace,
) -> TTSEngine:
    engine = TTSEngine.__new__(TTSEngine)
    engine._temp_files = mgr  # type: ignore[assignment]
    engine._cosy_backend = backend  # type: ignore[assignment]
    engine._player = SimpleNamespace(play=lambda _p: None)
    engine._turn_stream_session = None
    engine._turn_stream_session_lock = threading.Lock()
    engine._interrupt_epoch = 0
    engine._interrupt_epoch_lock = threading.Lock()
    engine._interrupt_event = threading.Event()
    engine._shutdown_event = threading.Event()
    return engine


class ReconcileReleasedOutputsTests(unittest.TestCase):
    def test_drops_stale_without_remove_or_retire(self) -> None:
        backend, mgr, client = _make_backend()
        stale = "/mem/stale-a.wav"
        mgr.register_owned(stale)
        backend._mark_failed_output(stale)
        mgr.drop_owned(stale)
        self.assertEqual(backend.retained_output_count(), 1)
        backend.reconcile_released_outputs()
        self.assertEqual(backend.retained_output_count(), 0)
        self.assertEqual(mgr.remove_calls, 0)
        self.assertEqual(mgr.create_calls, 0)
        self.assertEqual(client.retire_calls, 0)
        self.assertEqual(client.prepare_calls, 0)

    def test_still_owned_unchanged(self) -> None:
        backend, mgr, _client = _make_backend()
        path = "/mem/owned-a.wav"
        mgr.register_owned(path)
        backend._mark_failed_output(path)
        before = backend.retained_output_count()
        backend.reconcile_released_outputs()
        self.assertEqual(backend.retained_output_count(), before)

    def test_is_owned_exception_propagates_same_object(self) -> None:
        backend, mgr, _client = _make_backend()
        stale = "/mem/stale-exc.wav"
        mgr.register_owned(stale)
        backend._mark_failed_output(stale)
        mgr.drop_owned(stale)
        err = RuntimeError("is_owned probe failed")
        mgr._is_owned_error = err
        with self.assertRaises(RuntimeError) as ctx:
            backend.reconcile_released_outputs()
        self.assertIs(ctx.exception, err)
        self.assertEqual(backend.retained_output_count(), 1)

    def test_is_owned_value_error_propagates_same_object(self) -> None:
        backend, mgr, _client = _make_backend()
        path = "/mem/stale-val.wav"
        backend._mark_failed_output(path)
        err = ValueError("owned probe value")
        mgr._is_owned_error = err
        with self.assertRaises(ValueError) as ctx:
            backend.reconcile_released_outputs()
        self.assertIs(ctx.exception, err)

    def test_is_owned_keyboard_interrupt_propagates(self) -> None:
        backend, mgr, _client = _make_backend()
        path = "/mem/stale-ki.wav"
        backend._mark_failed_output(path)
        err = KeyboardInterrupt()
        mgr._is_owned_error = err
        with self.assertRaises(KeyboardInterrupt) as ctx:
            backend.reconcile_released_outputs()
        self.assertIs(ctx.exception, err)

    def test_pending_worker_retire_not_cleared(self) -> None:
        backend, mgr, _client = _make_backend()
        stale = "/mem/stale-retire-flag.wav"
        mgr.register_owned(stale)
        backend._mark_failed_output(stale)
        mgr.drop_owned(stale)
        with backend._lifecycle_lock:
            self.assertTrue(backend._pending_worker_retire)
        backend.reconcile_released_outputs()
        with backend._lifecycle_lock:
            self.assertTrue(backend._pending_worker_retire)

    def test_stop_failed_not_cleared(self) -> None:
        client = _FakePassiveCosyClient(stop_failed=True)
        backend, mgr, client = _make_backend(client=client)
        stale = "/mem/stale-stop.wav"
        mgr.register_owned(stale)
        backend._mark_failed_output(stale)
        mgr.drop_owned(stale)
        backend.reconcile_released_outputs()
        self.assertTrue(client.stop_failed)

    def test_revision_bump_via_mark_failed_aborts_stale_prune(self) -> None:
        backend, mgr, _client = _make_backend()
        stale = "/mem/stale-rev.wav"
        extra = "/mem/extra-rev.wav"
        mgr.register_owned(stale)
        backend._mark_failed_output(stale)
        mgr.drop_owned(stale)

        def _hook(path: str) -> bool:
            if path == stale:
                backend._mark_failed_output(extra)
            return path in mgr._owned

        mgr._is_owned_hook = _hook
        backend.reconcile_released_outputs()
        self.assertEqual(backend.retained_output_count(), 2)

    def test_samepath_remark_aborts_stale_prune(self) -> None:
        backend, mgr, _client = _make_backend()
        stale = "/mem/same-path.wav"
        mgr.register_owned(stale)
        backend._mark_failed_output(stale)
        mgr.drop_owned(stale)

        def _hook(path: str) -> bool:
            if path == stale:
                backend._mark_failed_output(stale)
            return False

        mgr._is_owned_hook = _hook
        backend.reconcile_released_outputs()
        self.assertEqual(backend.retained_output_count(), 1)


class EngineAdmissionReconcileTests(unittest.TestCase):
    def test_stale_retained_admission_recovers_via_reconcile(self) -> None:
        backend, mgr, _client = _make_backend()
        stale = "/mem/admission-stale.wav"
        mgr.register_owned(stale)
        backend._mark_failed_output(stale)
        mgr.drop_owned(stale)
        engine = _minimal_admission_engine(mgr, backend)
        engine._assert_turn_stream_admission_resources()

    def test_prepare_entry_recovers_after_stale_retained(self) -> None:
        backend, mgr, _client = _make_backend()
        stale = "/mem/prepare-stale.wav"
        mgr.register_owned(stale)
        backend._mark_failed_output(stale)
        mgr.drop_owned(stale)
        engine = _minimal_admission_engine(mgr, backend)
        self.assertTrue(
            engine._prepare_turn_stream_entry(0, raise_on_busy=True),
        )

    def test_still_owned_retained_blocks_admission(self) -> None:
        backend, mgr, _client = _make_backend()
        path = "/mem/admission-owned.wav"
        mgr.register_owned(path)
        backend._mark_failed_output(path)
        engine = _minimal_admission_engine(mgr, backend)
        with self.assertRaises(RuntimeError) as ctx:
            engine._assert_turn_stream_admission_resources()
        self.assertIn("retained", str(ctx.exception).lower())

    def test_tfm_pending_still_blocks_before_reconcile(self) -> None:
        mgr = _FakeOwnershipManager(pending=1)
        backend, _, _client = _make_backend(mgr=mgr)
        engine = _minimal_admission_engine(mgr, backend)
        with self.assertRaises(RuntimeError) as ctx:
            engine._assert_turn_stream_admission_resources()
        self.assertIn("pending", str(ctx.exception).lower())

    def test_backend_without_reconcile_method_unchanged(self) -> None:
        mgr = _FakeOwnershipManager()
        stale_backend = SimpleNamespace(
            retained_output_count=lambda: 1,
        )
        engine = _minimal_admission_engine(mgr, stale_backend)
        with self.assertRaises(RuntimeError) as ctx:
            engine._assert_turn_stream_admission_resources()
        self.assertIn("retained", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
