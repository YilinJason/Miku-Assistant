# -*- coding: utf-8 -*-
"""流式 Silero/NS core（fake ORT/APM/Mic）。"""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Callable, List
from unittest.mock import patch

import numpy as np

from src.voice.noise_suppression import (
    WebRtcNoiseStream,
    suppress_utterance_pcm,
)
import src.voice.noise_suppression as ns_module
from src.voice.silero_vad import (
    EXPECTED_ASSET_BYTES,
    FRAME_SAMPLES,
    SileroOnnxVAD,
)
from src.voice.silero_vad_stream import STREAM_FRAME_BYTES, SileroVadStreamContext

_SAVED_ORT = None
_SAVED_APM = None
_SAVED_MIC_INIT = None
_SAVED_MIC_ENTER = None
_DUMMY = b"\xab" * EXPECTED_ASSET_BYTES


class FakeOrtSession:
    instances: list["FakeOrtSession"] = []
    run_enter: threading.Event | None = None
    run_release: threading.Event | None = None
    run_calls: int = 0
    invalid_on_run: bool = False

    def __init__(self, model_bytes: bytes) -> None:
        self.model_bytes = model_bytes
        FakeOrtSession.instances.append(self)

    def get_inputs(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(name="input", type="tensor(float)", shape=[1, 576]),
            SimpleNamespace(name="state", type="tensor(float)", shape=[2, 1, 128]),
            SimpleNamespace(name="sr", type="tensor(int64)", shape=[]),
        ]

    def get_outputs(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(name="output", type="tensor(float)", shape=[1, 1]),
            SimpleNamespace(name="stateN", type="tensor(float)", shape=[2, 1, 128]),
        ]

    def get_providers(self) -> list[SimpleNamespace]:
        return ["CPUExecutionProvider"]

    def run(self, _names, _inputs) -> list[np.ndarray]:
        if FakeOrtSession.run_enter is not None:
            FakeOrtSession.run_enter.set()
        if FakeOrtSession.run_release is not None:
            if not FakeOrtSession.run_release.wait(timeout=2.0):
                raise TimeoutError("run blocked")
        FakeOrtSession.run_calls += 1
        if FakeOrtSession.invalid_on_run:
            raise RuntimeError("native boom")
        return [
            np.array([[0.55]], dtype=np.float32),
            np.zeros((2, 1, 128), dtype=np.float32),
        ]


class StatefulFakeProcessor:
    instances: list["StatefulFakeProcessor"] = []

    def __init__(self) -> None:
        self.offset = 0
        StatefulFakeProcessor.instances.append(self)

    def process(self, chunk: Any) -> Any:
        out = chunk.copy()
        out[0] = np.int16(self.offset & 0x7FFF)
        self.offset += 1
        return out


def setUpModule() -> None:
    global _SAVED_ORT, _SAVED_APM, _SAVED_MIC_INIT, _SAVED_MIC_ENTER
    try:
        import onnxruntime as ort
    except ImportError:
        ort = None  # type: ignore[assignment]
    if ort is not None:
        _SAVED_ORT = ort.InferenceSession

        def _blocked(*_a: Any, **_k: Any) -> None:
            raise AssertionError("real ORT blocked")

        ort.InferenceSession = _blocked  # type: ignore[misc, assignment]

    try:
        import pywebrtc_audio
    except ImportError:
        pywebrtc_audio = None  # type: ignore[assignment]
    if pywebrtc_audio is not None:
        _SAVED_APM = pywebrtc_audio.AudioProcessor

        def _blocked_apm(*_a: Any, **_k: Any) -> None:
            raise AssertionError("real AudioProcessor blocked")

        pywebrtc_audio.AudioProcessor = _blocked_apm  # type: ignore[misc, assignment]

    try:
        import speech_recognition as sr
    except ImportError:
        sr = None  # type: ignore[assignment]
    if sr is not None:
        _SAVED_MIC_INIT = sr.Microphone.__init__
        _SAVED_MIC_ENTER = sr.Microphone.__enter__

        def _mic_block(self, *_a: Any, **_k: Any) -> None:
            raise AssertionError("Mic blocked")

        def _mic_enter(self, *_a: Any, **_k: Any) -> Any:
            raise AssertionError("Mic blocked")

        sr.Microphone.__init__ = _mic_block  # type: ignore[method-assign]
        sr.Microphone.__enter__ = _mic_enter  # type: ignore[method-assign]


