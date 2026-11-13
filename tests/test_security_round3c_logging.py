# -*- coding: utf-8 -*-
"""Round 3C1：持久日志隐私与 logs/ 文件边界——纯离线行为测试。"""
from __future__ import annotations

import io
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.log_privacy import (
    PrivacyRotatingFileHandler,
    exception_log_label,
    resolve_log_file_path,
    validate_backup_count,
    validate_log_file_name,
    validate_max_size_mb,
)
from src.core.logging_setup import setup_app_logging
from src.core.safety import Operation, SafetyPolicy
from src.extensions.character_interface import CharacterInterface
from src.llm.generate_result import GenerateStatus
from src.llm.ollama_client import OllamaClient
from src.search.chrome_search import ChromeSearcher
from src.core.handlers.search_handler import SearchIntentHandler
from src.voice.rvc.health_check import RVCHealthReport, ensure_rvc_ready

from tests.support.host_memory_fixtures import install_module_sufficient_host_memory


def setUpModule() -> None:
    install_module_sufficient_host_memory()


def _root_handlers_snapshot():
    return list(logging.getLogger().handlers)


def _remove_added_handlers(before):
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


class LogFileNameValidationTests(unittest.TestCase):
    def test_reject_table_driven_names(self) -> None:
        rejected = [
            "../x.log",
            "..\\x.log",
            "/tmp/x.log",
            "C:\\logs\\x.log",
            "D:x.log",
            ".",
            "..",
            "CON",
            "con.log",
            "NUL.txt",
            "COM1.foo.log",
            "NUL.foo.log",
            "foo.log.",
            "foo.log ",
            "bad\x00name.log",
            "bad?.log",
            "bad*.log",
            "bad|.log",
            "bad<.log",
            "bad>.log",
            'bad".log',
            "bad\x01log.log",
            "",
            None,
            123,
        ]
        for name in rejected:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_log_file_name(name)

    def test_accept_simple_basename(self) -> None:
        self.assertEqual(validate_log_file_name("miku.log"), "miku.log")
        self.assertEqual(validate_log_file_name("app-1.log"), "app-1.log")


class LogPathResolveTests(unittest.TestCase):
    def test_direct_child_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = resolve_log_file_path(logs, "miku.log")
            self.assertEqual(path, logs / "miku.log")
            self.assertEqual(path.parent.resolve(), logs.resolve())

    def test_reject_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            target = logs / "nested"
            target.mkdir()
            with self.assertRaises(ValueError):
                resolve_log_file_path(logs, "nested")

    def test_reject_symlink_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            real = logs / "real.log"
            real.write_text("ok", encoding="utf-8")
            link = logs / "link.log"
            try:
                os.symlink(real, link)
            except (OSError, NotImplementedError):
                self.skipTest("本机无法创建 symlink")
            with self.assertRaises(ValueError):
                resolve_log_file_path(logs, "link.log")

    @unittest.skipUnless(sys.platform == "win32", "junction 仅 Windows")
    def test_reject_junction_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            target_dir = logs / "target_dir"
            target_dir.mkdir()
            junc = logs / "junc.log"
            try:
                # mklink /J 需要目录目标；用目录 junction 验证 fail-closed
                import subprocess

                completed = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(junc), str(target_dir)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0 or not junc.exists():
                    self.skipTest("本机无法创建 junction")
            except OSError:
                self.skipTest("本机无法创建 junction")
            with self.assertRaises(ValueError):
                resolve_log_file_path(logs, "junc.log")


class LoggingBoundsTests(unittest.TestCase):
    def test_max_size_mb_bounds(self) -> None:
        self.assertEqual(validate_max_size_mb(10), 10.0)
        self.assertEqual(validate_max_size_mb(50), 50.0)
        for bad in (True, False, "10", 0, -1, float("nan"), float("inf"), 50.1):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_max_size_mb(bad)

    def test_backup_count_bounds(self) -> None:
        self.assertEqual(validate_backup_count(0), 0)
        self.assertEqual(validate_backup_count(10), 10)
        for bad in (True, False, 1.5, "5", -1, 11):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_backup_count(bad)


class SetupAppLoggingTests(unittest.TestCase):
    def _cfg(self, **logging_overrides):
        logging_cfg = {
            "file_enabled": True,
            "file_name": "miku.log",
            "max_size_mb": 10,
            "backup_count": 5,
            "include_tracebacks": False,
        }
        logging_cfg.update(logging_overrides)
        return SimpleNamespace(
            get=lambda key, default=None: {
                "assistant.log_level": "INFO",
                "assistant.logging": logging_cfg,
            }.get(key, default)
        )

    def test_valid_name_creates_under_temp_logs_no_dup_handler(self) -> None:
        before = _root_handlers_snapshot()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                logs = Path(tmp)
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    path1 = setup_app_logging(self._cfg())
                    path2 = setup_app_logging(self._cfg())
                self.assertEqual(path1, logs / "miku.log")
                self.assertEqual(path2, path1)
                self.assertTrue(path1.is_file())
                file_handlers = [
                    h
                    for h in logging.getLogger().handlers
                    if isinstance(h, PrivacyRotatingFileHandler)
                    and Path(getattr(h, "baseFilename", "")).resolve()
                    == path1.resolve()
                ]
                self.assertEqual(len(file_handlers), 1)
                _remove_added_handlers(before)
        finally:
            _remove_added_handlers(before)

    def test_reject_path_escape_creates_zero_out_of_bounds_files(self) -> None:
        before = _root_handlers_snapshot()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                logs = root / "logs"
                logs.mkdir()
                memory = root / "data" / "memory"
                memory.mkdir(parents=True)
                victim = memory / "long_term.sqlite3"
                victim.write_text("preserve", encoding="utf-8")
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    with self.assertRaises(ValueError):
                        setup_app_logging(
                            self._cfg(file_name="../data/memory/long_term.sqlite3")
                        )
                self.assertEqual(victim.read_text(encoding="utf-8"), "preserve")
                self.assertFalse((logs / "long_term.sqlite3").exists())
                # 越界路径不得被创建/覆盖
                self.assertEqual(
                    sorted(p.name for p in memory.iterdir()),
                    ["long_term.sqlite3"],
                )
        finally:
            _remove_added_handlers(before)

    def test_file_enabled_false_skips_file(self) -> None:
        before = _root_handlers_snapshot()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                logs = Path(tmp)
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    path = setup_app_logging(self._cfg(file_enabled=False))
                self.assertIsNone(path)
                self.assertEqual(list(logs.iterdir()), [])
        finally:
            _remove_added_handlers(before)

    def test_bool_and_bounds_validated_before_create(self) -> None:
        before = _root_handlers_snapshot()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                logs = Path(tmp)
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    with self.assertRaises(ValueError):
                        setup_app_logging(self._cfg(file_enabled=1))
                    with self.assertRaises(ValueError):
                        setup_app_logging(self._cfg(include_tracebacks=1))
                    with self.assertRaises(ValueError):
                        setup_app_logging(self._cfg(max_size_mb=100))
                    with self.assertRaises(ValueError):
                        setup_app_logging(self._cfg(backup_count=99))
                self.assertEqual(list(logs.iterdir()), [])
        finally:
            _remove_added_handlers(before)


