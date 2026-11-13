"""Behavior tests for PEP 562 lazy exports on src.voice package boundaries."""
from __future__ import annotations

import json
import sys
import unittest

from src.core.paths import PROJECT_ROOT
from src.core.safety import SafetyPolicy, safe_subprocess_run

ROOT = PROJECT_ROOT
POLICY = SafetyPolicy({"permissions": {"subprocess": True}})

_SENTINEL = """
import importlib.abc
import sys

_BLOCK_ROOTS = frozenset({"numpy", "scipy", "torch", "soundfile"})
_BLOCK_MODS = frozenset(
    {
        "src.voice.tts.engine",
        "src.voice.stt",
        "src.voice.rvc.converter",
        "src.voice.rvc.postprocess",
        "src.voice.audio.player",
    }
)


class _VoiceHeavySentinel(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in _BLOCK_MODS or fullname.split(".", 1)[0] in _BLOCK_ROOTS:
            raise ImportError("sentinel_blocked:" + fullname)
        return None


sys.meta_path.insert(0, _VoiceHeavySentinel())
"""

# (package, export_name, defining_module, attribute)
EXPECTED_EXPORTS: list[tuple[str, str, str, str]] = [
    ("src.voice", "TTSEngine", "src.voice.tts.engine", "TTSEngine"),
    ("src.voice", "STTEngine", "src.voice.stt", "STTEngine"),
    ("src.voice.tts", "TTSEngine", "src.voice.tts.engine", "TTSEngine"),
    ("src.voice.audio", "AudioPlayer", "src.voice.audio.player", "AudioPlayer"),
    ("src.voice.audio", "TempFileManager", "src.voice.audio.temp_files", "TempFileManager"),
    ("src.voice.rvc", "RVCConverter", "src.voice.rvc.converter", "RVCConverter"),
    ("src.voice.rvc", "RVCHealthReport", "src.voice.rvc.health_check", "RVCHealthReport"),
    ("src.voice.rvc", "check_rvc_health", "src.voice.rvc.health_check", "check_rvc_health"),
    ("src.voice.rvc", "ensure_rvc_ready", "src.voice.rvc.health_check", "ensure_rvc_ready"),
    ("src.voice.cosyvoice", "CosyVoiceSettings", "src.voice.cosyvoice.settings", "CosyVoiceSettings"),
    (
        "src.voice.cosyvoice",
        "parse_cosyvoice_settings",
        "src.voice.cosyvoice.settings",
        "parse_cosyvoice_settings",
    ),
    (
        "src.voice.cosyvoice",
        "CosyVoiceShutdownStatus",
        "src.voice.cosyvoice.client",
        "CosyVoiceShutdownStatus",
    ),
    (
        "src.voice.cosyvoice",
        "CosyVoiceWorkerClient",
        "src.voice.cosyvoice.client",
        "CosyVoiceWorkerClient",
    ),
]

_IDENTITY_ROWS = json.dumps(EXPECTED_EXPORTS)


def _identity_child_body() -> str:
    return f"""
import importlib
import json
TABLE = json.loads({_IDENTITY_ROWS!r})
for pkg_name, export_name, mod_name, attr_name in TABLE:
    pkg = importlib.import_module(pkg_name)
    direct = getattr(importlib.import_module(mod_name), attr_name)
    via = getattr(pkg, export_name)
    if direct is not via:
        raise AssertionError((pkg_name, export_name, direct, via))
print("ok")
"""


