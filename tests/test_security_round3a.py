"""Round 3A：RVC 专用子进程能力与临时文件所有权——纯离线行为测试。"""
from __future__ import annotations

import io
import os
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.assembly import assemble_default, build_tts_engine, build_stt_engine
from src.core.rvc_capabilities import (
    RvcInferLaunch,
    RvcProbeKind,
    RvcWorkerLaunch,
    build_infer_argv,
    build_probe_argv,
    build_worker_argv,
)
from src.core.safety import (
    Operation,
    SafetyPolicy,
    TrustedRvcProfile,
    safe_subprocess_run,
    safe_system_power_run,
)
from src.system import windows_control as wc_mod
from src.system.windows_control import WindowsController
from src.voice.audio.temp_files import TempFileManager
from src.voice.rvc.infer import ensure_policy_rvc_python, run_rvc_infer_subprocess
from src.voice.stt import STTEngine

from tests.support.fake_owned_worker_job_backend import FakeOwnedWorkerJobBackend
from tests.support.local_tts_runners import bind_fake_rvc_runners
from tests.support.host_memory_fixtures import install_module_sufficient_host_memory


def setUpModule() -> None:
    install_module_sufficient_host_memory()


def _clear_dry_run():
    return patch.dict(os.environ, {"MIKU_DRY_RUN": ""}, clear=False)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakePopen:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdin = MagicMock()
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self._code = 0

    def poll(self):
        return self._code

    def terminate(self):
        self._code = 1

    def kill(self):
        self._code = 1

    def wait(self, timeout=None):
        return self._code


def _make_policy_with_python(tmp: Path | str, **perm_overrides) -> tuple[SafetyPolicy, Path, Path]:
    root = Path(tmp)
    python = root / "python.exe"
    python.write_bytes(b"")
    model = root / "model.pth"
    model.write_bytes(b"m")
    perms = {
        "subprocess": False,
        "rvc_worker": True,
        "rvc_infer": True,
        "rvc_probe": True,
        "system_power": False,
        "file_delete": True,
        "cloud_stt": True,
        "cloud_tts": True,
    }
    perms.update(perm_overrides)
    policy = SafetyPolicy(
        {"permissions": perms},
        trusted_rvc_profile=TrustedRvcProfile(python_path=python, model_path=model),
    )
    return policy, python, model


def _worker_launch(**overrides) -> RvcWorkerLaunch:
    base = dict(
        model_path="model.pth",
        device="cpu",
        pitch=0,
        index_rate=0.55,
        protect=0.5,
        filter_radius=3,
        rms_mix_rate=0.75,
        f0_method="rmvpe",
        parent_pid=os.getpid(),
        index_path=None,
    )
    base.update(overrides)
    return RvcWorkerLaunch(**base)


def _infer_launch(**overrides) -> RvcInferLaunch:
    base = dict(
        input_path="in.wav",
        output_path="out.wav",
        model_path="model.pth",
        device="cpu",
        pitch=0,
        index_rate=0.55,
        protect=0.5,
        filter_radius=3,
        rms_mix_rate=0.75,
        f0_method="rmvpe",
        index_path=None,
    )
    base.update(overrides)
    return RvcInferLaunch(**base)


