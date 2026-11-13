# -*- coding: utf-8 -*-
"""Silero ONNX VAD Phase 1 适配器（纯 fake；无真实 ORT/模型/麦克风）。"""
from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from src.voice.silero_vad import (
    BYTES_PER_SAMPLE,
    EXPECTED_ASSET_BYTES,
    EXPECTED_ASSET_SHA256,
    FRAME_SAMPLES,
    MAX_PCM_BYTES,
    SileroOnnxVAD,
    _build_cpu_session_options,
    canonical_silero_asset_path,
    default_repo_root,
    read_verified_silero_model_bytes,
    resolve_silero_asset_path,
    _validate_close_timeout_sec,
    _validate_session_io,
)

_SAVED_ORT_SESSION = None
_DUMMY_MODEL_BYTES = b"\xab" * EXPECTED_ASSET_BYTES


def setUpModule() -> None:
    global _SAVED_ORT_SESSION
    try:
        import onnxruntime as ort
    except ImportError:
        return
    _SAVED_ORT_SESSION = ort.InferenceSession

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "offline test must not construct real onnxruntime.InferenceSession"
        )

    ort.InferenceSession = _blocked  # type: ignore[misc, assignment]


def tearDownModule() -> None:
    global _SAVED_ORT_SESSION
    try:
        import onnxruntime as ort
    except ImportError:
        return
    if _SAVED_ORT_SESSION is not None:
        ort.InferenceSession = _SAVED_ORT_SESSION  # type: ignore[misc, assignment]
    _SAVED_ORT_SESSION = None


def _pcm_from_samples(count: int, *, byte: int = 5) -> bytes:
    return bytes([byte, 0]) * count


def _verified_ok_patch():
    return patch(
        "src.voice.silero_vad.read_verified_silero_model_bytes",
        return_value=(_DUMMY_MODEL_BYTES, None),
    )


def _silero_io_nodes() -> tuple[list[SimpleNamespace], list[SimpleNamespace]]:
    inputs = [
        SimpleNamespace(name="input", type="tensor(float)", shape=[1, 576]),
        SimpleNamespace(name="state", type="tensor(float)", shape=[2, 1, 128]),
        SimpleNamespace(name="sr", type="tensor(int64)", shape=[]),
    ]
    outputs = [
        SimpleNamespace(name="output", type="tensor(float)", shape=[1, 1]),
        SimpleNamespace(name="stateN", type="tensor(float)", shape=[2, 1, 128]),
    ]
    return inputs, outputs


def _native_cpu_graph_io_nodes() -> tuple[list[SimpleNamespace], list[SimpleNamespace]]:
    """Metadata from native supervised run input_v2_native_first_nov5d."""
    inputs = [
        SimpleNamespace(name="input", type="tensor(float)", shape=[None, None]),
        SimpleNamespace(name="state", type="tensor(float)", shape=[2, None, 128]),
        SimpleNamespace(name="sr", type="tensor(int64)", shape=[]),
    ]
    outputs = [
        SimpleNamespace(name="output", type="tensor(float)", shape=[None, 1]),
        SimpleNamespace(name="stateN", type="tensor(float)", shape=[None, None, None]),
    ]
    return inputs, outputs


class _StubGraphSession:
    def __init__(
        self,
        inputs: list[SimpleNamespace],
        outputs: list[SimpleNamespace],
        *,
        providers: list[str] | None = None,
    ) -> None:
        self._inputs = inputs
        self._outputs = outputs
        self._providers = providers or ["CPUExecutionProvider"]

    def get_inputs(self) -> list[SimpleNamespace]:
        return self._inputs

    def get_outputs(self) -> list[SimpleNamespace]:
        return self._outputs

    def get_providers(self) -> list[str]:
        return self._providers


