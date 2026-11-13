"""CosyVoice 生产后端切片 B：worker/client、协议、取消与生命周期（纯离线 fake）。"""
from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import threading
import time
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core.safety import SafetyPolicy, TrustedLocalTtsProfile
from src.voice.audio.temp_files import TempFileManager
from src.voice.cosyvoice.client import CosyVoiceWorkerClient
from src.core.paths import PROJECT_ROOT, TMP_DIR
from src.voice.cosyvoice.protocol import (
    ERR_INSUFFICIENT_RAM,
    ERR_PROTOCOL,
    ERR_RESOURCE_UNOBSERVABLE,
    SCHEMA_VERSION,
    STDOUT_QUEUE_CAPACITY,
    STARTUP_PHASE_BEGIN,
    STARTUP_PHASE_END,
    STARTUP_STAGES,
    StartupProgressAccumulator,
    StartupProgressReporter,
    build_ready_message,
    build_error_message,
    build_shutdown_request,
    build_startup_progress_message,
    build_synth_ok,
    build_synth_request,
    dumps_message,
    parse_json_line,
    validate_outbound_message,
)
from src.voice.cosyvoice.stdin_writer import CosyVoiceStdinWriter
from src.voice.cosyvoice.settings import CosyVoiceSettings, parse_cosyvoice_settings
from src.core.parent_process import parent_process_is_alive
from src.voice.cosyvoice.bounded_line import BoundedLineTooLongError, read_bounded_line
from src.voice.cosyvoice.protocol import ERR_SCHEMA
from src.voice.cosyvoice.stdout_reader import CosyVoiceStdoutReader
from src.voice.cosyvoice.worker_admission import run_resource_admission
from src.voice.cosyvoice.worker_cli import parse_worker_cli
from src.voice.cosyvoice.worker_inbound import parse_inbound_line
from src.voice.cosyvoice.cross_file_identity import capture_portable_file_identity
from src.voice.cosyvoice.output_verify import verify_synth_output
from src.voice.cosyvoice.path_trust import is_reparse_or_link
from src.voice.cosyvoice.worker_output import (
    collect_inference_speech,
    run_inference_instruct_to_wav,
    validate_worker_output_path,
)
from src.voice.cosyvoice.worker_runtime import (
    StdinReaderFatal,
    run_worker_loop,
    stdin_reader_thread,
)
from tests.support.cosyvoice_blocked_write import (
    BlockedWriteTeardown,
    BlockingFakeStdin,
    SYNTH_THREAD_JOIN_SEC,
    thread_body_capture,
    wait_write_entered,
    WRITE_ENTERED_RENDEZVOUS_SEC,
)
from tests.support.local_tts_runners import bind_fake_local_tts_runners
from tests.support.host_memory_fixtures import install_module_sufficient_host_memory
from tests.test_cosyvoice_production_slice_a import _policy_with_local_tts


def setUpModule() -> None:
    install_module_sufficient_host_memory()


def _portable_pair(path: Path | str) -> tuple[int, int]:
    ident = capture_portable_file_identity(path)
    return ident.dev, ident.ino


def _settings():
    cfg = parse_cosyvoice_settings({"engine": "cosyvoice"})
    assert cfg is not None
    return cfg


def _fast_settings() -> CosyVoiceSettings:
    base = _settings()
    return CosyVoiceSettings(
        startup_timeout_sec=5,
        synth_timeout_sec=10,
        idle_timeout_sec=60,
        max_text_chars=base.max_text_chars,
        cpu_threads=base.cpu_threads,
        min_free_ram_mb=base.min_free_ram_mb,
        min_free_vram_mb=base.min_free_vram_mb,
        cuda_memory_limit_mb=base.cuda_memory_limit_mb,
    )


class _LineQueueStream:
    """线程安全的字节流，模拟 stdout 管道（支持有界 read(1)）。"""

    def __init__(self) -> None:
        self._chunks: queue.Queue[str] = queue.Queue()
        self._pending = ""

    def write(self, data: str) -> int:
        if data:
            self._chunks.put(data)
        return len(data)

    def flush(self) -> None:
        return None

    def read(self, n: int = -1) -> str:
        if n != 1:
            raise NotImplementedError("仅支持 read(1)")
        if self._pending:
            ch = self._pending[0]
            self._pending = self._pending[1:]
            return ch
        try:
            chunk = self._chunks.get(timeout=0.5)
        except queue.Empty:
            return ""
        if not chunk:
            return ""
        self._pending = chunk
        ch = self._pending[0]
        self._pending = self._pending[1:]
        return ch

    def readline(self) -> str:
        parts: list[str] = []
        while True:
            ch = self.read(1)
            if not ch:
                break
            parts.append(ch)
            if ch == "\n":
                break
        return "".join(parts)

    def close(self) -> None:
        return None


class _EofSignallingLineQueueStream:
    """仅 EOF 用例：无数据时阻塞，直到显式 close() 才向 read 返回 EOF。"""

    def __init__(self) -> None:
        self._chunks: queue.Queue[str] = queue.Queue()
        self._pending = ""
        self._eof = threading.Event()
        self._eof_delivered = threading.Event()
        self._cond = threading.Condition()

    def write(self, data: str) -> int:
        with self._cond:
            if data:
                self._chunks.put(data)
            self._cond.notify_all()
        return len(data)

    def flush(self) -> None:
        return None

    def read(self, n: int = -1) -> str:
        if n != 1:
            raise NotImplementedError("仅支持 read(1)")
        with self._cond:
            while True:
                if self._pending:
                    ch = self._pending[0]
                    self._pending = self._pending[1:]
                    return ch
                try:
                    chunk = self._chunks.get_nowait()
                except queue.Empty:
                    if self._eof.is_set():
                        self._eof_delivered.set()
                        return ""
                    self._cond.wait()
                    continue
                if not chunk:
                    continue
                self._pending = chunk
                ch = self._pending[0]
                self._pending = self._pending[1:]
                return ch

    def readline(self) -> str:
        parts: list[str] = []
        while True:
            ch = self.read(1)
            if not ch:
                break
            parts.append(ch)
            if ch == "\n":
                break
        return "".join(parts)

    def close(self) -> None:
        with self._cond:
            self._eof.set()
            self._cond.notify_all()


class _FakeStdin:
    def __init__(self) -> None:
        self._lines: queue.Queue[str] = queue.Queue()
        self._closed = False

    def write(self, data: str) -> int:
        if data:
            self._lines.put(data)
        return len(data)

    def flush(self) -> None:
        return None

    def readline(self) -> str:
        while not self._closed:
            try:
                return self._lines.get(timeout=0.1)
            except queue.Empty:
                continue
        return ""

    def close(self) -> None:
        self._closed = True


_FAKE_WORKER_REGISTRY: list["_ProtocolWorkerProcess"] = []


def _shutdown_all_fake_workers(join_timeout: float = 2.0) -> None:
    for proc in list(_FAKE_WORKER_REGISTRY):
        proc.stop(join_timeout=join_timeout)
    _FAKE_WORKER_REGISTRY.clear()


def _register_tfm_shutdown(
    case: unittest.TestCase,
    tfm: TempFileManager,
    *,
    client: CosyVoiceWorkerClient | None = None,
    procs: list["_ProtocolWorkerProcess"] | None = None,
) -> None:
    """先结束 client / fake worker，再 shutdown 本测试登记的 TFM（仅清理 owned 文件）。"""

    extra_procs = list(procs or [])

    def _cleanup() -> None:
        import src.voice.cosyvoice.client as client_mod

        if client is not None:
            try:
                client.shutdown(timeout=0.5)
            except Exception:
                pass
        else:
            inst = client_mod._ACTIVE_CLIENT
            if inst is not None:
                try:
                    inst.shutdown(timeout=0.5)
                except Exception:
                    pass
                client_mod._ACTIVE_CLIENT = None
        for proc in extra_procs:
            try:
                proc.stop(join_timeout=2.0)
            except Exception:
                pass
        _shutdown_all_fake_workers(join_timeout=2.0)
        try:
            tfm.shutdown()
        except Exception:
            pass

    case.addCleanup(_cleanup)


def _tiny_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\0\0" * 160)


class _ProtocolWorkerProcess:
    """线程模拟 worker：stdout JSON 协议 + 可选写输出 WAV。"""

    def __init__(
        self,
        *,
        ready_delay: float = 0.0,
        ready_delay_after_start: bool = False,
        hang_after_ready: bool = False,
        fail_output: bool = False,
        exit_on_synth: bool = False,
        hold_synth: threading.Event | None = None,
        spawn_gate: threading.Event | None = None,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _LineQueueStream()
        self.stderr = io.StringIO()
        self._terminated = False
        self._killed = False
        self._ready_delay = ready_delay
        self._hang = hang_after_ready
        self._fail_output = fail_output
        self._exit_on_synth = exit_on_synth
        self._hold_synth = hold_synth
        self._spawn_gate = spawn_gate
        self._ready_delay_after_start = ready_delay_after_start
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        _FAKE_WORKER_REGISTRY.append(self)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=False)
        self._thread.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        self.stdin.close()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                raise AssertionError("fake worker 线程未在时限内退出")

    def _emit_ready(self) -> None:
        if not self._hang:
            self.stdout.write(build_ready_message(1.0))

    def _run(self) -> None:
        if self._spawn_gate is not None:
            while not self._spawn_gate.is_set():
                if self._stop.wait(0.05):
                    return
        if self._stop.is_set():
            return
        if self._ready_delay > 0 and not self._ready_delay_after_start:
            if self._stop.wait(self._ready_delay):
                return
        if self._stop.is_set():
            return
        self._emit_ready()
        if self._ready_delay_after_start and self._ready_delay > 0:
            if self._stop.wait(self._ready_delay):
                return
        while not self._stop.is_set():
            line = self.stdin.readline()
            if not line:
                if self._stop.is_set() or self.stdin._closed:
                    break
                continue
            kind, inbound = parse_inbound_line(line, max_text_chars=240)
            if kind == "shutdown":
                break
            if kind != "synth" or inbound is None:
                continue
            if self._fail_output:
                continue
            if self._hold_synth is not None:
                while not self._hold_synth.is_set():
                    if self._stop.wait(0.05):
                        break
                if self._stop.is_set():
                    break
            nbytes = 0
            try:
                out = Path(inbound.output_path)
                if out.is_file() and not self._fail_output:
                    _tiny_wav(out)
                    nbytes = out.stat().st_size
            except OSError:
                pass
            if nbytes <= 0:
                continue
            self.stdout.write(
                build_synth_ok(
                    request_id=inbound.request_id,
                    epoch=inbound.epoch,
                    duration_ms=1.0,
                    bytes_written=nbytes,
                )
            )
            self.stdout.flush()
            if self._exit_on_synth:
                break

    def poll(self):
        thread = self._thread
        if self._killed:
            if thread is None or not thread.is_alive():
                return -9
            return None
        if thread is not None and thread.is_alive():
            return None
        if thread is None and not self._terminated:
            return None
        return 0

    def wait(self, timeout=None):
        if self.poll() is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return 0

    def terminate(self) -> None:
        self._terminated = True
        self._stop.set()
        self.stdin.close()

    def kill(self) -> None:
        self._killed = True
        self._stop.set()
        self.stdin.close()


