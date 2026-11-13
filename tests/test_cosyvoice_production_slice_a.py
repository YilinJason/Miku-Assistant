"""CosyVoice 生产后端切片 A：资产、严格配置与 LOCAL_TTS_WORKER 能力。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core.config import Config
from src.core.safety import (
    Operation,
    SafetyPolicy,
    TrustedLocalTtsProfile,
)
from src.voice.cosyvoice.assets import (
    COSYVOICE_PYTHON_REL,
    MATCHA_DIR_REL,
    MODEL_DIR_REL,
    MODEL_REQUIRED_FILES,
    RUNTIME_WORKER_SCRIPT_REL,
    SAMPLE_CASES,
    SUITE_CACHE_REL,
    VENDOR_COSYVOICE_REL,
    WETEXT_DIR_REL,
)
from src.voice.cosyvoice.capabilities import (
    LocalTtsWorkerLaunch,
    build_runtime_popen_options,
    merge_local_tts_popen_kwargs,
)
from src.voice.cosyvoice.path_trust import is_reparse_or_link
from src.voice.cosyvoice.settings import parse_cosyvoice_settings
from src.voice.cosyvoice.worker_env import build_worker_env
from tests.support.local_tts_runners import bind_fake_local_tts_runners


def _clear_dry_run():
    return patch.dict(os.environ, {"MIKU_DRY_RUN": ""}, clear=False)


def _seed_repo(root: Path) -> None:
    for rel in (COSYVOICE_PYTHON_REL, RUNTIME_WORKER_SCRIPT_REL):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"py")
    model_root = root / MODEL_DIR_REL
    for rel in MODEL_REQUIRED_FILES:
        path = model_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"m")
    for rel in (
        VENDOR_COSYVOICE_REL,
        MATCHA_DIR_REL,
        WETEXT_DIR_REL,
        SUITE_CACHE_REL,
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)


class _FakePopen:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def _default_cosyvoice_settings():
    cfg = parse_cosyvoice_settings({"engine": "cosyvoice"})
    assert cfg is not None
    return cfg


def _policy_with_local_tts(tmp: str, **perm_overrides) -> tuple[SafetyPolicy, Path]:
    root = Path(tmp)
    _seed_repo(root)
    perms = {
        "local_tts_worker": True,
        "rvc_worker": False,
        "rvc_infer": False,
        "rvc_probe": False,
        "subprocess": False,
        "file_delete": True,
    }
    perms.update(perm_overrides)
    policy = SafetyPolicy(
        {"permissions": perms},
        trusted_local_tts_profile=TrustedLocalTtsProfile(repo_root=root),
    )
    return policy, root


class CosyVoiceSettingsTests(unittest.TestCase):
    def test_defaults_when_engine_cosyvoice(self) -> None:
        cfg = parse_cosyvoice_settings({"engine": "cosyvoice"})
        self.assertIsNotNone(cfg)
        assert cfg is not None
        self.assertEqual(cfg.startup_timeout_sec, 30)
        self.assertEqual(cfg.cuda_memory_limit_mb, 4096)

    def test_non_cosyvoice_returns_none(self) -> None:
        self.assertIsNone(parse_cosyvoice_settings({"engine": "edge-tts"}))

    def test_parse_has_no_side_effects(self) -> None:
        with patch("pathlib.Path.mkdir") as mkdir_mock, patch(
            "subprocess.Popen"
        ) as popen_mock, patch("threading.Thread") as thread_mock:
            with self.assertRaises(ValueError):
                parse_cosyvoice_settings(
                    {"engine": "cosyvoice", "cosyvoice": {"cpu_threads": True}}
                )
            parse_cosyvoice_settings({"engine": "cosyvoice"})
            mkdir_mock.assert_not_called()
            popen_mock.assert_not_called()
            thread_mock.assert_not_called()
        self.assertNotIn("torch", sys.modules)

    def test_bad_value_matrix(self) -> None:
        with self.assertRaises(ValueError):
            parse_cosyvoice_settings(
                {"engine": "cosyvoice", "cosyvoice": {"cpu_threads": True}}
            )
        with self.assertRaises(ValueError):
            parse_cosyvoice_settings(
                {
                    "engine": "cosyvoice",
                    "cosyvoice": {"startup_timeout_sec": float("nan")},
                }
            )
        with self.assertRaises(ValueError):
            parse_cosyvoice_settings(
                {
                    "engine": "cosyvoice",
                    "cosyvoice": {"idle_timeout_sec": float("inf")},
                }
            )
        with self.assertRaises(ValueError):
            parse_cosyvoice_settings(
                {
                    "engine": "cosyvoice",
                    "cosyvoice": {"max_text_chars": 10**400},
                }
            )
        with self.assertRaises(ValueError):
            parse_cosyvoice_settings(
                {"engine": "cosyvoice", "cosyvoice": {"startup_timeout_sec": 61}}
            )
        with self.assertRaises(ValueError):
            parse_cosyvoice_settings(
                {"engine": "cosyvoice", "cosyvoice": {"cuda_memory_limit_mb": 3000}}
            )
        with self.assertRaises(ValueError):
            parse_cosyvoice_settings(
                {"engine": "cosyvoice", "cosyvoice": {"model_path": "/tmp/x"}}
            )
        with self.assertRaises(ValueError):
            parse_cosyvoice_settings(
                {"engine": "cosyvoice", "cosyvoice": {"extra": 1}}
            )


class LocalTtsWorkerCapabilityTests(unittest.TestCase):
    def test_bare_and_app_default_deny_spawn(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            root = Path(tmp)
            _seed_repo(root)
            launch = LocalTtsWorkerLaunch(
                parent_pid=os.getpid(), settings=_default_cosyvoice_settings()
            )
            with self.assertRaises(PermissionError):
                SafetyPolicy().spawn_local_tts_worker(launch)
            sealed_app = SafetyPolicy.from_app_config(
                Config(),
                trusted_local_tts_profile=TrustedLocalTtsProfile(repo_root=root),
            )
            with self.assertRaises(PermissionError):
                sealed_app.spawn_local_tts_worker(launch)

    def test_dry_run_denies_even_with_permission(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MIKU_DRY_RUN": "1"}, clear=False
        ):
            policy, _root = _policy_with_local_tts(tmp)
            with self.assertRaises(PermissionError):
                policy.spawn_local_tts_worker(
                    LocalTtsWorkerLaunch(
                        parent_pid=os.getpid(), settings=_default_cosyvoice_settings()
                    )
                )

    def test_explicit_permission_reaches_fake_popen(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            policy, root = _policy_with_local_tts(tmp)
            pops: list = []

            def fake_popen(argv, **kwargs):
                pops.append((list(argv), dict(kwargs)))
                return _FakePopen(argv, kwargs)

            bind_fake_local_tts_runners(policy, popen_fn=fake_popen)
            launch = LocalTtsWorkerLaunch(
                parent_pid=os.getpid(), settings=_default_cosyvoice_settings()
            )
            with patch(
                "src.voice.cosyvoice.assets.verify_pinned_assets",
                return_value={"model_bytes_actual": 1},
            ):
                policy.spawn_local_tts_worker(launch)
            self.assertEqual(len(pops), 1)
            argv, kw = pops[0]
            expected_python = (root / COSYVOICE_PYTHON_REL).resolve()
            self.assertEqual(Path(argv[0]).resolve(), expected_python)
            self.assertIn("-u", argv)
            pid_idx = argv.index("--parent-pid")
            self.assertEqual(argv[pid_idx + 1], str(os.getpid()))
            self.assertIn("--startup-timeout-sec", argv)
            self.assertEqual(kw.get("stderr"), subprocess.DEVNULL)
            self.assertTrue(kw.get("text"))
            self.assertEqual(kw.get("encoding"), "utf-8")
            self.assertEqual(kw.get("errors"), "strict")
            self.assertEqual(kw.get("shell"), False)
            expected_cwd = (root / VENDOR_COSYVOICE_REL).resolve()
            self.assertEqual(Path(kw.get("cwd")).resolve(), expected_cwd)
            env = kw.get("env") or {}
            self.assertNotIn("PYTHONPATH", env)
            self.assertEqual(env.get("HF_HUB_OFFLINE"), "1")

    def test_secret_env_not_propagated(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            env = build_worker_env(
                cache_root=cache,
                environ={
                    "PATH": "/x",
                    "HF_API_TOKEN": "secret",
                    "HTTP_PROXY": "http://evil",
                    "PYTHONPATH": "/bad",
                },
            )
            self.assertEqual(env.get("PATH"), "/x")
            self.assertNotIn("HF_API_TOKEN", env)
            self.assertNotIn("HTTP_PROXY", env)
            self.assertNotIn("PYTHONPATH", env)

    def test_forbidden_popen_overrides_rejected(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            policy, _root = _policy_with_local_tts(tmp)
            bind_fake_local_tts_runners(
                policy, popen_fn=lambda *_a, **_k: _FakePopen()
            )
            launch = LocalTtsWorkerLaunch(
                parent_pid=os.getpid(), settings=_default_cosyvoice_settings()
            )
            with patch("src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}):
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(launch, shell=True)
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(launch, cwd="/tmp")
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(launch, env={"X": "1"})
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(launch, close_fds=False)
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(launch, creationflags=0)
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(launch, preexec_fn=os.getpid)

    def test_parent_pid_strict(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            policy, _root = _policy_with_local_tts(tmp)
            bind_fake_local_tts_runners(
                policy, popen_fn=lambda *_a, **_k: _FakePopen()
            )
            with patch("src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}):
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(
                        LocalTtsWorkerLaunch(
                            parent_pid=True, settings=_default_cosyvoice_settings()
                        )
                    )
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(
                        LocalTtsWorkerLaunch(
                            parent_pid=0, settings=_default_cosyvoice_settings()
                        )
                    )

    def test_utf8_popen_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            vendor = root / "v"
            cache = root / "c"
            vendor.mkdir()
            cache.mkdir()
            opts = build_runtime_popen_options(cache_root=cache, vendor_root=vendor)
            self.assertEqual(opts["encoding"], "utf-8")
            self.assertEqual(opts["errors"], "strict")
            payload = {"text": "早上好"}
            encoded = json.dumps(payload, ensure_ascii=False)
            decoded = json.loads(encoded)
            self.assertEqual(decoded["text"], "早上好")
            with self.assertRaises(PermissionError):
                merge_local_tts_popen_kwargs(opts, {"encoding": "cp1252"})

    def test_symlink_python_zero_popen(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink unavailable")
        with TemporaryDirectory() as tmp, _clear_dry_run():
            policy, root = _policy_with_local_tts(tmp)
            ext = root / "outside.exe"
            ext.write_bytes(b"e")
            python = root / COSYVOICE_PYTHON_REL
            python.unlink()
            try:
                os.symlink(ext, python)
            except OSError:
                self.skipTest("symlink creation denied")
            called = {"n": 0}

            def boom(*_a, **_k):
                called["n"] += 1

            policy.bind_process_runners(popen_fn=boom)
            with patch("src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}):
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(
                    LocalTtsWorkerLaunch(
                        parent_pid=os.getpid(), settings=_default_cosyvoice_settings()
                    )
                )
            self.assertEqual(called["n"], 0)

    def test_post_seal_ancestor_junction_zero_popen(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            policy, root = _policy_with_local_tts(tmp)
            matcha = root / MATCHA_DIR_REL
            outside = root / "outside_matcha"
            outside.mkdir()
            if matcha.is_dir():
                matcha.rmdir()
            linked = False
            if hasattr(os, "symlink"):
                try:
                    os.symlink(outside, matcha, target_is_directory=True)
                    linked = True
                except OSError:
                    pass
            if not linked:
                with patch(
                    "src.voice.cosyvoice.path_trust.is_reparse_or_link",
                    side_effect=lambda p: p == matcha,
                ):
                    called = {"n": 0}
                    policy.bind_process_runners(
                        popen_fn=lambda *_a, **_k: called.__setitem__("n", 1)
                    )
                    with patch(
                        "src.voice.cosyvoice.assets.verify_pinned_assets",
                        return_value={},
                    ):
                        with self.assertRaises(PermissionError):
                            policy.spawn_local_tts_worker(
                                LocalTtsWorkerLaunch(
                                    parent_pid=os.getpid(),
                                    settings=_default_cosyvoice_settings(),
                                )
                            )
                    self.assertEqual(called["n"], 0)
                return
            self.assertTrue(is_reparse_or_link(matcha))
            called = {"n": 0}
            policy.bind_process_runners(
                popen_fn=lambda *_a, **_k: called.__setitem__("n", 1)
            )
            with patch("src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}):
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(
                    LocalTtsWorkerLaunch(
                        parent_pid=os.getpid(), settings=_default_cosyvoice_settings()
                    )
                )
            self.assertEqual(called["n"], 0)

    def test_tampered_python_zero_popen(self) -> None:
        with TemporaryDirectory() as tmp, _clear_dry_run():
            policy, root = _policy_with_local_tts(tmp)
            called = {"n": 0}

            def boom(*_a, **_k):
                called["n"] += 1
                raise AssertionError("must not popen")

            policy.bind_process_runners(popen_fn=boom)
            python = root / COSYVOICE_PYTHON_REL
            python.write_bytes(b"altered")
            with patch("src.voice.cosyvoice.assets.verify_pinned_assets", return_value={}):
                with self.assertRaises(PermissionError):
                    policy.spawn_local_tts_worker(
                    LocalTtsWorkerLaunch(
                        parent_pid=os.getpid(), settings=_default_cosyvoice_settings()
                    )
                )
            self.assertEqual(called["n"], 0)


class ExpressiveDiagnosticCompatTests(unittest.TestCase):
    def test_suite_sample_cases_unchanged(self) -> None:
        import cosyvoice_eval_suite as suite  # noqa: PLC0415

        self.assertEqual(suite.SAMPLE_CASES, SAMPLE_CASES)
        self.assertEqual(suite.EXPECTED_COUNT, len(SAMPLE_CASES))

    def test_diagnose_build_worker_env_proxy(self) -> None:
        import diagnose_expressive_tts as diag  # noqa: PLC0415

        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c"
            paths = {"cache_root": cache}
            env = diag.build_worker_env(paths)
            self.assertEqual(env["TRANSFORMERS_OFFLINE"], "1")


if __name__ == "__main__":
    unittest.main()