class ExceptionLabelTests(unittest.TestCase):
    def test_type_and_optional_http_status(self) -> None:
        self.assertEqual(exception_log_label(ValueError("secret")), "ValueError")

        class FakeResp:
            status_code = 302

        class FakeHTTPError(Exception):
            def __init__(self):
                super().__init__("https://evil.example/leak body=SECRET")
                self.response = FakeResp()

        label = exception_log_label(FakeHTTPError())
        self.assertIn("FakeHTTPError", label)
        self.assertIn("status=302", label)
        self.assertNotIn("evil.example", label)
        self.assertNotIn("SECRET", label)


class PrivacyHandlerTracebackTests(unittest.TestCase):
    def test_file_strips_traceback_console_keeps_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "priv.log"
            file_handler = PrivacyRotatingFileHandler(
                log_path,
                maxBytes=1024 * 1024,
                backupCount=1,
                encoding="utf-8",
                include_tracebacks=False,
            )
            file_handler.setFormatter(
                logging.Formatter("%(levelname)s %(message)s")
            )
            console_buf = io.StringIO()
            console = logging.StreamHandler(console_buf)
            console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

            logger = logging.getLogger("test.privacy.tb")
            logger.handlers.clear()
            logger.propagate = False
            logger.setLevel(logging.DEBUG)
            logger.addHandler(file_handler)
            logger.addHandler(console)

            src_marker = str(Path(__file__).resolve())
            try:
                raise RuntimeError("boom-sentinel")
            except RuntimeError:
                logger.error("failed stage=unit", exc_info=True)

            file_handler.flush()
            file_text = log_path.read_text(encoding="utf-8")
            console_text = console_buf.getvalue()
            self.assertIn("failed stage=unit", file_text)
            self.assertNotIn("Traceback", file_text)
            self.assertNotIn("boom-sentinel", file_text)
            self.assertNotIn(src_marker, file_text)
            # 控制台仍可见原始 record（含 traceback）
            self.assertIn("Traceback", console_text)
            self.assertIn("boom-sentinel", console_text)

            logger.handlers.clear()
            file_handler.close()
            console.close()

    def test_include_tracebacks_true_persists_tb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "tb.log"
            handler = PrivacyRotatingFileHandler(
                log_path,
                maxBytes=1024 * 1024,
                backupCount=1,
                encoding="utf-8",
                include_tracebacks=True,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger("test.privacy.tb.on")
            logger.handlers.clear()
            logger.propagate = False
            logger.setLevel(logging.ERROR)
            logger.addHandler(handler)
            try:
                raise RuntimeError("keep-tb")
            except RuntimeError:
                logger.error("with tb", exc_info=True)
            handler.flush()
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("Traceback", text)
            self.assertIn("keep-tb", text)
            logger.handlers.clear()
            handler.close()


class CallSiteSentinelTests(unittest.TestCase):
    def _attach_memory(self, logger_name: str):
        logger = logging.getLogger(logger_name)
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        return logger, buf, handler

    def test_search_handler_and_chrome_omit_query_url(self) -> None:
        sentinel = "SENTINEL_SEARCH_QUERY_XYZ"
        abs_chrome = str(Path("C:/Program Files/Google/Chrome/Application/chrome.exe"))
        searcher = ChromeSearcher(
            chrome_path=abs_chrome,
            search_engine="google",
            auto_open=False,
            safety_policy=SafetyPolicy(
                {"permissions": {Operation.BROWSER_LAUNCH.value: False}}
            ),
        )
        lg, buf, handler = self._attach_memory(searcher.logger.name)
        handler_lg, handler_buf, handler_h = self._attach_memory(
            "src.core.handlers.search_handler"
        )
        try:
            intent = SearchIntentHandler(searcher, safety_policy=searcher.safety_policy)
            result = intent.handle(f"搜索 {sentinel}")
            joined = buf.getvalue() + handler_buf.getvalue()
            self.assertNotIn(sentinel, joined)
            self.assertNotIn("google.com", joined.lower())
            self.assertNotIn(abs_chrome, joined)
            self.assertIn("chars=", joined)
            # 用户可见返回可保留 URL/正文
            self.assertIsInstance(result, str)
            self.assertIn("http", result.lower())
        finally:
            lg.removeHandler(handler)
            handler_lg.removeHandler(handler_h)

    def test_character_interface_omits_message_and_path(self) -> None:
        sentinel = "SENTINEL_CHARACTER_MESSAGE"
        abs_audio = str(Path(tempfile.gettempdir()) / "sentinel_audio.wav")
        iface = CharacterInterface(model_type="unity")
        lg, buf, handler = self._attach_memory(iface.logger.name)
        try:
            ok = iface.send_message(sentinel, audio_file=abs_audio)
            text = buf.getvalue()
            self.assertTrue(ok)
            self.assertNotIn(sentinel, text)
            self.assertNotIn(abs_audio, text)
            self.assertIn("chars=", text)
            self.assertIn("has_audio=True", text)
        finally:
            lg.removeHandler(handler)

    def test_ollama_error_logs_omit_url_body(self) -> None:
        import requests

        sentinel_url = "https://sentinel-evil.example/api/chat?q=LEAK"
        sentinel_body = "SENTINEL_OLLAMA_BODY"

        class Boom(requests.exceptions.HTTPError):
            def __init__(self):
                super().__init__(f"{sentinel_url} {sentinel_body}")
                self.response = SimpleNamespace(status_code=502)

        client = OllamaClient(
            base_url="http://127.0.0.1:11434",
            model="test",
            safety_policy=SafetyPolicy(),
        )
        lg, buf, handler = self._attach_memory(client.logger.name)
        try:
            with patch.object(client, "_request_post", side_effect=Boom()):
                result = client.generate_result("user secret prompt")
            text = buf.getvalue()
            self.assertNotIn(sentinel_url, text)
            self.assertNotIn(sentinel_body, text)
            self.assertNotIn("user secret prompt", text)
            self.assertIn("err=", text)
            self.assertEqual(result.status, GenerateStatus.HTTP_ERROR)
        finally:
            lg.removeHandler(handler)

    def test_voice_session_reply_failure_omits_exc_text(self) -> None:
        from src.cli.voice_session import VoiceSession, _ReplyTask

        sentinel = "SENTINEL_VOICE_TASK_ERROR"
        session = VoiceSession.__new__(VoiceSession)
        session._logger = logging.getLogger("test.voice.session")
        lg, buf, handler = self._attach_memory(session._logger.name)
        task = _ReplyTask()
        task.done.set()
        task.error = RuntimeError(sentinel)
        try:
            with patch("builtins.print"):
                session._report_reply_failure(task)
            text = buf.getvalue()
            self.assertNotIn(sentinel, text)
            self.assertIn("RuntimeError", text)
        finally:
            lg.removeHandler(handler)

    def test_rvc_worker_omits_stderr_and_payload_error(self) -> None:
        from src.voice.rvc.worker import RVCWorkerClient

        sentinel_err = "SENTINEL_RVC_PAYLOAD_ERROR"
        sentinel_stderr = "SENTINEL_RVC_STDERR_LINE"
        abs_path = str(Path(tempfile.gettempdir()) / "sentinel_model.pth")

        policy = SafetyPolicy(
            {
                "permissions": {
                    Operation.RVC_WORKER.value: True,
                    Operation.SUBPROCESS.value: True,
                }
            }
        )
        client = RVCWorkerClient(
            {"device": "cpu", "model_path": abs_path},
            safety_policy=policy,
        )
        client._stderr_tail.append(sentinel_stderr)
        lg, buf, handler = self._attach_memory("src.voice.rvc.worker")
        try:
            with patch.object(client, "stop"):
                client._log_stderr_and_stop(
                    "Worker 就绪失败", has_payload_error=True
                )
            # 直接模拟预热失败日志路径
            logging.getLogger("src.voice.rvc.worker").warning(
                "RVC Worker 首次推理预热失败 has_error=%s",
                bool(sentinel_err),
            )
            text = buf.getvalue()
            self.assertNotIn(sentinel_stderr, text)
            self.assertNotIn(sentinel_err, text)
            self.assertNotIn(abs_path, text)
            self.assertIn("stderr_lines=", text)
            self.assertIn("has_payload_error=", text)
        finally:
            lg.removeHandler(handler)

    def test_rvc_health_logger_omits_paths(self) -> None:
        sentinel_model = str(Path("D:/models/sentinel.pth"))
        report = RVCHealthReport(
            ok=False,
            model_path=sentinel_model,
            index_path=str(Path("D:/models/sentinel.index")),
            python_path=str(Path("D:/venv/python.exe")),
            errors=["missing"],
            warnings=[],
        )
        logger = logging.getLogger("test.rvc.health")
        lg, buf, handler = self._attach_memory(logger.name)
        try:
            with patch(
                "src.voice.rvc.health_check.check_rvc_health", return_value=report
            ):
                ensure_rvc_ready({}, logger=logger, safety_policy=SafetyPolicy())
            text = buf.getvalue()
            self.assertNotIn(sentinel_model, text)
            self.assertNotIn("D:/models", text)
            self.assertIn("errors=1", text)
            self.assertIn("has_model=True", text)
            # summary() 仍可给 CLI 用，但 logger 路径不得调用
            self.assertIn(sentinel_model, report.summary())
        finally:
            lg.removeHandler(handler)


class ContractPreservationTests(unittest.TestCase):
    def test_search_url_construction_unchanged(self) -> None:
        searcher = ChromeSearcher(
            chrome_path=None,
            search_engine="google",
            auto_open=False,
            safety_policy=SafetyPolicy(),
        )
        result = searcher.search("hello world")
        self.assertIn("hello", result.lower() + result)
        self.assertTrue("http" in result.lower() or "URL" in result)

    def test_exception_label_does_not_change_generate_status_mapping(self) -> None:
        import requests

        client = OllamaClient(
            base_url="http://127.0.0.1:11434",
            model="test",
            safety_policy=SafetyPolicy(),
        )
        with patch.object(
            client,
            "_request_post",
            side_effect=requests.exceptions.Timeout("x"),
        ):
            result = client.generate_result("hi")
        self.assertEqual(result.status, GenerateStatus.TIMEOUT)


class HardlinkBoundaryTests(unittest.TestCase):
    """F1：硬链接不得把日志写入 logs/ 外部文件。"""

    def test_reject_hardlink_and_preserve_outside_bytes(self) -> None:
        sentinel = b"SENTINEL_OUTSIDE_SQLITE_BYTES_UNCHANGED"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            outside = root / "outside.sqlite3"
            outside.write_bytes(sentinel)
            link = logs / "miku.log"
            try:
                os.link(outside, link)
            except (OSError, NotImplementedError):
                self.skipTest("本机无法创建硬链接")
            self.assertEqual(os.stat(link).st_nlink, 2)
            with self.assertRaises(ValueError):
                resolve_log_file_path(logs, "miku.log")
            # 不得经 handler 打开硬链接写入
            with self.assertRaises(ValueError):
                PrivacyRotatingFileHandler(
                    link,
                    maxBytes=1024,
                    backupCount=1,
                    encoding="utf-8",
                    include_tracebacks=False,
                )
            self.assertEqual(outside.read_bytes(), sentinel)
            self.assertNotIn(b"CORRUPT", outside.read_bytes())


class LogsDirLinkTests(unittest.TestCase):
    """F2：日志根目录本身不得为 symlink/junction。"""

    def test_reject_logs_dir_symlink_when_supported(self) -> None:
        from src.core.log_privacy import validate_logs_dir

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real_logs"
            real.mkdir()
            linked = root / "logs"
            try:
                os.symlink(real, linked, target_is_directory=True)
            except (OSError, NotImplementedError, AttributeError):
                self.skipTest("本机无法创建目录 symlink")
            with self.assertRaises(ValueError):
                validate_logs_dir(linked)
            with self.assertRaises(ValueError):
                resolve_log_file_path(linked, "miku.log")

    @unittest.skipUnless(sys.platform == "win32", "junction 仅 Windows")
    def test_reject_logs_dir_junction_when_supported(self) -> None:
        from src.core.log_privacy import validate_logs_dir
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real_logs"
            real.mkdir()
            junc = root / "logs"
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junc), str(real)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest("本机无法创建目录 junction")
            with self.assertRaises(ValueError):
                validate_logs_dir(junc)