class _BlockedWriteWorkerProcess(_ProtocolWorkerProcess):
    """阻塞写入专用 fake worker：显式 EOF stdout + 可延迟 ready / 可延迟退出。"""

    def __init__(
        self,
        *,
        ready_gate: threading.Event | None = None,
        worker_exit_gate: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.stdin = BlockingFakeStdin()
        self.stdout = _EofSignallingLineQueueStream()
        self._ready_gate = ready_gate
        self._worker_exit_gate = worker_exit_gate
        self.exit_waiting = threading.Event()

    def _emit_ready(self) -> None:
        gate = self._ready_gate
        if gate is not None:
            gate.wait(WRITE_ENTERED_RENDEZVOUS_SEC)
        super()._emit_ready()

    def _close_stdout_eof(self) -> None:
        close = getattr(self.stdout, "close", None)
        if callable(close):
            close()

    def _run(self) -> None:
        try:
            super()._run()
        finally:
            gate = self._worker_exit_gate
            if gate is not None:
                self.exit_waiting.set()
                gate.wait(WRITE_ENTERED_RENDEZVOUS_SEC)
            self._close_stdout_eof()

    def wait(self, timeout=None):
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        code = self.poll()
        if code is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return code

    def terminate(self) -> None:
        super().terminate()
        self._close_stdout_eof()

    def kill(self) -> None:
        super().kill()
        self._close_stdout_eof()


class _StuckProcess(_ProtocolWorkerProcess):
    def terminate(self) -> None:
        return

    def kill(self) -> None:
        return


class _BlockingStdout:
    def __init__(self) -> None:
        self._release = threading.Event()

    def readline(self) -> str:
        self._release.wait(timeout=2.0)
        return ""

    def close(self) -> None:
        self._release.set()


class CosyVoiceProtocolTests(unittest.TestCase):
    def test_chinese_roundtrip(self) -> None:
        line = dumps_message(
            {"schema": SCHEMA_VERSION, "type": "ready", "load_ms": 1.0}
        )
        payload = parse_json_line(line)
        validate_outbound_message(payload)

    def test_worker_cli_rejects_unknown_flag(self) -> None:
        base = [
            "--parent-pid",
            "1",
            "--startup-timeout-sec",
            "30",
            "--synth-timeout-sec",
            "20",
            "--idle-timeout-sec",
            "60",
            "--max-text-chars",
            "240",
            "--cpu-threads",
            "4",
            "--min-free-ram-mb",
            "8192",
            "--min-free-vram-mb",
            "4096",
            "--cuda-memory-limit-mb",
            "4096",
            "--evil",
            "1",
        ]
        with self.assertRaises(ValueError):
            parse_worker_cli(base)

    def test_inbound_rejects_unknown_style(self) -> None:
        line = json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "type": "synth",
                "request_id": "r1",
                "epoch": 1,
                "style_id": "not_a_style",
                "text": "你好",
                "output_path": "x",
                "output_dev": 1,
                "output_ino": 2,
            },
            ensure_ascii=False,
        )
        kind, _ = parse_inbound_line(line + "\n", max_text_chars=240)
        self.assertEqual(kind, "invalid")


class CosyVoiceClientLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pin_patch = patch(
            "src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}
        )
        self._pin_patch.start()

    def tearDown(self) -> None:
        self._pin_patch.stop()
        import src.voice.cosyvoice.client as client_mod

        inst = client_mod._ACTIVE_CLIENT
        if inst is not None:
            try:
                inst.shutdown(timeout=0.5)
            except Exception:
                pass
        client_mod._ACTIVE_CLIENT = None
        _shutdown_all_fake_workers(join_timeout=2.0)

    def _client_with_fake(
        self, tmp: str, proc: _ProtocolWorkerProcess
    ) -> tuple[CosyVoiceWorkerClient, SafetyPolicy, TempFileManager]:
        policy, _root = _policy_with_local_tts(tmp)

        def _popen(*_a, **_k):
            proc.start()
            return proc

        bind_fake_local_tts_runners(policy, popen_fn=_popen)
        tfm = TempFileManager(safety_policy=policy)
        client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
        _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
        return client, policy, tfm

    def test_construct_has_no_side_effects(self) -> None:
        with TemporaryDirectory() as tmp, patch("subprocess.Popen") as popen_mock:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy)
            _register_tfm_shutdown(self, tfm)
            CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            popen_mock.assert_not_called()

    def test_single_instance_guard(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess()

            def _popen(*_a, **_k):
                proc.start()
                return proc

            policy, _ = _policy_with_local_tts(tmp)
            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            first = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=first, procs=[proc])
            with self.assertRaises(RuntimeError):
                CosyVoiceWorkerClient(_fast_settings(), policy, tfm)

    def test_synth_success_writes_owned_output(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess()
            client, _policy, tfm = self._client_with_fake(tmp, proc)
            out = tfm.create(".wav")
            ok = client.synthesize(
                "你好",
                style_id="warm_companion",
                output_path=out,
            )
            self.assertTrue(ok)
            self.assertGreater(Path(out).stat().st_size, 0)

    def test_busy_rejects_second_concurrent_request(self) -> None:
        with TemporaryDirectory() as tmp:
            hold = threading.Event()
            proc = _ProtocolWorkerProcess(hold_synth=hold)
            client, _policy, tfm = self._client_with_fake(tmp, proc)
            out1 = tfm.create(".wav")
            out2 = tfm.create(".wav")
            results: list[bool] = []

            def _first() -> None:
                results.append(
                    client.synthesize(
                        "第一",
                        style_id="warm_companion",
                        output_path=out1,
                    )
                )

            t = threading.Thread(target=_first)
            t.start()
            time.sleep(0.15)
            self.assertFalse(
                client.synthesize(
                    "第二",
                    style_id="warm_companion",
                    output_path=out2,
                )
            )
            hold.set()
            t.join(timeout=3.0)
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0])

    def test_retire_worker_blocks_begin_request_during_proc_wait(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class _WaitingProc:
            stdin = None
            stdout = None
            stderr = None
            running = True

            def poll(self):
                return None if self.running else 0

            def terminate(self):
                pass

            def kill(self):
                self.running = False
                release.set()

            def wait(self, timeout=None):
                entered.set()
                release.wait(timeout)
                self.running = False
                return 0

        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy)
            client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            client._proc = _WaitingProc()
            _register_tfm_shutdown(self, tfm, client=client)
            try:
                retire_thread = threading.Thread(
                    target=lambda: client.retire_worker(0.5), daemon=True
                )
                retire_thread.start()
                self.assertTrue(entered.wait(1.0))
                accepted = client._begin_request(client._snapshot_epoch())
                self.assertFalse(accepted)
                if accepted:
                    client._end_request()
                release.set()
                retire_thread.join(1.0)
            finally:
                release.set()
                client.shutdown(0.2)

    def test_text_length_bound(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess()
            client, _policy, tfm = self._client_with_fake(tmp, proc)
            out = tfm.create(".wav")
            self.assertFalse(
                client.synthesize(
                    "x" * 300,
                    style_id="warm_companion",
                    output_path=out,
                )
            )

    def test_non_owned_output_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess()
            client, _policy, _tfm = self._client_with_fake(tmp, proc)
            rogue = Path(tmp) / "rogue.wav"
            _tiny_wav(rogue)
            self.assertFalse(
                client.synthesize(
                    "你好",
                    style_id="warm_companion",
                    output_path=str(rogue),
                )
            )

    def test_synth_rejects_replacement_during_portable_capture_before_spawn(self) -> None:
        with TemporaryDirectory(prefix="cosy-owner-race-", dir=TMP_DIR) as tmp:
            spawns: list[int] = []
            proc = _ProtocolWorkerProcess()
            policy, _ = _policy_with_local_tts(tmp)

            def _popen(*_a, **_k):
                spawns.append(1)
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy, default_dir=tmp)
            client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            out = Path(tfm.create(".wav"))
            hold = Path(tmp) / "held_original.wav"
            real_capture = capture_portable_file_identity

            def _replace_then_capture(path):
                out.rename(hold)
                out.touch()
                return real_capture(path)

            with (
                patch(
                    "src.voice.cosyvoice.client.capture_portable_file_identity",
                    side_effect=_replace_then_capture,
                ),
                patch("subprocess.Popen") as popen_mock,
            ):
                self.assertFalse(
                    client.synthesize(
                        "你好",
                        style_id="warm_companion",
                        output_path=str(out),
                    )
                )
                popen_mock.assert_not_called()
            self.assertEqual(spawns, [])
            self.assertFalse(tfm.is_owned(out))

    def test_synth_rejects_dotdot_alias_before_spawn(self) -> None:
        with TemporaryDirectory() as tmp:
            spawns: list[int] = []
            proc = _ProtocolWorkerProcess()
            policy, _ = _policy_with_local_tts(tmp)

            def _popen(*_a, **_k):
                spawns.append(1)
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            owned = Path(tfm.create(".wav"))
            dotdot_alias = str(
                owned.parent / ".." / owned.parent.name / owned.name
            )
            self.assertEqual(Path(dotdot_alias).resolve(), owned.resolve())
            self.assertFalse(
                client.synthesize(
                    "你好",
                    style_id="warm_companion",
                    output_path=dotdot_alias,
                )
            )
            self.assertEqual(spawns, [])

            dev, ino = _portable_pair(owned)

            def _validate_canonical_key(path_str: str, dev: int, ino: int, **kwargs):
                return validate_worker_output_path(
                    str(owned.resolve()),
                    dev,
                    ino,
                    **kwargs,
                )

            self.assertIsNotNone(
                _validate_canonical_key(dotdot_alias, dev, ino)
            )
            client.shutdown(timeout=0.5)
            proc.stop(join_timeout=2.0)
            import src.voice.cosyvoice.client as client_mod

            client_mod._ACTIVE_CLIENT = None
            proc_old = _ProtocolWorkerProcess()
            spawns_old: list[int] = []

            def _popen_old(*_a, **_k):
                spawns_old.append(1)
                proc_old.start()
                return proc_old

            policy_old, _ = _policy_with_local_tts(tmp)
            bind_fake_local_tts_runners(policy_old, popen_fn=_popen_old)
            tfm_old = TempFileManager(safety_policy=policy_old)
            client_old = CosyVoiceWorkerClient(_fast_settings(), policy_old, tfm_old)
            _register_tfm_shutdown(
                self, tfm_old, client=client_old, procs=[proc_old]
            )
            owned_old = Path(tfm_old.create(".wav"))
            dotdot_old = str(
                owned_old.parent
                / ".."
                / owned_old.parent.name
                / owned_old.name
            )

            def _validate_canonical_key_old(
                path_str: str, dev: int, ino: int, **kwargs
            ):
                return validate_worker_output_path(
                    str(owned_old.resolve()),
                    dev,
                    ino,
                    **kwargs,
                )

            with patch(
                "src.voice.cosyvoice.client.validate_worker_output_path",
                side_effect=_validate_canonical_key_old,
            ):
                self.assertTrue(
                    client_old.synthesize(
                        "你好",
                        style_id="warm_companion",
                        output_path=dotdot_old,
                    )
                )
            self.assertNotEqual(spawns_old, [])

    def test_synth_rejects_reparse_alias_before_spawn(self) -> None:
        with TemporaryDirectory() as tmp:
            spawns: list[int] = []
            proc = _ProtocolWorkerProcess()
            policy, _ = _policy_with_local_tts(tmp)

            def _popen(*_a, **_k):
                spawns.append(1)
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            owned = tfm.create(".wav")
            canonical = str(Path(owned).resolve())
            lexical_alias = str(Path(owned).parent / "cv_reparse_lexical_alias.wav")
            self.assertNotEqual(lexical_alias, owned)
            self.assertNotEqual(lexical_alias, canonical)
            real_assert_owned = tfm.assert_owned_regular_file
            real_is_reparse = is_reparse_or_link

            def _assert_owned_alias(path, *, label: str) -> str:
                if str(path) == lexical_alias:
                    return canonical
                return real_assert_owned(path, label=label)

            def _alias_is_reparse(path: Path) -> bool:
                if str(path) == lexical_alias:
                    return True
                return real_is_reparse(path)

            with (
                patch.object(
                    tfm,
                    "assert_owned_regular_file",
                    side_effect=_assert_owned_alias,
                ),
                patch(
                    "src.voice.cosyvoice.worker_output.is_reparse_or_link",
                    side_effect=_alias_is_reparse,
                ),
            ):
                self.assertFalse(
                    client.synthesize(
                        "你好",
                        style_id="warm_companion",
                        output_path=lexical_alias,
                    )
                )
            self.assertEqual(spawns, [])

            dev, ino = _portable_pair(canonical)

            def _validate_canonical_key(path_str: str, dev: int, ino: int, **kwargs):
                return validate_worker_output_path(
                    canonical,
                    dev,
                    ino,
                    **kwargs,
                )

            self.assertIsNotNone(
                _validate_canonical_key(lexical_alias, dev, ino)
            )
            client.shutdown(timeout=0.5)
            proc.stop(join_timeout=2.0)
            import src.voice.cosyvoice.client as client_mod

            client_mod._ACTIVE_CLIENT = None
            proc_old = _ProtocolWorkerProcess()
            spawns_old: list[int] = []

            def _popen_old(*_a, **_k):
                spawns_old.append(1)
                proc_old.start()
                return proc_old

            policy_old, _ = _policy_with_local_tts(tmp)
            bind_fake_local_tts_runners(policy_old, popen_fn=_popen_old)
            tfm_old = TempFileManager(safety_policy=policy_old)
            client_old = CosyVoiceWorkerClient(_fast_settings(), policy_old, tfm_old)
            _register_tfm_shutdown(
                self, tfm_old, client=client_old, procs=[proc_old]
            )
            owned_old = tfm_old.create(".wav")
            canonical_old = str(Path(owned_old).resolve())
            alias_old = str(
                Path(owned_old).parent / "cv_reparse_lexical_alias_old.wav"
            )
            real_assert_old = tfm_old.assert_owned_regular_file

            def _assert_owned_old(path, *, label: str) -> str:
                if str(path) == alias_old:
                    return canonical_old
                return real_assert_old(path, label=label)

            def _is_reparse_old(path: Path) -> bool:
                if str(path) == alias_old:
                    return True
                return real_is_reparse(path)

            def _validate_canonical_key_old(
                path_str: str, dev: int, ino: int, **kwargs
            ):
                return validate_worker_output_path(
                    canonical_old,
                    dev,
                    ino,
                    **kwargs,
                )

            with (
                patch.object(
                    tfm_old,
                    "assert_owned_regular_file",
                    side_effect=_assert_owned_old,
                ),
                patch(
                    "src.voice.cosyvoice.worker_output.is_reparse_or_link",
                    side_effect=_is_reparse_old,
                ),
                patch(
                    "src.voice.cosyvoice.client.validate_worker_output_path",
                    side_effect=_validate_canonical_key_old,
                ),
            ):
                self.assertTrue(
                    client_old.synthesize(
                        "你好",
                        style_id="warm_companion",
                        output_path=alias_old,
                    )
                )
            self.assertNotEqual(spawns_old, [])

    def test_cancel_during_cold_start(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess(hang_after_ready=True)
            cancel = threading.Event()

            def _popen(*_a, **_k):
                proc.start()
                cancel.set()
                return proc

            policy, _ = _policy_with_local_tts(tmp)
            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            out = tfm.create(".wav")

            def _run() -> None:
                client.synthesize(
                    "你好",
                    style_id="warm_companion",
                    output_path=out,
                    cancel_event=cancel,
                )

            t = threading.Thread(target=_run)
            t.start()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())

    def test_interrupt_drops_late_result(self) -> None:
        with TemporaryDirectory() as tmp:
            hold = threading.Event()
            proc = _ProtocolWorkerProcess(hold_synth=hold)
            client, _policy, tfm = self._client_with_fake(tmp, proc)
            out = tfm.create(".wav")
            result: list[bool] = []

            def _run() -> None:
                result.append(
                    client.synthesize(
                        "晚到",
                        style_id="warm_companion",
                        output_path=out,
                    )
                )

            t = threading.Thread(target=_run)
            t.start()
            time.sleep(0.1)
            client.interrupt()
            hold.set()
            t.join(timeout=3.0)
            self.assertEqual(len(result), 1)
            self.assertFalse(result[0])

    def test_stop_failure_blocks_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            stuck = _StuckProcess()
            stuck.start()
            client, _policy, tfm = self._client_with_fake(tmp, stuck)
            out = tfm.create(".wav")
            client._proc = stuck  # noqa: SLF001
            client._reader = CosyVoiceStdoutReader(stuck.stdout)  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            with patch.object(client, "_active_request", True):
                client._terminate_worker_bounded()  # noqa: SLF001
            self.assertTrue(client.stop_failed)
            self.assertFalse(
                client.synthesize(
                    "你好",
                    style_id="warm_companion",
                    output_path=out,
                )
            )

    def test_shutdown_race_with_start(self) -> None:
        with TemporaryDirectory() as tmp:
            popen_barrier = threading.Event()
            proc = _ProtocolWorkerProcess(
                ready_delay=2.0,
                ready_delay_after_start=True,
            )
            policy, _ = _policy_with_local_tts(tmp)

            def _popen(*_a, **_k):
                popen_barrier.wait(timeout=5.0)
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            out = tfm.create(".wav")

            def _synth() -> None:
                client.synthesize(
                    "你好",
                    style_id="warm_companion",
                    output_path=out,
                )

            t = threading.Thread(target=_synth)
            t.start()
            time.sleep(0.08)
            status = client.shutdown(timeout=0.5)
            popen_barrier.set()
            t.join(timeout=3.0)
            proc.stop(join_timeout=2.0)
            self.assertFalse(
                client.synthesize(
                    "关闭后",
                    style_id="warm_companion",
                    output_path=tfm.create(".wav"),
                )
            )
            self.assertFalse(status.worker_alive)
            self.assertFalse(client._ready)
            import src.voice.cosyvoice.client as client_mod

            if status.complete:
                self.assertIsNone(client_mod._ACTIVE_CLIENT)
            else:
                self.assertIs(client_mod._ACTIVE_CLIENT, client)

    def test_startup_busy_rejects_immediately(self) -> None:
        with TemporaryDirectory() as tmp:
            gate = threading.Event()
            proc = _ProtocolWorkerProcess(
                spawn_gate=gate,
                ready_delay=2.0,
                ready_delay_after_start=True,
            )
            client, _policy, tfm = self._client_with_fake(tmp, proc)
            out1 = tfm.create(".wav")
            out2 = tfm.create(".wav")
            started = time.perf_counter()

            def _first() -> None:
                client.synthesize(
                    "第一",
                    style_id="warm_companion",
                    output_path=out1,
                )

            t = threading.Thread(target=_first)
            t.start()
            time.sleep(0.08)
            gate.set()
            self.assertFalse(
                client.synthesize(
                    "第二",
                    style_id="warm_companion",
                    output_path=out2,
                )
            )
            self.assertLess(time.perf_counter() - started, 1.0)
            t.join(timeout=5.0)
            proc.stop(join_timeout=2.0)

    def test_fake_empty_output_not_success(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _ProtocolWorkerProcess(fail_output=True)
            client, _policy, tfm = self._client_with_fake(tmp, proc)
            out = tfm.create(".wav")
            self.assertFalse(
                client.synthesize(
                    "你好",
                    style_id="warm_companion",
                    output_path=out,
                )
            )

    def test_stdout_reader_single_thread_and_queue_cap(self) -> None:
        stream = io.StringIO()
        for _ in range(STDOUT_QUEUE_CAPACITY + 4):
            stream.write(build_ready_message(1.0))
        stream.seek(0)
        reader = CosyVoiceStdoutReader(stream)
        reader.start()
        time.sleep(0.2)
        self.assertTrue(reader.is_alive())
        reader.close()
        self.assertTrue(reader.join(timeout=1.0))

    def test_reader_timeout_not_blocked_forever(self) -> None:
        stream = _BlockingStdout()
        reader = CosyVoiceStdoutReader(stream)
        reader.start()
        started = time.perf_counter()
        self.assertIsNone(reader.poll_message(timeout=0.05))
        self.assertLess(time.perf_counter() - started, 0.5)
        reader.close()
        stream.close()


class CosyVoiceBoundedIoTests(unittest.TestCase):
    def test_read_bounded_line_respects_max(self) -> None:
        stream = io.StringIO("a" * 20 + "\n")
        with self.assertRaises(BoundedLineTooLongError):
            read_bounded_line(stream, 8)

    def test_read_bounded_no_newline_eof(self) -> None:
        stream = io.StringIO("abc")
        self.assertEqual(read_bounded_line(stream, 16), "abc")

    def test_stdout_reader_eof_without_newline_is_fatal(self) -> None:
        stream = io.StringIO('{"schema":1,"type":"ready","load_ms":1.0}')
        reader = CosyVoiceStdoutReader(stream)
        reader.start()
        time.sleep(0.15)
        self.assertTrue(reader.had_fatal_protocol_error())
        reader.close()
        reader.join(timeout=1.0)


class CosyVoiceStdoutProtocolTests(unittest.TestCase):
    def test_invalid_schema_bool_is_fatal(self) -> None:
        stream = io.StringIO(
            json.dumps({"schema": True, "type": "ready", "load_ms": 1.0}) + "\n"
        )
        reader = CosyVoiceStdoutReader(stream)
        reader.start()
        time.sleep(0.15)
        self.assertTrue(reader.had_fatal_protocol_error())
        reader.close()
        reader.join(timeout=1.0)

    def test_oversized_line_without_newline_is_fatal(self) -> None:
        stream = io.StringIO("{" + "x" * 20000)
        reader = CosyVoiceStdoutReader(stream)
        reader.start()
        time.sleep(0.15)
        self.assertTrue(reader.had_fatal_protocol_error())
        reader.close()
        reader.join(timeout=1.0)


class CosyVoiceWorkerLoopTests(unittest.TestCase):
    def test_fake_generator_writes_wav(self) -> None:
        import numpy as np

        class _FakeModel:
            sample_rate = 16000

            def inference_instruct(self, *_a, **_k):
                def _gen():
                    yield {
                        "tts_speech": np.zeros(320, dtype=np.float32),
                    }

                return _gen()

        with TemporaryDirectory() as tmp:
            policy, _ = _policy_with_local_tts(tmp)
            tfm = TempFileManager(safety_policy=policy)
            _register_tfm_shutdown(self, tfm)
            out = tfm.create(".wav")
            dev, ino = _portable_pair(out)
            q: queue.Queue[str | None] = queue.Queue()
            lines: list[str] = []

            def _emit(line: str) -> None:
                lines.append(line)

            req = json.dumps(
                {
                    "schema": SCHEMA_VERSION,
                    "type": "synth",
                    "request_id": "r1",
                    "epoch": 0,
                    "style_id": "warm_companion",
                    "text": "你好",
                    "output_path": out,
                    "output_dev": dev,
                    "output_ino": ino,
                },
                ensure_ascii=False,
            ) + "\n"
            q.put(req)
            q.put(None)
            settings = _fast_settings()
            run_worker_loop(
                model=_FakeModel(),
                settings=settings,
                parent_pid=0,
                repo_root=PROJECT_ROOT,
                tmp_root=TMP_DIR,
                stdin_q=q,
                emit=_emit,
                parent_alive=lambda _pid: True,
            )
            self.assertTrue(any("synth_result" in ln for ln in lines))
            self.assertGreater(Path(out).stat().st_size, 44)

    def test_collect_inference_rejects_empty(self) -> None:
        class _Empty:
            def __iter__(self):
                return iter([])

        with self.assertRaises(RuntimeError):
            collect_inference_speech(_Empty(), sample_rate=16000)

    def test_stdin_reader_fatal_on_full_queue(self) -> None:
        fatal = StdinReaderFatal()
        out_q: queue.Queue[str | None] = queue.Queue(maxsize=1)
        out_q.put("occupied")
        stream = io.StringIO('{"schema":1,"type":"shutdown"}\n')
        stdin_reader_thread(stream, out_q, fatal=fatal)
        self.assertTrue(fatal.triggered)

    def test_worker_loop_protocol_error_on_stdin_fatal(self) -> None:
        fatal = StdinReaderFatal()
        fatal.triggered = True
        q: queue.Queue[str | None] = queue.Queue()
        q.put(None)
        lines: list[str] = []

        class _NoModel:
            sample_rate = 16000

        run_worker_loop(
            model=_NoModel(),
            settings=_fast_settings(),
            parent_pid=0,
            repo_root=PROJECT_ROOT,
            tmp_root=TMP_DIR,
            stdin_q=q,
            emit=lines.append,
            parent_alive=lambda _pid: True,
            stdin_fatal=fatal,
        )
        self.assertTrue(any(ERR_PROTOCOL in ln for ln in lines))


class CosyVoiceWorkerAdmissionTests(unittest.TestCase):
    def test_parent_process_current_pid_alive(self) -> None:
        self.assertTrue(parent_process_is_alive(os.getpid()))

    def test_admission_low_ram_zero_model(self) -> None:
        cfg = _fast_settings()
        tight = CosyVoiceSettings(
            startup_timeout_sec=cfg.startup_timeout_sec,
            synth_timeout_sec=cfg.synth_timeout_sec,
            idle_timeout_sec=cfg.idle_timeout_sec,
            max_text_chars=cfg.max_text_chars,
            cpu_threads=cfg.cpu_threads,
            min_free_ram_mb=10**9,
            min_free_vram_mb=cfg.min_free_vram_mb,
            cuda_memory_limit_mb=cfg.cuda_memory_limit_mb,
        )
        label = run_resource_admission(tight)
        self.assertEqual(label, ERR_INSUFFICIENT_RAM)

    def test_admission_cuda_fraction_failure(self) -> None:
        cfg = _fast_settings()

        class _Cuda:
            @staticmethod
            def is_available():
                return True

            @staticmethod
            def set_per_process_memory_fraction(_f):
                raise RuntimeError("nope")

            @staticmethod
            def mem_get_info():
                return (8 * 1024**3, 8 * 1024**3)

        class _Torch:
            cuda = _Cuda

            @staticmethod
            def set_num_threads(_n):
                return None

            @staticmethod
            def set_num_interop_threads(_n):
                return None

        def _import():
            return _Torch()

        with patch(
            "src.voice.cosyvoice.worker_admission.available_physical_ram_mb",
            return_value=10**6,
        ), patch(
            "src.voice.cosyvoice.worker_admission.available_cuda_free_vram_mb",
            return_value=(10**6, 10**6),
        ):
            label = run_resource_admission(cfg, import_torch=_import)
        self.assertEqual(label, ERR_RESOURCE_UNOBSERVABLE)

    def test_worker_loop_overflow_fatal_before_inference(self) -> None:
        import io as _io

        q: queue.Queue[str | None] = queue.Queue(maxsize=8)
        fatal = StdinReaderFatal()
        stdin_reader_thread(
            _io.StringIO(build_shutdown_request() * 9),
            q,
            fatal=fatal,
        )
        self.assertTrue(fatal.triggered)
        lines: list[str] = []

        class _NoModel:
            sample_rate = 16000

            def inference_instruct(self, *_a, **_k):
                raise AssertionError("must not infer")

        run_worker_loop(
            model=_NoModel(),
            settings=_fast_settings(),
            parent_pid=0,
            repo_root=PROJECT_ROOT,
            tmp_root=TMP_DIR,
            stdin_q=q,
            emit=lines.append,
            parent_alive=lambda _pid: True,
            stdin_fatal=fatal,
        )
        self.assertTrue(any(ERR_PROTOCOL in ln for ln in lines))
        self.assertFalse(any("shutdown_ack" in ln for ln in lines))

    def test_worker_cli_rejects_nan_cpu_threads(self) -> None:
        base = [
            "--parent-pid",
            "1",
            "--startup-timeout-sec",
            "30",
            "--synth-timeout-sec",
            "20",
            "--idle-timeout-sec",
            "60",
            "--max-text-chars",
            "240",
            "--cpu-threads",
            "nan",
            "--min-free-ram-mb",
            "8192",
            "--min-free-vram-mb",
            "4096",
            "--cuda-memory-limit-mb",
            "4096",
        ]
        with self.assertRaises(ValueError):
            parse_worker_cli(base)


class CosyVoiceReviewRegressionTests(unittest.TestCase):
    def test_dotdot_outside_tmp_rejected(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            tmp = root / "tmp"
            tmp.mkdir()
            outside = root / "outside.wav"
            _tiny_wav(outside)
            dev, ino = _portable_pair(outside)
            self.assertIsNone(
                validate_worker_output_path(
                    str(tmp / ".." / outside.name),
                    dev,
                    ino,
                    repo_root=root,
                    tmp_root=tmp,
                )
            )

    def test_nan_wav_not_verified_success(self) -> None:
        import numpy as np
        import soundfile as sf

        with TemporaryDirectory() as td:
            path = Path(td) / "nan.wav"
            sf.write(
                path,
                np.full(160, np.nan, dtype=np.float32),
                16000,
                subtype="FLOAT",
            )
            identity = path.stat()
            self.assertFalse(
                verify_synth_output(str(path), identity, path.stat().st_size)
            )

    def test_replacement_during_inference_fails(self) -> None:
        import contextlib
        import numpy as np

        with TemporaryDirectory() as td:
            tmp = Path(td) / "tmp"
            tmp.mkdir()
            out = tmp / "owned.wav"
            out.touch()
            expected_dev, expected_ino = _portable_pair(out)
            held = tmp / "old.wav"

            class Model:
                sample_rate = 16000

                def inference_instruct(self, *_a, **_k):
                    out.rename(held)
                    out.touch()
                    yield {"tts_speech": np.zeros(160, dtype=np.float32)}

            with self.assertRaises((RuntimeError, OSError)):
                run_inference_instruct_to_wav(
                    Model(),
                    text="t",
                    speaker="s",
                    instruct="i",
                    out_path=out,
                    redirect_stdout=contextlib.nullcontext,
                    expected_dev=expected_dev,
                    expected_ino=expected_ino,
                )
            self.assertEqual(_portable_pair(out), (expected_dev, expected_ino))

    def test_inbound_rejects_bool_schema_negative_epoch_long_rid(self) -> None:
        payload = json.loads(
            build_synth_request(
                request_id="r",
                epoch=0,
                style_id="warm_companion",
                text="t",
                output_path="C:/x.wav",
                output_dev=0,
                output_ino=0,
            )
        )
        payload["schema"] = True
        payload["epoch"] = -1
        payload["request_id"] = "r" * 129
        kind, _ = parse_inbound_line(json.dumps(payload) + "\n", max_text_chars=240)
        self.assertEqual(kind, "invalid")

    def test_stdin_writer_close_unblocks_waiter(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockedStream:
            def write(self, data: str) -> int:
                entered.set()
                release.wait(3.0)
                return len(data)

            def flush(self) -> None:
                return None

        writer = CosyVoiceStdinWriter(BlockedStream())
        writer.start()
        waiter = threading.Thread(
            target=lambda: writer.write_bounded(
                "x", deadline=time.monotonic() + 3.0
            )
        )
        waiter.start()
        self.assertTrue(entered.wait(1.0))
        writer.close()
        waiter.join(1.0)
        self.assertFalse(waiter.is_alive())
        release.set()
        self.assertTrue(writer.join(1.0))


class CosyVoiceOutputVerifyFdTests(unittest.TestCase):
    def _assert_fds_closed(self, opened: list[int]) -> None:
        for fd in opened:
            try:
                os.fstat(fd)
            except OSError:
                continue
            self.fail(f"fd {fd} still open after verify_synth_output")

    def test_identity_mismatch_closes_fd(self) -> None:
        import numpy as np
        import soundfile as sf

        with TemporaryDirectory() as td:
            root = Path(td)
            out = root / "out.wav"
            old = root / "old.wav"
            sf.write(out, np.zeros(160, dtype=np.float32), 16000)
            identity = out.stat()
            opened: list[int] = []
            real_open = os.open

            def capture_open(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                opened.append(fd)
                return fd

            def swap_after_validate(*_a, **_k):
                out.rename(old)
                sf.write(out, np.zeros(160, dtype=np.float32), 16000)
                return out

            with patch(
                "src.voice.cosyvoice.output_verify.validate_worker_output_path",
                side_effect=swap_after_validate,
            ), patch(
                "src.voice.cosyvoice.output_verify.os.open",
                side_effect=capture_open,
            ):
                self.assertFalse(
                    verify_synth_output(
                        str(out),
                        identity,
                        identity.st_size,
                        repo_root=root,
                        tmp_root=root,
                    )
                )
            self._assert_fds_closed(opened)

    def test_size_mismatch_closes_fd(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            path = root / "ok.wav"
            _tiny_wav(path)
            identity = path.stat()
            opened: list[int] = []
            real_open = os.open

            def capture_open(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                opened.append(fd)
                return fd

            with patch(
                "src.voice.cosyvoice.output_verify.os.open",
                side_effect=capture_open,
            ):
                self.assertFalse(
                    verify_synth_output(
                        str(path),
                        identity,
                        identity.st_size + 1,
                        repo_root=root,
                        tmp_root=root,
                    )
                )
            self._assert_fds_closed(opened)

    def test_fstat_failure_closes_fd(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            path = root / "ok.wav"
            _tiny_wav(path)
            identity = path.stat()
            opened: list[int] = []
            real_open = os.open

            def capture_open(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                opened.append(fd)
                return fd

            with patch(
                "src.voice.cosyvoice.output_verify.os.open",
                side_effect=capture_open,
            ), patch(
                "src.voice.cosyvoice.output_verify.os.fstat",
                side_effect=OSError("boom"),
            ):
                self.assertFalse(
                    verify_synth_output(
                        str(path),
                        identity,
                        path.stat().st_size,
                        repo_root=root,
                        tmp_root=root,
                    )
                )
            self._assert_fds_closed(opened)

    def test_valid_wav_closes_fd(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            path = root / "ok.wav"
            _tiny_wav(path)
            identity = path.stat()
            opened: list[int] = []
            real_open = os.open

            def capture_open(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                opened.append(fd)
                return fd

            with patch(
                "src.voice.cosyvoice.output_verify.os.open",
                side_effect=capture_open,
            ):
                self.assertTrue(
                    verify_synth_output(
                        str(path),
                        identity,
                        path.stat().st_size,
                        repo_root=root,
                        tmp_root=root,
                    )
                )
            self._assert_fds_closed(opened)


class CosyVoiceOutputIdentityTests(unittest.TestCase):
    def test_parent_rejects_swap_during_verify(self) -> None:
        import numpy as np
        import soundfile as sf
        from unittest.mock import patch

        with TemporaryDirectory() as td:
            tmp = Path(td) / "tmp"
            tmp.mkdir()
            out = tmp / "out.wav"
            old = tmp / "old.wav"
            sf.write(out, np.zeros(160, dtype=np.float32), 16000)
            identity = out.stat()
            swapped = False
            real_read = sf.SoundFile.read

            def swap_on_read(sf_self, *args, **kwargs):
                nonlocal swapped
                if not swapped:
                    out.rename(old)
                    sf.write(out, np.ones(160, dtype=np.float32), 16000)
                    swapped = True
                return real_read(sf_self, *args, **kwargs)

            with patch.object(sf.SoundFile, "read", swap_on_read):
                self.assertFalse(
                    verify_synth_output(
                        str(out),
                        identity,
                        identity.st_size,
                        repo_root=Path(td),
                        tmp_root=tmp,
                    )
                )

    def test_open_rejects_swapped_inode(self) -> None:
        import contextlib
        import numpy as np

        with TemporaryDirectory() as td:
            tmp = Path(td) / "tmp"
            tmp.mkdir()
            out = tmp / "owned.wav"
            out.touch()
            expected_dev, expected_ino = _portable_pair(out)
            swapped = tmp / "new.wav"
            swapped.touch()

            class Model:
                sample_rate = 16000

                def inference_instruct(self, *_a, **_k):
                    yield {"tts_speech": np.zeros(160, dtype=np.float32)}

            with self.assertRaises(RuntimeError):
                run_inference_instruct_to_wav(
                    Model(),
                    text="t",
                    speaker="s",
                    instruct="i",
                    out_path=swapped,
                    redirect_stdout=contextlib.nullcontext,
                    expected_dev=expected_dev,
                    expected_ino=expected_ino,
                )

    def test_normal_wav_verified_success(self) -> None:
        with TemporaryDirectory() as td:
            tmp = Path(td) / "tmp"
            tmp.mkdir()
            path = tmp / "ok.wav"
            _tiny_wav(path)
            identity = path.stat()
            self.assertTrue(
                verify_synth_output(
                    str(path),
                    identity,
                    path.stat().st_size,
                    repo_root=Path(td),
                    tmp_root=tmp,
                )
            )


class CosyVoiceClientStopBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pin_patch = patch(
            "src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}
        )
        self._pin_patch.start()

    def tearDown(self) -> None:
        self._pin_patch.stop()
        import src.voice.cosyvoice.client as client_mod

        inst = client_mod._ACTIVE_CLIENT
        if inst is not None:
            try:
                inst.shutdown(timeout=0.5)
            except Exception:
                pass
        client_mod._ACTIVE_CLIENT = None
        _shutdown_all_fake_workers(join_timeout=2.0)

    def _client(self, tmp: str, proc: _ProtocolWorkerProcess):
        policy, _ = _policy_with_local_tts(tmp)

        def _popen(*_a, **_k):
            proc.start()
            return proc

        bind_fake_local_tts_runners(policy, popen_fn=_popen)
        tfm = TempFileManager(safety_policy=policy)
        client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
        _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
        return client, tfm

    def test_shutdown_50ms_budget_retains_live_proc(self) -> None:
        with TemporaryDirectory() as tmp:
            stuck = _StuckProcess()
            stuck.start()
            client, _tfm = self._client(tmp, stuck)
            client._proc = stuck  # noqa: SLF001
            started = time.monotonic()
            status = client.shutdown(timeout=0.05)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.35)
            self.assertFalse(status.complete)
            self.assertIs(client._proc, stuck)

    def test_release_never_drops_live_proc(self) -> None:
        with TemporaryDirectory() as tmp:
            stuck = _StuckProcess()
            stuck.start()
            client, _tfm = self._client(tmp, stuck)
            client._proc = stuck  # noqa: SLF001
            client._release_process_if_clean(deadline=time.monotonic() + 0.05)
            self.assertIs(client._proc, stuck)

    def test_shutdown_retry_after_proc_exits(self) -> None:
        class _EventuallyDead(_StuckProcess):
            def __init__(self) -> None:
                super().__init__()
                self._dead = False

            def poll(self):
                return 0 if self._dead else None

        with TemporaryDirectory() as tmp:
            proc = _EventuallyDead()
            proc.start()
            client, _tfm = self._client(tmp, proc)
            client._proc = proc  # noqa: SLF001
            client._ready = True  # noqa: SLF001
            first = client.shutdown(timeout=0.05)
            self.assertFalse(first.complete)
            proc._dead = True
            second = client.shutdown(timeout=0.5)
            self.assertTrue(second.complete)
            self.assertIsNone(client._proc)

    def test_concurrent_shutdown_preserves_owner(self) -> None:
        with TemporaryDirectory() as tmp:
            stuck = _StuckProcess()
            stuck.start()
            client, _tfm = self._client(tmp, stuck)
            client._proc = stuck  # noqa: SLF001

            def _stop() -> None:
                client.shutdown(timeout=0.05)

            t1 = threading.Thread(target=_stop)
            t2 = threading.Thread(target=_stop)
            t1.start()
            t2.start()
            t1.join(timeout=1.0)
            t2.join(timeout=1.0)
            self.assertIs(client._proc, stuck)

    def _blocked_write_client(
        self,
        tmp: str,
        *,
        prepare_first: bool = True,
        ready_gate: threading.Event | None = None,
        worker_exit_gate: threading.Event | None = None,
    ):
        proc = _BlockedWriteWorkerProcess(
            ready_gate=ready_gate,
            worker_exit_gate=worker_exit_gate,
        )
        policy, _ = _policy_with_local_tts(tmp)

        def _popen(*_a, **_k):
            proc.start()
            return proc

        bind_fake_local_tts_runners(policy, popen_fn=_popen)
        tfm = TempFileManager(safety_policy=policy)
        client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
        _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
        if prepare_first:
            self.assertTrue(client.prepare())
        return client, tfm, proc

    def test_cancel_during_blocked_write(self) -> None:
        with TemporaryDirectory() as tmp:
            client, tfm, proc = self._blocked_write_client(tmp)
            stdin = proc.stdin
            assert isinstance(stdin, BlockingFakeStdin)
            thread_errors: list[BaseException] = []
            teardown = BlockedWriteTeardown(
                client, proc, stdin, thread_errors=thread_errors
            )
            out = tfm.create(".wav")
            cancel = threading.Event()
            result: list[bool] = []

            def _run() -> None:
                result.append(
                    client.synthesize(
                        "阻塞",
                        style_id="warm_companion",
                        output_path=out,
                        cancel_event=cancel,
                    )
                )

            t = threading.Thread(target=thread_body_capture(thread_errors, _run))
            teardown.synth_thread = t
            try:
                t.start()
                self.assertTrue(wait_write_entered(stdin))
                cancel.set()
                stdin.release.set()
                t.join(timeout=SYNTH_THREAD_JOIN_SEC)
                self.assertFalse(t.is_alive())
                self.assertEqual(result, [False])
                self.assertEqual(thread_errors, [])
            finally:
                teardown.run()

    def test_interrupt_during_blocked_write(self) -> None:
        with TemporaryDirectory() as tmp:
            client, tfm, proc = self._blocked_write_client(tmp)
            stdin = proc.stdin
            assert isinstance(stdin, BlockingFakeStdin)
            thread_errors: list[BaseException] = []
            teardown = BlockedWriteTeardown(
                client, proc, stdin, thread_errors=thread_errors
            )
            out = tfm.create(".wav")
            result: list[bool] = []

            def _run() -> None:
                result.append(
                    client.synthesize(
                        "阻塞",
                        style_id="warm_companion",
                        output_path=out,
                    )
                )

            t = threading.Thread(target=thread_body_capture(thread_errors, _run))
            teardown.synth_thread = t
            try:
                t.start()
                self.assertTrue(wait_write_entered(stdin))
                client.interrupt()
                t.join(timeout=SYNTH_THREAD_JOIN_SEC)
                self.assertFalse(t.is_alive())
                self.assertEqual(result, [False])
                self.assertEqual(thread_errors, [])
            finally:
                teardown.run()

    def test_shutdown_during_blocked_write(self) -> None:
        worker_exit_gate = threading.Event()
        with TemporaryDirectory() as tmp:
            client, tfm, proc = self._blocked_write_client(
                tmp, worker_exit_gate=worker_exit_gate
            )
            stdin = proc.stdin
            assert isinstance(stdin, BlockingFakeStdin)
            thread_errors: list[BaseException] = []
            teardown = BlockedWriteTeardown(
                client,
                proc,
                stdin,
                thread_errors=thread_errors,
                worker_exit_gate=worker_exit_gate,
            )
            out = tfm.create(".wav")
            result: list[bool] = []

            def _run() -> None:
                result.append(
                    client.synthesize(
                        "阻塞",
                        style_id="warm_companion",
                        output_path=out,
                    )
                )

            t = threading.Thread(target=thread_body_capture(thread_errors, _run))
            teardown.synth_thread = t
            try:
                t.start()
                self.assertTrue(wait_write_entered(stdin))
                status = client.shutdown(timeout=0.5)
                self.assertFalse(status.complete)
                self.assertIsNone(proc.poll())
                self.assertTrue(proc.exit_waiting.is_set())
                t.join(timeout=SYNTH_THREAD_JOIN_SEC)
                self.assertFalse(t.is_alive())
                self.assertEqual(result, [False])
                self.assertEqual(thread_errors, [])
                stdin.release.set()
                worker_exit_gate.set()
                retry = client.continue_shutdown(timeout=0.5)
                if not retry.complete:
                    retry = client.continue_shutdown(timeout=0.5)
                self.assertTrue(retry.complete)
            finally:
                teardown.run()

    def test_stop_phase_waits_for_blocked_write_rendezvous(self) -> None:
        ready_gate = threading.Event()
        with TemporaryDirectory() as tmp:
            client, tfm, proc = self._blocked_write_client(
                tmp, prepare_first=False, ready_gate=ready_gate
            )
            stdin = proc.stdin
            assert isinstance(stdin, BlockingFakeStdin)
            thread_errors: list[BaseException] = []
            teardown = BlockedWriteTeardown(
                client,
                proc,
                stdin,
                ready_gate=ready_gate,
                thread_errors=thread_errors,
            )
            out = tfm.create(".wav")
            stop_phase_started = threading.Event()

            def _run() -> None:
                client.synthesize(
                    "阻塞",
                    style_id="warm_companion",
                    output_path=out,
                )

            def _shutdown_after_rendezvous() -> None:
                self.assertTrue(wait_write_entered(stdin))
                stop_phase_started.set()
                client.shutdown(timeout=0.5)

            synth_t = threading.Thread(
                target=thread_body_capture(thread_errors, _run)
            )
            stop_t = threading.Thread(
                target=thread_body_capture(
                    thread_errors, _shutdown_after_rendezvous
                )
            )
            teardown.synth_thread = synth_t
            teardown.stop_thread = stop_t
            try:
                synth_t.start()
                stop_t.start()
                self.assertFalse(stdin.entered.is_set())
                self.assertFalse(stop_phase_started.is_set())
                ready_gate.set()
                self.assertTrue(wait_write_entered(stdin))
                self.assertTrue(
                    stop_phase_started.wait(WRITE_ENTERED_RENDEZVOUS_SEC)
                )
                stdin.release.set()
                stop_t.join(timeout=SYNTH_THREAD_JOIN_SEC)
                synth_t.join(timeout=SYNTH_THREAD_JOIN_SEC)
                self.assertFalse(stop_t.is_alive())
                self.assertFalse(synth_t.is_alive())
                self.assertEqual(thread_errors, [])
            finally:
                teardown.run()

    def test_blocked_write_teardown_reports_late_thread_errors(self) -> None:
        from types import SimpleNamespace

        late: list[BaseException] = []

        class _LateClient:
            def shutdown(self, **_kwargs):
                late.append(AssertionError("late background error"))
                return SimpleNamespace(complete=True)

            def continue_shutdown(self, **_kwargs):
                return SimpleNamespace(complete=True)

        class _Proc:
            def stop(self, **_kwargs) -> None:
                return None

        with self.assertRaises(AssertionError):
            BlockedWriteTeardown(
                _LateClient(), _Proc(), BlockingFakeStdin(), thread_errors=late
            ).run()

    def test_blocked_write_teardown_reports_incomplete_final_close(self) -> None:
        from types import SimpleNamespace

        class _IncompleteClient:
            def shutdown(self, **_kwargs):
                return SimpleNamespace(complete=False)

            def continue_shutdown(self, **_kwargs):
                return SimpleNamespace(complete=False)

        class _Proc:
            def stop(self, **_kwargs) -> None:
                return None

        with self.assertRaises(RuntimeError):
            BlockedWriteTeardown(
                _IncompleteClient(), _Proc(), BlockingFakeStdin()
            ).run()

    def test_blocked_write_teardown_base_exception_group_preserves_control_flow(
        self,
    ) -> None:
        class _InterruptedClient:
            def shutdown(self, **_kwargs):
                raise KeyboardInterrupt("original control flow")

        class _BrokenProc:
            def stop(self, **_kwargs):
                raise ValueError("cleanup failure")

        with self.assertRaises(BaseExceptionGroup) as ctx:
            BlockedWriteTeardown(
                _InterruptedClient(), _BrokenProc(), BlockingFakeStdin()
            ).run()
        self.assertTrue(
            any(
                isinstance(exc, KeyboardInterrupt)
                for exc in ctx.exception.exceptions
            )
        )

    def test_post_close_spawn_stubborn_proc_blocks_second_client(self) -> None:
        class _StubbornOrphan:
            stdin = stdout = stderr = None

            def __init__(self) -> None:
                self._dead = False

            def poll(self):
                return 0 if self._dead else None

            def terminate(self) -> None:
                return

            def kill(self) -> None:
                return

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("stubborn", timeout)

        with TemporaryDirectory() as tmp:
            orphan_holder: list[_StubbornOrphan] = []
            policy, _ = _policy_with_local_tts(tmp)

            class _LateSpawnPolicy:
                def spawn_local_tts_worker(self, launch):
                    client_ref._closing = True  # noqa: SLF001
                    proc = _StubbornOrphan()
                    orphan_holder.append(proc)
                    return proc

            tfm = TempFileManager(safety_policy=policy)
            client_ref = CosyVoiceWorkerClient(_fast_settings(), _LateSpawnPolicy(), tfm)
            _register_tfm_shutdown(self, tfm, client=client_ref)
            client_ref._start_worker_poll(0, None)  # noqa: SLF001
            status = client_ref.shutdown(timeout=0.05)
            self.assertFalse(status.complete)
            self.assertTrue(status.worker_alive)
            self.assertIsNotNone(client_ref._proc)
            self.assertTrue(client_ref.stop_failed)
            import src.voice.cosyvoice.client as client_mod

            self.assertIs(client_mod._ACTIVE_CLIENT, client_ref)
            with self.assertRaises(RuntimeError):
                CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            orphan_holder[0]._dead = True
            retry = client_ref.shutdown(timeout=0.5)
            self.assertTrue(retry.complete)
            self.assertIsNone(client_ref._proc)

    def test_synth_write_timeout_sends_termination(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _BlockedWriteWorkerProcess()
            policy, _ = _policy_with_local_tts(tmp)

            def _popen(*_a, **_k):
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tight = CosyVoiceSettings(
                startup_timeout_sec=5,
                synth_timeout_sec=1,
                idle_timeout_sec=60,
                max_text_chars=240,
                cpu_threads=4,
                min_free_ram_mb=8192,
                min_free_vram_mb=4096,
                cuda_memory_limit_mb=4096,
            )
            tfm = TempFileManager(safety_policy=policy)
            client = CosyVoiceWorkerClient(tight, policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            stdin = proc.stdin
            assert isinstance(stdin, BlockingFakeStdin)
            teardown = BlockedWriteTeardown(client, proc, stdin)
            self.assertTrue(client.prepare())
            out = tfm.create(".wav")
            started = time.monotonic()

            def _run() -> None:
                client.synthesize(
                    "阻塞",
                    style_id="warm_companion",
                    output_path=out,
                )

            t = threading.Thread(target=_run)
            teardown.synth_thread = t
            try:
                t.start()
                self.assertTrue(wait_write_entered(stdin))
                t.join(timeout=SYNTH_THREAD_JOIN_SEC)
                self.assertFalse(t.is_alive())
                self.assertLess(time.monotonic() - started, 2.5)
                self.assertTrue(proc._terminated)
            finally:
                teardown.run()

    def test_expired_deadline_stop_signals_without_wait(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _StuckProcess()
            proc.start()
            client, _tfm = self._client(tmp, proc)
            client._proc = proc  # noqa: SLF001
            calls = 0
            real_term = proc.terminate

            def _term() -> None:
                nonlocal calls
                calls += 1
                real_term()

            proc.terminate = _term  # type: ignore[method-assign]
            started = time.monotonic()
            client._terminate_worker_bounded(deadline=time.monotonic() - 1.0)  # noqa: SLF001
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.25)
            self.assertGreater(calls, 0)
            self.assertIs(client._proc, proc)
            proc._killed = True
            proc._stop.set()
            client.shutdown(timeout=0.5)

    def test_spawn_barrier_shutdown_before_ready_then_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            enter = threading.Event()
            allow_popen_return = threading.Event()
            proc = _ProtocolWorkerProcess(ready_delay=2.0)
            policy, _ = _policy_with_local_tts(tmp)

            def _popen(*_a, **_k):
                enter.set()
                allow_popen_return.wait(timeout=5.0)
                proc.start()
                return proc

            bind_fake_local_tts_runners(policy, popen_fn=_popen)
            tfm = TempFileManager(safety_policy=policy)
            client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            out = tfm.create(".wav")
            ready_seen: list[bool] = []

            def _synth() -> None:
                ready_seen.append(
                    client.synthesize(
                        "你好",
                        style_id="warm_companion",
                        output_path=out,
                    )
                )

            t = threading.Thread(target=_synth)
            t.start()
            self.assertTrue(enter.wait(2.0))
            status = client.shutdown(timeout=0.5)
            allow_popen_return.set()
            t.join(timeout=3.0)
            self.assertFalse(client._ready)
            self.assertFalse(any(ready_seen))
            proc.stop(join_timeout=2.0)
            import src.voice.cosyvoice.client as client_mod

            if client_mod._ACTIVE_CLIENT is client:
                client.shutdown(timeout=2.0)
            proc2 = _ProtocolWorkerProcess()
            with TemporaryDirectory() as tmp2:
                client2, tfm2 = self._client(tmp2, proc2)
                out2 = tfm2.create(".wav")
                self.assertTrue(
                    client2.synthesize(
                        "重试",
                        style_id="warm_companion",
                        output_path=out2,
                    )
                )
                proc2.stop(join_timeout=2.0)


class CosyVoiceProtocolStrictTests(unittest.TestCase):
    def test_duration_ms_bool_rejected(self) -> None:
        payload = {
            "schema": SCHEMA_VERSION,
            "type": "synth_result",
            "ok": True,
            "epoch": 0,
            "request_id": "r",
            "duration_ms": True,
            "bytes": 100,
        }
        with self.assertRaises(ValueError):
            validate_outbound_message(payload)

    def test_duration_ms_huge_int_rejected(self) -> None:
        payload = {
            "schema": SCHEMA_VERSION,
            "type": "synth_result",
            "ok": True,
            "epoch": 0,
            "request_id": "r",
            "duration_ms": 10**400,
            "bytes": 100,
        }
        with self.assertRaises((ValueError, OverflowError)):
            validate_outbound_message(payload)

    def test_error_epoch_rid_strict(self) -> None:
        line = build_error_message(ERR_PROTOCOL, request_id="ok", epoch=1)
        validate_outbound_message(json.loads(line.strip()))
        with self.assertRaises(ValueError):
            build_error_message(ERR_PROTOCOL, request_id="", epoch=0)


class CosyVoiceWorkerFatalSynthTests(unittest.TestCase):
    def test_fatal_during_inference_no_success(self) -> None:
        import numpy as np

        class Model:
            sample_rate = 16000

            def inference_instruct(self, *_a, **_k):
                fatal.triggered = True
                yield {"tts_speech": np.zeros(160, dtype=np.float32)}

        with TemporaryDirectory() as td:
            policy, _ = _policy_with_local_tts(td)
            tfm = TempFileManager(safety_policy=policy)
            _register_tfm_shutdown(self, tfm)
            out = tfm.create(".wav")
            dev, ino = _portable_pair(out)
            fatal = StdinReaderFatal()
            q: queue.Queue[str | None] = queue.Queue()
            lines: list[str] = []

            def _emit(line: str) -> None:
                lines.append(line)

            req = json.dumps(
                {
                    "schema": SCHEMA_VERSION,
                    "type": "synth",
                    "request_id": "r1",
                    "epoch": 0,
                    "style_id": "warm_companion",
                    "text": "你好",
                    "output_path": out,
                    "output_dev": dev,
                    "output_ino": ino,
                },
                ensure_ascii=False,
            ) + "\n"
            q.put(req)
            q.put(None)
            run_worker_loop(
                model=Model(),
                settings=_fast_settings(),
                parent_pid=0,
                repo_root=PROJECT_ROOT,
                tmp_root=TMP_DIR,
                stdin_q=q,
                emit=_emit,
                parent_alive=lambda _pid: True,
                stdin_fatal=fatal,
            )
            self.assertFalse(any("synth_result" in ln for ln in lines))
            self.assertTrue(any(ERR_PROTOCOL in ln for ln in lines))


class CosyVoiceWorkerPathsTests(unittest.TestCase):
    def test_resolve_propagates_layout_error(self) -> None:
        from src.voice.cosyvoice.protocol import ERR_ASSET_PIN
        from src.voice.cosyvoice.worker_paths import resolve_trusted_worker_paths

        with patch(
            "src.voice.cosyvoice.worker_paths.validate_worker_lexical_layout",
            return_value=ERR_ASSET_PIN,
        ):
            with self.assertRaises(PermissionError):
                resolve_trusted_worker_paths(Path("."))


class CosyVoiceWorkerMainAdmissionTests(unittest.TestCase):
    def _run_main(
        self,
        *,
        settings: CosyVoiceSettings | None = None,
        admission: str | None = None,
        layout_err: str | None = None,
        pin_raises: bool = False,
        load_raises: bool = False,
    ):
        import scripts.cosyvoice_runtime_worker as wrk
        from unittest.mock import MagicMock

        cfg = settings or _fast_settings()
        cli = MagicMock()
        cli.settings = cfg
        cli.parent_pid = os.getpid()
        release = patch.object(wrk, "release_model_lock")
        acquire = patch.object(wrk, "try_acquire_model_lock", return_value=True)
        import_cls = patch.object(
            wrk,
            "_import_cosyvoice_automodel_class",
            return_value=MagicMock(name="AutoModel"),
        )
        construct = patch.object(wrk, "_construct_automodel", return_value=object())
        loop = patch.object(wrk, "run_worker_loop")
        layout = patch(
            "src.voice.cosyvoice.worker_paths.validate_worker_lexical_layout",
            return_value=layout_err,
        )
        admit = patch.object(
            wrk,
            "run_resource_admission",
            return_value=admission,
        )
        pin = patch.object(
            wrk,
            "verify_pinned_assets",
            side_effect=Exception("pin") if pin_raises else MagicMock(return_value={}),
        )
        paths = patch.object(
            wrk,
            "resolve_trusted_worker_paths",
            return_value={
                "cosy_root": Path("c"),
                "matcha_root": Path("m"),
                "model_dir": Path("d"),
                "wetext_dir": Path("w"),
                "cache_root": Path("cache"),
            },
        )
        cache = patch.object(wrk, "_ensure_cache_dirs", return_value=True)
        wetext = patch.object(wrk, "_apply_local_wetext")
        cli_parse = patch.object(wrk, "parse_worker_cli", return_value=cli)
        watchdog = patch.object(wrk, "_start_parent_watchdog")
        with (
            acquire,
            release as rel_mock,
            import_cls as import_mock,
            construct as construct_mock,
            loop,
            layout,
            admit,
            pin,
            paths,
            cache,
            wetext,
            cli_parse,
            watchdog,
        ):
            if load_raises:
                construct_mock.side_effect = RuntimeError("load")
            code = wrk.main()
        return code, import_mock, construct_mock, rel_mock

    def test_main_low_ram_never_calls_load_model(self) -> None:
        cfg = _fast_settings()
        tight = CosyVoiceSettings(
            startup_timeout_sec=cfg.startup_timeout_sec,
            synth_timeout_sec=cfg.synth_timeout_sec,
            idle_timeout_sec=cfg.idle_timeout_sec,
            max_text_chars=cfg.max_text_chars,
            cpu_threads=cfg.cpu_threads,
            min_free_ram_mb=10**9,
            min_free_vram_mb=cfg.min_free_vram_mb,
            cuda_memory_limit_mb=cfg.cuda_memory_limit_mb,
        )
        code, import_mock, construct_mock, rel_mock = self._run_main(
            settings=tight, admission=ERR_INSUFFICIENT_RAM
        )
        import_mock.assert_not_called()
        construct_mock.assert_not_called()
        self.assertEqual(code, 2)
        rel_mock.assert_called()

    def test_main_layout_fail_releases_lock(self) -> None:
        from src.voice.cosyvoice.protocol import ERR_ASSET_PIN

        code, import_mock, construct_mock, rel_mock = self._run_main(
            layout_err=ERR_ASSET_PIN
        )
        import_mock.assert_not_called()
        construct_mock.assert_not_called()
        self.assertEqual(code, 2)
        rel_mock.assert_called()

    def test_main_vram_fail_no_load(self) -> None:
        from src.voice.cosyvoice.protocol import ERR_INSUFFICIENT_VRAM

        code, import_mock, construct_mock, _rel = self._run_main(
            admission=ERR_INSUFFICIENT_VRAM
        )
        import_mock.assert_not_called()
        construct_mock.assert_not_called()
        self.assertEqual(code, 2)

    def test_main_resource_unobservable_no_load(self) -> None:
        from src.voice.cosyvoice.protocol import ERR_RESOURCE_UNOBSERVABLE

        code, import_mock, construct_mock, _rel = self._run_main(
            admission=ERR_RESOURCE_UNOBSERVABLE
        )
        import_mock.assert_not_called()
        construct_mock.assert_not_called()
        self.assertEqual(code, 2)

    def test_main_cuda_unavailable_no_load(self) -> None:
        from src.voice.cosyvoice.protocol import ERR_CUDA_UNAVAILABLE

        code, import_mock, construct_mock, _rel = self._run_main(
            admission=ERR_CUDA_UNAVAILABLE
        )
        import_mock.assert_not_called()
        construct_mock.assert_not_called()
        self.assertEqual(code, 2)

    def test_main_pin_fail_no_load(self) -> None:
        code, import_mock, construct_mock, rel_mock = self._run_main(pin_raises=True)
        import_mock.assert_not_called()
        construct_mock.assert_not_called()
        self.assertEqual(code, 2)
        rel_mock.assert_called()

    def test_main_load_once_and_release(self) -> None:
        code, import_mock, construct_mock, rel_mock = self._run_main()
        import_mock.assert_called_once()
        construct_mock.assert_called_once()
        self.assertEqual(code, 0)
        rel_mock.assert_called()

    def test_main_load_exception_releases_lock(self) -> None:
        code, import_mock, construct_mock, rel_mock = self._run_main(load_raises=True)
        import_mock.assert_called_once()
        construct_mock.assert_called_once()
        self.assertEqual(code, 2)
        rel_mock.assert_called()

    def test_main_emits_bounded_startup_progress(self) -> None:
        import scripts.cosyvoice_runtime_worker as wrk

        emitted: list[str] = []

        def _capture_emit(line: str) -> None:
            emitted.append(line)

        with patch.object(wrk, "_emit", side_effect=_capture_emit):
            code, _import_mock, _construct_mock, _rel = self._run_main()
        self.assertEqual(code, 0)
        progress_count = 0
        for ln in emitted:
            try:
                payload = json.loads(ln.strip())
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "startup_progress":
                progress_count += 1
        self.assertEqual(progress_count, len(STARTUP_STAGES) * 2)


class _StartupProgressWorkerProcess(_ProtocolWorkerProcess):
    """在 ready 前发出有界 startup_progress 序列。"""

    def __init__(
        self,
        *,
        delay_before_ready: float = 0.0,
        duplicate_progress: bool = False,
        never_ready: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.stdout = _EofSignallingLineQueueStream()
        self._delay_before_ready = delay_before_ready
        self._duplicate_progress = duplicate_progress
        self._never_ready = never_ready

    def _emit_startup_progress(self) -> None:
        reporter = StartupProgressReporter(
            lambda line: self.stdout.write(line),
        )
        for stage in STARTUP_STAGES:
            if not reporter.begin(stage) or not reporter.end(stage):
                return
        if self._duplicate_progress:
            self.stdout.write(
                build_startup_progress_message(
                    stage=STARTUP_STAGES[0],
                    phase=STARTUP_PHASE_BEGIN,
                    elapsed_ms=1.0,
                )
            )

    def _run(self) -> None:
        try:
            if self._spawn_gate is not None:
                while not self._spawn_gate.is_set():
                    if self._stop.wait(0.05):
                        return
            if self._stop.is_set():
                return
            self._emit_startup_progress()
            if self._never_ready:
                while not self._stop.wait(0.05):
                    if self._stop.is_set():
                        break
                return
            if self._delay_before_ready > 0:
                if self._stop.wait(self._delay_before_ready):
                    return
            if self._stop.is_set():
                return
            self._emit_ready()
            if self._ready_delay_after_start and self._ready_delay > 0:
                if self._stop.wait(self._ready_delay):
                    return
            while not self._stop.is_set():
                line = self.stdin.readline()
                if not line:
                    if self._stop.is_set() or self.stdin._closed:
                        break
                    continue
                kind, inbound = parse_inbound_line(line, max_text_chars=240)
                if kind == "shutdown":
                    break
                if kind != "synth" or inbound is None:
                    continue
                if self._fail_output:
                    continue
                if self._hold_synth is not None:
                    while not self._hold_synth.is_set():
                        if self._stop.wait(0.05):
                            break
                    if self._stop.is_set():
                        break
                nbytes = 0
                try:
                    out = Path(inbound.output_path)
                    if out.is_file() and not self._fail_output:
                        _tiny_wav(out)
                        nbytes = out.stat().st_size
                except OSError:
                    pass
                if nbytes <= 0:
                    continue
                self.stdout.write(
                    build_synth_ok(
                        request_id=inbound.request_id,
                        epoch=inbound.epoch,
                        duration_ms=1.0,
                        bytes_written=nbytes,
                    )
                )
                self.stdout.flush()
                if self._exit_on_synth:
                    break
        finally:
            try:
                self.stdout.close()
            except Exception:
                pass


class CosyVoiceStartupProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pin_patch = patch(
            "src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}
        )
        self._pin_patch.start()

    def _client_with_fake(
        self, tmp: str, proc: _ProtocolWorkerProcess
    ) -> tuple[CosyVoiceWorkerClient, SafetyPolicy, TempFileManager]:
        policy, _root = _policy_with_local_tts(tmp)

        def _popen(*_a, **_k):
            proc.start()
            return proc

        bind_fake_local_tts_runners(policy, popen_fn=_popen)
        tfm = TempFileManager(safety_policy=policy)
        client = CosyVoiceWorkerClient(_fast_settings(), policy, tfm)
        _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
        return client, policy, tfm

    def tearDown(self) -> None:
        self._pin_patch.stop()
        import src.voice.cosyvoice.client as client_mod

        inst = client_mod._ACTIVE_CLIENT
        if inst is not None:
            try:
                inst.shutdown(timeout=0.5)
            except Exception:
                pass
        client_mod._ACTIVE_CLIENT = None
        _shutdown_all_fake_workers(join_timeout=2.0)

    def test_startup_progress_schema_valid(self) -> None:
        line = build_startup_progress_message(
            stage="layout",
            phase=STARTUP_PHASE_BEGIN,
            elapsed_ms=12.5,
        )
        payload = validate_outbound_message(json.loads(line.strip()))
        self.assertEqual(payload["type"], "startup_progress")

    def test_startup_progress_rejects_unknown_stage(self) -> None:
        payload = {
            "schema": SCHEMA_VERSION,
            "type": "startup_progress",
            "stage": "evil",
            "phase": STARTUP_PHASE_BEGIN,
            "elapsed_ms": 1.0,
        }
        with self.assertRaises(ValueError):
            validate_outbound_message(payload)

    def test_startup_progress_rejects_non_finite_elapsed(self) -> None:
        with self.assertRaises(ValueError):
            build_startup_progress_message(
                stage="layout",
                phase=STARTUP_PHASE_BEGIN,
                elapsed_ms=float("nan"),
            )

    def test_accumulator_rejects_end_without_begin(self) -> None:
        acc = StartupProgressAccumulator()
        payload = json.loads(
            build_startup_progress_message(
                stage="layout",
                phase=STARTUP_PHASE_END,
                elapsed_ms=1.0,
            ).strip()
        )
        with self.assertRaises(ValueError):
            acc.accept(payload)

    def test_accumulator_rejects_too_many_events(self) -> None:
        acc = StartupProgressAccumulator()
        for stage in STARTUP_STAGES:
            acc.accept(
                json.loads(
                    build_startup_progress_message(
                        stage=stage,
                        phase=STARTUP_PHASE_BEGIN,
                        elapsed_ms=1.0,
                    ).strip()
                )
            )
            acc.accept(
                json.loads(
                    build_startup_progress_message(
                        stage=stage,
                        phase=STARTUP_PHASE_END,
                        elapsed_ms=2.0,
                    ).strip()
                )
            )
        with self.assertRaises(ValueError):
            acc.accept(
                json.loads(
                    build_startup_progress_message(
                        stage="layout",
                        phase=STARTUP_PHASE_BEGIN,
                        elapsed_ms=3.0,
                    ).strip()
                )
            )

    def test_reporter_duplicate_begin_fails(self) -> None:
        lines: list[str] = []
        reporter = StartupProgressReporter(lines.append)
        self.assertTrue(reporter.begin("layout"))
        self.assertFalse(reporter.begin("layout"))

    def test_reporter_elapsed_ms_uses_injected_clock(self) -> None:
        ticks = iter([10.0, 10.05, 10.2])

        def _clock() -> float:
            return next(ticks)

        lines: list[str] = []
        reporter = StartupProgressReporter(lines.append, clock=_clock)
        self.assertTrue(reporter.begin("layout"))
        self.assertTrue(reporter.end("layout"))
        begin_ms = json.loads(lines[0].strip())["elapsed_ms"]
        end_ms = json.loads(lines[1].strip())["elapsed_ms"]
        self.assertEqual(begin_ms, 50.0)
        self.assertEqual(end_ms, 200.0)

    def test_client_snapshot_after_success(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _StartupProgressWorkerProcess()
            client, _, tfm = self._client_with_fake(tmp, proc)
            out = tfm.create(".wav")
            self.assertTrue(
                client.synthesize(
                    "你好",
                    style_id="warm_companion",
                    output_path=out,
                )
            )
            snap = client.startup_progress_snapshot()
            self.assertEqual(len(snap.events), len(STARTUP_STAGES) * 2)
            proc.stop(join_timeout=2.0)

    def test_duplicate_progress_fails_startup(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _StartupProgressWorkerProcess(duplicate_progress=True)
            client, _, tfm = self._client_with_fake(tmp, proc)
            out = tfm.create(".wav")
            self.assertFalse(
                client.synthesize(
                    "你好",
                    style_id="warm_companion",
                    output_path=out,
                )
            )
            self.assertFalse(client._ready)
            proc.stop(join_timeout=2.0)

    def test_progress_does_not_extend_startup_deadline(self) -> None:
        cfg = _fast_settings()
        tight = CosyVoiceSettings(
            startup_timeout_sec=1,
            synth_timeout_sec=cfg.synth_timeout_sec,
            idle_timeout_sec=cfg.idle_timeout_sec,
            max_text_chars=cfg.max_text_chars,
            cpu_threads=cfg.cpu_threads,
            min_free_ram_mb=cfg.min_free_ram_mb,
            min_free_vram_mb=cfg.min_free_vram_mb,
            cuda_memory_limit_mb=cfg.cuda_memory_limit_mb,
        )
        with TemporaryDirectory() as tmp:
            proc = _StartupProgressWorkerProcess(
                delay_before_ready=1.25,
                never_ready=False,
            )
            client, _, tfm = self._client_with_fake(tmp, proc)
            client._settings = tight  # noqa: SLF001
            out = tfm.create(".wav")
            t0 = time.monotonic()
            self.assertFalse(
                client.synthesize(
                    "你好",
                    style_id="warm_companion",
                    output_path=out,
                )
            )
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 2.5)
            self.assertGreaterEqual(elapsed, 0.9)
            snap = client.startup_progress_snapshot()
            self.assertEqual(len(snap.events), len(STARTUP_STAGES) * 2)
            proc.stop(join_timeout=2.0)

    def test_cancel_during_startup_progress(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _StartupProgressWorkerProcess(never_ready=True)
            client, _, tfm = self._client_with_fake(tmp, proc)
            cancel = threading.Event()
            out = tfm.create(".wav")

            def _run() -> None:
                client.synthesize(
                    "你好",
                    style_id="warm_companion",
                    output_path=out,
                    cancel_event=cancel,
                )

            t = threading.Thread(target=_run)
            t.start()
            time.sleep(0.15)
            cancel.set()
            t.join(3.0)
            self.assertFalse(t.is_alive())
            status = client.shutdown(timeout=1.0)
            self.assertTrue(status.complete or not client.process_alive())
            proc.stop(join_timeout=2.0)

    def test_stdout_redirect_keeps_third_party_off_protocol_stream(self) -> None:
        import scripts.cosyvoice_runtime_worker as wrk
        import sys

        from src.voice.cosyvoice.worker_runtime import redirect_stdout_to_stderr

        protocol_buf = io.StringIO()
        stderr_buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr_buf
        try:
            with patch.object(wrk, "_PROTOCOL_STDOUT", protocol_buf):
                with redirect_stdout_to_stderr():
                    sys.stdout.write("third_party_noise")
                    wrk._emit('{"schema":1,"type":"ready","load_ms":0}\n')
            self.assertIn("third_party_noise", stderr_buf.getvalue())
            self.assertIn("ready", protocol_buf.getvalue())
            self.assertNotIn("third_party_noise", protocol_buf.getvalue())
        finally:
            sys.stderr = old_stderr

    def test_wetext_apply_redirects_import_stdout_off_protocol(self) -> None:
        import builtins
        import sys
        import types

        import scripts.cosyvoice_runtime_worker as wrk
        from src.voice.cosyvoice.assets import WETEXT_DIR_REL

        noise = "wetext_fake_import_stdout"
        stderr_buf = io.StringIO()
        protocol_buf = io.StringIO()
        old_stderr = sys.stderr
        real_import = builtins.__import__
        saved = {
            k: sys.modules[k]
            for k in ("wetext", "wetext.wetext")
            if k in sys.modules
        }
        for key in ("wetext", "wetext.wetext"):
            sys.modules.pop(key, None)

        def _noisy_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name in ("wetext", "wetext.wetext"):
                sys.stdout.write(noise)
                mod = types.ModuleType("wetext.wetext")
                mod.snapshot_download = lambda *_a, **_k: str(Path("."))
                pkg = types.ModuleType("wetext")
                pkg.wetext = mod
                sys.modules["wetext"] = pkg
                sys.modules["wetext.wetext"] = mod
                return pkg
            return real_import(name, globals, locals, fromlist, level)

        sys.stderr = stderr_buf
        try:
            with TemporaryDirectory() as td:
                repo_root = Path(td)
                wetext_dir = repo_root / WETEXT_DIR_REL
                wetext_dir.mkdir(parents=True)
                with patch.object(builtins, "__import__", side_effect=_noisy_import):
                    with patch.object(wrk, "_PROTOCOL_STDOUT", protocol_buf):
                        wrk._apply_local_wetext(wetext_dir, repo_root=repo_root)
            self.assertIn(noise, stderr_buf.getvalue())
            self.assertNotIn(noise, protocol_buf.getvalue())
        finally:
            sys.stderr = old_stderr
            for key in ("wetext", "wetext.wetext"):
                if key in saved:
                    sys.modules[key] = saved[key]
                else:
                    sys.modules.pop(key, None)

    def test_reader_fatal_on_bad_startup_progress(self) -> None:
        stream = _LineQueueStream()
        stream.write(
            build_startup_progress_message(
                stage="layout",
                phase=STARTUP_PHASE_BEGIN,
                elapsed_ms=1.0,
            )
        )
        stream.write(
            dumps_message(
                {
                    "schema": SCHEMA_VERSION,
                    "type": "startup_progress",
                    "stage": "layout",
                    "phase": STARTUP_PHASE_END,
                    "elapsed_ms": float("nan"),
                }
            )
        )
        reader = CosyVoiceStdoutReader(stream)
        reader.start()
        msg = reader.poll_message(timeout=1.0)
        self.assertIsNotNone(msg)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not reader.had_fatal_protocol_error():
            time.sleep(0.02)
        self.assertTrue(reader.had_fatal_protocol_error())
        reader.close()
        self.assertTrue(reader.join(1.0))


class _AdvanceableClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class _IdleTeardownWorkerProcess(_ProtocolWorkerProcess):
    """主循环已因 idle 退出，但 teardown 阶段 poll 仍返回 None。"""

    def __init__(self, *, worker_tag: str = "a", **kwargs) -> None:
        super().__init__(**kwargs)
        self.worker_tag = worker_tag
        self.synths_handled = 0
        self._stdin_consumer_stopped = False
        self._teardown_hold = threading.Event()
        self._teardown_hold.set()
        self._teardown_entered = threading.Event()

    def _run(self) -> None:
        if self._spawn_gate is not None:
            while not self._spawn_gate.is_set():
                if self._stop.wait(0.05):
                    return
        if self._stop.is_set():
            return
        self._emit_ready()
        while not self._stop.is_set():
            line = self.stdin.readline()
            if not line:
                if self._stop.is_set() or self.stdin._closed:
                    break
                continue
            kind, inbound = parse_inbound_line(line, max_text_chars=240)
            if kind == "shutdown":
                break
            if kind != "synth" or inbound is None:
                continue
            nbytes = 0
            try:
                out = Path(inbound.output_path)
                if out.is_file():
                    _tiny_wav(out)
                    nbytes = out.stat().st_size
            except OSError:
                pass
            if nbytes <= 0:
                continue
            self.synths_handled += 1
            self.stdout.write(
                build_synth_ok(
                    request_id=inbound.request_id,
                    epoch=inbound.epoch,
                    duration_ms=1.0,
                    bytes_written=nbytes,
                )
            )
            self.stdout.flush()
            self._stdin_consumer_stopped = True
            self._teardown_entered.set()
            break
        while self._teardown_hold.is_set() and not self._stop.is_set():
            self._stop.wait(0.05)

    def poll(self):
        if self._stdin_consumer_stopped and self._teardown_hold.is_set():
            return None
        thread = self._thread
        if self._killed:
            if thread is None or not thread.is_alive():
                return -9
            return None
        if thread is not None and thread.is_alive():
            return None
        return 0

    def terminate(self) -> None:
        self._teardown_hold.clear()
        super().terminate()

    def wait(self, timeout=None):
        thread = self._thread
        if thread is not None and thread.is_alive():
            if timeout is not None:
                thread.join(timeout=timeout)
            else:
                thread.join()
            if thread.is_alive():
                raise subprocess.TimeoutExpired("fake", timeout)
        if self.poll() is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return 0


class _EofIdleTeardownWorkerProcess(_IdleTeardownWorkerProcess):
    """idle teardown 场景 + 真实 pipe 式 stdout EOF（无数据不误报 EOF）。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stdout = _EofSignallingLineQueueStream()

    def _run(self) -> None:
        try:
            super()._run()
        finally:
            try:
                self.stdout.close()
            except Exception:
                pass


class CosyVoiceIdleRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pin_patch = patch(
            "src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}
        )
        self._pin_patch.start()
        self._clock = _AdvanceableClock()

    def tearDown(self) -> None:
        self._pin_patch.stop()
        import src.voice.cosyvoice.client as client_mod

        inst = client_mod._ACTIVE_CLIENT
        if inst is not None:
            try:
                inst.shutdown(timeout=0.5)
            except Exception:
                pass
        client_mod._ACTIVE_CLIENT = None
        _shutdown_all_fake_workers(join_timeout=2.0)

    def _idle_settings(self, idle_timeout_sec: int = 10) -> CosyVoiceSettings:
        base = _fast_settings()
        return CosyVoiceSettings(
            startup_timeout_sec=base.startup_timeout_sec,
            synth_timeout_sec=base.synth_timeout_sec,
            idle_timeout_sec=idle_timeout_sec,
            max_text_chars=base.max_text_chars,
            cpu_threads=base.cpu_threads,
            min_free_ram_mb=base.min_free_ram_mb,
            min_free_vram_mb=base.min_free_vram_mb,
            cuda_memory_limit_mb=base.cuda_memory_limit_mb,
        )

    def _client(
        self,
        tmp: str,
        *,
        idle_timeout_sec: int = 10,
        popen_fn,
    ) -> tuple[CosyVoiceWorkerClient, TempFileManager]:
        policy, _ = _policy_with_local_tts(tmp)
        bind_fake_local_tts_runners(policy, popen_fn=popen_fn)
        tfm = TempFileManager(safety_policy=policy)
        client = CosyVoiceWorkerClient(
            self._idle_settings(idle_timeout_sec), policy, tfm
        )
        _register_tfm_shutdown(self, tfm, client=client)
        return client, tfm

    def test_idle_retired_proc_still_alive_spawns_fresh_worker(self) -> None:
        with TemporaryDirectory() as tmp:
            procs: list[_IdleTeardownWorkerProcess] = []

            def _popen(*_a, **_k):
                tag = chr(ord("a") + len(procs))
                proc = _IdleTeardownWorkerProcess(worker_tag=tag)
                procs.append(proc)
                proc.start()
                return proc

            client, tfm = self._client(tmp, popen_fn=_popen)
            _register_tfm_shutdown(self, tfm, client=client, procs=procs)
            out1 = tfm.create(".wav")
            with patch(
                "src.voice.cosyvoice.client._MONOTONIC",
                self._clock,
            ):
                self.assertTrue(
                    client.synthesize(
                        "第一",
                        style_id="warm_companion",
                        output_path=out1,
                    )
                )
                self.assertEqual(len(procs), 1)
                self.assertEqual(procs[0].synths_handled, 1)
                self._clock.advance(9.6)
                out2 = tfm.create(".wav")
                self.assertTrue(
                    client.synthesize(
                        "第二",
                        style_id="warm_companion",
                        output_path=out2,
                    )
                )
            self.assertEqual(len(procs), 2)
            self.assertEqual(procs[1].synths_handled, 1)
            self.assertNotEqual(procs[0].worker_tag, procs[1].worker_tag)

    def test_warm_reuse_within_idle_window(self) -> None:
        with TemporaryDirectory() as tmp:
            spawns: list[int] = []
            proc = _ProtocolWorkerProcess()

            def _popen(*_a, **_k):
                spawns.append(1)
                proc.start()
                return proc

            client, tfm = self._client(tmp, popen_fn=_popen)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            out1 = tfm.create(".wav")
            out2 = tfm.create(".wav")
            with patch(
                "src.voice.cosyvoice.client._MONOTONIC",
                self._clock,
            ):
                self.assertTrue(
                    client.synthesize(
                        "一",
                        style_id="warm_companion",
                        output_path=out1,
                    )
                )
                self._clock.advance(1.0)
                self.assertTrue(
                    client.synthesize(
                        "二",
                        style_id="warm_companion",
                        output_path=out2,
                    )
                )
            self.assertEqual(spawns, [1])

    def test_idle_reap_stop_failure_retains_owner(self) -> None:
        with TemporaryDirectory() as tmp:
            spawns: list[int] = []

            def _popen(*_a, **_k):
                spawns.append(1)
                stuck.start()
                return stuck

            stuck = _StuckProcess()
            client, tfm = self._client(tmp, popen_fn=_popen)
            _register_tfm_shutdown(self, tfm, client=client, procs=[stuck])
            out1 = tfm.create(".wav")
            with patch(
                "src.voice.cosyvoice.client._MONOTONIC",
                self._clock,
            ):
                self.assertTrue(
                    client.synthesize(
                        "一",
                        style_id="warm_companion",
                        output_path=out1,
                    )
                )
                self._clock.advance(9.6)
                out2 = tfm.create(".wav")
                self.assertFalse(
                    client.synthesize(
                        "二",
                        style_id="warm_companion",
                        output_path=out2,
                    )
                )
            self.assertTrue(client.stop_failed)
            proc = client._proc
            inner = getattr(proc, "_redirector", proc)
            self.assertIs(inner, stuck)
            self.assertEqual(spawns, [1])

    def test_reader_eof_not_reused_as_ready(self) -> None:
        with TemporaryDirectory() as tmp:
            procs: list[_EofIdleTeardownWorkerProcess] = []

            def _popen(*_a, **_k):
                proc = _EofIdleTeardownWorkerProcess()
                procs.append(proc)
                proc.start()
                return proc

            client, tfm = self._client(tmp, popen_fn=_popen)
            _register_tfm_shutdown(self, tfm, client=client, procs=procs)
            out1 = tfm.create(".wav")
            self.assertTrue(
                client.synthesize(
                    "一",
                    style_id="warm_companion",
                    output_path=out1,
                )
            )
            proc0 = procs[0]
            self.assertTrue(proc0._teardown_entered.wait(timeout=2.0))
            self.assertTrue(proc0._stdin_consumer_stopped)
            self.assertTrue(proc0.poll() is None)
            reader = client._reader  # noqa: SLF001
            self.assertIsNotNone(reader)
            proc0.stdout.close()
            self.assertTrue(proc0.stdout._eof_delivered.wait(timeout=2.0))
            self.assertTrue(reader.join(timeout=2.0))
            self.assertFalse(reader.is_alive())
            self.assertTrue(reader.had_fatal_protocol_error())
            self.assertTrue(client.process_alive())
            self.assertFalse(client._worker_reuse_ok())  # noqa: SLF001
            out2 = tfm.create(".wav")
            self.assertTrue(
                client.synthesize(
                    "二",
                    style_id="warm_companion",
                    output_path=out2,
                )
            )
            self.assertEqual(len(procs), 2)
            self.assertEqual(procs[1].synths_handled, 1)

    def test_cancel_during_idle_reap_returns_without_spawn(self) -> None:
        with TemporaryDirectory() as tmp:
            spawns: list[int] = []
            stuck = _StuckProcess()

            def _popen(*_a, **_k):
                spawns.append(1)
                stuck.start()
                return stuck

            client, tfm = self._client(tmp, popen_fn=_popen)
            _register_tfm_shutdown(self, tfm, client=client, procs=[stuck])
            out1 = tfm.create(".wav")
            cancel = threading.Event()
            with patch(
                "src.voice.cosyvoice.client._MONOTONIC",
                self._clock,
            ):
                self.assertTrue(
                    client.synthesize(
                        "一",
                        style_id="warm_companion",
                        output_path=out1,
                    )
                )
                self._clock.advance(9.6)
                cancel.set()
                out2 = tfm.create(".wav")
                started = time.monotonic()
                self.assertFalse(
                    client.synthesize(
                        "二",
                        style_id="warm_companion",
                        output_path=out2,
                        cancel_event=cancel,
                    )
                )
                self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(spawns, [1])

    def test_closing_during_idle_reap_rejects(self) -> None:
        with TemporaryDirectory() as tmp:
            proc = _IdleTeardownWorkerProcess()

            def _popen(*_a, **_k):
                proc.start()
                return proc

            client, tfm = self._client(tmp, popen_fn=_popen)
            _register_tfm_shutdown(self, tfm, client=client, procs=[proc])
            out1 = tfm.create(".wav")
            with patch(
                "src.voice.cosyvoice.client._MONOTONIC",
                self._clock,
            ):
                self.assertTrue(
                    client.synthesize(
                        "一",
                        style_id="warm_companion",
                        output_path=out1,
                    )
                )
                self._clock.advance(9.6)
                client._closing = True  # noqa: SLF001
                out2 = tfm.create(".wav")
                self.assertFalse(
                    client.synthesize(
                        "二",
                        style_id="warm_companion",
                        output_path=out2,
                    )
                )


class CosyVoiceStdinWriterCancelTests(unittest.TestCase):
    def test_write_bounded_honours_cancel_callback(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        cancel = threading.Event()

        class BlockedStream:
            def write(self, data: str) -> int:
                entered.set()
                release.wait(3.0)
                return len(data)

            def flush(self) -> None:
                return None

        writer = CosyVoiceStdinWriter(BlockedStream())
        writer.start()
        result: list[bool] = []

        def _run() -> None:
            result.append(
                writer.write_bounded(
                    "line",
                    deadline=time.monotonic() + 3.0,
                    should_cancel=cancel.is_set,
                )
            )

        t = threading.Thread(target=_run)
        t.start()
        self.assertTrue(entered.wait(1.0))
        cancel.set()
        t.join(1.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(result, [False])
        release.set()
        writer.close()
        self.assertTrue(writer.join(1.0))


if __name__ == "__main__":
    unittest.main()