class RvcCapabilityArgvTests(unittest.TestCase):
    def test_legal_worker_infer_probe_reach_fake_runners(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            policy, python, model = _make_policy_with_python(root)
            backend = FakeOwnedWorkerJobBackend()
            runs: list = []
            pops: list = []

            def fake_run(argv, **kwargs):
                runs.append((list(argv), dict(kwargs)))
                return _FakeCompleted(0)

            def fake_popen(argv, **kwargs):
                pops.append((list(argv), dict(kwargs)))
                redirector = _FakePopen(argv, **kwargs)
                handle = backend.last_created_handle
                if handle is not None:
                    backend.attach_redirector(handle, redirector)
                return redirector

            bind_fake_rvc_runners(
                policy,
                run_fn=fake_run,
                popen_fn=fake_popen,
                owned_worker_job_backend=backend,
            )

            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            owned_in = mgr.create('.wav')
            owned_out = mgr.create('.wav')
            w_launch = _worker_launch(model_path=str(model))
            i_launch = _infer_launch(
                input_path=owned_in, output_path=owned_out, model_path=str(model)
            )
            worker_argv = build_worker_argv(python, w_launch)
            infer_argv = build_infer_argv(python, i_launch)
            probe_argv = build_probe_argv(python, RvcProbeKind.IMPORT_RVC)

            policy.spawn_rvc_worker(w_launch, stdin=subprocess.PIPE)
            policy.run_rvc_infer(
                i_launch,
                temp_file_manager=mgr,
                capture_output=True,
            )
            policy.run_rvc_probe(RvcProbeKind.IMPORT_RVC, capture_output=True)

            self.assertEqual(len(pops), 3)
            pop_argv = pops[0][0]
            self.assertEqual(len(pop_argv), len(worker_argv))
            self.assertEqual(
                Path(pop_argv[0]).resolve(), Path(worker_argv[0]).resolve()
            )
            self.assertEqual(pop_argv[1:], worker_argv[1:])
            self.assertFalse(pops[0][1].get('shell'))
            infer_actual = pops[1][0]
            self.assertEqual(len(infer_actual), len(infer_argv))
            self.assertEqual(
                Path(infer_actual[0]).resolve(), Path(infer_argv[0]).resolve()
            )
            self.assertEqual(infer_actual[1:], infer_argv[1:])
            probe_actual = pops[2][0]
            self.assertEqual(len(probe_actual), len(probe_argv))
            self.assertEqual(
                Path(probe_actual[0]).resolve(), Path(probe_argv[0]).resolve()
            )
            self.assertEqual(probe_actual[1:], probe_argv[1:])
            self.assertEqual(len(runs), 0)
            mgr.shutdown()

    def test_illegal_shell_and_string_rejected_before_runner(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            policy, _python, _model = _make_policy_with_python(tmp)
            called = {"n": 0}

            def boom(*_a, **_k):
                called["n"] += 1
                raise AssertionError("runner must not be called")

            policy.bind_process_runners(run_fn=boom, popen_fn=boom)

            with self.assertRaises(PermissionError):
                policy.spawn_rvc_worker(_worker_launch(model_path=str(_model)), shell=True)
            from src.core.rvc_capabilities import reject_string_command

            with self.assertRaises(PermissionError):
                reject_string_command("echo hi")
            with self.assertRaises(PermissionError):
                safe_subprocess_run(policy, "echo hi")  # type: ignore[arg-type]
            self.assertEqual(called["n"], 0)

    def test_duplicate_missing_unknown_flags_rejected(self) -> None:
        from src.core.rvc_capabilities import (
            _INFER_ALLOWED_FLAGS,
            _INFER_REQUIRED_FLAGS,
            _validate_flag_pairs,
        )

        with self.assertRaises(PermissionError):
            _validate_flag_pairs(
                [("--input", "a"), ("--input", "b")],
                allowed=_INFER_ALLOWED_FLAGS,
                required=_INFER_REQUIRED_FLAGS,
            )
        with self.assertRaises(PermissionError):
            _validate_flag_pairs(
                [("--input", "a")],
                allowed=_INFER_ALLOWED_FLAGS,
                required=_INFER_REQUIRED_FLAGS,
            )
        with self.assertRaises(PermissionError):
            _validate_flag_pairs(
                [("--not-a-flag", "x")],
                allowed=_INFER_ALLOWED_FLAGS,
                required=(),
            )


class RvcCapabilityGateTests(unittest.TestCase):
    def test_generic_subprocess_false_dedicated_still_works(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            policy, _python, _model = _make_policy_with_python(tmp, subprocess=False)
            self.assertFalse(policy.is_allowed(Operation.SUBPROCESS))
            runs = []

            def fake_popen(argv, **kwargs):
                runs.append(list(argv))
                return _FakePopen(argv, **kwargs)

            bind_fake_rvc_runners(
                policy, popen_fn=fake_popen, owned_worker_job_backend=FakeOwnedWorkerJobBackend()
            )
            mgr = TempFileManager(safety_policy=policy, default_dir=tmp)
            owned_in = mgr.create(".wav")
            owned_out = mgr.create(".wav")
            policy.run_rvc_infer(
                _infer_launch(
                    input_path=owned_in,
                    output_path=owned_out,
                    model_path=str(_model),
                ),
                temp_file_manager=mgr,
                capture_output=True,
            )
            self.assertEqual(len(runs), 1)
            mgr.shutdown()
            with self.assertRaises(PermissionError):
                safe_subprocess_run(policy, ["echo", "x"])

    def test_dedicated_disabled_or_dry_run_rejects(self) -> None:
        with TemporaryDirectory() as tmp:
            policy, _py, _model = _make_policy_with_python(
                tmp, rvc_worker=False, rvc_infer=False, rvc_probe=False
            )
            policy.bind_process_runners(run_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
            with self.assertRaises(PermissionError):
                policy.spawn_rvc_worker(_worker_launch())
            with self.assertRaises(PermissionError):
                policy.run_rvc_infer(_infer_launch())
            with self.assertRaises(PermissionError):
                policy.run_rvc_probe(RvcProbeKind.CUDA_AVAILABLE)

        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MIKU_DRY_RUN": "1"}):
                policy, _py, _model = _make_policy_with_python(tmp)
                self.assertFalse(policy.is_allowed(Operation.RVC_WORKER))
                with self.assertRaises(PermissionError):
                    policy.run_rvc_infer(_infer_launch())

    def test_infer_subprocess_uses_dedicated_not_generic(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            policy, python, model = _make_policy_with_python(root, subprocess=False)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            owned_in = mgr.create('.wav')
            owned_out = mgr.create('.wav')
            Path(owned_out).write_bytes(b'wav')
            runs = []

            def fake_popen(argv, **kwargs):
                runs.append(list(argv))
                out_i = list(argv).index("--output") + 1
                Path(argv[out_i]).write_bytes(b"wav")
                return _FakePopen(argv, **kwargs)

            bind_fake_rvc_runners(
                policy,
                popen_fn=fake_popen,
                owned_worker_job_backend=FakeOwnedWorkerJobBackend(),
            )
            ok = run_rvc_infer_subprocess(
                owned_in,
                owned_out,
                {
                    'model_path': str(model),
                    'device': 'cpu',
                    'python_path': str(python),
                    'worker_enabled': False,
                },
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            self.assertTrue(ok)
            self.assertEqual(len(runs), 1)
            self.assertIn('rvc_infer.py', ' '.join(runs[0]))
            mgr.shutdown()

class SharedPolicyAssemblyTests(unittest.TestCase):
    def test_assemble_text_shares_policy_without_voice_stack(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            cfg_path = Path(tmp) / "c.yaml"
            cfg_path.write_text(
                "ollama:\n  base_url: http://127.0.0.1:9\n  model: x\n"
                "security:\n  permissions:\n    subprocess: false\n"
                "    rvc_worker: true\n",
                encoding="utf-8",
            )

            class FakeConfig:
                def __init__(self, path=None):
                    self._data = {
                        "ollama.base_url": "http://127.0.0.1:9",
                        "ollama.model": "x",
                        "ollama.timeout": 1,
                        "ollama.keep_alive": "0",
                        "ollama.history.max_stored_turns": 10,
                        "system.windows_control_enabled": False,
                        "search": {},
                        "extensions.character_model": {"enabled": False},
                        "memory": {"enabled": False},
                        "resource": {},
                        "security": {
                            "permissions": {
                                "subprocess": False,
                                "rvc_worker": True,
                                "rvc_infer": True,
                                "rvc_probe": True,
                                "file_delete": True,
                            }
                        },
                    }

                def get(self, key, default=None):
                    return self._data.get(key, default)

                def get_rvc_config(self):
                    return None

            parts = assemble_default(
                startup_mode="text",
                config_loader=FakeConfig,
                logging_setup=lambda _c: None,
                dir_preparer=lambda: None,
                ollama_client_cls=lambda **k: SimpleNamespace(**k),
                start_cleaner=False,
                file_cleaner_from_config=lambda *a, **k: None,
            )
            self.assertIs(parts.safety_policy, parts.temp_file_manager.safety_policy)
            self.assertIsNone(parts.tts_engine)
            self.assertIsNone(parts.stt_engine)
            summary = parts.safety_policy.summary()
            self.assertIsInstance(summary, dict)
            self.assertTrue(all(isinstance(v, bool) for v in summary.values()))
            self.assertNotIn("argv", str(summary).lower())

    def test_voice_builders_share_same_policy_and_temp_manager(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            policy = SafetyPolicy(
                {"permissions": {"file_delete": True, "subprocess": False}}
            )
            mgr = TempFileManager(safety_policy=policy, default_dir=tmp)
            cfg = SimpleNamespace(
                get=lambda key, default=None: {
                    "voice.tts": {
                        "engine": "pyttsx3",
                        "fallback_enabled": False,
                        "pipeline_enabled": False,
                    },
                    "voice.stt": {"engine": "speech_recognition"},
                }.get(key, default),
                get_rvc_config=lambda: None,
            )
            with patch("src.voice.tts.engine.AudioPlayer"), patch(
                "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
            ), patch(
                "src.voice.tts.engine.TTSEngine._initialize_engine"
            ), patch(
                "src.voice.stt.STTEngine._initialize_engine"
            ):
                tts = build_tts_engine(
                    cfg,
                    policy,
                    warm_rvc_on_init=False,
                    temp_file_manager=mgr,
                )
                stt = build_stt_engine(
                    cfg,
                    safety_policy=policy,
                    temp_file_manager=mgr,
                )
                try:
                    self.assertIs(tts.safety_policy, policy)
                    self.assertIs(tts._temp_files, mgr)
                    self.assertIs(stt._temp_files, mgr)
                    self.assertIs(stt.safety_policy, policy)
                finally:
                    tts.shutdown(stop_rvc=False)
                    agg = tts.get_shutdown_status()
                    self.assertTrue(agg.complete)
                    tfm_status = mgr.shutdown()
                    self.assertTrue(getattr(tfm_status, "complete", False))
                    self.assertEqual(mgr.owned_count(), 0)
                    self.assertFalse(mgr.delayed_worker_alive())


class TempFileOwnershipTests(unittest.TestCase):
    def test_owned_exactly_once_and_rejects_unsafe(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = SafetyPolicy({"permissions": {"file_delete": True}})
            mgr = TempFileManager(
                safety_policy=policy, default_dir=root, delayed_queue_capacity=8
            )
            owned = mgr.create(".wav", directory=root)
            self.assertTrue(Path(owned).is_file())
            mgr.remove(owned)
            self.assertFalse(Path(owned).exists())
            mgr.remove(owned)  # idempotent

            preexisting = root / "preexist.wav"
            preexisting.write_text("x", encoding="utf-8")
            mgr.remove(str(preexisting))
            self.assertTrue(preexisting.exists())

            outside = Path(tmp).parent / "escape.wav"
            # 目录本身
            mgr.remove(str(root))
            self.assertTrue(root.exists())

            nested = root / "sub"
            nested.mkdir()
            mgr.remove(str(nested))
            self.assertTrue(nested.exists())

            mgr.shutdown()
            with self.assertRaises(RuntimeError):
                mgr.create(".wav", directory=root)

    def test_close_and_concurrent_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = SafetyPolicy({"permissions": {"file_delete": True}})
            mgr = TempFileManager(
                safety_policy=policy, default_dir=root, delayed_queue_capacity=4
            )
            errors: list = []

            def worker(i: int) -> None:
                try:
                    p = mgr.create(f".{i}.wav", directory=root)
                    mgr.remove(p)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            self.assertEqual(errors, [])
            before = sum(
                1 for t in threading.enumerate() if t.name == "TempFileDelayedDelete"
            )
            # 填满延迟队列也不增线程
            for i in range(10):
                p = mgr.create(".wav", directory=root)
                mgr.remove_delayed(p, delay=30.0, max_retries=1)
            after = sum(
                1 for t in threading.enumerate() if t.name == "TempFileDelayedDelete"
            )
            self.assertLessEqual(after, max(1, before + 1))
            mgr.shutdown(timeout=0.5)
            mgr.shutdown(timeout=0.1)


class SttWhisperTempCleanupTests(unittest.TestCase):
    def test_whisper_temp_uses_manager_and_preserves_error(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = SafetyPolicy({"permissions": {"file_delete": True}})
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            engine = STTEngine(
                engine="speech_recognition",
                provider=MagicMock(),
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            engine._model = MagicMock()
            engine.engine_type = "whisper"
            engine._model.transcribe.side_effect = RuntimeError("boom-transcribe")

            class _Audio:
                def get_wav_data(self):
                    return b"RIFF"

            result = engine._recognize_whisper_result(_Audio())
            self.assertEqual(result.status.name, "INTERNAL_ERROR")
            self.assertIn("boom-transcribe", result.error_detail or "")
            # 临时文件应已清理（目录中无残留 miku_）
            leftovers = list(root.glob("miku_*.wav"))
            self.assertEqual(leftovers, [])
            mgr.shutdown()


class SystemPowerAndRegistryTests(unittest.TestCase):
    def test_power_needs_permission_and_confirm(self) -> None:
        with _clear_dry_run():
            confirm = MagicMock(return_value=True)
            runner = MagicMock(return_value=_FakeCompleted(0))
            with patch.object(wc_mod, "confirm_system_action", confirm):
                policy = SafetyPolicy(
                    {"permissions": {"system_power": True, "subprocess": False}}
                )
                policy.bind_process_runners(run_fn=runner)
                controller = WindowsController(
                    safety_policy=policy, require_confirmation=True
                )
                result = controller.check_and_execute("关机")
                confirm.assert_called_once()
                runner.assert_called_once()
                self.assertIn("关机", result)

            confirm2 = MagicMock(return_value=True)
            runner2 = MagicMock()
            with patch.object(wc_mod, "confirm_system_action", confirm2):
                denied = SafetyPolicy(
                    {"permissions": {"system_power": False, "subprocess": True}}
                )
                denied.bind_process_runners(run_fn=runner2)
                controller = WindowsController(
                    safety_policy=denied, require_confirmation=True
                )
                out = controller.check_and_execute("关机")
                self.assertIn("拦截", out)
                confirm2.assert_not_called()
                runner2.assert_not_called()

    def test_registry_write_immutable_and_summary_safe(self) -> None:
        policy = SafetyPolicy(
            {"permissions": {"registry_write": True, "subprocess": True}}
        )
        self.assertFalse(policy.is_allowed(Operation.REGISTRY_WRITE))
        summary = policy.summary()
        self.assertFalse(summary.get("registry_write"))
        self.assertTrue(all(isinstance(v, bool) for v in summary.values()))


class EnsurePolicyPythonTests(unittest.TestCase):
    def test_mismatched_python_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.exe"
            b = root / "b.exe"
            a.write_bytes(b"")
            b.write_bytes(b"")
            policy = SafetyPolicy(
                {"permissions": {"rvc_infer": True}},
                trusted_rvc_profile=TrustedRvcProfile(python_path=a),
            )
            with self.assertRaises(PermissionError):
                ensure_policy_rvc_python(policy, {"python_path": str(b)})

    def test_lower_layer_cannot_register_or_seal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.exe"
            a.write_bytes(b"")
            policy = SafetyPolicy({"permissions": {"rvc_infer": True}})
            with self.assertRaises(PermissionError):
                policy.register_rvc_python(a)
            with self.assertRaises(PermissionError):
                policy.register_rvc_profile(python_path=a, model_path=a)
            with self.assertRaises(PermissionError):
                policy.seal_rvc_profile(python_path=a)


if __name__ == "__main__":
    unittest.main()
