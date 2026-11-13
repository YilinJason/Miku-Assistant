"""Assembly default voice factories: import assembly without eager TTS/STT."""
from __future__ import annotations

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

_TEXT_ASSEMBLE_BODY = """
import sys
from unittest.mock import MagicMock
from src.core.assembly import assemble_default
from src.core.router import IntentRouter

class _Cfg:
    def __init__(self):
        self.data = {
            "ollama": {"base_url": "http://127.0.0.1:9", "model": "m", "timeout": 1},
            "voice": {
                "tts": {
                    "engine": "edge-tts",
                    "language": "zh-CN",
                    "rate": 150,
                    "volume": 0.8,
                    "voice": "v",
                    "fallback_enabled": True,
                    "fallback_engine": "pyttsx3",
                    "pipeline_enabled": True,
                },
                "stt": {
                    "engine": "speech_recognition",
                    "language": "zh-CN",
                    "energy_threshold": 1,
                },
            },
            "system": {"windows_control_enabled": False},
            "search": {"auto_open": False},
            "extensions": {"character_model": {"enabled": False}},
            "resource": {"profile": "balanced"},
        }

    def get(self, key, default=None):
        cur = self.data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur if cur is not None else default

    def get_rvc_config(self):
        return None

tts_calls = []
stt_calls = []

def fake_tts(**kwargs):
    tts_calls.append(kwargs)
    return MagicMock(name="tts", shutdown=lambda: None)

def fake_stt(**kwargs):
    stt_calls.append(kwargs)
    return MagicMock(name="stt")

parts = assemble_default(
    startup_mode="text",
    config_loader=lambda _p: _Cfg(),
    logging_setup=lambda _c: None,
    dir_preparer=lambda: None,
    ollama_client_cls=lambda **kw: MagicMock(name="llm"),
    tts_engine_cls=fake_tts,
    stt_engine_cls=fake_stt,
    windows_controller_cls=lambda **kw: None,
    chrome_searcher_cls=lambda **kw: object(),
    character_interface_cls=lambda **kw: None,
    file_cleaner_from_config=lambda *a, **k: None,
    router_builder=lambda **k: IntentRouter([]),
    start_cleaner=False,
)
assert parts.tts_engine is None
assert parts.stt_engine is None
assert tts_calls == []
assert stt_calls == []
assert "src.voice.tts.engine" not in sys.modules
assert "src.voice.stt" not in sys.modules
print("ok")
"""


def _run_child(body: str, *, sentinel: bool = True, timeout: float = 60.0) -> None:
    prefix = f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
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
            f"child failed stdout={result.stdout!r} stderr={result.stderr!r} code={result.returncode}"
        )


class AssemblyLazyImportTests(unittest.TestCase):
    def test_import_assembly_without_voice_implementations(self) -> None:
        _run_child(
            """
import src.core.assembly as assembly
import sys
assert "src.voice.tts.engine" not in sys.modules
assert "src.voice.stt" not in sys.modules
assert callable(assembly.default_tts_engine_cls)
assert callable(assembly.default_stt_engine_cls)
print("ok")
"""
        )

    def test_import_assistant_without_voice_implementations(self) -> None:
        _run_child(
            """
import sys
import src.core.assistant  # noqa: F401
assert "src.voice.tts.engine" not in sys.modules
assert "src.voice.stt" not in sys.modules
print("ok")
"""
        )

    def test_text_assemble_does_not_construct_voice(self) -> None:
        _run_child(_TEXT_ASSEMBLE_BODY)

    def test_injected_factory_never_imports_real_tts_class(self) -> None:
        _run_child(
            """
import sys
from src.core.assembly import build_tts_engine
from src.core.safety import SafetyPolicy

class _Cfg:
    def get(self, key, default=None):
        if key == "voice.tts":
            return {"engine": "edge-tts"}
        return default
    def get_rvc_config(self):
        return None

calls = []
def fake(**kwargs):
    calls.append(kwargs)
    return object()

policy = SafetyPolicy({"permissions": {}})
build_tts_engine(_Cfg(), policy, tts_engine_cls=fake)
assert len(calls) == 1
assert "src.voice.tts.engine" not in sys.modules
print("ok")
"""
        )

    def test_default_factory_imports_engine_module_once(self) -> None:
        _run_child(
            """
import sys
from unittest.mock import MagicMock, patch
from src.core.assembly import build_tts_engine
from src.core.safety import SafetyPolicy

class _Cfg:
    def get(self, key, default=None):
        if key == "voice.tts":
            return {"engine": "edge-tts"}
        return default
    def get_rvc_config(self):
        return None

policy = SafetyPolicy({"permissions": {}})
assert "src.voice.tts.engine" not in sys.modules
with patch("src.voice.tts.engine.TTSEngine", return_value=MagicMock(name="tts")) as mock_cls:
    build_tts_engine(_Cfg(), policy)
    mock_cls.assert_called_once()
assert "src.voice.tts.engine" in sys.modules
print("ok")
""",
            sentinel=False,
        )

    def test_lazy_module_export_matches_engine_class(self) -> None:
        _run_child(
            """
from src.core import assembly
from src.voice.tts.engine import TTSEngine as direct
assert assembly.TTSEngine is direct
from src.voice.stt import STTEngine as stt_direct
assert assembly.STTEngine is stt_direct
print("ok")
""",
            sentinel=False,
        )

    def test_explicit_none_stt_still_fails_on_call(self) -> None:
        from src.core.assembly import build_stt_engine
        from src.core.safety import SafetyPolicy

        class _Cfg:
            def get(self, key: str, default=None):
                return {} if key == "voice.stt" else default

        with self.assertRaises(TypeError):
            build_stt_engine(_Cfg(), stt_engine_cls=None, safety_policy=SafetyPolicy({}))


if __name__ == "__main__":
    unittest.main()