class FakeOrtSession:
    instances: list["FakeOrtSession"] = []
    construct_barrier: threading.Barrier | None = None
    construct_enter: threading.Event | None = None
    construct_release: threading.Event | None = None
    run_enter: threading.Event | None = None
    run_release: threading.Event | None = None
    graph_inputs: list[SimpleNamespace] | None = None
    graph_outputs: list[SimpleNamespace] | None = None

    def __init__(self, model_bytes: bytes) -> None:
        if FakeOrtSession.construct_enter is not None:
            FakeOrtSession.construct_enter.set()
        if FakeOrtSession.construct_release is not None:
            if not FakeOrtSession.construct_release.wait(timeout=2.0):
                raise TimeoutError("session factory blocked too long")
        if FakeOrtSession.construct_barrier is not None:
            FakeOrtSession.construct_barrier.wait(timeout=2.0)
        self.model_bytes = model_bytes
        self.run_calls = 0
        FakeOrtSession.instances.append(self)

    def get_inputs(self) -> list[SimpleNamespace]:
        if self.graph_inputs is not None:
            return self.graph_inputs
        return _silero_io_nodes()[0]

    def get_outputs(self) -> list[SimpleNamespace]:
        if self.graph_outputs is not None:
            return self.graph_outputs
        return _silero_io_nodes()[1]

    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]

    def run(self, output_names, inputs) -> list[np.ndarray]:
        if FakeOrtSession.run_enter is not None:
            FakeOrtSession.run_enter.set()
        if FakeOrtSession.run_release is not None:
            if not FakeOrtSession.run_release.wait(timeout=2.0):
                raise TimeoutError("run blocked too long")
        self.run_calls += 1
        return [
            np.array([[0.42]], dtype=np.float32),
            np.zeros((2, 1, 128), dtype=np.float32),
        ]


def _repo_with_asset(tmp: Path) -> Path:
    asset_dir = tmp / "vendor" / "silero-vad"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "silero_vad.onnx").write_bytes(b"\x00" * EXPECTED_ASSET_BYTES)
    return tmp


class SileroAssetReadTests(unittest.TestCase):
    def test_missing_and_hash_mismatch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, err = read_verified_silero_model_bytes(root)
            self.assertIsNone(data)
            self.assertEqual(err, "asset_missing")
            root = _repo_with_asset(Path(tmp))
            data, err = read_verified_silero_model_bytes(root)
            self.assertEqual(err, "asset_hash_mismatch")

    def test_same_size_mutation_reread_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _repo_with_asset(Path(tmp))
            path = canonical_silero_asset_path(root)
            payload = b"\x00" * EXPECTED_ASSET_BYTES
            digest = __import__("hashlib").sha256(payload).hexdigest()
            with patch("src.voice.silero_vad.EXPECTED_ASSET_SHA256", digest):
                data, err = read_verified_silero_model_bytes(root)
                self.assertIsNone(err)
                self.assertEqual(len(data or b""), EXPECTED_ASSET_BYTES)
                path.write_bytes(b"\x01" + payload[1:])
                _, err2 = read_verified_silero_model_bytes(root)
                self.assertEqual(err2, "asset_hash_mismatch")

    def test_symlink_in_chain_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = canonical_silero_asset_path(root)
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(b"\x00" * EXPECTED_ASSET_BYTES)
            with patch.object(Path, "is_symlink", return_value=True):
                _, err = read_verified_silero_model_bytes(root)
            self.assertEqual(err, "asset_symlink")

    def test_reparse_point_in_chain_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _repo_with_asset(Path(tmp))
            target = canonical_silero_asset_path(root)

            def fake_lstat(path: os.PathLike[str] | str):
                p = Path(path)
                st = os.stat(p, follow_symlinks=False)
                if p == target.parent:
                    return SimpleNamespace(
                        st_mode=st.st_mode,
                        st_file_attributes=getattr(st, "st_file_attributes", 0)
                        | 0x400,
                    )
                return st

            with patch("src.voice.silero_vad.os.lstat", side_effect=fake_lstat):
                with patch.object(Path, "is_symlink", return_value=False):
                    with patch.object(Path, "is_junction", return_value=False):
                        _, err = read_verified_silero_model_bytes(root)
            self.assertEqual(err, "asset_symlink")

    def test_repo_asset_if_installed(self) -> None:
        path = canonical_silero_asset_path(default_repo_root())
        if not path.is_file():
            self.skipTest("vendor asset not present")
        data, err = read_verified_silero_model_bytes(default_repo_root())
        self.assertIsNone(err)
        self.assertIsNotNone(data)
        self.assertEqual(len(data), EXPECTED_ASSET_BYTES)


class SileroOnnxVADScoreTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeOrtSession.instances.clear()
        FakeOrtSession.construct_barrier = None
        FakeOrtSession.construct_enter = None
        FakeOrtSession.construct_release = None
        FakeOrtSession.run_enter = None
        FakeOrtSession.run_release = None
        FakeOrtSession.graph_inputs = None
        FakeOrtSession.graph_outputs = None

    def _vad(self, tmp: Path) -> SileroOnnxVAD:
        return SileroOnnxVAD(
            repo_root=_repo_with_asset(tmp),
            session_factory=lambda b: FakeOrtSession(b),
        )

    def test_score_ok_passes_verified_bytes_to_factory(self) -> None:
        with TemporaryDirectory() as tmp:
            with _verified_ok_patch():
                vad = self._vad(Path(tmp))
                result = vad.score_pcm(_pcm_from_samples(FRAME_SAMPLES + 100))
            self.assertTrue(result.ok)
            self.assertEqual(FakeOrtSession.instances[0].model_bytes, _DUMMY_MODEL_BYTES)
            vad.close()

    def test_invalid_pcm_never_reads_asset(self) -> None:
        with TemporaryDirectory() as tmp:
            with _verified_ok_patch() as read_mock:
                vad = self._vad(Path(tmp))
                self.assertEqual(vad.score_pcm(b"").reason, "pcm_empty")
                read_mock.assert_not_called()
                vad.close()

    def test_last_frame_cancel_before_success(self) -> None:
        with TemporaryDirectory() as tmp:
            cancel = threading.Event()

            class CancelAfterFirst(FakeOrtSession):
                def run(self, output_names, inputs):
                    cancel.set()
                    return super().run(output_names, inputs)

            with _verified_ok_patch():
                vad = SileroOnnxVAD(
                    repo_root=_repo_with_asset(Path(tmp)),
                    session_factory=lambda b: CancelAfterFirst(b),
                )
                result = vad.score_pcm(
                    _pcm_from_samples(FRAME_SAMPLES * 2),
                    cancel_event=cancel,
                )
            self.assertEqual(result.reason, "cancelled")
            vad.close()

    def test_close_timeout_then_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            run_enter = threading.Event()
            run_release = threading.Event()
            FakeOrtSession.run_enter = run_enter
            FakeOrtSession.run_release = run_release
            with _verified_ok_patch():
                vad = SileroOnnxVAD(
                    repo_root=_repo_with_asset(Path(tmp)),
                    session_factory=lambda b: FakeOrtSession(b),
                    close_lock_timeout_sec=0.05,
                )
                errors: list[BaseException] = []

                def worker() -> None:
                    try:
                        vad.score_pcm(_pcm_from_samples(FRAME_SAMPLES))
                    except BaseException as exc:
                        errors.append(exc)

                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(run_enter.wait(timeout=2.0))
                self.assertFalse(vad.close(timeout_sec=0.05))
                run_release.set()
                thread.join(timeout=2.0)
                self.assertFalse(errors)
                self.assertTrue(vad.close(timeout_sec=1.0))

    def test_invalid_close_timeout_raises_and_leaves_usable(self) -> None:
        with TemporaryDirectory() as tmp:
            with _verified_ok_patch():
                vad = self._vad(Path(tmp))
                self.assertFalse(vad._closing.is_set())
                with self.assertRaises(ValueError):
                    vad.close(timeout_sec=float("nan"))
                self.assertFalse(vad._closing.is_set())
                result = vad.score_pcm(_pcm_from_samples(FRAME_SAMPLES))
                self.assertTrue(result.ok)
                vad.close()

    def test_invalid_constructor_close_budget_raises(self) -> None:
        with self.assertRaises(ValueError):
            SileroOnnxVAD(close_lock_timeout_sec=True)

    def test_close_during_session_factory_load(self) -> None:
        with TemporaryDirectory() as tmp:
            factory_enter = threading.Event()
            factory_release = threading.Event()
            FakeOrtSession.construct_enter = factory_enter
            FakeOrtSession.construct_release = factory_release
            with _verified_ok_patch():
                vad = SileroOnnxVAD(
                    repo_root=_repo_with_asset(Path(tmp)),
                    session_factory=lambda b: FakeOrtSession(b),
                    close_lock_timeout_sec=0.05,
                )
                score_result: list = []
                errors: list[BaseException] = []

                def score_worker() -> None:
                    try:
                        score_result.append(
                            vad.score_pcm(_pcm_from_samples(FRAME_SAMPLES))
                        )
                    except BaseException as exc:
                        errors.append(exc)

                score_thread = threading.Thread(target=score_worker)
                score_thread.start()
                self.assertTrue(factory_enter.wait(timeout=2.0))
                self.assertFalse(vad.close(timeout_sec=0.05))
                self.assertTrue(vad._closing.is_set())
                factory_release.set()
                score_thread.join(timeout=2.0)
                self.assertFalse(errors)
                self.assertEqual(len(score_result), 1)
                self.assertEqual(score_result[0].reason, "closing")
                self.assertIsNone(vad._session)

    def test_native_cpu_graph_metadata_accepted(self) -> None:
        inputs, outputs = _native_cpu_graph_io_nodes()
        session = _StubGraphSession(inputs, outputs)
        self.assertIsNone(_validate_session_io(session))

    def test_score_ok_with_native_graph_metadata_fixture(self) -> None:
        with TemporaryDirectory() as tmp:
            inputs, outputs = _native_cpu_graph_io_nodes()

            def factory(model_bytes: bytes) -> FakeOrtSession:
                session = FakeOrtSession(model_bytes)
                session.graph_inputs = inputs
                session.graph_outputs = outputs
                return session

            with _verified_ok_patch():
                vad = SileroOnnxVAD(
                    repo_root=_repo_with_asset(Path(tmp)),
                    session_factory=factory,
                )
                result = vad.score_pcm(_pcm_from_samples(FRAME_SAMPLES))
            self.assertTrue(result.ok)
            vad.close()

    def test_graph_wrong_concrete_input_dim_rejected(self) -> None:
        inputs, outputs = _native_cpu_graph_io_nodes()
        bad_inputs = [
            SimpleNamespace(name="input", type="tensor(float)", shape=[1, 512]),
            inputs[1],
            inputs[2],
        ]
        self.assertEqual(
            _validate_session_io(_StubGraphSession(bad_inputs, outputs)),
            "model_io_invalid",
        )

    def test_graph_bool_and_negative_shape_rejected(self) -> None:
        inputs, outputs = _native_cpu_graph_io_nodes()
        bool_inputs = [
            SimpleNamespace(name="input", type="tensor(float)", shape=[True, 576]),
            inputs[1],
            inputs[2],
        ]
        self.assertEqual(
            _validate_session_io(_StubGraphSession(bool_inputs, outputs)),
            "model_io_invalid",
        )
        negative_inputs = [
            SimpleNamespace(name="input", type="tensor(float)", shape=[-1, 576]),
            inputs[1],
            inputs[2],
        ]
        self.assertEqual(
            _validate_session_io(_StubGraphSession(negative_inputs, outputs)),
            "model_io_invalid",
        )

    def test_graph_wrong_state_rank_and_sr_type_rejected(self) -> None:
        inputs, outputs = _native_cpu_graph_io_nodes()
        bad_state = [
            inputs[0],
            SimpleNamespace(name="state", type="tensor(float)", shape=[2, 128]),
            inputs[2],
        ]
        self.assertEqual(
            _validate_session_io(_StubGraphSession(bad_state, outputs)),
            "model_io_invalid",
        )
        bad_sr = [
            inputs[0],
            inputs[1],
            SimpleNamespace(name="sr", type="tensor(float)", shape=[]),
        ]
        self.assertEqual(
            _validate_session_io(_StubGraphSession(bad_sr, outputs)),
            "model_io_invalid",
        )

    def test_validate_close_timeout_sec_bounds(self) -> None:
        self.assertEqual(_validate_close_timeout_sec(0), 0.0)
        self.assertEqual(_validate_close_timeout_sec(2), 2.0)
        with self.assertRaises(ValueError):
            _validate_close_timeout_sec(-0.1)
        with self.assertRaises(ValueError):
            _validate_close_timeout_sec(2.1)
        with self.assertRaises(ValueError):
            _validate_close_timeout_sec(float("inf"))

    def test_concurrent_score_one_busy(self) -> None:
        with TemporaryDirectory() as tmp:
            run_enter = threading.Event()
            run_release = threading.Event()
            FakeOrtSession.run_enter = run_enter
            FakeOrtSession.run_release = run_release
            with _verified_ok_patch():
                vad = SileroOnnxVAD(
                    repo_root=_repo_with_asset(Path(tmp)),
                    session_factory=lambda b: FakeOrtSession(b),
                )
                results: list = []
                errors: list[BaseException] = []

                def worker() -> None:
                    try:
                        results.append(vad.score_pcm(_pcm_from_samples(FRAME_SAMPLES)))
                    except BaseException as exc:
                        errors.append(exc)

                t1 = threading.Thread(target=worker)
                t2 = threading.Thread(target=worker)
                t1.start()
                t2.start()
                self.assertTrue(run_enter.wait(timeout=2.0))
                t2.join(timeout=2.0)
                run_release.set()
                t1.join(timeout=2.0)
                self.assertFalse(errors)
                self.assertEqual(len(FakeOrtSession.instances), 1)
                reasons = {item.reason for item in results}
                self.assertIn("ok", reasons)
                self.assertIn("busy", reasons)
                vad.close()

    def test_native_output_contract(self) -> None:
        with TemporaryDirectory() as tmp:

            class BadOutputs(FakeOrtSession):
                def run(self, output_names, inputs):
                    return [None, np.zeros((2, 1, 128), dtype=np.float32)]

            with _verified_ok_patch():
                vad = SileroOnnxVAD(
                    repo_root=_repo_with_asset(Path(tmp)),
                    session_factory=lambda b: BadOutputs(b),
                )
                self.assertEqual(
                    vad.score_pcm(_pcm_from_samples(FRAME_SAMPLES)).reason,
                    "probability_invalid",
                )
            vad.close()

    def test_model_io_exact_and_provider(self) -> None:
        with TemporaryDirectory() as tmp:

            class ExtraInput(FakeOrtSession):
                def get_inputs(self):
                    nodes = _silero_io_nodes()[0] + [
                        SimpleNamespace(name="extra", type="tensor(float)", shape=[1])
                    ]
                    return nodes

            with _verified_ok_patch():
                vad = SileroOnnxVAD(
                    repo_root=_repo_with_asset(Path(tmp)),
                    session_factory=lambda b: ExtraInput(b),
                )
                self.assertEqual(
                    vad.score_pcm(_pcm_from_samples(FRAME_SAMPLES)).reason,
                    "model_io_invalid",
                )
            vad.close()

    def test_missing_ort_oserror(self) -> None:
        with TemporaryDirectory() as tmp:
            with _verified_ok_patch(), patch(
                "src.voice.silero_vad._import_onnxruntime",
                return_value=(None, "ort_unavailable"),
            ):
                vad = self._vad(Path(tmp))
                self.assertEqual(
                    vad.score_pcm(_pcm_from_samples(FRAME_SAMPLES)).reason,
                    "ort_unavailable",
                )
                vad.close()

    def test_cpu_session_options_are_single_threaded(self) -> None:
        ort, err = __import__("src.voice.silero_vad", fromlist=["_import_onnxruntime"])._import_onnxruntime()
        if ort is None:
            self.skipTest("onnxruntime not installed")
        options = _build_cpu_session_options(ort)
        self.assertEqual(options.intra_op_num_threads, 1)
        self.assertEqual(options.inter_op_num_threads, 1)
        self.assertFalse(options.enable_profiling)


if __name__ == "__main__":
    unittest.main()
