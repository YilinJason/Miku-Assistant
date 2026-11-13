"""CosyVoice checkpoint mmap 注入（离线 fake torch，无真实模型/设备）。"""
from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import src.voice.cosyvoice.checkpoint_loading as checkpoint_loading
from src.voice.cosyvoice.checkpoint_loading import (
    CpuCheckpointStagingError,
    cpu_checkpoint_staging_scope,
)

_ZIP_HEADER = b"PK\x03\x04"
_LEGACY_BYTES = b"legacy-not-zip"
_MAP_UNSET = object()
_WEIGHTS_UNSET = object()
_MMAP_UNSET = object()


class _RecordingLoad:
    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []

    def record(
        self,
        *,
        args: tuple,
        extra_kwargs: dict,
        map_location: object,
        map_location_given: bool,
        weights_only: object,
        weights_only_given: bool,
        mmap: object,
        mmap_given: bool,
    ) -> None:
        self.calls.append(
            SimpleNamespace(
                args=args,
                extra_kwargs=dict(extra_kwargs),
                map_location=map_location,
                map_location_given=map_location_given,
                weights_only=weights_only,
                weights_only_given=weights_only_given,
                mmap=mmap,
                mmap_given=mmap_given,
            )
        )


def _load_with_mmap_param(
    *args,
    map_location=_MAP_UNSET,
    weights_only=_WEIGHTS_UNSET,
    mmap=_MMAP_UNSET,
    **kwargs,
):
    _load_with_mmap_param.recorder.record(  # type: ignore[attr-defined]
        args=args,
        extra_kwargs=kwargs,
        map_location=map_location,
        map_location_given=map_location is not _MAP_UNSET,
        weights_only=weights_only,
        weights_only_given=weights_only is not _WEIGHTS_UNSET,
        mmap=mmap,
        mmap_given=mmap is not _MMAP_UNSET,
    )
    return {"tensor": True}


def _load_without_mmap_param(*args, **kwargs):
    map_location = kwargs.get("map_location", _MAP_UNSET)
    weights_only = kwargs.get("weights_only", _WEIGHTS_UNSET)
    _load_without_mmap_param.recorder.record(  # type: ignore[attr-defined]
        args=args,
        extra_kwargs={
            k: v
            for k, v in kwargs.items()
            if k not in ("map_location", "weights_only")
        },
        map_location=map_location,
        map_location_given="map_location" in kwargs,
        weights_only=weights_only,
        weights_only_given="weights_only" in kwargs,
        mmap=kwargs.get("mmap", _MMAP_UNSET),
        mmap_given="mmap" in kwargs,
    )
    return {"tensor": True}


def _fake_torch_module(load_fn) -> types.ModuleType:
    mod = types.ModuleType("torch")
    mod.load = load_fn
    mod.device = lambda *a, **k: SimpleNamespace(type="cuda")
    return mod


class CheckpointMmapInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._torch_backup = sys.modules.get("torch")
        self._recorder = _RecordingLoad()
        _load_with_mmap_param.recorder = self._recorder  # type: ignore[attr-defined]
        _load_without_mmap_param.recorder = self._recorder  # type: ignore[attr-defined]

    def tearDown(self) -> None:
        if self._torch_backup is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = self._torch_backup

    def _enter_scope(self, model_dir: Path, *, with_mmap_sig: bool = True):
        fn = _load_with_mmap_param if with_mmap_sig else _load_without_mmap_param
        sys.modules["torch"] = _fake_torch_module(fn)
        return cpu_checkpoint_staging_scope(model_dir)

    def test_zip_llm_injects_mmap_true_when_supported(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(_ZIP_HEADER + b"rest")
            with self._enter_scope(root):
                import torch

                torch.load(
                    root / "llm.pt",
                    map_location="cuda",
                    weights_only=True,
                )
            self.assertEqual(len(self._recorder.calls), 1)
            call = self._recorder.calls[0]
            self.assertTrue(call.map_location_given)
            self.assertEqual(call.map_location, "cpu")
            self.assertTrue(call.weights_only_given)
            self.assertIs(call.weights_only, True)
            self.assertTrue(call.mmap_given)
            self.assertIs(call.mmap, True)

    def test_explicit_mmap_false_preserved(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "flow.pt").write_bytes(_ZIP_HEADER)
            with self._enter_scope(root):
                import torch

                torch.load(
                    root / "flow.pt",
                    map_location="cuda",
                    weights_only=True,
                    mmap=False,
                )
            call = self._recorder.calls[0]
            self.assertTrue(call.mmap_given)
            self.assertIs(call.mmap, False)
            self.assertEqual(call.map_location, "cpu")

    def test_explicit_mmap_none_preserved(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "hift.pt").write_bytes(_ZIP_HEADER)
            with self._enter_scope(root):
                import torch

                torch.load(
                    root / "hift.pt",
                    map_location="cuda",
                    weights_only=True,
                    mmap=None,
                )
            call = self._recorder.calls[0]
            self.assertTrue(call.mmap_given)
            self.assertIsNone(call.mmap)

    def test_legacy_header_no_mmap_injection_still_cpu_staged(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(_LEGACY_BYTES)
            with self._enter_scope(root):
                import torch

                torch.load(
                    root / "llm.pt",
                    map_location="cuda",
                    weights_only=True,
                )
            call = self._recorder.calls[0]
            self.assertFalse(call.mmap_given)
            self.assertEqual(call.map_location, "cpu")

    def test_unsupported_mmap_signature_no_injection(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(_ZIP_HEADER)
            with self._enter_scope(root, with_mmap_sig=False):
                import torch

                torch.load(
                    root / "llm.pt",
                    map_location="cuda",
                    weights_only=True,
                )
            call = self._recorder.calls[0]
            self.assertFalse(call.mmap_given)
            self.assertEqual(call.map_location, "cpu")

    def test_non_target_file_unchanged_no_mmap(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "spk2info.pt").write_bytes(_ZIP_HEADER)
            with self._enter_scope(root):
                import torch

                torch.load(
                    root / "spk2info.pt",
                    map_location="cuda",
                    weights_only=True,
                )
            call = self._recorder.calls[0]
            self.assertEqual(call.map_location, "cuda")
            self.assertFalse(call.mmap_given)

    def test_cuda_positional_staged_with_mmap(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "flow.pt").write_bytes(_ZIP_HEADER)
            with self._enter_scope(root):
                import torch

                torch.load(root / "flow.pt", "cuda:0", weights_only=True)
            call = self._recorder.calls[0]
            self.assertEqual(call.args[0], root / "flow.pt")
            self.assertEqual(call.args[1], "cpu")
            self.assertFalse(call.map_location_given)
            self.assertTrue(call.weights_only_given)
            self.assertTrue(call.mmap_given)
            self.assertIs(call.mmap, True)

    def test_load_error_propagates_single_call_same_object(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(_ZIP_HEADER)
            call_count = {"n": 0}
            err = OSError("load failed")

            def failing_with_mmap(
                *a,
                map_location=_MAP_UNSET,
                weights_only=_WEIGHTS_UNSET,
                mmap=_MMAP_UNSET,
                **extra,
            ):
                call_count["n"] += 1
                raise err

            sys.modules["torch"] = _fake_torch_module(failing_with_mmap)
            with self.assertRaises(OSError) as caught:
                with cpu_checkpoint_staging_scope(root):
                    import torch

                    torch.load(
                        root / "llm.pt",
                        map_location="cuda",
                        weights_only=True,
                    )
            self.assertEqual(call_count["n"], 1)
            self.assertIs(caught.exception, err)

    def test_signature_runtime_error_disables_mmap_cpu_staging_kept(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(_ZIP_HEADER)
            sys.modules["torch"] = _fake_torch_module(_load_with_mmap_param)
            with patch.object(
                checkpoint_loading.inspect,
                "signature",
                side_effect=RuntimeError("reflect failed"),
            ):
                with cpu_checkpoint_staging_scope(root):
                    import torch

                    torch.load(
                        root / "llm.pt",
                        map_location="cuda",
                        weights_only=True,
                    )
            call = self._recorder.calls[0]
            self.assertFalse(call.mmap_given)
            self.assertEqual(call.map_location, "cpu")

    def test_explicit_mmap_true_preserved(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(_ZIP_HEADER)
            with self._enter_scope(root):
                import torch

                torch.load(
                    root / "llm.pt",
                    map_location="cuda",
                    weights_only=True,
                    mmap=True,
                )
            call = self._recorder.calls[0]
            self.assertTrue(call.mmap_given)
            self.assertIs(call.mmap, True)

    def test_header_read_oserror_no_mmap_still_cpu_staged(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            missing = root / "llm.pt"
            with self._enter_scope(root):
                import torch

                torch.load(missing, map_location="cuda", weights_only=True)
            call = self._recorder.calls[0]
            self.assertFalse(call.mmap_given)
            self.assertEqual(call.map_location, "cpu")

    def test_retained_alias_from_scope_a_not_staged_during_scope_b(self) -> None:
        with TemporaryDirectory() as td:
            root_a = Path(td) / "a"
            root_b = Path(td) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "llm.pt").write_bytes(_ZIP_HEADER)
            (root_b / "llm.pt").write_bytes(_ZIP_HEADER)
            sys.modules["torch"] = _fake_torch_module(_load_with_mmap_param)
            import torch

            with cpu_checkpoint_staging_scope(root_a):
                alias_a = torch.load
            with cpu_checkpoint_staging_scope(root_b):
                alias_a(root_a / "llm.pt", map_location="cuda", weights_only=True)
                torch.load(
                    root_b / "llm.pt",
                    map_location="cuda",
                    weights_only=True,
                )
            self.assertEqual(len(self._recorder.calls), 2)
            alias_call = self._recorder.calls[0]
            scope_b_call = self._recorder.calls[1]
            self.assertEqual(alias_call.map_location, "cuda")
            self.assertFalse(alias_call.mmap_given)
            self.assertEqual(scope_b_call.map_location, "cpu")
            self.assertTrue(scope_b_call.mmap_given)
            self.assertIs(scope_b_call.mmap, True)

    def test_retained_alias_after_scope_not_staged_no_mmap(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(_ZIP_HEADER)
            sys.modules["torch"] = _fake_torch_module(_load_with_mmap_param)
            import torch

            with cpu_checkpoint_staging_scope(root):
                retained = torch.load
            retained(
                root / "llm.pt",
                map_location="cuda",
                weights_only=True,
            )
            call = self._recorder.calls[0]
            self.assertEqual(call.map_location, "cuda")
            self.assertFalse(call.mmap_given)

    def test_other_thread_delegates_original_join(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(_ZIP_HEADER)
            gate = threading.Event()
            done = threading.Event()
            errors: list[BaseException] = []

            def other_thread() -> None:
                try:
                    import torch

                    if not gate.wait(timeout=5):
                        raise TimeoutError("gate wait timed out")
                    torch.load(
                        root / "llm.pt",
                        map_location="cuda",
                        weights_only=True,
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    done.set()

            sys.modules["torch"] = _fake_torch_module(_load_with_mmap_param)
            with cpu_checkpoint_staging_scope(root):
                t = threading.Thread(target=other_thread)
                t.start()
                try:
                    pass
                finally:
                    gate.set()
                    t.join(timeout=5)
                    self.assertFalse(t.is_alive())
                self.assertFalse(errors)
                self.assertTrue(done.is_set())
                self.assertEqual(len(self._recorder.calls), 1)
                call = self._recorder.calls[0]
                self.assertEqual(call.map_location, "cuda")
                self.assertFalse(call.mmap_given)

    def test_scope_b_restores_patch_after_join(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(_ZIP_HEADER)
            sys.modules["torch"] = _fake_torch_module(_load_with_mmap_param)
            import torch

            original = torch.load
            with cpu_checkpoint_staging_scope(root):
                patched = torch.load
            self.assertIs(torch.load, original)
            self.assertIsNot(patched, original)

    def test_concurrent_second_scope_raises_and_first_releases(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            entered = threading.Event()
            release = threading.Event()
            holder_errors: list[BaseException] = []

            def holder() -> None:
                try:
                    with self._enter_scope(root):
                        entered.set()
                        if not release.wait(timeout=5):
                            raise TimeoutError("release wait timed out")
                except BaseException as exc:
                    holder_errors.append(exc)

            t = threading.Thread(target=holder)
            t.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                with self.assertRaises(CpuCheckpointStagingError):
                    with cpu_checkpoint_staging_scope(root):
                        pass
            finally:
                release.set()
                t.join(timeout=5)
                self.assertFalse(t.is_alive())
                self.assertFalse(holder_errors)


if __name__ == "__main__":
    unittest.main()
