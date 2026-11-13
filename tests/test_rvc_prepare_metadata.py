"""RVC 准备状态声明/读取矩阵（stdlib fake，无模型）。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.core.resource_settings import ResourceSettings
from src.core.rvc_prepare_metadata import (
    RVC_PREPARE_READ_FAILED,
    RvcPrepareState,
    engine_declares_rvc_prepare_state,
    read_rvc_prepare_state,
    rvc_prepare_failed_sticky,
    rvc_prepare_read_failed,
)
from src.core.voice_activation import bound_tts_models_ready

_SETTINGS = ResourceSettings(rvc_warmup_on_voice_start=True)

_PENDING = RvcPrepareState(
    enabled=True, inflight=False, attempt_completed=False, ready=False
)
_DONE = RvcPrepareState(
    enabled=True, inflight=False, attempt_completed=True, ready=True
)


class RvcPrepareMetadataMatrixTests(unittest.TestCase):
    def test_declaration_and_read_matrix(self) -> None:
        class _ClassReader:
            engine_type = "edge-tts"

            def __init__(self) -> None:
                self._rvc = SimpleNamespace(enabled=True)

            @classmethod
            def get_rvc_prepare_state(cls):
                return _DONE

        class _StaticReader:
            engine_type = "edge-tts"

            def __init__(self) -> None:
                self._rvc = SimpleNamespace(enabled=True)

            @staticmethod
            def get_rvc_prepare_state():
                return _PENDING

        class _BrokenReader:
            engine_type = "edge-tts"

            def __init__(self) -> None:
                self._rvc = SimpleNamespace(enabled=True)

            def get_rvc_prepare_state(self):
                raise RuntimeError("controlled metadata failure")

        class _DisabledReader:
            engine_type = "edge-tts"
            get_rvc_prepare_state = None

            def __init__(self) -> None:
                self._rvc = SimpleNamespace(enabled=True)

        cases = [
            (
                "missing_api_legacy",
                SimpleNamespace(engine_type="edge-tts", _rvc=SimpleNamespace(enabled=True)),
                False,
                None,
                True,
            ),
            (
                "explicit_none_fail_closed",
                _DisabledReader(),
                True,
                RVC_PREPARE_READ_FAILED,
                False,
            ),
            (
                "noncallable_int_fail_closed",
                SimpleNamespace(
                    engine_type="edge-tts",
                    _rvc=SimpleNamespace(enabled=True),
                    get_rvc_prepare_state=42,
                ),
                True,
                RVC_PREPARE_READ_FAILED,
                False,
            ),
            (
                "broken_getter_fail_closed",
                _BrokenReader(),
                True,
                RVC_PREPARE_READ_FAILED,
                False,
            ),
            (
                "bad_return_fail_closed",
                SimpleNamespace(
                    engine_type="edge-tts",
                    _rvc=SimpleNamespace(enabled=True),
                    get_rvc_prepare_state=lambda: {"not": "state"},
                ),
                True,
                RVC_PREPARE_READ_FAILED,
                False,
            ),
            (
                "instance_getter_pending",
                SimpleNamespace(
                    engine_type="edge-tts",
                    _rvc=SimpleNamespace(enabled=True),
                    get_rvc_prepare_state=lambda: _PENDING,
                ),
                True,
                _PENDING,
                False,
            ),
            (
                "instance_getter_complete",
                SimpleNamespace(
                    engine_type="edge-tts",
                    _rvc=SimpleNamespace(enabled=True),
                    get_rvc_prepare_state=lambda: _DONE,
                ),
                True,
                _DONE,
                True,
            ),
            (
                "classmethod_pending",
                _StaticReader(),
                True,
                _PENDING,
                False,
            ),
            (
                "classmethod_complete",
                _ClassReader(),
                True,
                _DONE,
                True,
            ),
        ]
        for name, engine, declares, expected_read, bound_ready in cases:
            with self.subTest(case=name):
                self.assertIs(
                    engine_declares_rvc_prepare_state(engine),
                    declares,
                    name,
                )
                read = read_rvc_prepare_state(engine)
                if expected_read is RVC_PREPARE_READ_FAILED:
                    self.assertTrue(rvc_prepare_read_failed(read), name)
                else:
                    self.assertEqual(read, expected_read, name)
                self.assertIs(
                    bound_tts_models_ready(
                        engine,
                        local_prepare_ok=True,
                        resource_settings=_SETTINGS,
                    ),
                    bound_ready,
                    name,
                )

    def test_keyboard_interrupt_not_swallowed(self) -> None:
        class _Kbi:
            engine_type = "edge-tts"

            def get_rvc_prepare_state(self):
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            read_rvc_prepare_state(_Kbi())


if __name__ == "__main__":
    unittest.main()