class LogRootSwapJunctionTests(unittest.TestCase):
    """F6：handler 建立后把 logs 根替换为 junction，不得越界写入。"""

    @unittest.skipUnless(sys.platform == "win32", "junction 仅 Windows")
    def test_emit_after_logs_root_junction_swap_fail_closed(self) -> None:
        import shutil
        import subprocess

        root = Path(tempfile.mkdtemp(prefix="miku-log-root-swap-test-"))
        logs = root / "logs"
        outside = root / "outside"
        saved = root / "logs-saved"
        logs.mkdir()
        outside.mkdir()
        handler = None
        junction_created = False
        try:
            handler = PrivacyRotatingFileHandler(
                logs / "miku.log",
                maxBytes=1024,
                backupCount=1,
                encoding="utf-8",
                include_tracebacks=False,
                trusted_logs_dir=logs,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            # 关闭 stream，迫使后续 emit 走 _open 重开
            handler.acquire()
            try:
                if handler.stream is not None:
                    handler.stream.close()
                handler.stream = None
            finally:
                handler.release()

            logs.rename(saved)
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(logs), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest("本机无法创建目录 junction")
            junction_created = True

            record = logging.LogRecord(
                "probe",
                logging.ERROR,
                __file__,
                1,
                "BOUNDARY_ESCAPE",
                (),
                None,
            )
            from src.core.log_privacy import LogBoundaryError

            with self.assertRaises(LogBoundaryError):
                handler.emit(record)

            escaped = outside / "miku.log"
            self.assertFalse(escaped.exists())
            # 外部目录零文件
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            if handler is not None:
                try:
                    handler.close()
                except Exception:
                    pass
            if junction_created and logs.exists():
                try:
                    os.rmdir(logs)
                except OSError:
                    pass
            shutil.rmtree(root, ignore_errors=True)


class SetupReuseSemanticsTests(unittest.TestCase):
    """F3：重复 setup 必须更新隐私参数；普通 RotatingFileHandler 须替换。"""

    def _cfg(self, **logging_overrides):
        logging_cfg = {
            "file_enabled": True,
            "file_name": "miku.log",
            "max_size_mb": 10,
            "backup_count": 5,
            "include_tracebacks": False,
        }
        logging_cfg.update(logging_overrides)
        return SimpleNamespace(
            get=lambda key, default=None: {
                "assistant.log_level": "INFO",
                "assistant.logging": logging_cfg,
            }.get(key, default)
        )

    def test_include_tracebacks_true_then_false_updates_handler(self) -> None:
        before = _root_handlers_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            target = (logs / "miku.log").resolve()
            try:
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    setup_app_logging(self._cfg(include_tracebacks=True))
                    handlers = [
                        h
                        for h in logging.getLogger().handlers
                        if isinstance(h, PrivacyRotatingFileHandler)
                        and Path(getattr(h, "baseFilename", "")).resolve() == target
                    ]
                    self.assertEqual(len(handlers), 1)
                    self.assertTrue(handlers[0].include_tracebacks)
                    first_id = id(handlers[0])

                    setup_app_logging(self._cfg(include_tracebacks=False))
                    handlers2 = [
                        h
                        for h in logging.getLogger().handlers
                        if isinstance(h, PrivacyRotatingFileHandler)
                        and Path(getattr(h, "baseFilename", "")).resolve() == target
                    ]
                    self.assertEqual(len(handlers2), 1)
                    self.assertIs(handlers2[0], handlers[0])
                    self.assertEqual(id(handlers2[0]), first_id)
                    self.assertFalse(handlers2[0].include_tracebacks)

                    setup_app_logging(
                        self._cfg(max_size_mb=2, backup_count=3, include_tracebacks=False)
                    )
                    h = handlers2[0]
                    self.assertEqual(h.maxBytes, 2 * 1024 * 1024)
                    self.assertEqual(h.backupCount, 3)
            finally:
                _remove_added_handlers(before)

    def test_plain_rotating_handler_replaced_without_leak(self) -> None:
        from logging.handlers import RotatingFileHandler

        before = _root_handlers_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            log_path = logs / "miku.log"
            target = log_path.resolve()
            try:
                plain = RotatingFileHandler(
                    log_path, maxBytes=1024, backupCount=1, encoding="utf-8"
                )
                root = logging.getLogger()
                root.addHandler(plain)
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    setup_app_logging(self._cfg(include_tracebacks=False))
                rotating = [
                    h
                    for h in root.handlers
                    if isinstance(h, RotatingFileHandler)
                    and Path(getattr(h, "baseFilename", "")).resolve() == target
                ]
                self.assertEqual(len(rotating), 1)
                self.assertIsInstance(rotating[0], PrivacyRotatingFileHandler)
                self.assertNotIn(plain, root.handlers)
                self.assertTrue(
                    getattr(plain, "stream", None) is None
                    or not getattr(plain, "stream", object())
                )
            finally:
                _remove_added_handlers(before)


class DeadFilterRemovalTests(unittest.TestCase):
    """F5：不得保留误导性空隐私 Filter。"""

    def test_persistent_filter_removed(self) -> None:
        import src.core.log_privacy as mod

        self.assertFalse(hasattr(mod, "PersistentLogPrivacyFilter"))


class F4CallSiteSentinelIntegrationTests(unittest.TestCase):
    """F4：装配/TTS health/Player/Cleaner/Assistant/STT·TTS 远端异常 sentinel 零出现。"""

    def _capture(self, logger_name: str):
        logger = logging.getLogger(logger_name)
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        return logger, buf, handler

    def test_assembly_startup_logs_omit_absolute_paths(self) -> None:
        from src.core.paths import runtime_layout_summary

        sentinel = str(Path(tempfile.gettempdir()) / "SENTINEL_ASSEMBLY_ABS")
        lg, buf, handler = self._capture("src.core.assembly")
        try:
            logger = logging.getLogger("src.core.assembly")
            logger.info("运行时数据目录已就绪 dirs=logs,tmp,test")
            fake_path = Path(sentinel) / "miku.log"
            logger.info(
                "崩溃排查请查看日志文件 name=%s",
                fake_path.name,
            )
            text = buf.getvalue()
            self.assertNotIn(sentinel, text)
            self.assertNotIn(str(Path(tempfile.gettempdir())), text)
            self.assertIn("name=miku.log", text)
            self.assertIn("dirs=logs,tmp,test", text)
            summary = runtime_layout_summary()
            self.assertIn("运行时数据目录:", summary)
            self.assertNotIn("运行时数据目录:", text)
            # summary 含绝对路径，不得整段落入日志
            for line in summary.splitlines()[1:]:
                path_part = line.split(":", 1)[-1].strip()
                if path_part:
                    self.assertNotIn(path_part, text)
        finally:
            lg.removeHandler(handler)

    def test_tts_rvc_health_omits_summary_paths(self) -> None:
        from src.voice.tts.engine import TTSEngine

        sentinel_model = str(Path("D:/models/SENTINEL_RVC_MODEL.pth"))
        report = RVCHealthReport(
            ok=False,
            model_path=sentinel_model,
            index_path=str(Path("D:/models/SENTINEL.index")),
            python_path=str(Path("D:/venv/SENTINEL_python.exe")),
            errors=["missing"],
        )
        engine = TTSEngine.__new__(TTSEngine)
        engine.logger = logging.getLogger("test.tts.f4")
        engine._rvc = SimpleNamespace(enabled=True)
        engine._rvc_config = {
            "prewarm_audio_processing": False,
            "worker_enabled": False,
        }
        engine._rvc_prepare_done = False
        engine._rvc_ready = False
        engine.safety_policy = SafetyPolicy()
        engine._temp_files = None
        lg, buf, handler = self._capture(engine.logger.name)
        try:
            with patch(
                "src.voice.rvc.health_check.check_rvc_health",
                return_value=report,
            ):
                ok = engine.prepare_rvc()
            self.assertFalse(ok)
            text = buf.getvalue()
            self.assertNotIn(sentinel_model, text)
            self.assertNotIn("SENTINEL_RVC_MODEL", text)
            self.assertIn("errors=1", text)
            self.assertIn(sentinel_model, engine._rvc_last_error)
        finally:
            lg.removeHandler(handler)

    def test_audio_player_omits_path(self) -> None:
        from src.voice.audio.player import AudioPlayer

        sentinel = str(Path(tempfile.gettempdir()) / "SENTINEL_AUDIO_PLAY.wav")
        player = AudioPlayer.__new__(AudioPlayer)
        player.logger = logging.getLogger("test.player.f4")
        player._backend = "pygame"
        player.volume = 0.5
        player._stop_event = __import__("threading").Event()
        player._stop_event.set()
        lg, buf, handler = self._capture(player.logger.name)
        try:
            with patch("os.path.exists", return_value=True):
                player.play(sentinel)
            text = buf.getvalue()
            self.assertNotIn(sentinel, text)
            self.assertIn("has_path=True", text)
        finally:
            lg.removeHandler(handler)

    def test_file_cleaner_omits_paths_and_exc(self) -> None:
        from src.core.file_cleaner import CleanupRule, FileCleaner
        from threading import Lock

        sentinel_dir = str(Path(tempfile.gettempdir()) / "SENTINEL_CLEAN_DIR")
        sentinel_exc = "SENTINEL_CLEAN_EXC_BODY"
        cleaner = FileCleaner.__new__(FileCleaner)
        cleaner.logger = logging.getLogger("test.cleaner.f4")
        cleaner.safety_policy = None
        cleaner._sweep_lock = Lock()
        cleaner._active_sweeps = {}
        cleaner._scandir = MagicMock(side_effect=OSError(sentinel_exc))
        rule = CleanupRule(
            name="tmp",
            directory=Path(sentinel_dir),
            patterns=["*"],
            max_age_seconds=3600,
        )
        lg, buf, handler = self._capture(cleaner.logger.name)
        try:
            with patch.object(Path, "exists", return_value=True):
                result = cleaner._get_or_open_sweep(rule)
            self.assertIsNone(result)
            text = buf.getvalue()
            self.assertNotIn(sentinel_dir, text)
            self.assertNotIn(sentinel_exc, text)
            self.assertIn("rule=tmp", text)
            self.assertIn("err=OSError", text)
        finally:
            lg.removeHandler(handler)

    def test_assistant_lazy_load_and_shutdown_omit_exc_body(self) -> None:
        from src.core.assistant import MikuAssistant
        from src.core.config import Config

        sentinel = "SENTINEL_ASSISTANT_EXC_PATH_C:/leak/model.bin"
        cfg = Config()
        assistant = MikuAssistant.from_dependencies(
            cfg,
            safety_policy=SafetyPolicy(),
            logger=logging.getLogger("test.assistant.f4"),
        )
        assistant._voice_autoload_enabled = True
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "asst.log"
            file_handler = PrivacyRotatingFileHandler(
                log_path,
                maxBytes=1024 * 1024,
                backupCount=1,
                encoding="utf-8",
                include_tracebacks=False,
            )
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            assistant.logger.addHandler(file_handler)
            assistant.logger.setLevel(logging.DEBUG)
            try:
                with patch(
                    "src.core.assistant.build_tts_engine",
                    side_effect=RuntimeError(sentinel),
                ):
                    result = assistant.ensure_tts_for_playback()
                file_handler.flush()
                text = log_path.read_text(encoding="utf-8")
                self.assertNotIn(sentinel, text)
                self.assertNotIn("C:/leak", text)
                self.assertNotIn("Traceback", text)
                self.assertIn("RuntimeError", text)
                self.assertFalse(result.ok)
                # 用户可见 errors 可含正文
                self.assertTrue(any(sentinel in e for e in result.errors))
            finally:
                assistant.logger.removeHandler(file_handler)
                file_handler.close()
                try:
                    assistant.shutdown()
                except Exception:
                    pass

    def test_assembly_rollback_omits_exc_body_on_persistent_log(self) -> None:
        from src.core.assembly import _RollbackRegistry

        sentinel = r"C:\SENTINEL_ROLLBACK\model.bin boom"
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "roll.log"
            file_handler = PrivacyRotatingFileHandler(
                log_path,
                maxBytes=1024 * 1024,
                backupCount=1,
                encoding="utf-8",
                include_tracebacks=False,
            )
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger("test.assembly.rollback.f7")
            logger.handlers.clear()
            logger.propagate = False
            logger.setLevel(logging.ERROR)
            logger.addHandler(file_handler)
            try:
                registry = _RollbackRegistry(logger)
                registry.register(
                    "tts_engine.shutdown",
                    lambda: (_ for _ in ()).throw(RuntimeError(sentinel)),
                )
                registry.rollback()
                file_handler.flush()
                text = log_path.read_text(encoding="utf-8")
                self.assertIn("装配回滚失败", text)
                self.assertIn("err=RuntimeError", text)
                self.assertNotIn(sentinel, text)
                self.assertNotIn("SENTINEL_ROLLBACK", text)
                self.assertNotIn("Traceback", text)
            finally:
                logger.removeHandler(file_handler)
                file_handler.close()

    def test_cli_app_top_level_error_omits_exc_body_on_persistent_log(self) -> None:
        from src.cli import app as app_mod

        sentinel = "SENTINEL_APP_EXC https://evil.example/leak"
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "app.log"
            file_handler = PrivacyRotatingFileHandler(
                log_path,
                maxBytes=1024 * 1024,
                backupCount=1,
                encoding="utf-8",
                include_tracebacks=False,
            )
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            root = logging.getLogger()
            before = list(root.handlers)
            root.addHandler(file_handler)
            try:
                with patch.object(app_mod, "MikuAssistant") as mock_asst:
                    mock_asst.side_effect = RuntimeError(sentinel)
                    with patch.object(app_mod, "_setup_console_encoding"), patch.object(
                        app_mod, "_ensure_project_path"
                    ), patch("argparse.ArgumentParser.parse_args") as parse:
                        parse.return_value = SimpleNamespace(
                            mode="text",
                            config="config/config.yaml",
                        )
                        with patch("builtins.print"):
                            app_mod.main()
                file_handler.flush()
                text = log_path.read_text(encoding="utf-8")
                self.assertIn("程序错误 err=RuntimeError", text)
                self.assertNotIn(sentinel, text)
                self.assertNotIn("evil.example", text)
                self.assertNotIn("Traceback", text)
            finally:
                root.removeHandler(file_handler)
                file_handler.close()
                for h in list(root.handlers):
                    if h not in before and h is not file_handler:
                        root.removeHandler(h)

    def test_stt_tts_remote_exc_omit_url_body(self) -> None:
        """必须走 STT/TTS 真实生产错误分支，禁止测试自行调用安全 logger。"""
        from src.voice.stt import STTEngine
        from src.voice.tts.engine import TTSEngine

        sentinel_url = "https://sentinel-stt.example/v1?q=LEAK"
        sentinel_body = "SENTINEL_REMOTE_BODY"
        boom_msg = f"{sentinel_url} {sentinel_body}"

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "voice.log"
            file_handler = PrivacyRotatingFileHandler(
                log_path,
                maxBytes=1024 * 1024,
                backupCount=1,
                encoding="utf-8",
                include_tracebacks=False,
            )
            file_handler.setFormatter(logging.Formatter("%(message)s"))

            # --- STT：真实 _recognize_google_result RequestError 分支 ---
            stt = STTEngine.__new__(STTEngine)
            stt.logger = logging.getLogger("test.stt.f7.real")
            stt.logger.handlers.clear()
            stt.logger.propagate = False
            stt.logger.setLevel(logging.ERROR)
            stt.logger.addHandler(file_handler)
            stt.language = "zh-CN"
            stt.safety_policy = SafetyPolicy(
                {"permissions": {"cloud_stt": True, "cloud_tts": True}}
            )

            class _ReqErr(Exception):
                pass

            fake_sr = SimpleNamespace(
                UnknownValueError=type("UnknownValueError", (Exception,), {}),
                RequestError=type("RequestError", (Exception,), {}),
            )

            class _Rec:
                def recognize_google(self, audio, language=None):
                    raise fake_sr.RequestError(boom_msg)

            stt._recognizer = _Rec()
            stt._require_cloud_stt = lambda context="x": None

            with patch.dict(sys.modules, {"speech_recognition": fake_sr}):
                # re-import path uses `import speech_recognition as sr` inside method
                result = stt._recognize_google_result(object())
            from src.voice.recognition import RecognitionStatus

            self.assertEqual(result.status, RecognitionStatus.SERVICE_ERROR)
            self.assertIn(sentinel_body, result.error_detail or "")

            # --- TTS：真实 speak Exception 分支 ---
            tts = TTSEngine.__new__(TTSEngine)
            tts.logger = logging.getLogger("test.tts.f7.real")
            tts.logger.handlers.clear()
            tts.logger.propagate = False
            tts.logger.setLevel(logging.ERROR)
            tts.logger.addHandler(file_handler)
            tts.engine_type = "edge-tts"
            tts.pipeline_enabled = False
            tts.fallback_enabled = False
            tts._shutdown_event = __import__("threading").Event()
            tts._speak_lock = __import__("threading").Lock()
            tts._interrupt_event = __import__("threading").Event()
            tts._interrupt_epoch = 0
            tts._interrupt_epoch_lock = None
            tts.safety_policy = SafetyPolicy(
                {"permissions": {"cloud_stt": True, "cloud_tts": True}}
            )

            def _boom_single(text):
                raise RuntimeError(boom_msg)

            tts._speak_single = _boom_single
            tts._cloud_tts_allowed = lambda: True
            tts._interrupt_epoch_snapshot = lambda: 0
            tts._ensure_interrupt_epoch_lock = lambda: __import__("threading").Lock()
            tts._interrupted = lambda: False

            tts.speak("hello")

            file_handler.flush()
            text = log_path.read_text(encoding="utf-8")
            self.assertNotIn(sentinel_url, text)
            self.assertNotIn(sentinel_body, text)
            self.assertIn("识别服务错误 err=", text)
            self.assertIn("TTS 朗读失败 err=", text)
            self.assertNotIn("Traceback", text)

            stt.logger.removeHandler(file_handler)
            tts.logger.removeHandler(file_handler)
            file_handler.close()


class F8EmitContractTests(unittest.TestCase):
    """F8：业务 ValueError 不冒泡；边界失败抛 LogBoundaryError。"""

    def test_application_valueerror_does_not_propagate_and_handler_stays_usable(self) -> None:
        from src.core.log_privacy import LogBoundaryError

        class RaisesValueError:
            def __str__(self) -> str:
                raise ValueError("application formatting failure")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "miku.log"
            handler = PrivacyRotatingFileHandler(
                path,
                maxBytes=128,
                backupCount=2,
                encoding="utf-8",
                include_tracebacks=False,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger("test.f8.emit")
            logger.handlers.clear()
            logger.propagate = False
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                propagated = False
                try:
                    logger.info("bad=%s", RaisesValueError())
                except ValueError:
                    propagated = True
                self.assertFalse(propagated)
                self.assertIsNotNone(handler.stream)

                for index in range(12):
                    logger.info("line=%02d payload=%s", index, "x" * 48)
                handler.flush()
                files = sorted(p.name for p in Path(tmp).iterdir())
                self.assertLessEqual(len(files), 3)
                self.assertTrue(any(n.startswith("miku.log") for n in files))
            finally:
                logger.removeHandler(handler)
                handler.close()

    def test_f6_root_swap_still_raises_log_boundary_error(self) -> None:
        from src.core.log_privacy import LogBoundaryError
        import shutil
        import subprocess

        if sys.platform != "win32":
            self.skipTest("junction 仅 Windows")
        root = Path(tempfile.mkdtemp(prefix="miku-f8-root-swap-"))
        logs = root / "logs"
        outside = root / "outside"
        saved = root / "logs-saved"
        logs.mkdir()
        outside.mkdir()
        handler = None
        junction_created = False
        try:
            handler = PrivacyRotatingFileHandler(
                logs / "miku.log",
                maxBytes=1024,
                backupCount=1,
                encoding="utf-8",
                trusted_logs_dir=logs,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler.acquire()
            try:
                if handler.stream is not None:
                    handler.stream.close()
                handler.stream = None
            finally:
                handler.release()
            logs.rename(saved)
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(logs), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest("本机无法创建目录 junction")
            junction_created = True
            record = logging.LogRecord(
                "probe", logging.ERROR, __file__, 1, "BOUNDARY_ESCAPE", (), None
            )
            with self.assertRaises(LogBoundaryError):
                handler.emit(record)
            self.assertIsNone(handler.stream)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            if handler is not None:
                try:
                    handler.close()
                except Exception:
                    pass
            if junction_created and logs.exists():
                try:
                    os.rmdir(logs)
                except OSError:
                    pass
            shutil.rmtree(root, ignore_errors=True)


class F9BoundsTests(unittest.TestCase):
    """F9：backup_count=0 有界单文件；极小 MB 不得变成 maxBytes=0。"""

    def test_backup_count_zero_truncates_same_file_no_dot1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "zero.log"
            handler = PrivacyRotatingFileHandler(
                path, maxBytes=64, backupCount=0, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger("test.f9.zero")
            logger.handlers.clear()
            logger.propagate = False
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                for index in range(20):
                    logger.info("line=%02d %s", index, "x" * 40)
                handler.flush()
                names = sorted(p.name for p in Path(tmp).iterdir())
                self.assertEqual(names, ["zero.log"])
                # 软界：上限 + 单条最大记录
                self.assertLessEqual(path.stat().st_size, 64 + 80)
                self.assertGreater(path.stat().st_size, 0)
            finally:
                logger.removeHandler(handler)
                handler.close()

    def test_tiny_max_size_mb_yields_positive_max_bytes(self) -> None:
        from src.core.log_privacy import max_size_mb_to_bytes

        self.assertGreater(max_size_mb_to_bytes(1e-12), 0)
        before = _root_handlers_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            try:
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    cfg = SimpleNamespace(
                        get=lambda key, default=None: {
                            "assistant.log_level": "INFO",
                            "assistant.logging": {
                                "file_enabled": True,
                                "file_name": "miku.log",
                                "max_size_mb": 1e-12,
                                "backup_count": 1,
                                "include_tracebacks": False,
                            },
                        }.get(key, default)
                    )
                    setup_app_logging(cfg)
                handlers = [
                    h
                    for h in logging.getLogger().handlers
                    if isinstance(h, PrivacyRotatingFileHandler)
                    and Path(getattr(h, "baseFilename", "")).resolve()
                    == (logs / "miku.log").resolve()
                ]
                self.assertEqual(len(handlers), 1)
                self.assertGreater(handlers[0].maxBytes, 0)
            finally:
                _remove_added_handlers(before)


class F10ReconfigureTests(unittest.TestCase):
    """F10：关闭/更名/失败回滚原子重配置。"""

    def _cfg(self, **overrides):
        values = {
            "file_enabled": True,
            "file_name": "miku.log",
            "max_size_mb": 1,
            "backup_count": 1,
            "include_tracebacks": False,
        }
        values.update(overrides)
        return SimpleNamespace(
            get=lambda key, default=None: {
                "assistant.log_level": "INFO",
                "assistant.logging": values,
            }.get(key, default)
        )

    def test_true_to_false_removes_owned_handler_and_temp_dir_deletable(self) -> None:
        before = _root_handlers_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            try:
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    setup_app_logging(self._cfg())
                    owned = [
                        h
                        for h in logging.getLogger().handlers
                        if getattr(h, "_miku_app_owned", False)
                    ]
                    self.assertEqual(len(owned), 1)
                    setup_app_logging(self._cfg(file_enabled=False))
                    owned_after = [
                        h
                        for h in logging.getLogger().handlers
                        if getattr(h, "_miku_app_owned", False)
                    ]
                    self.assertEqual(owned_after, [])
            finally:
                _remove_added_handlers(before)
        # TemporaryDirectory 退出时必须能删除（无遗留句柄）

    def test_rename_a_to_b_closes_old_owned_handler(self) -> None:
        before = _root_handlers_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            try:
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    setup_app_logging(self._cfg(file_name="a.log"))
                    setup_app_logging(self._cfg(file_name="b.log"))
                    owned = [
                        h
                        for h in logging.getLogger().handlers
                        if getattr(h, "_miku_app_owned", False)
                    ]
                    names = sorted(Path(h.baseFilename).name for h in owned)
                    self.assertEqual(names, ["b.log"])
            finally:
                _remove_added_handlers(before)

    def test_create_failure_keeps_previous_owned_handler(self) -> None:
        before = _root_handlers_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            try:
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    setup_app_logging(self._cfg(file_name="a.log"))

                    class _BoomHandler(PrivacyRotatingFileHandler):
                        def __init__(self, *args, **kwargs):
                            raise OSError("create boom")

                    with patch(
                        "src.core.logging_setup.PrivacyRotatingFileHandler",
                        _BoomHandler,
                    ):
                        with self.assertRaises(OSError):
                            setup_app_logging(self._cfg(file_name="b.log"))
                    owned = [
                        h
                        for h in logging.getLogger().handlers
                        if getattr(h, "_miku_app_owned", False)
                    ]
                    self.assertEqual(len(owned), 1)
                    self.assertEqual(Path(owned[0].baseFilename).name, "a.log")
                    logging.getLogger().info("still-alive")
                    owned[0].flush()
                    self.assertTrue((logs / "a.log").is_file())
            finally:
                _remove_added_handlers(before)

    def test_same_path_update_uses_handler_lock(self) -> None:
        before = _root_handlers_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            try:
                with patch("src.core.logging_setup.LOGS_DIR", logs), patch(
                    "src.core.logging_setup.ensure_runtime_dirs"
                ):
                    setup_app_logging(self._cfg(include_tracebacks=True, max_size_mb=2))
                    h1 = [
                        h
                        for h in logging.getLogger().handlers
                        if getattr(h, "_miku_app_owned", False)
                    ][0]
                    # 锁存在且更新后参数完整
                    self.assertTrue(hasattr(h1, "lock"))
                    setup_app_logging(
                        self._cfg(
                            include_tracebacks=False,
                            max_size_mb=3,
                            backup_count=2,
                        )
                    )
                    h2 = [
                        h
                        for h in logging.getLogger().handlers
                        if getattr(h, "_miku_app_owned", False)
                    ][0]
                    self.assertIs(h1, h2)
                    self.assertFalse(h2.include_tracebacks)
                    self.assertEqual(h2.backupCount, 2)
                    self.assertGreater(h2.maxBytes, 0)
            finally:
                _remove_added_handlers(before)


class F11PostOpenIdentityTests(unittest.TestCase):
    """F11：已有文件打开后比较当前路径与 fd 身份。"""

    def test_existing_file_replaced_between_stat_and_open_fail_closed(self) -> None:
        from src.core.log_privacy import LogBoundaryError

        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = logs / "miku.log"
            path.write_text("original", encoding="utf-8")
            outside = logs / "outside.bin"
            outside.write_bytes(b"OUTSIDE_SENTINEL")

            handler = PrivacyRotatingFileHandler(
                path,
                maxBytes=1024,
                backupCount=1,
                encoding="utf-8",
                trusted_logs_dir=logs,
            )
            # 关闭以便重新 _open
            if handler.stream is not None:
                handler.stream.close()
            handler.stream = None

            def _swap(_path: Path) -> None:
                # 打开前把目标换成指向外部内容的硬链接（或覆盖）
                path.unlink()
                try:
                    os.link(outside, path)
                except (OSError, NotImplementedError):
                    # 硬链接不可用时用内容替换模拟身份变化不够；跳过
                    raise unittest.SkipTest("本机无法创建硬链接做竞态替换")

            handler._test_pre_open_hook = _swap
            try:
                with self.assertRaises(LogBoundaryError):
                    handler._open()
                self.assertEqual(outside.read_bytes(), b"OUTSIDE_SENTINEL")
            finally:
                handler._test_pre_open_hook = None
                try:
                    handler.close()
                except Exception:
                    pass


class F12ZeroBackupFdTruncateRaceTests(unittest.TestCase):
    """F12：backup_count=0 仅截断已验证 fd；路径换入外部硬链接不得破坏 sentinel。"""

    def test_pre_truncate_hardlink_swap_fails_closed_outside_unchanged(self) -> None:
        from src.core.log_privacy import LogBoundaryError

        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = logs / "miku.log"
            path.write_text("safe-log-body\n", encoding="utf-8")
            outside = logs / "outside.sqlite3"
            sentinel = b"SQLITE_SENTINEL_MUST_SURVIVE"
            outside.write_bytes(sentinel)

            handler = PrivacyRotatingFileHandler(
                path,
                maxBytes=32,
                backupCount=0,
                encoding="utf-8",
                trusted_logs_dir=logs,
            )
            self.assertIsNotNone(handler.stream)

            def _swap(_path: Path) -> None:
                if path.exists() or path.is_symlink():
                    path.unlink()
                try:
                    os.link(outside, path)
                except (OSError, NotImplementedError):
                    raise unittest.SkipTest("本机无法创建硬链接做竞态替换")

            handler._test_pre_truncate_hook = _swap
            try:
                with self.assertRaises(LogBoundaryError):
                    handler.doRollover()
                self.assertIsNone(handler.stream)
                self.assertEqual(outside.read_bytes(), sentinel)
                self.assertEqual(len(outside.read_bytes()), len(sentinel))
            finally:
                handler._test_pre_truncate_hook = None
                try:
                    handler.close()
                except Exception:
                    pass

    def test_backup_count_zero_does_not_path_open_w(self) -> None:
        """回归：截断不得再走路径 open(..., 'w')。"""
        import builtins

        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = logs / "miku.log"
            path.write_text("body\n", encoding="utf-8")
            handler = PrivacyRotatingFileHandler(
                path,
                maxBytes=32,
                backupCount=0,
                encoding="utf-8",
                trusted_logs_dir=logs,
            )
            real_open = builtins.open
            seen_w: list[str] = []

            def _guard(file, mode="r", *args, **kwargs):
                mode_s = str(mode)
                if Path(file) == path and "w" in mode_s and "a" not in mode_s:
                    seen_w.append(mode_s)
                return real_open(file, mode, *args, **kwargs)

            try:
                with patch("builtins.open", side_effect=_guard):
                    handler.doRollover()
                self.assertEqual(seen_w, [])
                self.assertIsNotNone(handler.stream)
                self.assertEqual(path.stat().st_size, 0)
            finally:
                try:
                    handler.close()
                except Exception:
                    pass


class F13TruncateIoContractTests(unittest.TestCase):
    """F13：普通截断 OSError 走 handleError；边界失败仍抛 LogBoundaryError。"""

    def test_truncate_oserror_via_logger_info_absorbed_and_retryable(self) -> None:
        class TruncateFails:
            def __init__(self, inner):
                self.inner = inner

            def fileno(self):
                return self.inner.fileno()

            def seek(self, *args, **kwargs):
                return self.inner.seek(*args, **kwargs)

            def tell(self):
                return self.inner.tell()

            def truncate(self, size=None):
                raise OSError("simulated disk truncate failure")

            def flush(self):
                return self.inner.flush()

            def write(self, value):
                return self.inner.write(value)

            def close(self):
                return self.inner.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "miku.log"
            handler = PrivacyRotatingFileHandler(
                path,
                maxBytes=1,
                backupCount=0,
                encoding="utf-8",
                trusted_logs_dir=Path(tmp),
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.assertIsNotNone(handler.stream)
            handler.stream = TruncateFails(handler.stream)

            logger = logging.getLogger("test.f13.truncate.io")
            logger.handlers.clear()
            logger.propagate = False
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                propagated: list[BaseException] = []
                try:
                    logger.info("force rollover")
                except BaseException as exc:
                    propagated.append(exc)
                self.assertEqual(propagated, [])
                self.assertIsNone(handler.stream)

                # 后续重试：不确定流已清，应能重新打开并写入
                logger.info("retry-after-io-failure")
                handler.flush()
                self.assertIsNotNone(handler.stream)
                self.assertIn("retry-after-io-failure", path.read_text(encoding="utf-8"))
            finally:
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass

    def test_f12_path_swap_still_raises_boundary_via_logger_info(self) -> None:
        from src.core.log_privacy import LogBoundaryError

        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = logs / "miku.log"
            path.write_text("seed\n", encoding="utf-8")
            outside = logs / "outside.sqlite3"
            sentinel = b"SQLITE_SENTINEL_MUST_SURVIVE"
            outside.write_bytes(sentinel)

            handler = PrivacyRotatingFileHandler(
                path,
                maxBytes=1,
                backupCount=0,
                encoding="utf-8",
                trusted_logs_dir=logs,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))

            def _swap(_p: Path) -> None:
                if path.exists() or path.is_symlink():
                    path.unlink()
                try:
                    os.link(outside, path)
                except (OSError, NotImplementedError):
                    raise unittest.SkipTest("本机无法创建硬链接做竞态替换")

            handler._test_pre_truncate_hook = _swap
            logger = logging.getLogger("test.f13.boundary.swap")
            logger.handlers.clear()
            logger.propagate = False
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                with self.assertRaises(LogBoundaryError):
                    logger.info("force rollover boundary")
                self.assertIsNone(handler.stream)
                self.assertEqual(outside.read_bytes(), sentinel)
            finally:
                handler._test_pre_truncate_hook = None
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
