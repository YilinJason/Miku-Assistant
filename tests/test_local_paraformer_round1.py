"""Local Paraformer round-1 regression tests (injected fakes; not executed in CI by author)."""
from __future__ import annotations

import ast
import math
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.assembly import validate_assembly_voice_stt_config
from src.core.host_memory import check_model_startup_memory
from src.core.safety import SafetyPolicy
from src.core.voice_activation import (
    UnpublishedVoice,
    stt_shutdown_reports_complete,
    try_retire_unpublished_voice,
)
from src.voice.paraformer.assets import (
    OFFICIAL_GIT_BLOB_SHA1,
    OFFICIAL_MODEL_PT_SHA256,
    _git_blob_sha1,
)
from src.voice.paraformer.capabilities import merge_local_asr_popen_kwargs
from src.voice.paraformer.client import ParaformerWorkerClient
from src.voice.paraformer.network_guard import (
    install_outbound_network_guard,
    restore_outbound_network_guard,
)
from src.voice.paraformer.protocol import (
    MAX_INBOUND_JSON_LINE_CHARS,
    MAX_PCM_BYTES,
    build_transcribe_request,
    decode_transcribe_pcm,
    parse_json_line,
    validate_outbound_message,
)
from src.voice.paraformer.settings import ParaformerSettings
from src.voice.paraformer.worker_env import build_asr_worker_env
from src.voice.recognition import RecognitionResult, RecognitionStatus
from src.voice.stt import STTEngine


class ProtocolGeometryTests(unittest.TestCase):
    def test_one_and_fifteen_second_pcm_lines(self) -> None:
        for sec in (1, 15):
            with self.subTest(sec=sec):
                pcm = b"\x00\x01" * (16000 * sec)
                line = build_transcribe_request("req-1", pcm)
                self.assertLessEqual(len(line), MAX_INBOUND_JSON_LINE_CHARS + 1)
                msg = parse_json_line(line, max_chars=MAX_INBOUND_JSON_LINE_CHARS)
                self.assertEqual(decode_transcribe_pcm(msg), pcm)

    def test_exact_max_pcm_bytes(self) -> None:
        pcm = b"\x00\x01" * (MAX_PCM_BYTES // 2)
        self.assertEqual(len(pcm), MAX_PCM_BYTES)
        build_transcribe_request("rid", pcm)

    def test_oversized_pcm_rejected(self) -> None:
        pcm = b"\x00" * (MAX_PCM_BYTES + 2)
        with self.assertRaises(ValueError):
            build_transcribe_request("rid", pcm)

    def test_empty_pcm_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_transcribe_request("rid", b"")

    def test_schema_bool_not_version(self) -> None:
        with self.assertRaises(ValueError):
            validate_outbound_message({"schema": True, "msg": "ready", "status": "ready"})

    def test_empty_result_cannot_smuggle_text(self) -> None:
        with self.assertRaises(ValueError):
            validate_outbound_message(
                {
                    "schema": 1,
                    "msg": "transcribe_result",
                    "request_id": "r",
                    "status": "empty",
                    "text": "secret",
                }
            )


class CapabilityEnvTests(unittest.TestCase):
    def test_fixed_windows_layout_env(self) -> None:
        root = Path(__file__).resolve().parents[1]
        env = build_asr_worker_env(
            repo_root=root,
            model_dir=root / "vendor" / "paraformer-zh",
            cpu_threads=2,
        )
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "-1")
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["TORCH_FORCE_WEIGHTS_ONLY_LOAD"], "1")

    def test_no_arbitrary_popen_option(self) -> None:
        with self.assertRaises(PermissionError):
            merge_local_asr_popen_kwargs({}, {"creationflags": 16})


