"""CosyVoice CPU checkpoint staging（离线 fake torch，无真实模型）。"""
from __future__ import annotations

import contextlib
import sys
import threading
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import src.voice.cosyvoice.checkpoint_loading as checkpoint_loading
from src.voice.cosyvoice.checkpoint_loading import (
    CpuCheckpointStagingError,
    cpu_checkpoint_staging_scope,
)


class _RecordingLoad:
    calls: list[tuple[tuple, dict]]

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, dict(kwargs)))
        return {"tensor": True}


def _fake_torch_module(recorder: _RecordingLoad) -> types.ModuleType:
    mod = types.ModuleType("torch")
    mod.load = recorder
    mod.device = SimpleNamespace

    class _CudaDevice:
        type = "cuda"

        def __init__(self, index: int = 0) -> None:
            self.index = index

    mod.device = lambda *a, **k: _CudaDevice() if not a else _CudaDevice()
    return mod


class CpuCheckpointStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._torch_backup = sys.modules.get("torch")
        self._recorder = _RecordingLoad()

    def tearDown(self) -> None:
        if self._torch_backup is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = self._torch_backup

    def _enter_scope(self, model_dir: Path):
        sys.modules["torch"] = _fake_torch_module(self._recorder)
        return cpu_checkpoint_staging_scope(model_dir)

    def test_cuda_keyword_llm_staged_to_cpu_single_call(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(b"x")
            with self._enter_scope(root):
                import torch

                torch.load(
                    root / "llm.pt",
                    map_location="cuda",
                    weights_only=True,
                )
            self.assertEqual(len(self._recorder.calls), 1)
            args, kwargs = self._recorder.calls[0]
            self.assertEqual(args[0], root / "llm.pt")
            self.assertEqual(kwargs.get("map_location"), "cpu")
            self.assertIs(kwargs.get("weights_only"), True)
            self.assertEqual(torch.load, self._recorder)

    def test_cuda_positional_flow_staged(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "flow.pt").write_bytes(b"x")
            with self._enter_scope(root):
                import torch

                torch.load(root / "flow.pt", "cuda:0", weights_only=True)
            args, kwargs = self._recorder.calls[0]
            self.assertEqual(args[0], root / "flow.pt")
            self.assertEqual(args[1], "cpu")
            self.assertNotIn("map_location", kwargs)

    def test_cuda_device_object_hift_staged(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "hift.pt").write_bytes(b"x")
            with self._enter_scope(root):
                import torch

                dev = SimpleNamespace(type="cuda")
                torch.load(str(root / "hift.pt"), map_location=dev, weights_only=True)
            _, kwargs = self._recorder.calls[0]
            self.assertEqual(kwargs["map_location"], "cpu")

    def test_cpu_map_location_unchanged(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(b"x")
            with self._enter_scope(root):
                import torch

                torch.load(root / "llm.pt", map_location="cpu", weights_only=True)
            _, kwargs = self._recorder.calls[0]
            self.assertEqual(kwargs["map_location"], "cpu")

    def test_spk2info_cuda_not_staged(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "spk2info.pt").write_bytes(b"x")
            with self._enter_scope(root):
                import torch

                torch.load(root / "spk2info.pt", map_location="cuda", weights_only=True)
            _, kwargs = self._recorder.calls[0]
            self.assertEqual(kwargs["map_location"], "cuda")

    def test_unknown_path_not_staged(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            other = root / "nested"
            other.mkdir()
            (other / "llm.pt").write_bytes(b"x")
            with self._enter_scope(root):
                import torch

                torch.load(other / "llm.pt", map_location="cuda", weights_only=True)
            _, kwargs = self._recorder.calls[0]
            self.assertEqual(kwargs["map_location"], "cuda")

    def test_file_object_not_staged(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            with self._enter_scope(root):
                import torch

                buf = MagicMock()
                torch.load(buf, map_location="cuda", weights_only=True)
            args, kwargs = self._recorder.calls[0]
            self.assertIs(args[0], buf)
            self.assertEqual(kwargs["map_location"], "cuda")

    def test_weights_only_false_not_staged(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(b"x")
            with self._enter_scope(root):
                import torch

                torch.load(root / "llm.pt", map_location="cuda", weights_only=False)
            _, kwargs = self._recorder.calls[0]
            self.assertEqual(kwargs["map_location"], "cuda")

    def test_weights_only_omitted_not_staged(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(b"x")
            with self._enter_scope(root):
                import torch

                torch.load(root / "llm.pt", map_location="cuda")
            _, kwargs = self._recorder.calls[0]
            self.assertEqual(kwargs.get("map_location"), "cuda")
            self.assertNotIn("weights_only", kwargs)

    def test_extra_kwargs_preserved(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(b"x")
            with self._enter_scope(root):
                import torch

                torch.load(
                    root / "llm.pt",
                    map_location="cuda",
                    weights_only=True,
                    mmap=True,
                )
            _, kwargs = self._recorder.calls[0]
            self.assertTrue(kwargs["mmap"])
            self.assertEqual(kwargs["map_location"], "cpu")

    def test_exception_restores_torch_load(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            sys.modules["torch"] = _fake_torch_module(self._recorder)
            import torch

            original = torch.load
            with self.assertRaises(RuntimeError):
                with cpu_checkpoint_staging_scope(root):
                    raise RuntimeError("boom")
            self.assertIs(torch.load, original)

    def test_keyboard_interrupt_restores(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            sys.modules["torch"] = _fake_torch_module(self._recorder)
            import torch

            original = torch.load
            with self.assertRaises(KeyboardInterrupt):
                with cpu_checkpoint_staging_scope(root):
                    raise KeyboardInterrupt
            self.assertIs(torch.load, original)

    def test_init_failure_releases_lock_allows_reentry(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            sys.modules["torch"] = _fake_torch_module(self._recorder)
            with patch.object(
                checkpoint_loading,
                "_make_patched_load",
                side_effect=ValueError("init failed"),
            ):
                with self.assertRaises(ValueError):
                    with cpu_checkpoint_staging_scope(root):
                        pass
            lock_free = checkpoint_loading._SCOPE_LOCK.acquire(blocking=False)
            if lock_free:
                checkpoint_loading._SCOPE_LOCK.release()
            self.assertTrue(lock_free)
            (root / "llm.pt").write_bytes(b"x")
            with cpu_checkpoint_staging_scope(root):
                import torch

                torch.load(root / "llm.pt", map_location="cuda", weights_only=True)

    def test_nested_scope_raises(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            with self._enter_scope(root):
                with self.assertRaises(CpuCheckpointStagingError):
                    with cpu_checkpoint_staging_scope(root):
                        pass

    def test_concurrent_second_scope_raises(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            entered = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []

            def holder() -> None:
                try:
                    with self._enter_scope(root):
                        entered.set()
                        release.wait(timeout=5)
                except BaseException as exc:
                    errors.append(exc)

            t = threading.Thread(target=holder, name="holder")
            t.start()
            self.assertTrue(entered.wait(timeout=5))
            try:
                with self.assertRaises(CpuCheckpointStagingError):
                    with cpu_checkpoint_staging_scope(root):
                        pass
            finally:
                release.set()
                t.join(timeout=5)
            self.assertFalse(errors)

    def test_other_thread_load_unmodified(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "llm.pt").write_bytes(b"x")
            other_calls: list[tuple[tuple, dict]] = []
            gate = threading.Event()
            done = threading.Event()

            def other_thread() -> None:
                import torch

                gate.wait(timeout=5)
                other_calls.append(
                    (
                        (root / "llm.pt",),
                        {"map_location": "cuda", "weights_only": True},
                    )
                )
                torch.load(
                    root / "llm.pt",
                    map_location="cuda",
                    weights_only=True,
                )
                done.set()

            sys.modules["torch"] = _fake_torch_module(self._recorder)
            t = threading.Thread(target=other_thread)
            t.start()
            with cpu_checkpoint_staging_scope(root):
                gate.set()
                self.assertTrue(done.wait(timeout=5))
            t.join(timeout=5)
            self.assertEqual(len(self._recorder.calls), 1)
            _, kwargs = self._recorder.calls[0]
            self.assertEqual(kwargs["map_location"], "cuda")

    def test_success_exit_restores(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            sys.modules["torch"] = _fake_torch_module(self._recorder)
            import torch

            original = torch.load
            with cpu_checkpoint_staging_scope(root):
                patched = torch.load
            self.assertIs(torch.load, original)
            self.assertIsNot(patched, original)

    def test_retained_alias_delegates_after_scope_exit(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            sys.modules["torch"] = _fake_torch_module(self._recorder)
            import torch

            with cpu_checkpoint_staging_scope(root):
                retained = torch.load
            retained("unrelated.pt", map_location="cpu", weights_only=True)
            self.assertEqual(len(self._recorder.calls), 1)
            _, kwargs = self._recorder.calls[0]
            self.assertEqual(kwargs.get("map_location"), "cpu")

    def test_retained_alias_from_scope_a_not_staged_during_scope_b(self) -> None:
        with TemporaryDirectory() as td:
            root_a = Path(td) / "a"
            root_b = Path(td) / "b"
            root_a.mkdir()
            root_b.mkdir()
            (root_a / "llm.pt").write_bytes(b"x")
            (root_b / "llm.pt").write_bytes(b"x")
            sys.modules["torch"] = _fake_torch_module(self._recorder)
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
            _, kwargs_alias = self._recorder.calls[0]
            _, kwargs_b = self._recorder.calls[1]
            self.assertEqual(kwargs_alias.get("map_location"), "cuda")
            self.assertEqual(kwargs_b.get("map_location"), "cpu")


class ConstructAutomodelStagingTests(unittest.TestCase):
    def test_construct_automodel_wraps_cpu_scope(self) -> None:
        import scripts.cosyvoice_runtime_worker as wrk

        model_dir = Path("modeldir")
        automodel = MagicMock(return_value="model")
        events: list[str] = []

        class _TrackingScope:
            def __init__(self, md: Path) -> None:
                self.md = md

            def __enter__(self):
                events.append(f"enter:{self.md}")
                return self

            def __exit__(self, *a):
                events.append("exit:cpu")
                return False

        with (
            patch.object(wrk, "cpu_checkpoint_staging_scope", _TrackingScope),
            patch.object(
                wrk,
                "redirect_stdout_to_stderr",
                return_value=contextlib.nullcontext(),
            ),
        ):
            out = wrk._construct_automodel(automodel, model_dir)
        self.assertEqual(out, "model")
        automodel.assert_called_once_with(model_dir=str(model_dir))
        self.assertEqual(events, [f"enter:{model_dir}", "exit:cpu"])


if __name__ == "__main__":
    unittest.main()