def _run_child(body: str, *, timeout: float = 60.0, sentinel: bool = True) -> None:
    prefix = f"import sys, json; sys.path.insert(0, {str(ROOT)!r})\n"
    if sentinel:
        prefix += _SENTINEL + "\n"
    code = prefix + body
    result = safe_subprocess_run(
        POLICY,
        [sys.executable, "-X", "utf8", "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "child failed\n"
            f"stdout={result.stdout!r}\n"
            f"stderr={result.stderr!r}\n"
            f"code={result.returncode}"
        )


class VoiceLazyExportSubprocessTests(unittest.TestCase):
    def test_package_import_and_dir_without_heavy_modules(self) -> None:
        _run_child(
            """
import src.voice
import src.voice.tts
import src.voice.audio
import src.voice.rvc
import src.voice.cosyvoice
import sys
names = set(dir(src.voice))
assert "__name__" in names
assert "__file__" in names
assert set(src.voice.__all__) <= names
assert "src.voice.stt" not in sys.modules
assert "src.voice.tts.engine" not in sys.modules
print("ok")
"""
        )

    def test_worker_resources_import_without_voice_stack(self) -> None:
        _run_child(
            """
import src.voice.cosyvoice.worker_resources as wr
assert callable(wr.available_physical_ram_mb)
print("ok")
"""
        )

    def test_temp_file_manager_without_audio_player(self) -> None:
        _run_child(
            """
import sys
from src.voice.audio import TempFileManager
assert TempFileManager is not None
assert "src.voice.audio.player" not in sys.modules
print("ok")
"""
        )

    def test_unknown_export_raises_attribute_error(self) -> None:
        _run_child(
            """
import src.voice
try:
    src.voice.__getattr__("NotAnExport")
except AttributeError:
    print("ok")
else:
    raise SystemExit(1)
"""
        )


class VoiceLazyExportIdentityTests(unittest.TestCase):
    def test_all_export_identities_match_defining_modules(self) -> None:
        _run_child(_identity_child_body(), sentinel=False)


class VoiceLazyExportRetryAndConcurrencyTests(unittest.TestCase):
    def test_import_error_retry_without_globals_cache(self) -> None:
        _run_child(
            """
import importlib.abc
import sys
import src.voice.tts as tts_pkg

state = {"n": 0}


class _FlakyEngine(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "src.voice.tts.engine":
            state["n"] += 1
            if state["n"] == 1:
                raise ImportError("flaky_once")
        return None


sys.meta_path.insert(0, _FlakyEngine())
try:
    tts_pkg.TTSEngine
except ImportError as exc:
    if "flaky_once" not in str(exc):
        raise
else:
    raise SystemExit(2)
assert "TTSEngine" not in tts_pkg.__dict__
engine = tts_pkg.TTSEngine
from src.voice.tts.engine import TTSEngine as direct
assert engine is direct
assert state["n"] >= 2
print("ok")
""",
            sentinel=False,
        )

    def test_keyboard_interrupt_not_swallowed_and_retry_succeeds(self) -> None:
        _run_child(
            """
import importlib.abc
import sys
import src.voice.tts as tts_pkg

state = {"n": 0}


class _OnceKBI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "src.voice.tts.engine":
            state["n"] += 1
            if state["n"] == 1:
                raise KeyboardInterrupt("controlled_kbi")
        return None


sys.meta_path.insert(0, _OnceKBI())
try:
    tts_pkg.TTSEngine
except KeyboardInterrupt as exc:
    if str(exc) != "controlled_kbi":
        raise
else:
    raise SystemExit(1)
assert "TTSEngine" not in tts_pkg.__dict__
from src.voice.tts.engine import TTSEngine as direct
engine = tts_pkg.TTSEngine
assert engine is direct
print("ok")
""",
            sentinel=False,
        )

    def test_system_exit_not_swallowed_and_retry_succeeds(self) -> None:
        _run_child(
            """
import importlib.abc
import sys
import src.voice as voice_pkg

state = {"n": 0}


class _OnceExit(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "src.voice.stt":
            state["n"] += 1
            if state["n"] == 1:
                raise SystemExit(42)
        return None


sys.meta_path.insert(0, _OnceExit())
try:
    voice_pkg.STTEngine
except SystemExit as exc:
    if exc.code != 42:
        raise
else:
    raise SystemExit(1)
assert "STTEngine" not in voice_pkg.__dict__
from src.voice.stt import STTEngine as direct
engine = voice_pkg.STTEngine
assert engine is direct
print("ok")
""",
            sentinel=False,
        )

    def test_concurrent_first_access_audio_same_identity(self) -> None:
        _run_child(
            """
import sys
import threading
import src.voice.audio as audio_pkg

assert "src.voice.audio.temp_files" not in sys.modules
errors = []
results = []
barrier = threading.Barrier(3)
lock = threading.Lock()


def worker():
    try:
        barrier.wait()
        from src.voice.audio import TempFileManager as tfm
        with lock:
            results.append(tfm)
    except BaseException as exc:
        with lock:
            errors.append(exc)


threads = [threading.Thread(target=worker) for _ in range(2)]
for thread in threads:
    thread.start()
try:
    barrier.wait()
    from src.voice.audio import TempFileManager as main_tfm
    with lock:
        results.append(main_tfm)
except BaseException as exc:
    errors.append(exc)
for thread in threads:
    thread.join()
if errors:
    raise errors[0]
assert len(results) == 3 and results[0] is results[1] is results[2]
from src.voice.audio.temp_files import TempFileManager as direct
assert results[0] is direct
print("ok")
""",
            sentinel=False,
        )

    def test_concurrent_first_access_tts_same_identity(self) -> None:
        _run_child(
            """
import sys
import threading
import src.voice.tts as tts_pkg

assert "src.voice.tts.engine" not in sys.modules
errors = []
results = []
barrier = threading.Barrier(3)
lock = threading.Lock()


def worker():
    try:
        barrier.wait()
        from src.voice.tts import TTSEngine as engine
        with lock:
            results.append(engine)
    except BaseException as exc:
        with lock:
            errors.append(exc)


threads = [threading.Thread(target=worker) for _ in range(2)]
for thread in threads:
    thread.start()
try:
    barrier.wait()
    from src.voice.tts import TTSEngine as main_engine
    with lock:
        results.append(main_engine)
except BaseException as exc:
    errors.append(exc)
for thread in threads:
    thread.join()
if errors:
    raise errors[0]
assert len(results) == 3 and results[0] is results[1] is results[2]
from src.voice.tts.engine import TTSEngine as direct
assert results[0] is direct
print("ok")
""",
            sentinel=False,
        )


if __name__ == "__main__":
    unittest.main()