class ClientLifecycleTests(unittest.TestCase):
    def _client(self) -> ParaformerWorkerClient:
        return ParaformerWorkerClient(
            ParaformerSettings(),
            SafetyPolicy({"permissions": {"local_asr_worker": True}}),
        )

    def test_unused_shutdown_terminal(self) -> None:
        c = self._client()
        self.assertTrue(c.shutdown())
        out = c.transcribe(b"\x00\x01")
        self.assertEqual(out.error_label, "closing")

    def test_incomplete_owner_retained(self) -> None:
        c = self._client()

        class LiveProcess:
            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                pass

        p = LiveProcess()
        c._proc = p
        self.assertFalse(c.shutdown(budget_sec=0.25))
        self.assertIs(c._proc, p)

    def test_shutdown_does_not_block_on_inflight_lock(self) -> None:
        c = self._client()
        c._proc = MagicMock()
        c._proc.poll.return_value = None
        c._inflight_lock.acquire()
        ended = threading.Event()

        def run() -> None:
            c.shutdown(budget_sec=0.25)
            ended.set()

        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(ended.wait(1.0))
        c._inflight_lock.release()
        t.join(2.0)

    def test_busy_admission(self) -> None:
        c = self._client()
        c._active_request = True
        out = c.transcribe(b"\x00\x01")
        self.assertEqual(out.error_label, "busy")

    def test_pcm_validated_before_spawn(self) -> None:
        c = self._client()
        out = c.transcribe(b"")
        self.assertEqual(out.error_label, "pcm_invalid")
        self.assertIsNone(c._proc)


class STTEngineOwnershipTests(unittest.TestCase):
    def test_constructor_no_torch(self) -> None:
        before = set(sys.modules)
        STTEngine(engine="paraformer", safety_policy=SafetyPolicy())
        self.assertNotIn("torch", set(sys.modules) - before)

    def test_shutdown_strict_true(self) -> None:
        engine = STTEngine(engine="paraformer", safety_policy=SafetyPolicy())
        self.assertTrue(engine.shutdown())
        self.assertTrue(stt_shutdown_reports_complete(engine))

    def test_post_close_recognize_never_opens_mic(self) -> None:
        engine = STTEngine(engine="paraformer", safety_policy=SafetyPolicy())
        engine._recognizer = object()
        engine.shutdown()
        with patch("speech_recognition.Microphone") as mic:
            result = engine.recognize()
        mic.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)

    def test_invalid_timeout_before_mic(self) -> None:
        engine = STTEngine(engine="paraformer", safety_policy=SafetyPolicy())
        engine._recognizer = object()
        with patch("speech_recognition.Microphone") as mic:
            result = engine.recognize(timeout=float("nan"))
        mic.assert_not_called()
        self.assertEqual(result.status, RecognitionStatus.NOT_READY)


class PinnedAssetFixtureTests(unittest.TestCase):
    def test_git_blob_pin_helper(self) -> None:
        import tempfile

        data = b"hello paraformer pin"
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
            path = Path(tmp.name)
        try:
            self.assertEqual(
                len(_git_blob_sha1(path, expected_size=len(data))),
                40,
            )
            self.assertIn("config.yaml", OFFICIAL_GIT_BLOB_SHA1)
            self.assertEqual(len(OFFICIAL_MODEL_PT_SHA256), 64)
        finally:
            path.unlink(missing_ok=True)


class NetworkGuardTests(unittest.TestCase):
    def test_socket_type_preserved(self) -> None:
        import socket

        before = socket.socket
        install_outbound_network_guard()
        try:
            self.assertIs(socket.socket, before)
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", 9))
        finally:
            restore_outbound_network_guard()


class SettingsValidationTests(unittest.TestCase):
    def test_direct_dataclass_validation(self) -> None:
        for kw in ({"cpu_threads": True}, {"inference_deadline_sec": math.nan}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                ParaformerSettings(**kw)


class VoiceRetirementTests(unittest.TestCase):
    def test_unpublished_stt_requires_complete_shutdown(self) -> None:
        stt = MagicMock()
        stt.engine_type = "paraformer"
        stt.shutdown.return_value = False
        stt.is_shutdown_complete.return_value = False
        pending = UnpublishedVoice(stt=stt)
        self.assertFalse(try_retire_unpublished_voice(pending, stop_rvc=False))
        self.assertIs(pending.stt, stt)


class SyntaxCompileTests(unittest.TestCase):
    def test_modules_parse(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for rel in (
            "src/voice/paraformer/client.py",
            "src/voice/paraformer/protocol.py",
            "scripts/paraformer_runtime_worker.py",
            "src/voice/stt.py",
        ):
            path = root / rel
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


if __name__ == "__main__":
    unittest.main()
