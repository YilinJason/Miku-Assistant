"""Round 3A Codex F1–F7 反例与回归：纯离线 fake / TemporaryDirectory。"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.assistant import MikuAssistant
from src.core.assembly import AssembledParts
from src.core.rvc_capabilities import RvcInferLaunch, RvcProbeKind, RvcWorkerLaunch
from src.core.safety import (
    Operation,
    SafetyPolicy,
    TempFileShutdownStatus,
    TrustedRvcProfile,
)
from src.core.speech_queue import TurnSpeechPlayer
from src.voice.audio.temp_files import TempFileManager
from src.voice.rvc.converter import RVCConverter
from src.voice.rvc.infer import (
    get_rvc_worker,
    run_rvc_infer,
    run_rvc_infer_subprocess,
    stop_rvc_worker,
)
from src.voice.rvc.worker import RVCWorkerClient
from src.voice.tts.engine import TTSEngine

from tests.support.fake_owned_worker_job_backend import FakeOwnedWorkerJobBackend
from tests.support.local_tts_runners import bind_fake_rvc_runners
from tests.support.host_memory_fixtures import install_module_sufficient_host_memory


def setUpModule() -> None:
    install_module_sufficient_host_memory()


class _InferOkPopen:
    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self._code = 0
        try:
            out_i = self.argv.index("--output") + 1
            Path(self.argv[out_i]).write_bytes(b"rvc-out")
        except (ValueError, IndexError):
            pass

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        return self._code

    def terminate(self):
        self._code = 1

    def kill(self):
        self._code = 1


class _HangInferPopen:
    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self._code = None

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(self.argv, timeout)

    def terminate(self):
        self._code = 1

    def kill(self):
        self._code = 1


def _clear_dry_run():
    return patch.dict(os.environ, {"MIKU_DRY_RUN": ""}, clear=False)


def _allow_policy(**extra) -> SafetyPolicy:
    perms = {
        "subprocess": False,
        "rvc_worker": True,
        "rvc_infer": True,
        "rvc_probe": True,
        "file_delete": True,
        "system_power": False,
        "cloud_stt": True,
        "cloud_tts": True,
    }
    perms.update(extra)
    return SafetyPolicy({"permissions": perms})


def _sealed_policy(
    python: Path,
    model: Path,
    *,
    index: Path | None = None,
    **extra,
) -> SafetyPolicy:
    perms = {
        "subprocess": False,
        "rvc_worker": True,
        "rvc_infer": True,
        "rvc_probe": True,
        "file_delete": True,
        "system_power": False,
        "cloud_stt": True,
        "cloud_tts": True,
    }
    perms.update(extra)
    return SafetyPolicy(
        {"permissions": perms},
        trusted_rvc_profile=TrustedRvcProfile(
            python_path=python,
            model_path=model,
            index_path=index,
        ),
    )


def _deny_rvc_policy() -> SafetyPolicy:
    return SafetyPolicy(
        {
            "permissions": {
                "rvc_worker": False,
                "rvc_infer": False,
                "rvc_probe": False,
                "subprocess": False,
                "file_delete": True,
                "cloud_stt": True,
                "cloud_tts": True,
            }
        }
    )


class _FakeCompleted:
    def __init__(self, code: int = 0):
        self.returncode = code
        self.stdout = ""
        self.stderr = ""


class F1BarePolicyBypassTests(unittest.TestCase):
    def test_bare_policy_denies_rvc_capabilities(self) -> None:
        summary = SafetyPolicy().summary()
        self.assertFalse(summary["rvc_worker"])
        self.assertFalse(summary["rvc_infer"])
        self.assertFalse(summary["rvc_probe"])

    def test_omitted_policy_paths_never_call_fake_runner(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            model = root / "m.pth"
            model.write_bytes(b"x")
            called = {"n": 0}

            def boom(*_a, **_k):
                called["n"] += 1
                raise AssertionError("must not run")

            # 直接 Worker：缺策略
            with self.assertRaises(PermissionError):
                RVCWorkerClient({"device": "cpu", "model_path": str(model)})

            # 一次性推理缺策略
            self.assertFalse(
                run_rvc_infer_subprocess(
                    str(root / "in.wav"),
                    str(root / "out.wav"),
                    {"model_path": str(model), "device": "cpu"},
                )
            )

            # Converter 缺策略
            conv = RVCConverter({"model_path": str(model)})
            self.assertIsNone(conv.convert_file(str(root / "in.wav")))
            self.assertEqual(called["n"], 0)

            # 装配关闭后仍省略策略
            deny = _deny_rvc_policy()
            deny.bind_process_runners(run_fn=boom, popen_fn=boom)
            self.assertFalse(
                run_rvc_infer(
                    str(root / "in.wav"),
                    str(root / "out.wav"),
                    {"model_path": str(model), "worker_enabled": False},
                    None,
                )
            )
            self.assertEqual(called["n"], 0)


class F2WorkerPolicySwitchTests(unittest.TestCase):
    def tearDown(self) -> None:
        stop_rvc_worker()

    def test_allow_then_deny_stops_and_does_not_convert(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            model = root / "m.pth"
            model.write_bytes(b"x")
            (root / "py.exe").write_bytes(b"")
            allow = _sealed_policy(root / "py.exe", model)

            fake = MagicMock()
            fake.convert.return_value = True
            fake.safety_policy = allow
            fake.stop = MagicMock()

            with patch("src.voice.rvc.worker.RVCWorkerClient", return_value=fake):
                w1 = get_rvc_worker(
                    {"model_path": str(model), "device": "cpu"}, allow
                )
                self.assertIs(w1, fake)

            deny = _deny_rvc_policy()
            convert_called = {"n": 0}

            class DenyClient:
                def __init__(self, *a, **k):
                    self.safety_policy = deny
                    self.stop = MagicMock()
                    self.interrupt_current = MagicMock(return_value=False)

                def convert(self, *a, **k):
                    convert_called["n"] += 1
                    return True

                def bind_temp_file_manager(self, *_a):
                    return None

            with patch("src.voice.rvc.worker.RVCWorkerClient", DenyClient):
                ok = run_rvc_infer(
                    str(root / "in.wav"),
                    str(root / "out.wav"),
                    {"model_path": str(model), "worker_enabled": True},
                    deny,
                )
            self.assertFalse(ok)
            self.assertEqual(convert_called["n"], 0)
            fake.stop.assert_called()

    def test_same_policy_reuses_without_rebuild(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            model = root / "m.pth"
            model.write_bytes(b"x")
            (root / "py.exe").write_bytes(b"")
            allow = _sealed_policy(root / "py.exe", model)
            builds = {"n": 0}

            class Client:
                def __init__(self, *a, **k):
                    builds["n"] += 1
                    self.safety_policy = allow
                    self.stop = MagicMock()
                    self.interrupt_current = MagicMock(return_value=False)

                def bind_temp_file_manager(self, *_a):
                    return None

            cfg = {"model_path": str(model), "device": "cpu"}
            with patch("src.voice.rvc.worker.RVCWorkerClient", Client):
                a = get_rvc_worker(cfg, allow)
                b = get_rvc_worker(cfg, allow)
            self.assertIs(a, b)
            self.assertEqual(builds["n"], 1)
            stop_rvc_worker()

    def test_deny_then_allow_rebuilds(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            model = root / "m.pth"
            model.write_bytes(b"x")
            (root / "py.exe").write_bytes(b"")
            deny = _deny_rvc_policy()
            allow = _sealed_policy(root / "py.exe", model)
            instances = []

            class Client:
                def __init__(self, cfg, safety_policy=None, **k):
                    self.safety_policy = safety_policy
                    self.stop = MagicMock()
                    self.interrupt_current = MagicMock(return_value=False)
                    instances.append(self)

                def bind_temp_file_manager(self, *_a):
                    return None

            cfg = {"model_path": str(model), "device": "cpu"}
            with patch("src.voice.rvc.worker.RVCWorkerClient", Client):
                # deny 仍可构造客户端对象，但 convert/start 会拒
                get_rvc_worker(cfg, deny)
                get_rvc_worker(cfg, allow)
            self.assertEqual(len(instances), 2)
            instances[0].stop.assert_called()
            stop_rvc_worker()


class F3PathProfileTests(unittest.TestCase):
    def test_preexisting_output_and_unregistered_input_rejected(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            python = root / "python.exe"
            python.write_bytes(b"")
            model = root / "m.pth"
            model.write_bytes(b"x")
            policy = _sealed_policy(python, model)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            pops: list = []

            def fake_popen(argv, **kwargs):
                pops.append(list(argv))
                return _InferOkPopen(argv, **kwargs)

            bind_fake_rvc_runners(policy, popen_fn=fake_popen)

            preexisting = root / "preexisting-user-file.txt"
            preexisting.write_text("secret", encoding="utf-8")
            owned_in = mgr.create(".wav")

            launch = RvcInferLaunch(
                input_path=owned_in,
                output_path=str(preexisting),
                model_path=str(model),
                device="cpu",
                pitch=0,
                index_rate=0.55,
                protect=0.5,
                filter_radius=3,
                rms_mix_rate=0.75,
                f0_method="rmvpe",
            )
            with self.assertRaises(PermissionError):
                policy.run_rvc_infer(launch, temp_file_manager=mgr)
            self.assertEqual(pops, [])
            self.assertTrue(preexisting.exists())

            # 未登记输入
            owned_out = mgr.create(".wav")
            launch2 = RvcInferLaunch(
                input_path=str(root / "not-owned.wav"),
                output_path=owned_out,
                model_path=str(model),
                device="cpu",
                pitch=0,
                index_rate=0.55,
                protect=0.5,
                filter_radius=3,
                rms_mix_rate=0.75,
                f0_method="rmvpe",
            )
            with self.assertRaises(PermissionError):
                policy.run_rvc_infer(launch2, temp_file_manager=mgr)
            self.assertEqual(pops, [])

            # 模型替换
            other = root / "other.pth"
            other.write_bytes(b"y")
            launch3 = RvcInferLaunch(
                input_path=owned_in,
                output_path=owned_out,
                model_path=str(other),
                device="cpu",
                pitch=0,
                index_rate=0.55,
                protect=0.5,
                filter_radius=3,
                rms_mix_rate=0.75,
                f0_method="rmvpe",
            )
            with self.assertRaises(PermissionError):
                policy.run_rvc_infer(launch3, temp_file_manager=mgr)
            self.assertEqual(pops, [])

            # 合法 I/O 到达 runner
            policy.run_rvc_infer(
                RvcInferLaunch(
                    input_path=owned_in,
                    output_path=owned_out,
                    model_path=str(model),
                    device="cpu",
                    pitch=0,
                    index_rate=0.55,
                    protect=0.5,
                    filter_radius=3,
                    rms_mix_rate=0.75,
                    f0_method="rmvpe",
                ),
                temp_file_manager=mgr,
                capture_output=True,
            )
            self.assertEqual(len(pops), 1)
            mgr.shutdown()


class F4NoAdoptTests(unittest.TestCase):
    def test_cannot_register_or_delete_preexisting(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            preexisting = root / "preexisting.txt"
            preexisting.write_text("x", encoding="utf-8")
            self.assertFalse(hasattr(TempFileManager, "register_owned"))
            mgr.remove(str(preexisting))
            self.assertTrue(preexisting.exists())

            owned = mgr.create(".txt")
            mgr.remove(owned)
            self.assertFalse(Path(owned).exists())
            mgr.remove(owned)  # idempotent
            mgr.shutdown()


class F5AuthorizeBeforeMkdirTests(unittest.TestCase):
    def test_unauthorized_directory_not_created(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            outside = root / "outside_child_not_registered_as_root"
            # outside is under root which IS registered via default_dir — use sibling
        with TemporaryDirectory() as tmp1, TemporaryDirectory() as tmp2:
            trusted = Path(tmp1)
            untrusted = Path(tmp2) / "newdir"
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=trusted)
            with self.assertRaises(PermissionError):
                mgr.create(".wav", directory=untrusted)
            self.assertFalse(untrusted.exists())
            mgr.shutdown()


class F6CleanupMatrixAndShutdownOrderTests(unittest.TestCase):
    def test_edge_and_rvc_cleanup_matrix(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)

            with patch("src.voice.tts.engine.AudioPlayer"), patch(
                "src.voice.rvc.infer.stop_rvc_worker"
            ), patch(
                "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
            ):
                engine = TTSEngine(
                    engine="pyttsx3",
                    fallback_enabled=False,
                    pipeline_enabled=False,
                    rvc_config={},
                    safety_policy=policy,
                    temp_file_manager=mgr,
                    warm_rvc_on_init=False,
                )

            # edge 保存异常：create 的 mp3 必须被回收，owned 归零
            async def boom_save(_path):
                raise RuntimeError("edge-boom")

            with patch("edge_tts.Communicate") as comm:
                instance = MagicMock()
                instance.save = boom_save
                comm.return_value = instance
                with self.assertRaises(RuntimeError) as ctx:
                    import asyncio

                    asyncio.run(engine._edge_tts_to_mp3("hi"))
                self.assertIn("edge-boom", str(ctx.exception))
            self.assertEqual(mgr.owned_count(), 0)
            self.assertEqual(mgr.pending_delayed(), 0)

            # MP3→WAV 抛错：新建 wav 回收；原始业务异常保留
            mp3 = mgr.create(".mp3")
            with patch(
                "src.voice.tts.engine.mp3_to_rvc_wav",
                side_effect=RuntimeError("wav-boom"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    engine._mp3_to_wav(mp3, {})
                self.assertIn("wav-boom", str(ctx.exception))
            self.assertEqual(mgr.owned_count(), 1)  # 仅原 mp3 仍 owned
            self.assertTrue(mgr.is_owned(mp3))
            mgr.remove(mp3)
            self.assertEqual(mgr.owned_count(), 0)

            # RVC 拒绝路径：不掩盖、不泄漏输出
            model = root / "m.pth"
            model.write_bytes(b"x")
            deny = _deny_rvc_policy()
            conv = RVCConverter(
                {"model_path": str(model)},
                safety_policy=deny,
                temp_file_manager=mgr,
            )
            inp = mgr.create(".wav")
            self.assertIsNone(conv.convert_file(inp))
            self.assertEqual(mgr.owned_count(), 1)  # 仅输入仍 owned
            mgr.remove(inp)

            # 成功路径：fake infer 成功后输出仍 owned（交给上层清理）
            py = root / "python.exe"
            py.write_bytes(b"")
            allow = _sealed_policy(py, model)
            allow.bind_process_runners(
                run_fn=lambda *a, **k: _FakeCompleted(0),
                popen_fn=lambda *a, **k: MagicMock(),
            )
            mgr2 = TempFileManager(safety_policy=allow, default_dir=root)
            conv_ok = RVCConverter(
                {
                    "model_path": str(model),
                    "device": "cpu",
                    "python_path": str(py),
                    "worker_enabled": False,
                },
                safety_policy=allow,
                temp_file_manager=mgr2,
            )
            owned_in = mgr2.create(".wav")
            Path(owned_in).write_bytes(b"RIFF")
            with patch(
                "src.voice.rvc.converter.run_rvc_infer", return_value=True
            ), patch(
                "src.voice.rvc.converter.polish_rvc_wav"
            ):
                out = conv_ok.convert_file(owned_in)
            self.assertIsNotNone(out)
            self.assertTrue(mgr2.is_owned(out))
            mgr2.remove_many(owned_in, out)
            self.assertEqual(mgr2.owned_count(), 0)

            # shutdown：pending=0，worker 不存活，不能新登记
            engine.shutdown(stop_rvc=False)
            mgr.shutdown()
            mgr2.shutdown()
            self.assertEqual(mgr.pending_delayed(), 0)
            self.assertFalse(mgr.delayed_worker_alive())
            with self.assertRaises(RuntimeError):
                mgr.create(".wav")

    def test_shutdown_order_speech_then_tts_then_temp(self) -> None:
        order: list[str] = []

        class FakePlayer:
            def stop_accepting(self):
                order.append("speech_stop")

            def shutdown(self, timeout=2.0):
                order.append("speech_shutdown")
                return True

        class FakeTTS:
            def shutdown(self, stop_rvc=True):
                order.append("tts")

        class FakeTemp:
            def shutdown(self, timeout=2.0):
                order.append("temp")
                return TempFileShutdownStatus(
                    complete=True, owned=0, pending=0, inflight=0, worker_alive=False, active_io_leases=0
                )

        parts = AssembledParts(
            config=SimpleNamespace(get=lambda *a, **k: None),
            safety_policy=_deny_rvc_policy(),
            log_file=None,
            llm_client=None,
            tts_engine=FakeTTS(),
            stt_engine=None,
            windows_controller=None,
            chrome_searcher=None,
            character_interface=None,
            file_cleaner=None,
            router=MagicMock(),
            temp_file_manager=FakeTemp(),
        )
        assistant = MikuAssistant.from_dependencies(
            parts.config,
            safety_policy=parts.safety_policy,
            llm_client=None,
            tts_engine=parts.tts_engine,
            stt_engine=None,
            windows_controller=None,
            chrome_searcher=None,
            character_interface=None,
            file_cleaner=None,
            router=parts.router,
        )
        assistant._temp_file_manager = parts.temp_file_manager  # noqa: SLF001
        assistant.temp_file_manager = parts.temp_file_manager
        assistant._speech_player = FakePlayer()  # noqa: SLF001
        assistant.tts_engine = parts.tts_engine
        assistant.shutdown()
        self.assertEqual(order, ["speech_stop", "speech_shutdown", "tts", "temp"])
        self.assertTrue(assistant._shutdown_flow_done)  # noqa: SLF001

    def test_speech_shutdown_timeout_is_retryable(self) -> None:
        calls = {"n": 0}

        class FlakyPlayer:
            def stop_accepting(self):
                return None

            def shutdown(self, timeout=2.0):
                calls["n"] += 1
                return calls["n"] >= 2

        class CountingTTS:
            def __init__(self):
                self.n = 0

            def shutdown(self, stop_rvc=True):
                self.n += 1

        class CountingTemp:
            def __init__(self):
                self.n = 0

            def shutdown(self, timeout=2.0):
                self.n += 1
                return TempFileShutdownStatus(
                    complete=True, owned=0, pending=0, inflight=0, worker_alive=False, active_io_leases=0
                )

        tts = CountingTTS()
        temp = CountingTemp()
        assistant = MikuAssistant.from_dependencies(
            SimpleNamespace(get=lambda *a, **k: None),
            safety_policy=_deny_rvc_policy(),
            router=MagicMock(),
            tts_engine=tts,
        )
        assistant._temp_file_manager = temp  # noqa: SLF001
        assistant.temp_file_manager = temp
        assistant._speech_player = FlakyPlayer()  # noqa: SLF001

        with self.assertLogs(assistant.logger, level="ERROR") as cm:
            assistant.shutdown()
        self.assertTrue(
            any("可再次 shutdown 重试" in r or "speech_player" in r for r in cm.output)
        )
        self.assertFalse(assistant._shutdown_flow_done)  # noqa: SLF001
        self.assertFalse(assistant._non_memory_shutdown_done)  # noqa: SLF001
        self.assertIsNotNone(assistant._speech_player)  # noqa: SLF001
        # F14：speech 未完成时不得关闭 TTS/Temp
        self.assertEqual(tts.n, 0)
        self.assertEqual(temp.n, 0)
        self.assertEqual(calls["n"], 1)

        assistant.shutdown()
        self.assertEqual(calls["n"], 2)
        self.assertEqual(tts.n, 1)  # exactly-once after speech ok
        self.assertEqual(temp.n, 1)
        self.assertTrue(assistant._shutdown_flow_done)  # noqa: SLF001
        self.assertIsNone(assistant._speech_player)  # noqa: SLF001

    def test_speech_shutdown_permanent_false_stays_incomplete(self) -> None:
        class AlwaysSlow:
            def stop_accepting(self):
                return None

            def shutdown(self, timeout=2.0):
                return False

        class CountingTTS:
            def __init__(self):
                self.n = 0

            def shutdown(self, stop_rvc=True):
                self.n += 1

        class CountingTemp:
            def __init__(self):
                self.n = 0

            def shutdown(self, timeout=2.0):
                self.n += 1
                return TempFileShutdownStatus(
                    complete=True, owned=0, pending=0, inflight=0, worker_alive=False, active_io_leases=0
                )

        tts = CountingTTS()
        temp = CountingTemp()
        assistant = MikuAssistant.from_dependencies(
            SimpleNamespace(get=lambda *a, **k: None),
            safety_policy=_deny_rvc_policy(),
            router=MagicMock(),
            tts_engine=tts,
        )
        assistant._temp_file_manager = temp  # noqa: SLF001
        assistant.temp_file_manager = temp
        assistant._speech_player = AlwaysSlow()  # noqa: SLF001
        assistant.shutdown()
        assistant.shutdown()
        self.assertEqual(tts.n, 0)
        self.assertEqual(temp.n, 0)
        self.assertFalse(assistant._shutdown_flow_done)  # noqa: SLF001
        self.assertIsNotNone(assistant._speech_player)  # noqa: SLF001


class F1FromAppConfigStillEnablesRvc(unittest.TestCase):
    def test_from_app_config_defaults_enable_rvc(self) -> None:
        class Cfg:
            def get(self, key, default=None):
                if key == "security":
                    return {"permissions": {"subprocess": False}}
                return default

        policy = SafetyPolicy.from_app_config(Cfg())
        self.assertTrue(policy.is_allowed(Operation.RVC_WORKER))
        self.assertTrue(policy.is_allowed(Operation.RVC_INFER))
        self.assertTrue(policy.is_allowed(Operation.RVC_PROBE))
        self.assertFalse(policy.is_allowed(Operation.SUBPROCESS))


class F8SealProfileAndIdentityTests(unittest.TestCase):
    def test_unsealed_worker_infer_probe_never_call_runner(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"")
            model.write_bytes(b"x")
            called = {"n": 0}

            def boom(*_a, **_k):
                called["n"] += 1
                raise AssertionError("runner must not run")

            policy = _allow_policy()
            policy.bind_process_runners(run_fn=boom, popen_fn=boom)
            cfg = {
                "model_path": str(model),
                "device": "cpu",
                "python_path": str(py),
                "worker_enabled": False,
            }
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            inp = mgr.create(".wav")
            out = mgr.create(".wav")
            self.assertFalse(
                run_rvc_infer_subprocess(inp, out, cfg, policy, temp_file_manager=mgr)
            )
            client = RVCWorkerClient(cfg, safety_policy=policy, temp_file_manager=mgr)
            self.assertFalse(client.start())
            with self.assertRaises(PermissionError):
                policy.run_rvc_probe(RvcProbeKind.IMPORT_RVC)
            self.assertEqual(called["n"], 0)
            mgr.shutdown()

    def test_lower_layer_cannot_register_or_seal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"")
            model.write_bytes(b"x")
            unsealed = _allow_policy()
            with self.assertRaises(PermissionError):
                unsealed.seal_rvc_profile(python_path=py, model_path=model)
            with self.assertRaises(PermissionError):
                unsealed.register_rvc_python(py)
            with self.assertRaises(PermissionError):
                unsealed.register_rvc_profile(python_path=py, model_path=model)
            sealed = _sealed_policy(py, model)
            with self.assertRaises(PermissionError):
                sealed.seal_rvc_profile(python_path=py, model_path=model)
            other = root / "other.pth"
            other.write_bytes(b"y")
            with self.assertRaises(PermissionError):
                sealed.seal_rvc_profile(python_path=py, model_path=other)

    def test_same_path_replacement_rejected_before_runner(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"py1")
            model.write_bytes(b"m1")
            policy = _sealed_policy(py, model)
            called = {"n": 0}

            def boom(*_a, **_k):
                called["n"] += 1
                raise AssertionError("must not run")

            policy.bind_process_runners(run_fn=boom, popen_fn=boom)
            # 同名删除重建
            model.unlink()
            model.write_bytes(b"m2-replaced")
            with self.assertRaises(PermissionError):
                policy.assert_rvc_launch_assets(model_path=model)
            self.assertEqual(called["n"], 0)

    def test_in_place_model_mutation_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"py1")
            model.write_bytes(b"m1")
            policy = _sealed_policy(py, model)
            # 原地改写同 inode：严格元数据应拒绝
            model.write_bytes(b"m1-mutated-in-place!!")
            with self.assertRaises(PermissionError):
                policy.assert_rvc_launch_assets(model_path=model)


class F9WorkerConvertRequiresManagerTests(unittest.TestCase):
    def test_convert_without_manager_fail_closed(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"")
            model.write_bytes(b"x")
            policy = _sealed_policy(py, model)
            started = {"n": 0}

            client = RVCWorkerClient(
                {"model_path": str(model), "device": "cpu", "python_path": str(py)},
                safety_policy=policy,
                temp_file_manager=None,
            )

            def fake_start():
                started["n"] += 1
                return True

            client._start_locked = fake_start  # noqa: SLF001
            self.assertFalse(client.convert(str(root / "a.wav"), str(root / "b.wav")))
            self.assertEqual(started["n"], 0)

    def test_convert_requires_owned_identity(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"")
            model.write_bytes(b"x")
            policy = _sealed_policy(py, model)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            client = RVCWorkerClient(
                {"model_path": str(model), "device": "cpu", "python_path": str(py)},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            started = {"n": 0}
            client._start_locked = lambda: started.__setitem__("n", started["n"] + 1) or True  # noqa: SLF001
            # 外部路径
            self.assertFalse(client.convert(str(root / "x.wav"), str(root / "y.wav")))
            self.assertEqual(started["n"], 0)
            # 合法 owned
            a = mgr.create(".wav")
            b = mgr.create(".wav")
            # 同名替换输入
            Path(a).unlink()
            Path(a).write_bytes(b"replaced")
            self.assertFalse(client.convert(a, b))
            self.assertEqual(started["n"], 0)
            mgr.shutdown()


class F10TempIdentityOwnershipTests(unittest.TestCase):
    def test_missing_and_same_name_replacement_not_deleted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            Path(path).unlink()
            # 缺失：assert 拒绝，所有权撤销
            with self.assertRaises(PermissionError):
                mgr.assert_owned_regular_file(path, label="输入")
            self.assertFalse(mgr.is_owned(path))
            self.assertEqual(mgr.owned_count(), 0)

            path2 = mgr.create(".wav")
            Path(path2).unlink()
            replacement = Path(path2)
            replacement.write_text("new-content", encoding="utf-8")
            # 同名新建：不得删除替代物
            mgr.remove(path2)
            self.assertTrue(replacement.exists())
            self.assertEqual(replacement.read_text(encoding="utf-8"), "new-content")
            self.assertEqual(mgr.owned_count(), 0)

            # hardlink 替换（若平台支持）
            path3 = mgr.create(".wav")
            target = root / "other.wav"
            target.write_text("hardlink-target", encoding="utf-8")
            try:
                Path(path3).unlink()
                os.link(str(target), path3)
            except OSError:
                mgr.remove(path3)
                mgr.shutdown()
                self.skipTest("hardlink not supported")
            with self.assertRaises(PermissionError):
                mgr.assert_owned_regular_file(path3, label="输入")
            mgr.remove(path3)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "hardlink-target")
            self.assertTrue(Path(path3).exists())  # 替代 hardlink 未被删
            mgr.shutdown()

    def test_delayed_queue_replacement_not_deleted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            identity_path = path
            # 占用模拟：直接入队后替换
            with mgr._lock:  # noqa: SLF001
                expected = mgr._owned[path]  # noqa: SLF001
                mgr._enqueue_delayed_locked(path, expected, 0.05, 2)  # noqa: SLF001
            Path(path).unlink()
            Path(path).write_text("queued-replacement", encoding="utf-8")
            import time

            time.sleep(0.4)
            self.assertTrue(Path(identity_path).exists())
            self.assertEqual(
                Path(identity_path).read_text(encoding="utf-8"), "queued-replacement"
            )
            mgr.shutdown()
            self.assertEqual(mgr.pending_delayed(), 0)


class F12ConcurrentShutdownCleanupTests(unittest.TestCase):
    def test_shutdown_reclaims_long_delay_queue(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            Path(path).write_bytes(b"audio")
            mgr.remove_delayed(path, delay=30.0, max_retries=2)
            import time

            t0 = time.perf_counter()
            status = mgr.shutdown(timeout=0.8)
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 2.0)
            self.assertTrue(status.complete)
            self.assertEqual(status.owned, 0)
            self.assertEqual(status.pending, 0)
            self.assertEqual(status.inflight, 0)
            self.assertFalse(status.worker_alive)
            self.assertFalse(Path(path).exists())

    def test_shutdown_incomplete_when_remove_permanently_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            path = mgr.create(".wav")
            Path(path).write_bytes(b"locked")

            real_remove = os.remove

            def boom(p):
                if str(Path(p).resolve()) == str(Path(path).resolve()):
                    raise PermissionError("simulated lock")
                return real_remove(p)

            with patch("src.voice.audio.temp_files.os.remove", side_effect=boom):
                status = mgr.shutdown(timeout=0.3)
            self.assertFalse(status.complete)
            self.assertGreaterEqual(status.owned, 1)
            self.assertTrue(Path(path).exists())
            # 第二次：解除占用后应能完成
            status2 = mgr.shutdown(timeout=0.5)
            self.assertTrue(status2.complete)
            self.assertEqual(status2.owned, 0)
            self.assertFalse(Path(path).exists())


class F15TempShutdownContractTests(unittest.TestCase):
    def test_retry_after_lock_then_success(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            paths = [mgr.create(".wav") for _ in range(3)]
            for p in paths:
                Path(p).write_bytes(b"x")
                mgr.remove_delayed(p, delay=20.0, max_retries=2)
            attempts = {"n": 0}
            real_remove = os.remove

            def flaky(p):
                attempts["n"] += 1
                if attempts["n"] <= 2:
                    raise OSError(32, "busy")
                return real_remove(p)

            with patch("src.voice.audio.temp_files.os.remove", side_effect=flaky):
                s1 = mgr.shutdown(timeout=0.4)
            s2 = mgr.shutdown(timeout=1.0)
            self.assertTrue(s2.complete or s1.complete)
            final = mgr.shutdown_status()
            self.assertEqual(final.owned, 0)
            self.assertEqual(final.pending, 0)
            self.assertFalse(final.worker_alive)


class F19ShutdownBudgetTests(unittest.TestCase):
    def test_deadline_stops_batch_drain_and_keeps_retryable_pending(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(
                safety_policy=policy, default_dir=root, delayed_queue_capacity=64
            )
            paths = []
            for _ in range(24):
                p = mgr.create(".wav")
                Path(p).write_bytes(b"x")
                paths.append(p)
                mgr.remove_delayed(p, delay=30.0, max_retries=1)

            calls = {"n": 0}
            block = {"on": True}
            real_remove = os.remove

            def slow_remove(p):
                calls["n"] += 1
                if block["on"]:
                    time.sleep(0.03)
                return real_remove(p)

            import time

            with patch("src.voice.audio.temp_files.os.remove", side_effect=slow_remove):
                t0 = time.perf_counter()
                status = mgr.shutdown(timeout=0.05)
                elapsed = time.perf_counter() - t0
                calls_after = calls["n"]
                time.sleep(0.2)
                # deadline 后不得继续批量取删
                self.assertEqual(calls["n"], calls_after)

            self.assertFalse(status.complete)
            self.assertGreater(status.owned + status.pending, 0)
            self.assertLess(elapsed, 0.35)
            # 第二次：解除阻塞后应可完成
            block["on"] = False
            status2 = mgr.shutdown(timeout=2.0)
            self.assertTrue(status2.complete)
            self.assertEqual(status2.owned, 0)
            self.assertEqual(status2.pending, 0)
            self.assertFalse(status2.worker_alive)
            for p in paths:
                self.assertFalse(Path(p).exists())


class F16SpeakCleanupTests(unittest.TestCase):
    def test_play_failure_still_clears_temps(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)

            class BoomPlayer:
                def play(self, path):
                    raise RuntimeError("speaker-boom")

                def stop(self):
                    return None

            with patch("src.voice.tts.engine.AudioPlayer", return_value=BoomPlayer()), patch(
                "src.voice.rvc.infer.stop_rvc_worker"
            ), patch(
                "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
            ):
                engine = TTSEngine(
                    engine="edge-tts",
                    fallback_enabled=False,
                    pipeline_enabled=False,
                    rvc_config={},
                    safety_policy=policy,
                    temp_file_manager=mgr,
                    warm_rvc_on_init=False,
                )
            engine._player = BoomPlayer()  # noqa: SLF001

            async def fake_save(path):
                Path(path).write_bytes(b"mp3")

            with patch("edge_tts.Communicate") as comm, patch(
                "src.voice.tts.engine.mp3_to_rvc_wav",
                side_effect=lambda src, dst, cfg=None: Path(dst).write_bytes(b"wav"),
            ):
                instance = MagicMock()
                instance.save = fake_save
                comm.return_value = instance
                with self.assertRaises(RuntimeError) as ctx:
                    engine._speak_single("hello")
                self.assertIn("speaker-boom", str(ctx.exception))
            self.assertEqual(mgr.owned_count(), 0)
            status = mgr.shutdown(timeout=0.5)
            self.assertTrue(status.complete)

    def test_interrupt_clears_single_temps(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)

            class OkPlayer:
                def play(self, path):
                    return None

                def stop(self):
                    return None

            with patch("src.voice.tts.engine.AudioPlayer", return_value=OkPlayer()), patch(
                "src.voice.rvc.infer.stop_rvc_worker"
            ), patch(
                "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
            ):
                engine = TTSEngine(
                    engine="edge-tts",
                    fallback_enabled=False,
                    pipeline_enabled=False,
                    rvc_config={},
                    safety_policy=policy,
                    temp_file_manager=mgr,
                    warm_rvc_on_init=False,
                )
            engine._player = OkPlayer()  # noqa: SLF001
            engine._interrupt_event.set()  # noqa: SLF001

            async def fake_save(path):
                Path(path).write_bytes(b"mp3")

            with patch("edge_tts.Communicate") as comm, patch(
                "src.voice.tts.engine.mp3_to_rvc_wav",
                side_effect=lambda src, dst, cfg=None: Path(dst).write_bytes(b"wav"),
            ):
                instance = MagicMock()
                instance.save = fake_save
                comm.return_value = instance
                engine._speak_single("hello")
            self.assertEqual(mgr.owned_count(), 0)
            status = mgr.shutdown(timeout=0.5)
            self.assertTrue(status.complete)


class F20PipelineLateProducerTests(unittest.TestCase):
    def test_late_producer_after_player_error_self_cleans(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            gate = threading.Event()
            saves = {"n": 0}

            class BoomPlayer:
                def play(self, path):
                    raise RuntimeError("pipeline-player-boom")

                def stop(self):
                    return None

            with patch("src.voice.tts.engine.AudioPlayer", return_value=BoomPlayer()), patch(
                "src.voice.rvc.infer.stop_rvc_worker"
            ), patch(
                "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
            ):
                engine = TTSEngine(
                    engine="edge-tts",
                    fallback_enabled=False,
                    pipeline_enabled=True,
                    rvc_config={},
                    safety_policy=policy,
                    temp_file_manager=mgr,
                    warm_rvc_on_init=False,
                )
            engine._player = BoomPlayer()  # noqa: SLF001

            async def gated_save(path):
                saves["n"] += 1
                if saves["n"] >= 2:
                    gate.wait(timeout=3.0)
                Path(path).write_bytes(b"mp3")

            with patch("edge_tts.Communicate") as comm, patch(
                "src.voice.tts.engine.mp3_to_rvc_wav",
                side_effect=lambda src, dst, cfg=None: Path(dst).write_bytes(b"wav"),
            ):
                instance = MagicMock()
                instance.save = gated_save
                comm.return_value = instance
                with self.assertRaises(RuntimeError) as ctx:
                    engine._speak_pipeline(["one", "two"])
                self.assertIn("pipeline-player-boom", str(ctx.exception))
                gate.set()
                # 给 late producer 有界时间完成自清理
                time.sleep(0.3)
                for _ in range(10):
                    st = mgr.shutdown(timeout=0.3)
                    if st.complete:
                        break
                    time.sleep(0.05)
            final = mgr.shutdown_status()
            self.assertEqual(final.owned, 0)
            self.assertEqual(final.pending, 0)
            self.assertFalse(final.worker_alive)

    def test_pipeline_cancel_mid_synth_clears(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            started = threading.Event()

            class OkPlayer:
                def play(self, path):
                    return None

                def stop(self):
                    return None

            with patch("src.voice.tts.engine.AudioPlayer", return_value=OkPlayer()), patch(
                "src.voice.rvc.infer.stop_rvc_worker"
            ), patch(
                "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
            ):
                engine = TTSEngine(
                    engine="edge-tts",
                    fallback_enabled=False,
                    pipeline_enabled=True,
                    rvc_config={},
                    safety_policy=policy,
                    temp_file_manager=mgr,
                    warm_rvc_on_init=False,
                )
            engine._player = OkPlayer()  # noqa: SLF001

            async def slow_save(path):
                started.set()
                time.sleep(0.15)
                if engine._interrupted():  # noqa: SLF001
                    raise RuntimeError("cancelled-synth")
                Path(path).write_bytes(b"mp3")

            with patch("edge_tts.Communicate") as comm, patch(
                "src.voice.tts.engine.mp3_to_rvc_wav",
                side_effect=lambda src, dst, cfg=None: Path(dst).write_bytes(b"wav"),
            ):
                instance = MagicMock()
                instance.save = slow_save
                comm.return_value = instance

                def cancel_soon():
                    started.wait(timeout=2.0)
                    engine._interrupt_event.set()  # noqa: SLF001

                threading.Thread(target=cancel_soon, daemon=True).start()
                try:
                    engine._speak_pipeline(["a", "b", "c"])
                except RuntimeError:
                    pass  # 合成中取消可能表面为合成异常；仍须回收
            for _ in range(10):
                st = mgr.shutdown(timeout=0.3)
                if st.complete:
                    break
            self.assertEqual(mgr.owned_count(), 0)
            self.assertEqual(mgr.pending_delayed(), 0)


class F21OwnedManagerRetryTests(unittest.TestCase):
    def test_owned_manager_incomplete_is_retryable(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            policy = _allow_policy()
            calls = {"n": 0}
            statuses = [
                TempFileShutdownStatus(
                    complete=False, owned=1, pending=0, inflight=0, worker_alive=False, active_io_leases=0
                ),
                TempFileShutdownStatus(
                    complete=True, owned=0, pending=0, inflight=0, worker_alive=False, active_io_leases=0
                ),
            ]

            class FakeMgr:
                def shutdown(self, timeout=2.0):
                    idx = min(calls["n"], len(statuses) - 1)
                    calls["n"] += 1
                    return statuses[idx]

            with patch("src.voice.tts.engine.AudioPlayer"), patch(
                "src.voice.rvc.infer.stop_rvc_worker"
            ), patch(
                "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
            ), patch(
                "src.voice.tts.engine.TempFileManager", return_value=FakeMgr()
            ):
                engine = TTSEngine(
                    engine="pyttsx3",
                    fallback_enabled=False,
                    pipeline_enabled=False,
                    rvc_config={},
                    safety_policy=policy,
                    temp_file_manager=None,
                    warm_rvc_on_init=False,
                )
            engine._owns_temp_files = True  # noqa: SLF001
            engine._temp_files = FakeMgr()  # noqa: SLF001
            engine._temp_files_shutdown_done = False  # noqa: SLF001
            engine.shutdown(stop_rvc=False)
            self.assertFalse(engine._temp_files_shutdown_done)  # noqa: SLF001
            self.assertEqual(calls["n"], 1)
            engine.shutdown(stop_rvc=False)
            self.assertTrue(engine._temp_files_shutdown_done)  # noqa: SLF001
            self.assertEqual(calls["n"], 2)

    def test_owned_manager_permanent_incomplete_never_marks_done(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            policy = _allow_policy()
            calls = {"n": 0}

            class ForeverIncomplete:
                def shutdown(self, timeout=2.0):
                    calls["n"] += 1
                    return TempFileShutdownStatus(
                        complete=False,
                        owned=1,
                        pending=1,
                        inflight=0,
                        worker_alive=False, active_io_leases=0,
                    )

            with patch("src.voice.tts.engine.AudioPlayer"), patch(
                "src.voice.rvc.infer.stop_rvc_worker"
            ), patch(
                "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
            ):
                engine = TTSEngine(
                    engine="pyttsx3",
                    fallback_enabled=False,
                    pipeline_enabled=False,
                    rvc_config={},
                    safety_policy=policy,
                    temp_file_manager=None,
                    warm_rvc_on_init=False,
                )
            engine._owns_temp_files = True  # noqa: SLF001
            engine._temp_files = ForeverIncomplete()  # noqa: SLF001
            engine._temp_files_shutdown_done = False  # noqa: SLF001
            engine.shutdown(stop_rvc=False)
            engine.shutdown(stop_rvc=False)
            self.assertEqual(calls["n"], 2)
            self.assertFalse(engine._temp_files_shutdown_done)  # noqa: SLF001


class F17LifecycleMatrixTests(unittest.TestCase):
    def test_rvc_oneshot_success_path(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"")
            model.write_bytes(b"x")
            policy = _sealed_policy(py, model)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            pops = {"n": 0}

            def fake_popen(argv, **kwargs):
                pops["n"] += 1
                return _InferOkPopen(argv, **kwargs)

            bind_fake_rvc_runners(policy, popen_fn=fake_popen)
            owned_in = mgr.create(".wav")
            Path(owned_in).write_bytes(b"in")
            owned_out = mgr.create(".wav")
            ok = run_rvc_infer_subprocess(
                owned_in,
                owned_out,
                {
                    "model_path": str(model),
                    "device": "cpu",
                    "python_path": str(py),
                    "worker_enabled": False,
                },
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            self.assertTrue(ok)
            self.assertEqual(pops["n"], 1)
            mgr.remove_many(owned_in, owned_out)
            status = mgr.shutdown(timeout=0.5)
            self.assertTrue(status.complete)

    def test_rvc_oneshot_timeout_returns_false_and_keeps_owned_for_cleanup(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"")
            model.write_bytes(b"x")
            policy = _sealed_policy(py, model)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            pops = {"n": 0}

            def fake_popen(argv, **kwargs):
                pops["n"] += 1
                return _HangInferPopen(argv, **kwargs)

            bind_fake_rvc_runners(policy, popen_fn=fake_popen)
            owned_in = mgr.create(".wav")
            Path(owned_in).write_bytes(b"in")
            owned_out = mgr.create(".wav")
            ok = run_rvc_infer_subprocess(
                owned_in,
                owned_out,
                {
                    "model_path": str(model),
                    "device": "cpu",
                    "python_path": str(py),
                    "subprocess_timeout": 0.01,
                    "worker_enabled": False,
                },
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            self.assertFalse(ok)
            self.assertEqual(pops["n"], 1)
            # Converter 风格回收输出
            mgr.remove(owned_out)
            self.assertFalse(mgr.is_owned(owned_out))
            mgr.remove(owned_in)
            status = mgr.shutdown(timeout=0.5)
            self.assertTrue(status.complete)

    def test_rvc_worker_timeout_and_cancel(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"")
            model.write_bytes(b"x")
            policy = _sealed_policy(py, model)
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            client = RVCWorkerClient(
                {"model_path": str(model), "device": "cpu", "python_path": str(py)},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            proc = MagicMock()
            proc.stdin = MagicMock()
            proc.stdout = MagicMock()
            proc.stderr = MagicMock()
            proc.poll.return_value = None
            client._proc = proc  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            client._start_locked = lambda: True  # noqa: SLF001

            def _exit_after_terminate(*_a, **_k) -> None:
                proc.poll.return_value = 0

            proc.terminate.side_effect = _exit_after_terminate
            proc.kill.side_effect = _exit_after_terminate

            owned_in = mgr.create(".wav")
            owned_out = mgr.create(".wav")
            with patch(
                "src.voice.rvc.worker._read_json_line", return_value=None
            ):
                ok = client.convert(owned_in, owned_out, timeout=0.05)
            self.assertFalse(ok)
            self.assertTrue(proc.terminate.called or proc.kill.called)

            # cancel 路径
            client2 = RVCWorkerClient(
                {"model_path": str(model), "device": "cpu", "python_path": str(py)},
                safety_policy=policy,
                temp_file_manager=mgr,
            )
            proc2 = MagicMock()
            proc2.stdin = MagicMock()
            proc2.stdout = MagicMock()
            proc2.stderr = MagicMock()
            proc2.poll.return_value = None

            def _mark_exited() -> None:
                proc2.poll.return_value = 0

            proc2.terminate.side_effect = _mark_exited
            client2._proc = proc2  # noqa: SLF001
            client2._ready = True  # noqa: SLF001
            client2._converting.set()  # noqa: SLF001
            self.assertTrue(client2.interrupt_current())
            self.assertIsNone(client2._proc)  # noqa: SLF001
            mgr.remove_many(owned_in, owned_out)
            status = mgr.shutdown(timeout=0.5)
            self.assertTrue(status.complete)

    def test_edge_success_path_clears_after_speak(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)

            class OkPlayer:
                def play(self, path):
                    self.last = path

                def stop(self):
                    return None

            player = OkPlayer()
            with patch("src.voice.tts.engine.AudioPlayer", return_value=player), patch(
                "src.voice.rvc.infer.stop_rvc_worker"
            ), patch(
                "src.voice.tts.engine.TTSEngine.prepare_rvc", return_value=False
            ):
                engine = TTSEngine(
                    engine="edge-tts",
                    fallback_enabled=False,
                    pipeline_enabled=False,
                    rvc_config={},
                    safety_policy=policy,
                    temp_file_manager=mgr,
                    warm_rvc_on_init=False,
                )
            engine._player = player  # noqa: SLF001

            async def fake_save(path):
                Path(path).write_bytes(b"mp3")

            with patch("edge_tts.Communicate") as comm, patch(
                "src.voice.tts.engine.mp3_to_rvc_wav",
                side_effect=lambda src, dst, cfg=None: Path(dst).write_bytes(b"wav"),
            ):
                instance = MagicMock()
                instance.save = fake_save
                comm.return_value = instance
                engine._speak_single("ok")
            self.assertEqual(mgr.owned_count(), 0)
            status = mgr.shutdown(timeout=0.5)
            self.assertTrue(status.complete)

    def test_concurrent_shutdown_with_pending_owned(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = _allow_policy()
            mgr = TempFileManager(safety_policy=policy, default_dir=root)
            paths = [mgr.create(".wav") for _ in range(5)]
            for p in paths:
                Path(p).write_bytes(b"z")
                mgr.remove_delayed(p, delay=5.0, max_retries=1)
            results = []

            def closer():
                results.append(mgr.shutdown(timeout=1.0))

            threads = [threading.Thread(target=closer) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=3.0)
            final = mgr.shutdown(timeout=1.0)
            self.assertTrue(final.complete)
            self.assertEqual(final.owned, 0)
            self.assertEqual(final.pending, 0)


class F18ConstructorOnlySealTests(unittest.TestCase):
    def test_published_policy_cannot_seal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"")
            model.write_bytes(b"x")
            policy = SafetyPolicy({"permissions": {"rvc_worker": True}})
            with self.assertRaises(PermissionError):
                policy.seal_rvc_profile(python_path=py, model_path=model)
            self.assertFalse(policy.rvc_profile_sealed())

    def test_constructor_seal_is_immutable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / "python.exe"
            model = root / "m.pth"
            py.write_bytes(b"")
            model.write_bytes(b"x")
            policy = _sealed_policy(py, model)
            self.assertTrue(policy.rvc_profile_sealed())
            with self.assertRaises(PermissionError):
                policy.seal_rvc_profile(python_path=py, model_path=model)


if __name__ == "__main__":
    unittest.main()