def tearDownModule() -> None:
    global _SAVED_ORT, _SAVED_APM, _SAVED_MIC_INIT, _SAVED_MIC_ENTER
    try:
        import onnxruntime as ort
    except ImportError:
        ort = None  # type: ignore[assignment]
    if ort is not None and _SAVED_ORT is not None:
        ort.InferenceSession = _SAVED_ORT  # type: ignore[misc, assignment]
    try:
        import pywebrtc_audio
    except ImportError:
        pywebrtc_audio = None  # type: ignore[assignment]
    if pywebrtc_audio is not None and _SAVED_APM is not None:
        pywebrtc_audio.AudioProcessor = _SAVED_APM  # type: ignore[misc, assignment]
    try:
        import speech_recognition as sr
    except ImportError:
        return
    if _SAVED_MIC_INIT is not None:
        sr.Microphone.__init__ = _SAVED_MIC_INIT  # type: ignore[method-assign]
    if _SAVED_MIC_ENTER is not None:
        sr.Microphone.__enter__ = _SAVED_MIC_ENTER  # type: ignore[method-assign]


def _repo_with_asset(tmp: Path) -> Path:
    asset_dir = tmp / "vendor" / "silero-vad"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "silero_vad.onnx").write_bytes(_DUMMY)
    return tmp


def _vad(owner: SileroOnnxVAD) -> SileroVadStreamContext:
    ctx, err = owner.create_stream_context()
    assert ctx is not None and err is None
    return ctx


def _patches(root: Path):
    digest = __import__("hashlib").sha256(_DUMMY).hexdigest()
    return (
        patch("src.voice.silero_vad.EXPECTED_ASSET_SHA256", digest),
        patch(
            "src.voice.silero_vad.read_verified_silero_model_bytes",
            return_value=(_DUMMY, None),
        ),
        patch(
            "src.voice.silero_vad._create_cpu_inference_session",
            side_effect=lambda b, **_: FakeOrtSession(b),
        ),
    )


class SileroStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeOrtSession.instances.clear()
        FakeOrtSession.run_calls = 0
        FakeOrtSession.run_enter = None
        FakeOrtSession.run_release = None
        FakeOrtSession.invalid_on_run = False

    def test_single_context_reuses_session(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _repo_with_asset(Path(tmp))
            with _patches(root)[0], _patches(root)[1], _patches(root)[2]:
                owner = SileroOnnxVAD(repo_root=root)
                ctx = _vad(owner)
                pcm = bytes([3, 0]) * FRAME_SAMPLES
                r1 = ctx.process_frame(pcm)
                r2 = ctx.process_frame(pcm)
                self.assertTrue(r1.ok and r2.ok)
                self.assertEqual(len(FakeOrtSession.instances), 1)
                self.assertEqual(FakeOrtSession.run_calls, 2)
                self.assertTrue(ctx.close())
                owner.close()

    def test_context_close_during_run(self) -> None:
        release = threading.Event()
        FakeOrtSession.run_enter = threading.Event()
        FakeOrtSession.run_release = release
        errors: list[BaseException] = []

        def _close_worker(ctx: SileroVadStreamContext) -> None:
            try:
                FakeOrtSession.run_enter.wait(timeout=2.0)
                self.assertFalse(ctx.close(timeout_sec=0))
                release.set()
            except BaseException as exc:
                errors.append(exc)

        with TemporaryDirectory() as tmp:
            root = _repo_with_asset(Path(tmp))
            with _patches(root)[0], _patches(root)[1], _patches(root)[2]:
                owner = SileroOnnxVAD(repo_root=root)
                ctx = _vad(owner)
                pcm = bytes([2, 0]) * FRAME_SAMPLES
                worker = threading.Thread(target=_close_worker, args=(ctx,))
                worker.start()
                result = ctx.process_frame(pcm)
                worker.join(timeout=3.0)
                self.assertFalse(errors)
                self.assertFalse(worker.is_alive())
                self.assertFalse(result.ok)
                self.assertTrue(ctx.close())
                owner.close()

    def test_owner_close_during_run(self) -> None:
        release = threading.Event()
        FakeOrtSession.run_enter = threading.Event()
        FakeOrtSession.run_release = release
        errors: list[BaseException] = []

        def _owner_close_worker(owner: SileroOnnxVAD) -> None:
            try:
                FakeOrtSession.run_enter.wait(timeout=2.0)
                self.assertFalse(owner.close(timeout_sec=0))
                release.set()
            except BaseException as exc:
                errors.append(exc)

        with TemporaryDirectory() as tmp:
            root = _repo_with_asset(Path(tmp))
            with _patches(root)[0], _patches(root)[1], _patches(root)[2]:
                owner = SileroOnnxVAD(repo_root=root)
                ctx = _vad(owner)
                pcm = bytes([2, 0]) * FRAME_SAMPLES
                worker = threading.Thread(target=_owner_close_worker, args=(owner,))
                worker.start()
                result = ctx.process_frame(pcm)
                worker.join(timeout=3.0)
                self.assertFalse(errors)
                self.assertFalse(worker.is_alive())
                self.assertFalse(result.ok)
                self.assertTrue(owner.close())

    def test_reset_while_idle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _repo_with_asset(Path(tmp))
            with _patches(root)[0], _patches(root)[1], _patches(root)[2]:
                owner = SileroOnnxVAD(repo_root=root)
                ctx = _vad(owner)
                self.assertTrue(ctx.reset())
                pcm = bytes([1, 0]) * FRAME_SAMPLES
                self.assertTrue(ctx.process_frame(pcm).ok)
                ctx.close()
                owner.close()

    def test_native_error_invalidates_stream(self) -> None:
        FakeOrtSession.invalid_on_run = True
        with TemporaryDirectory() as tmp:
            root = _repo_with_asset(Path(tmp))
            with _patches(root)[0], _patches(root)[1], _patches(root)[2]:
                owner = SileroOnnxVAD(repo_root=root)
                ctx = _vad(owner)
                pcm = bytes([1, 0]) * FRAME_SAMPLES
                result = ctx.process_frame(pcm)
                self.assertFalse(result.ok)
                self.assertTrue(ctx.invalidated)
                self.assertFalse(ctx.process_frame(pcm).ok)
                ctx.close()
                owner.close()


class NoiseStreamTests(unittest.TestCase):
    def _factory(self) -> Callable[[], StatefulFakeProcessor]:
        return StatefulFakeProcessor

    def test_chunk_boundary_invariant(self) -> None:
        pcm = bytes([4, 0]) * (160 * 5 + 50)
        self.assertEqual(len(pcm) % 2, 0)
        with patch.object(ns_module, "_import_pywebrtc_audio", return_value=(object(), None)):
            stream1 = WebRtcNoiseStream(processor_factory=self._factory())
            one = stream1.process_chunk(pcm)
            self.assertTrue(one.ok)
            one_tail = stream1.finish()
            self.assertTrue(one_tail.ok)
            whole = one.pcm + one_tail.pcm
            self.assertEqual(len(whole), len(pcm))
            stream1.close()

            parts: List[bytes] = []
            stream2 = WebRtcNoiseStream(processor_factory=self._factory())
            offset = 0
            while offset < len(pcm):
                chunk = pcm[offset : offset + 640]
                part = stream2.process_chunk(chunk)
                self.assertTrue(part.ok, part.reason)
                parts.append(part.pcm)
                offset += len(chunk)
            end = stream2.finish()
            self.assertTrue(end.ok, end.reason)
            parts.append(end.pcm)
            stream2.close()
            joined = b"".join(parts)
            self.assertEqual(joined, whole)
            self.assertNotEqual(joined, b"")

    def test_cancel_on_last_native_frame(self) -> None:
        pcm = bytes([3, 0]) * 160
        cancel = threading.Event()

        class _CancelOnProcess(StatefulFakeProcessor):
            def process(self, chunk: Any) -> Any:
                cancel.set()
                return super().process(chunk)

        stream = WebRtcNoiseStream(processor_factory=lambda: _CancelOnProcess())
        with patch.object(ns_module, "_import_pywebrtc_audio", return_value=(object(), None)):
            result = stream.process_chunk(pcm, cancel_event=cancel)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "cancelled")

    def test_close_while_processing_returns_false_then_retry(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class _Slow(StatefulFakeProcessor):
            def process(self, chunk: Any) -> Any:
                entered.set()
                if not release.wait(2.0):
                    raise TimeoutError("release")
                return super().process(chunk)

        stream = WebRtcNoiseStream(processor_factory=lambda: _Slow())
        done: list[bool] = []

        def _closer() -> None:
            entered.wait(2.0)
            done.append(stream.close(timeout_sec=0))
            release.set()

        with patch.object(ns_module, "_import_pywebrtc_audio", return_value=(object(), None)):
            pcm = bytes([1, 0]) * 320
            worker = threading.Thread(target=_closer)
            worker.start()
            stream.process_chunk(pcm)
            worker.join(timeout=3.0)
            self.assertIn(False, done)
            self.assertTrue(stream.close())
            self.assertTrue(stream.closed)

    def test_finish_then_chunk_rejected(self) -> None:
        with patch.object(ns_module, "_import_pywebrtc_audio", return_value=(object(), None)):
            stream = WebRtcNoiseStream(processor_factory=self._factory())
            pcm = bytes([2, 0]) * 160
            self.assertTrue(stream.process_chunk(pcm).ok)
            self.assertTrue(stream.finish().ok)
            late = stream.process_chunk(pcm)
            self.assertFalse(late.ok)
            stream.close()

    def test_pre_call_cancel_invalidates_continuity(self) -> None:
        cancel = threading.Event()
        with patch.object(ns_module, "_import_pywebrtc_audio", return_value=(object(), None)):
            stream = WebRtcNoiseStream(processor_factory=self._factory())
            self.assertTrue(stream.process_chunk(bytes([1, 0]) * 20).ok)
            cancel.set()
            bad = stream.process_chunk(bytes([2, 0]) * 20, cancel_event=cancel)
            self.assertFalse(bad.ok)
            self.assertTrue(stream.invalidated)
            cancel.clear()
            self.assertFalse(stream.process_chunk(bytes([3, 0]) * 160).ok)
            self.assertTrue(stream.close())

    def test_final_cancel_in_process_invalidates(self) -> None:
        cancel = threading.Event()

        class _CancelProc(StatefulFakeProcessor):
            def process(self, chunk: Any) -> Any:
                cancel.set()
                return super().process(chunk)

        with patch.object(ns_module, "_import_pywebrtc_audio", return_value=(object(), None)):
            stream = WebRtcNoiseStream(processor_factory=lambda: _CancelProc())
            result = stream.process_chunk(bytes([1, 0]) * 160, cancel_event=cancel)
            self.assertFalse(result.ok)
            self.assertEqual(result.pcm, b"")
            self.assertFalse(stream.process_chunk(bytes([1, 0]) * 160).ok)
            self.assertTrue(stream.close())

    def test_close_during_native_wait_retains_processor(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        results: list[Any] = []

        class _Slow(StatefulFakeProcessor):
            def process(self, chunk: Any) -> Any:
                entered.set()
                if not release.wait(2.0):
                    raise TimeoutError("release")
                return super().process(chunk)

        stream = WebRtcNoiseStream(processor_factory=lambda: _Slow())

        def run() -> None:
            try:
                with patch.object(
                    ns_module, "_import_pywebrtc_audio", return_value=(object(), None)
                ):
                    results.append(stream.process_chunk(bytes([1, 0]) * 160))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(ns_module, "_import_pywebrtc_audio", return_value=(object(), None)):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(2.0))
            self.assertFalse(stream.close(timeout_sec=0))
            self.assertIsNotNone(stream._processor)
            release.set()
            thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertTrue(stream.close(timeout_sec=2.0))
        self.assertIsNone(stream._processor)

    def test_batch_wraps_stream(self) -> None:
        pcm = bytes([6, 0]) * (160 * 3 + 10)
        with patch.object(ns_module, "_import_pywebrtc_audio", return_value=(object(), None)):
            batch = suppress_utterance_pcm(pcm, processor_factory=self._factory())
        self.assertTrue(batch.ok)
        self.assertEqual(len(batch.pcm), len(pcm))


if __name__ == "__main__":
    unittest.main()
