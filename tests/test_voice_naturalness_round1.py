# -*- coding: utf-8 -*-
"""语音自然度 Round 1：edge 句级韵律、出口接线、RVC YAML 透传与 A/B dry-run。"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

from src.core.assembly import build_tts_engine
from src.core.safety import SafetyPolicy
from src.voice.audio.temp_files import TempFileManager
from src.voice.prosody import (
    EdgeProsodySettings,
    NEUTRAL_EDGE_PROSODY,
    classify_sentence_ending,
    format_signed_hz,
    format_signed_percent,
)
from src.voice.rvc.vc_params import build_vc_kwargs
from src.voice.tts.engine import TTSEngine


class _MemConfig:
    def __init__(self, data: dict) -> None:
        self.data = data

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def get_rvc_config(self) -> Optional[dict]:
        return self.get("voice.tts.rvc")


class ProsodyParseTests(unittest.TestCase):
    def test_valid_and_enabled_false_neutral(self) -> None:
        settings = EdgeProsodySettings.from_mapping(
            {
                "enabled": True,
                "rate": "-4%",
                "pitch": "+0Hz",
                "volume": "+0%",
                "question_rate_delta_pct": 2,
                "question_pitch_delta_hz": 4,
                "exclamation_rate_delta_pct": 2,
                "exclamation_pitch_delta_hz": 3,
                "ellipsis_rate_delta_pct": -3,
                "ellipsis_pitch_delta_hz": -2,
            }
        )
        self.assertEqual(
            settings.resolve_for_sentence("今天不错。").rate, "-4%"
        )
        disabled = EdgeProsodySettings.from_mapping(
            {"enabled": False, "rate": "-4%", "pitch": "+10Hz", "volume": "-5%"}
        )
        self.assertEqual(
            disabled.resolve_for_sentence("今天不错？"), NEUTRAL_EDGE_PROSODY
        )
        self.assertEqual(EdgeProsodySettings.from_mapping(None).enabled, False)

    def test_invalid_formats_and_types(self) -> None:
        cases = [
            {"enabled": True, "rate": "4%"},  # 无符号
            {"enabled": True, "rate": "-4"},
            {"enabled": True, "pitch": "+0"},
            {"enabled": True, "pitch": "0Hz"},
            {"enabled": True, "volume": True},
            {"enabled": "yes"},
            {"enabled": True, "question_rate_delta_pct": True},
            {"enabled": True, "question_pitch_delta_hz": 1.5},
            {"enabled": True, "rate": 0},
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    EdgeProsodySettings.from_mapping(raw)

    def test_clamp(self) -> None:
        self.assertEqual(format_signed_percent(100), "+30%")
        self.assertEqual(format_signed_percent(-100), "-30%")
        self.assertEqual(format_signed_hz(99), "+50Hz")
        self.assertEqual(format_signed_hz(-99), "-50Hz")
        settings = EdgeProsodySettings.from_mapping(
            {
                "enabled": True,
                "rate": "+30%",
                "pitch": "+50Hz",
                "volume": "+0%",
                "question_rate_delta_pct": 10,
                "question_pitch_delta_hz": 10,
                "exclamation_rate_delta_pct": 0,
                "exclamation_pitch_delta_hz": 0,
                "ellipsis_rate_delta_pct": 0,
                "ellipsis_pitch_delta_hz": 0,
            }
        )
        p = settings.resolve_for_sentence("真的吗？")
        self.assertEqual(p.rate, "+30%")
        self.assertEqual(p.pitch, "+50Hz")


class ProsodySentenceKindTests(unittest.TestCase):
    def test_priority_and_punctuation(self) -> None:
        self.assertEqual(classify_sentence_ending("好吗？"), "question")
        self.assertEqual(classify_sentence_ending("Really?"), "question")
        self.assertEqual(classify_sentence_ending("太棒了！"), "exclamation")
        self.assertEqual(classify_sentence_ending("Wow!"), "exclamation")
        self.assertEqual(classify_sentence_ending("嗯…"), "ellipsis")
        self.assertEqual(classify_sentence_ending("well..."), "ellipsis")
        self.assertEqual(classify_sentence_ending("你好。"), "neutral")
        # 问号优先于感叹（末尾决定）
        self.assertEqual(classify_sentence_ending("什么！？"), "question")
        self.assertEqual(classify_sentence_ending("什么?!"), "question")

    def test_deltas_applied(self) -> None:
        settings = EdgeProsodySettings.from_mapping(
            {
                "enabled": True,
                "rate": "-4%",
                "pitch": "+0Hz",
                "volume": "+0%",
                "question_rate_delta_pct": 2,
                "question_pitch_delta_hz": 4,
                "exclamation_rate_delta_pct": 2,
                "exclamation_pitch_delta_hz": 3,
                "ellipsis_rate_delta_pct": -3,
                "ellipsis_pitch_delta_hz": -2,
            }
        )
        self.assertEqual(
            settings.resolve_for_sentence("陈述。"),
            settings.resolve_for_sentence("陈述"),
        )
        q = settings.resolve_for_sentence("问句？")
        self.assertEqual((q.rate, q.pitch, q.volume), ("-2%", "+4Hz", "+0%"))
        e = settings.resolve_for_sentence("感叹！")
        self.assertEqual((e.rate, e.pitch), ("-2%", "+3Hz"))
        soft = settings.resolve_for_sentence("舒缓…")
        self.assertEqual((soft.rate, soft.pitch), ("-7%", "-2Hz"))


class EdgeExportWiringTests(unittest.TestCase):
    def _engine(self, *, edge_prosody: Optional[dict], tmp: str) -> TTSEngine:
        policy = SafetyPolicy(
            {"permissions": {"cloud_tts": True, "file_delete": True}}
        )
        policy.register_file_delete_dir(tmp)
        with patch.object(TTSEngine, "prepare_rvc"), patch.object(
            TTSEngine, "_initialize_engine"
        ):
            engine = TTSEngine(
                engine="edge-tts",
                pipeline_enabled=False,
                rvc_config={},
                safety_policy=policy,
                warm_rvc_on_init=False,
                edge_prosody=edge_prosody,
                temp_file_manager=TempFileManager(
                    safety_policy=policy, default_dir=Path(tmp)
                ),
            )
        engine.engine_type = "edge-tts"
        return engine

    def test_edge_tts_to_mp3_and_save_kwargs_and_body(self) -> None:
        prosody = {
            "enabled": True,
            "rate": "-4%",
            "pitch": "+0Hz",
            "volume": "+0%",
            "question_rate_delta_pct": 2,
            "question_pitch_delta_hz": 4,
            "exclamation_rate_delta_pct": 2,
            "exclamation_pitch_delta_hz": 3,
            "ellipsis_rate_delta_pct": -3,
            "ellipsis_pitch_delta_hz": -2,
        }
        with TemporaryDirectory() as tmp:
            engine = self._engine(edge_prosody=prosody, tmp=tmp)
            calls: List[dict] = []

            class FakeComm:
                def __init__(self, text, voice, rate="+0%", pitch="+0Hz", volume="+0%"):
                    calls.append(
                        {
                            "text": text,
                            "voice": voice,
                            "rate": rate,
                            "pitch": pitch,
                            "volume": volume,
                        }
                    )
                    self.text = text

                async def save(self, path: str) -> None:
                    Path(path).write_bytes(b"mp3")

            with patch("edge_tts.Communicate", FakeComm):
                path = asyncio.run(engine._edge_tts_to_mp3("你好吗？"))
                self.assertTrue(Path(path).is_file())
                out = Path(tmp) / "out.mp3"
                engine.save_to_file("太好了！", str(out))
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["text"], "你好吗？")
            self.assertEqual(calls[0]["rate"], "-2%")
            self.assertEqual(calls[0]["pitch"], "+4Hz")
            self.assertEqual(calls[0]["volume"], "+0%")
            self.assertEqual(calls[1]["text"], "太好了！")
            self.assertEqual(calls[1]["rate"], "-2%")
            self.assertEqual(calls[1]["pitch"], "+3Hz")
            engine.shutdown(stop_rvc=False)

    def test_pipeline_one_call_per_sentence_profiles(self) -> None:
        prosody = {
            "enabled": True,
            "rate": "-4%",
            "pitch": "+0Hz",
            "volume": "+0%",
            "question_rate_delta_pct": 2,
            "question_pitch_delta_hz": 4,
            "exclamation_rate_delta_pct": 2,
            "exclamation_pitch_delta_hz": 3,
            "ellipsis_rate_delta_pct": -3,
            "ellipsis_pitch_delta_hz": -2,
        }
        with TemporaryDirectory() as tmp:
            policy = SafetyPolicy(
                {"permissions": {"cloud_tts": True, "file_delete": True}}
            )
            policy.register_file_delete_dir(tmp)
            with patch.object(TTSEngine, "prepare_rvc"), patch.object(
                TTSEngine, "_initialize_engine"
            ):
                engine = TTSEngine(
                    engine="edge-tts",
                    pipeline_enabled=True,
                    rvc_config={},
                    safety_policy=policy,
                    warm_rvc_on_init=False,
                    edge_prosody=prosody,
                    temp_file_manager=TempFileManager(
                        safety_policy=policy, default_dir=Path(tmp)
                    ),
                )
            engine.engine_type = "edge-tts"
            calls: List[dict] = []

            class FakeComm:
                def __init__(self, text, voice, rate="+0%", pitch="+0Hz", volume="+0%"):
                    calls.append({"text": text, "rate": rate, "pitch": pitch})

                async def save(self, path: str) -> None:
                    Path(path).write_bytes(b"mp3")

            played: List[str] = []

            def fake_play(path: str, **_kw: Any) -> None:
                played.append(path)

            engine._player.play = fake_play  # type: ignore[method-assign]
            engine._mp3_to_wav = lambda p, _c=None: p  # type: ignore[method-assign]

            text = "今天不错。你在吗？太好了！嗯…"
            with patch("edge_tts.Communicate", FakeComm):
                engine.speak(text)
            self.assertEqual(len(calls), 4)
            self.assertEqual(len(played), 4)
            rates = {(c["rate"], c["pitch"]) for c in calls}
            self.assertIn(("-4%", "+0Hz"), rates)
            self.assertIn(("-2%", "+4Hz"), rates)
            self.assertIn(("-2%", "+3Hz"), rates)
            self.assertIn(("-7%", "-2Hz"), rates)
            engine.shutdown(stop_rvc=False)

    def test_interrupt_stops_further_communicate(self) -> None:
        prosody = {
            "enabled": True,
            "rate": "-4%",
            "pitch": "+0Hz",
            "volume": "+0%",
            "question_rate_delta_pct": 2,
            "question_pitch_delta_hz": 4,
            "exclamation_rate_delta_pct": 2,
            "exclamation_pitch_delta_hz": 3,
            "ellipsis_rate_delta_pct": -3,
            "ellipsis_pitch_delta_hz": -2,
        }
        with TemporaryDirectory() as tmp:
            engine = self._engine(edge_prosody=prosody, tmp=tmp)
            calls: List[str] = []

            class FakeComm:
                def __init__(self, text, voice, rate="+0%", pitch="+0Hz", volume="+0%"):
                    calls.append(text)

                async def save(self, path: str) -> None:
                    Path(path).write_bytes(b"mp3")

            engine.pipeline_enabled = True
            engine._mp3_to_wav = lambda p, _c=None: p  # type: ignore[method-assign]
            engine._player.play = lambda *_a, **_k: engine.interrupt()  # type: ignore[method-assign]

            with patch("edge_tts.Communicate", FakeComm):
                engine.speak("第一句。第二句。第三句。")
            # 打断后不应继续合成全部句子
            self.assertLess(len(calls), 3)
            self.assertGreaterEqual(len(calls), 1)
            owned_before = engine._temp_files.owned_count()
            engine.shutdown(stop_rvc=False)
            self.assertEqual(engine._temp_files.owned_count(), 0)
            self.assertLessEqual(owned_before, 4)

    def test_invalid_prosody_fails_before_rvc_side_effects(self) -> None:
        prepared = {"n": 0}

        def boom_prepare(self):  # noqa: ANN001
            prepared["n"] += 1

        with patch.object(TTSEngine, "prepare_rvc", boom_prepare):
            with self.assertRaises(ValueError):
                TTSEngine(
                    engine="edge-tts",
                    rvc_config={"model_path": "x.pth"},
                    warm_rvc_on_init=True,
                    edge_prosody={"enabled": True, "rate": "4%"},
                )
        self.assertEqual(prepared["n"], 0)


class AssemblyAndRvcDefaultsTests(unittest.TestCase):
    def test_assembly_forwards_and_default_neutral(self) -> None:
        captured: dict = {}

        class SpyTTS:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        cfg = _MemConfig(
            {
                "voice": {
                    "tts": {
                        "engine": "edge-tts",
                        "edge_prosody": {
                            "enabled": True,
                            "rate": "-4%",
                            "pitch": "+0Hz",
                            "volume": "+0%",
                            "question_rate_delta_pct": 2,
                            "question_pitch_delta_hz": 4,
                            "exclamation_rate_delta_pct": 2,
                            "exclamation_pitch_delta_hz": 3,
                            "ellipsis_rate_delta_pct": -3,
                            "ellipsis_pitch_delta_hz": -2,
                        },
                    }
                }
            }
        )
        build_tts_engine(
            cfg,
            SafetyPolicy({"permissions": {"cloud_tts": True}}),
            tts_engine_cls=SpyTTS,
            warm_rvc_on_init=False,
        )
        self.assertEqual(captured["edge_prosody"]["rate"], "-4%")

        captured.clear()
        build_tts_engine(
            _MemConfig({"voice": {"tts": {"engine": "edge-tts"}}}),
            SafetyPolicy({"permissions": {"cloud_tts": True}}),
            tts_engine_cls=SpyTTS,
            warm_rvc_on_init=False,
        )
        self.assertIsNone(captured.get("edge_prosody"))

    def test_vc_params_compat_default_and_yaml_override(self) -> None:
        defaults = build_vc_kwargs({})
        self.assertEqual(defaults["filter_radius"], 3)
        self.assertEqual(defaults["rms_mix_rate"], 0.75)
        tuned = build_vc_kwargs({"filter_radius": 1, "rms_mix_rate": 0.68})
        self.assertEqual(tuned["filter_radius"], 1)
        self.assertEqual(tuned["rms_mix_rate"], 0.68)


class DryRunAbEntryTests(unittest.TestCase):
    _PROSODY = {
        "enabled": True,
        "rate": "-4%",
        "pitch": "+0Hz",
        "volume": "+0%",
        "question_rate_delta_pct": 2,
        "question_pitch_delta_hz": 4,
        "exclamation_rate_delta_pct": 2,
        "exclamation_pitch_delta_hz": 3,
        "ellipsis_rate_delta_pct": -3,
        "ellipsis_pitch_delta_hz": -2,
    }

    def _fake_config(self) -> Any:
        class FakeConfig:
            def get(self, key: str, default: Any = None) -> Any:
                if key == "voice.tts":
                    return {"edge_prosody": dict(DryRunAbEntryTests._PROSODY)}
                return default

            def get_rvc_config(self) -> dict:
                return {
                    "filter_radius": 1,
                    "rms_mix_rate": 0.68,
                    "pitch": 0,
                    "index_rate": 0.55,
                    "model_path": "unused.pth",
                }

        return FakeConfig()

    def test_dry_run_zero_network_rvc_files(self) -> None:
        import test_rvc_conversion as ab

        with patch.object(ab, "Config", return_value=self._fake_config()), patch(
            "edge_tts.Communicate"
        ) as comm, patch.object(ab, "check_rvc_health") as health, patch.object(
            ab, "TTSEngine"
        ) as tts_cls, patch.object(ab, "build_execute_safety_policy") as build_pol, patch.object(
            Path, "mkdir"
        ) as mkdir:
            code = ab.main([])
            self.assertEqual(code, 0)
            self.assertEqual(comm.call_count, 0)
            self.assertEqual(health.call_count, 0)
            self.assertEqual(tts_cls.call_count, 0)
            self.assertEqual(build_pol.call_count, 0)
            self.assertEqual(mkdir.call_count, 0)

    def test_default_and_explicit_core_suite_unchanged(self) -> None:
        import io
        import test_rvc_conversion as ab
        from contextlib import redirect_stdout

        self.assertEqual(len(ab.CORE_SAMPLE_CASES), 4)
        self.assertEqual(ab.SAMPLE_CASES, ab.CORE_SAMPLE_CASES)
        self.assertEqual(
            ab._naturalness_output_dir("core"),
            ab.TEST_DIR / "voice-naturalness",
        )
        self.assertEqual(
            [label for label, _ in ab.CORE_SAMPLE_CASES],
            ["neutral", "question", "exclamation", "ellipsis"],
        )

        for argv in ([], ["--suite", "core"]):
            with self.subTest(argv=argv):
                buf = io.StringIO()
                with patch.object(ab, "Config", return_value=self._fake_config()), redirect_stdout(
                    buf
                ):
                    code = ab.main(argv)
                self.assertEqual(code, 0)
                out = buf.getvalue()
                self.assertIn("suite: core（4 组）", out)
                plan_line = out.split("计划输出目录:")[1].splitlines()[0]
                self.assertIn("voice-naturalness", plan_line)
                self.assertNotIn("extended", plan_line)
                for label in ("neutral", "question", "exclamation", "ellipsis"):
                    self.assertIn(f"[{label}]", out)


class ExtendedSuiteTests(unittest.TestCase):
    """扩展语气状态试听集：样本/目录/dry-run/fake execute。"""

    _PROSODY = DryRunAbEntryTests._PROSODY

    def _fake_config(self) -> Any:
        return DryRunAbEntryTests()._fake_config()

    def _assets(self, tmp: Path) -> dict:
        return ExecuteSafetySealTests()._fake_assets(tmp)

    def _config(self, rvc: dict) -> Any:
        return ExecuteSafetySealTests()._config(rvc)

    def test_extended_cases_labels_texts_and_output_dir(self) -> None:
        import test_rvc_conversion as ab

        expected = (
            ("warm_greeting", "早上好，今天也一起慢慢来。"),
            ("caring_question", "你听起来有点累，需要先休息一下吗？"),
            ("curious_question", "后来发生了什么，你愿意告诉我吗？"),
            ("pleasant_surprise", "你真的做到了？！"),
            ("happy_excitement", "真的成功了！太棒了！"),
            ("encouragement", "没关系！我们再试一次就好！"),
            ("soothing", "不用着急……我会一直陪着你……"),
            ("hesitant_shy", "嗯……其实，我也很期待……"),
            ("serious_calm", "这一步需要谨慎处理，我们先确认清楚。"),
            ("good_night", "晚安，做个好梦。"),
        )
        self.assertEqual(ab.EXTENDED_SAMPLE_CASES, expected)
        self.assertEqual(len(ab.EXTENDED_SAMPLE_CASES), 10)
        self.assertEqual(
            ab._naturalness_output_dir("extended"),
            ab.TEST_DIR / "voice-naturalness" / "extended",
        )
        # core 目录不变，且不与 extended 重合
        self.assertNotEqual(
            ab._naturalness_output_dir("core"),
            ab._naturalness_output_dir("extended"),
        )

    def test_extended_dry_run_zero_side_effects_and_prosody_priority(self) -> None:
        import io
        import test_rvc_conversion as ab
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with patch.object(ab, "Config", return_value=self._fake_config()), patch(
            "edge_tts.Communicate"
        ) as comm, patch.object(ab, "check_rvc_health") as health, patch.object(
            ab, "TTSEngine"
        ) as tts_cls, patch.object(ab, "build_execute_safety_policy") as build_pol, patch.object(
            Path, "mkdir"
        ) as mkdir, redirect_stdout(buf):
            code = ab.main(["--suite", "extended"])
        self.assertEqual(code, 0)
        self.assertEqual(comm.call_count, 0)
        self.assertEqual(health.call_count, 0)
        self.assertEqual(tts_cls.call_count, 0)
        self.assertEqual(build_pol.call_count, 0)
        self.assertEqual(mkdir.call_count, 0)

        out = buf.getvalue()
        self.assertIn("suite: extended（10 组）", out)
        self.assertIn("extended", out.split("计划输出目录:")[1].splitlines()[0])
        for label, _ in ab.EXTENDED_SAMPLE_CASES:
            self.assertIn(f"[{label}]", out)

        # 有效 profile：？！→question；安慰/犹豫→ellipsis；认真/晚安→neutral
        settings = EdgeProsodySettings.from_mapping(self._PROSODY)
        surprise = settings.resolve_for_sentence("你真的做到了？！")
        self.assertEqual(classify_sentence_ending("你真的做到了？！"), "question")
        self.assertEqual(surprise.rate, "-2%")
        self.assertEqual(surprise.pitch, "+4Hz")

        soothing = settings.resolve_for_sentence("不用着急……我会一直陪着你……")
        shy = settings.resolve_for_sentence("嗯……其实，我也很期待……")
        self.assertEqual(classify_sentence_ending("不用着急……我会一直陪着你……"), "ellipsis")
        self.assertEqual(classify_sentence_ending("嗯……其实，我也很期待……"), "ellipsis")
        self.assertEqual(soothing.rate, "-7%")
        self.assertEqual(soothing.pitch, "-2Hz")
        self.assertEqual(shy.rate, "-7%")

        serious = settings.resolve_for_sentence("这一步需要谨慎处理，我们先确认清楚。")
        night = settings.resolve_for_sentence("晚安，做个好梦。")
        self.assertEqual(classify_sentence_ending("这一步需要谨慎处理，我们先确认清楚。"), "neutral")
        self.assertEqual(classify_sentence_ending("晚安，做个好梦。"), "neutral")
        self.assertEqual(serious.rate, "-4%")
        self.assertEqual(serious.pitch, "+0Hz")
        self.assertEqual(night.rate, "-4%")

        self.assertIn("[pleasant_surprise] rate=-2% pitch=+4Hz", out)
        self.assertIn("[soothing] rate=-7% pitch=-2Hz", out)
        self.assertIn("[hesitant_shy] rate=-7% pitch=-2Hz", out)
        self.assertIn("[serious_calm] rate=-4% pitch=+0Hz", out)
        self.assertIn("[good_night] rate=-4% pitch=+0Hz", out)

    def test_fake_extended_execute_ten_pairs_exact_counts(self) -> None:
        import test_rvc_conversion as ab
        from src.voice.rvc.health_check import RVCHealthReport

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rvc = self._assets(root)
            cfg = self._config(rvc)
            out_parent = root / "voice-naturalness" / "extended"
            out_parent.mkdir(parents=True, exist_ok=True)
            counts = {"save": 0, "wav": 0, "rvc": 0, "shutdown": 0}

            class FakeTTS:
                def __init__(self, **kwargs: Any) -> None:
                    pol = kwargs["safety_policy"]
                    pol.register_file_delete_dir(out_parent)
                    self._temp_files = TempFileManager(
                        safety_policy=pol, default_dir=out_parent
                    )

                def save_to_file(self, text: str, filename: str) -> None:
                    counts["save"] += 1
                    Path(filename).write_bytes(b"mp3")

                def _mp3_to_wav(self, path: str, _cfg=None) -> str:
                    counts["wav"] += 1
                    wav = Path(path).with_suffix(".wav")
                    wav.write_bytes(b"wav")
                    return str(wav)

                def apply_rvc_model(self, path: str) -> str:
                    counts["rvc"] += 1
                    out = Path(path).with_name(Path(path).stem + "_rvc.wav")
                    out.write_bytes(b"rvc")
                    return str(out)

                def shutdown(self, *, stop_rvc: bool = True) -> None:
                    counts["shutdown"] += 1
                    self._temp_files.shutdown(timeout=0.2)

            with patch.object(
                ab, "resolve_rvc_python", return_value=Path(rvc["python_path"])
            ), patch.object(
                ab, "check_rvc_health", return_value=RVCHealthReport(ok=True)
            ), patch.object(ab, "TTSEngine", FakeTTS), patch.object(
                ab, "_naturalness_output_dir", return_value=out_parent
            ):
                code = ab.run_execute(cfg, suite="extended")

            self.assertEqual(code, 0)
            self.assertEqual(counts["save"], 10)
            self.assertEqual(counts["wav"], 10)
            self.assertEqual(counts["rvc"], 10)
            self.assertEqual(counts["shutdown"], 1)
            for index, (label, _) in enumerate(ab.EXTENDED_SAMPLE_CASES, 1):
                self.assertTrue(
                    (out_parent / f"{index:02d}_{label}_base.mp3").is_file()
                )
                self.assertTrue(
                    (out_parent / f"{index:02d}_{label}_rvc.wav").is_file()
                )
            # 不得写入 core 根目录同名文件
            core_root = root / "voice-naturalness"
            self.assertFalse((core_root / "01_warm_greeting_base.mp3").exists())

    def test_extended_mid_failure_nonzero_cleanup_contract(self) -> None:
        import test_rvc_conversion as ab
        from src.voice.rvc.health_check import RVCHealthReport

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rvc = self._assets(root)
            cfg = self._config(rvc)
            out_parent = root / "voice-naturalness" / "extended"
            out_parent.mkdir(parents=True, exist_ok=True)
            shutdowns = {"n": 0}

            class FakeTTS:
                def __init__(self, **kwargs: Any) -> None:
                    self._n = 0
                    pol = kwargs["safety_policy"]
                    pol.register_file_delete_dir(out_parent)
                    self._temp_files = TempFileManager(
                        safety_policy=pol, default_dir=out_parent
                    )

                def save_to_file(self, text: str, filename: str) -> None:
                    Path(filename).write_bytes(b"mp3")

                def _mp3_to_wav(self, path: str, _cfg=None) -> str:
                    wav = Path(path).with_suffix(".wav")
                    wav.write_bytes(b"wav")
                    return str(wav)

                def apply_rvc_model(self, path: str) -> Optional[str]:
                    self._n += 1
                    if self._n == 3:
                        return None
                    out = Path(path).with_name(Path(path).stem + "_rvc.wav")
                    out.write_bytes(b"rvc")
                    return str(out)

                def shutdown(self, *, stop_rvc: bool = True) -> None:
                    shutdowns["n"] += 1
                    self._temp_files.shutdown(timeout=0.2)

            with patch.object(
                ab, "resolve_rvc_python", return_value=Path(rvc["python_path"])
            ), patch.object(
                ab, "check_rvc_health", return_value=RVCHealthReport(ok=True)
            ), patch.object(ab, "TTSEngine", FakeTTS), patch.object(
                ab, "_naturalness_output_dir", return_value=out_parent
            ):
                code = ab.run_execute(cfg, suite="extended")

            self.assertEqual(code, 1)
            self.assertEqual(shutdowns["n"], 1)
            # 前两组成对保留；第 3 组半成品清理；后续未生成
            self.assertTrue((out_parent / "01_warm_greeting_base.mp3").is_file())
            self.assertTrue((out_parent / "01_warm_greeting_rvc.wav").is_file())
            self.assertTrue((out_parent / "02_caring_question_base.mp3").is_file())
            self.assertTrue((out_parent / "02_caring_question_rvc.wav").is_file())
            self.assertFalse((out_parent / "03_curious_question_base.mp3").exists())
            self.assertFalse((out_parent / "03_curious_question_rvc.wav").exists())
            self.assertFalse((out_parent / "04_pleasant_surprise_base.mp3").exists())
            self.assertFalse((out_parent / "10_good_night_base.mp3").exists())


class ExecuteSafetySealTests(unittest.TestCase):
    """fake --execute：TrustedRvcProfile 封存与副作用矩阵（无真实进程/网络）。"""

    _PROSODY = {
        "enabled": True,
        "rate": "-4%",
        "pitch": "+0Hz",
        "volume": "+0%",
        "question_rate_delta_pct": 2,
        "question_pitch_delta_hz": 4,
        "exclamation_rate_delta_pct": 2,
        "exclamation_pitch_delta_hz": 3,
        "ellipsis_rate_delta_pct": -3,
        "ellipsis_pitch_delta_hz": -2,
    }

    def _fake_assets(self, tmp: Path, *, with_index: bool = True) -> dict:
        python = tmp / "python.exe"
        model = tmp / "model.pth"
        index = tmp / "model.index"
        python.write_bytes(b"PY")
        model.write_bytes(b"MODEL")
        if with_index:
            index.write_bytes(b"IDX")
        cfg = {
            "model_path": str(model),
            "index_path": str(index) if with_index else str(tmp / "missing.index"),
            "python_path": str(python),
            "filter_radius": 1,
            "rms_mix_rate": 0.68,
            "pitch": 0,
            "index_rate": 0.55,
        }
        return cfg

    def _config(self, rvc: dict) -> Any:
        class Cfg:
            def get(self, key: str, default: Any = None) -> Any:
                if key == "voice.tts":
                    return {"engine": "edge-tts", "edge_prosody": dict(ExecuteSafetySealTests._PROSODY)}
                if key == "security":
                    return {"permissions": {"cloud_tts": True, "file_delete": True}}
                return default

            def get_rvc_config(self) -> dict:
                return dict(rvc)

        return Cfg()

    def test_execute_shares_sealed_policy_with_health_and_tts(self) -> None:
        import test_rvc_conversion as ab
        from src.voice.rvc.health_check import RVCHealthReport

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rvc = self._fake_assets(root)
            cfg = self._config(rvc)
            seen_health: list = []
            seen_tts: list = []

            def fake_health(rvc_cfg, *, safety_policy=None):
                seen_health.append(safety_policy)
                return RVCHealthReport(ok=True, model_path=rvc_cfg.get("model_path"))

            class FakeTTS:
                def __init__(self, **kwargs: Any) -> None:
                    seen_tts.append(kwargs.get("safety_policy"))
                    self.kwargs = kwargs
                    self.shutdown_calls = 0
                    pol = kwargs["safety_policy"]
                    pol.register_file_delete_dir(out_parent)
                    self._temp_files = TempFileManager(
                        safety_policy=pol, default_dir=out_parent
                    )

                def save_to_file(self, text: str, filename: str) -> None:
                    Path(filename).write_bytes(b"mp3")

                def _mp3_to_wav(self, path: str, _cfg=None) -> str:
                    wav = Path(path).with_suffix(".wav")
                    wav.write_bytes(b"wav")
                    return str(wav)

                def apply_rvc_model(self, path: str) -> str:
                    out = Path(path).with_name(Path(path).stem + "_rvc.wav")
                    out.write_bytes(b"rvc")
                    return str(out)

                def shutdown(self, *, stop_rvc: bool = True) -> None:
                    self.shutdown_calls += 1
                    self._temp_files.shutdown(timeout=0.2)

            out_parent = root / "voice-naturalness"
            out_parent.mkdir(parents=True, exist_ok=True)
            with patch.object(ab, "resolve_rvc_python", return_value=Path(rvc["python_path"])), patch.object(
                ab, "check_rvc_health", side_effect=fake_health
            ), patch.object(ab, "TTSEngine", FakeTTS), patch.object(
                ab, "_naturalness_output_dir", return_value=out_parent
            ):
                code = ab.run_execute(cfg)

            self.assertEqual(code, 0)
            self.assertEqual(len(seen_health), 1)
            self.assertEqual(len(seen_tts), 1)
            self.assertIs(seen_health[0], seen_tts[0])
            policy = seen_health[0]
            self.assertTrue(policy.rvc_profile_sealed())
            self.assertEqual(policy.rvc_python().resolve(), Path(rvc["python_path"]).resolve())
            self.assertEqual(policy.rvc_model().resolve(), Path(rvc["model_path"]).resolve())
            self.assertEqual(policy.rvc_index().resolve(), Path(rvc["index_path"]).resolve())
            pairs = list(out_parent.glob("*_base.mp3"))
            self.assertEqual(len(pairs), 4)
            finals = [
                p
                for p in out_parent.glob("*_rvc.wav")
                if "_base_" not in p.name
            ]
            self.assertEqual(len(finals), 4)

    def test_health_reject_zero_tts_and_mkdir(self) -> None:
        import test_rvc_conversion as ab
        from src.voice.rvc.health_check import RVCHealthReport

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rvc = self._fake_assets(root)
            cfg = self._config(rvc)
            out_parent = root / "voice-naturalness"

            with patch.object(ab, "resolve_rvc_python", return_value=Path(rvc["python_path"])), patch.object(
                ab,
                "check_rvc_health",
                return_value=RVCHealthReport(ok=False, errors=["nope"]),
            ), patch.object(ab, "TTSEngine") as tts_cls, patch.object(
                ab, "_naturalness_output_dir", return_value=out_parent
            ):
                code = ab.run_execute(cfg)
            self.assertEqual(code, 1)
            self.assertEqual(tts_cls.call_count, 0)
            self.assertFalse(out_parent.exists())

    def test_missing_interpreter_or_model_zero_probe(self) -> None:
        import test_rvc_conversion as ab

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "model.pth"
            model.write_bytes(b"M")
            missing_py = root / "missing-python.exe"
            rvc = {
                "model_path": str(model),
                "python_path": str(missing_py),
                "index_path": "",
            }
            cfg = self._config(rvc)
            out_parent = root / "out"

            with patch.object(ab, "resolve_rvc_python", return_value=missing_py), patch.object(
                ab, "check_rvc_health"
            ) as health, patch.object(ab, "TTSEngine") as tts_cls, patch.object(
                ab, "_naturalness_output_dir", return_value=out_parent
            ):
                code = ab.run_execute(cfg)
            self.assertEqual(code, 1)
            self.assertEqual(health.call_count, 0)
            self.assertEqual(tts_cls.call_count, 0)
            self.assertFalse(out_parent.exists())

            # 缺模型
            py = root / "python.exe"
            py.write_bytes(b"PY")
            rvc2 = {
                "model_path": str(root / "gone.pth"),
                "python_path": str(py),
            }
            with patch.object(ab, "resolve_rvc_python", return_value=py), patch.object(
                ab, "check_rvc_health"
            ) as health2, patch.object(ab, "TTSEngine") as tts2:
                code2 = ab.run_execute(self._config(rvc2))
            self.assertEqual(code2, 1)
            self.assertEqual(health2.call_count, 0)
            self.assertEqual(tts2.call_count, 0)

    def test_fake_success_shutdown_once_partial_rvc_nonzero(self) -> None:
        import test_rvc_conversion as ab
        from src.voice.rvc.health_check import RVCHealthReport

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rvc = self._fake_assets(root, with_index=False)
            cfg = self._config(rvc)
            out_parent = root / "voice-naturalness"
            out_parent.mkdir(parents=True, exist_ok=True)
            shutdowns = {"n": 0}

            class FakeTTS:
                def __init__(self, **kwargs: Any) -> None:
                    self._n = 0
                    pol = kwargs["safety_policy"]
                    assert pol.rvc_profile_sealed()
                    assert pol.rvc_index() is None
                    pol.register_file_delete_dir(out_parent)
                    self._temp_files = TempFileManager(
                        safety_policy=pol, default_dir=out_parent
                    )

                def save_to_file(self, text: str, filename: str) -> None:
                    Path(filename).write_bytes(b"mp3")

                def _mp3_to_wav(self, path: str, _cfg=None) -> str:
                    wav = Path(path).with_suffix(".wav")
                    wav.write_bytes(b"wav")
                    return str(wav)

                def apply_rvc_model(self, path: str) -> Optional[str]:
                    self._n += 1
                    if self._n == 2:
                        return None
                    out = Path(path).with_name(Path(path).stem + "_rvc.wav")
                    out.write_bytes(b"rvc")
                    return str(out)

                def shutdown(self, *, stop_rvc: bool = True) -> None:
                    shutdowns["n"] += 1
                    self._temp_files.shutdown(timeout=0.2)

            with patch.object(ab, "resolve_rvc_python", return_value=Path(rvc["python_path"])), patch.object(
                ab,
                "check_rvc_health",
                return_value=RVCHealthReport(ok=True),
            ), patch.object(ab, "TTSEngine", FakeTTS), patch.object(
                ab, "_naturalness_output_dir", return_value=out_parent
            ):
                code = ab.run_execute(cfg)
            self.assertEqual(code, 1)
            self.assertEqual(shutdowns["n"], 1)
            # 第 1 组成对成功保留；第 2 组失败后立即停止并清理本轮半成品
            self.assertTrue((out_parent / "01_neutral_base.mp3").is_file())
            self.assertTrue((out_parent / "01_neutral_rvc.wav").is_file())
            self.assertFalse((out_parent / "02_question_base.mp3").exists())
            self.assertFalse((out_parent / "02_question_rvc.wav").exists())
            self.assertFalse((out_parent / "03_exclamation_base.mp3").exists())


class ExecuteFailClosedTests(unittest.TestCase):
    """F2：Edge 空文件/旧产物/转码失败 fail-closed。"""

    _PROSODY = ExecuteSafetySealTests._PROSODY

    def _assets(self, tmp: Path) -> dict:
        return ExecuteSafetySealTests()._fake_assets(tmp)

    def _config(self, rvc: dict) -> Any:
        return ExecuteSafetySealTests()._config(rvc)

    def test_edge_empty_or_missing_zero_transcode_rvc_shutdown_once(self) -> None:
        import test_rvc_conversion as ab
        from src.voice.rvc.health_check import RVCHealthReport

        for mode in ("empty", "missing"):
            with self.subTest(mode=mode):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    rvc = self._assets(root)
                    cfg = self._config(rvc)
                    out_parent = root / "voice-naturalness"
                    out_parent.mkdir(parents=True, exist_ok=True)
                    counts = {"wav": 0, "rvc": 0, "shutdown": 0}

                    class FakeTTS:
                        def __init__(self, **kwargs: Any) -> None:
                            pol = kwargs["safety_policy"]
                            pol.register_file_delete_dir(out_parent)
                            self._temp_files = TempFileManager(
                                safety_policy=pol, default_dir=out_parent
                            )

                        def save_to_file(self, text: str, filename: str) -> None:
                            if mode == "empty":
                                Path(filename).write_bytes(b"")
                            # missing: 不创建文件

                        def _mp3_to_wav(self, path: str, _cfg=None) -> str:
                            counts["wav"] += 1
                            raise AssertionError("must not transcode")

                        def apply_rvc_model(self, path: str) -> str:
                            counts["rvc"] += 1
                            raise AssertionError("must not rvc")

                        def shutdown(self, *, stop_rvc: bool = True) -> None:
                            counts["shutdown"] += 1
                            if not stop_rvc:
                                raise AssertionError("expected stop_rvc=True")
                            self._temp_files.shutdown(timeout=0.2)

                    with patch.object(
                        ab, "resolve_rvc_python", return_value=Path(rvc["python_path"])
                    ), patch.object(
                        ab, "check_rvc_health", return_value=RVCHealthReport(ok=True)
                    ), patch.object(ab, "TTSEngine", FakeTTS), patch.object(
                        ab, "_naturalness_output_dir", return_value=out_parent
                    ):
                        code = ab.run_execute(cfg)
                    self.assertEqual(code, 1)
                    self.assertEqual(counts["wav"], 0)
                    self.assertEqual(counts["rvc"], 0)
                    self.assertEqual(counts["shutdown"], 1)
                    self.assertFalse((out_parent / "01_neutral_base.mp3").exists())

    def test_stale_artifacts_not_reused_when_edge_fails(self) -> None:
        import test_rvc_conversion as ab
        from src.voice.rvc.health_check import RVCHealthReport

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rvc = self._assets(root)
            cfg = self._config(rvc)
            out_parent = root / "voice-naturalness"
            out_parent.mkdir(parents=True, exist_ok=True)
            stale_base = out_parent / "01_neutral_base.mp3"
            stale_rvc = out_parent / "01_neutral_rvc.wav"
            stale_base.write_bytes(b"OLD_BASE")
            stale_rvc.write_bytes(b"OLD_RVC")
            counts = {"wav": 0, "rvc": 0, "shutdown": 0}

            class FakeTTS:
                def __init__(self, **kwargs: Any) -> None:
                    pol = kwargs["safety_policy"]
                    pol.register_file_delete_dir(out_parent)
                    self._temp_files = TempFileManager(
                        safety_policy=pol, default_dir=out_parent
                    )

                def save_to_file(self, text: str, filename: str) -> None:
                    Path(filename).write_bytes(b"")  # 失败空文件

                def _mp3_to_wav(self, path: str, _cfg=None) -> str:
                    counts["wav"] += 1
                    raise AssertionError("no reuse")

                def apply_rvc_model(self, path: str) -> str:
                    counts["rvc"] += 1
                    raise AssertionError("no reuse")

                def shutdown(self, *, stop_rvc: bool = True) -> None:
                    counts["shutdown"] += 1
                    self._temp_files.shutdown(timeout=0.2)

            with patch.object(
                ab, "resolve_rvc_python", return_value=Path(rvc["python_path"])
            ), patch.object(
                ab, "check_rvc_health", return_value=RVCHealthReport(ok=True)
            ), patch.object(ab, "TTSEngine", FakeTTS), patch.object(
                ab, "_naturalness_output_dir", return_value=out_parent
            ):
                code = ab.run_execute(cfg)
            self.assertEqual(code, 1)
            self.assertEqual(counts["wav"], 0)
            self.assertEqual(counts["rvc"], 0)
            self.assertEqual(counts["shutdown"], 1)
            self.assertFalse(stale_base.exists())
            self.assertFalse(stale_rvc.exists())

    def test_transcode_or_rvc_fail_cleans_and_nonzero(self) -> None:
        import test_rvc_conversion as ab
        from src.voice.rvc.health_check import RVCHealthReport

        for fail_stage in ("transcode", "rvc"):
            with self.subTest(fail_stage=fail_stage):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    rvc = self._assets(root)
                    cfg = self._config(rvc)
                    out_parent = root / "voice-naturalness"
                    out_parent.mkdir(parents=True, exist_ok=True)
                    shutdowns = {"n": 0}

                    class FakeTTS:
                        def __init__(self, **kwargs: Any) -> None:
                            pol = kwargs["safety_policy"]
                            pol.register_file_delete_dir(out_parent)
                            self._temp_files = TempFileManager(
                                safety_policy=pol, default_dir=out_parent
                            )

                        def save_to_file(self, text: str, filename: str) -> None:
                            Path(filename).write_bytes(b"mp3")

                        def _mp3_to_wav(self, path: str, _cfg=None) -> str:
                            if fail_stage == "transcode":
                                raise RuntimeError("transcode_boom")
                            wav = Path(path).with_suffix(".wav")
                            wav.write_bytes(b"wav")
                            return str(wav)

                        def apply_rvc_model(self, path: str) -> Optional[str]:
                            if fail_stage == "rvc":
                                return None
                            out = Path(path).with_name(Path(path).stem + "_rvc.wav")
                            out.write_bytes(b"rvc")
                            return str(out)

                        def shutdown(self, *, stop_rvc: bool = True) -> None:
                            shutdowns["n"] += 1
                            self._temp_files.shutdown(timeout=0.2)

                    with patch.object(
                        ab, "resolve_rvc_python", return_value=Path(rvc["python_path"])
                    ), patch.object(
                        ab, "check_rvc_health", return_value=RVCHealthReport(ok=True)
                    ), patch.object(ab, "TTSEngine", FakeTTS), patch.object(
                        ab, "_naturalness_output_dir", return_value=out_parent
                    ):
                        code = ab.run_execute(cfg)
                    self.assertEqual(code, 1)
                    self.assertEqual(shutdowns["n"], 1)
                    self.assertFalse((out_parent / "01_neutral_base.mp3").exists())
                    self.assertFalse((out_parent / "01_neutral_rvc.wav").exists())

    def test_purge_adopt_reject_or_remove_incomplete_blocks_tts(self) -> None:
        """旧非空产物存在时，adopt 拒绝或 remove 未删净 → 零 TTS/转码/RVC。"""
        import test_rvc_conversion as ab
        from src.voice.rvc.health_check import RVCHealthReport

        for mode in ("adopt_reject", "remove_incomplete"):
            with self.subTest(mode=mode):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    rvc = self._assets(root)
                    cfg = self._config(rvc)
                    out_parent = root / "voice-naturalness"
                    out_parent.mkdir(parents=True, exist_ok=True)
                    stale_base = out_parent / "01_neutral_base.mp3"
                    stale_rvc = out_parent / "01_neutral_rvc.wav"
                    stale_base.write_bytes(b"OLD_BASE")
                    stale_rvc.write_bytes(b"OLD_RVC")
                    counts = {"save": 0, "wav": 0, "rvc": 0, "shutdown": 0}

                    class TrackingTM(TempFileManager):
                        def adopt_existing(self, file_path):  # noqa: ANN001
                            if mode == "adopt_reject":
                                raise PermissionError("拒绝登记 symlink/junction 或非普通文件")
                            return super().adopt_existing(file_path)

                        def remove(self, file_path: str, max_retries: int = 8, delay: float = 0.5) -> None:
                            if mode == "remove_incomplete":
                                # 模拟占用入延迟队列：登记但不删除
                                return
                            super().remove(file_path, max_retries=max_retries, delay=delay)

                    class FakeTTS:
                        def __init__(self, **kwargs: Any) -> None:
                            pol = kwargs["safety_policy"]
                            pol.register_file_delete_dir(out_parent)
                            self._temp_files = TrackingTM(
                                safety_policy=pol, default_dir=out_parent
                            )

                        def save_to_file(self, text: str, filename: str) -> None:
                            counts["save"] += 1
                            raise AssertionError("must not save after purge fail")

                        def _mp3_to_wav(self, path: str, _cfg=None) -> str:
                            counts["wav"] += 1
                            raise AssertionError("must not transcode")

                        def apply_rvc_model(self, path: str) -> str:
                            counts["rvc"] += 1
                            raise AssertionError("must not rvc")

                        def shutdown(self, *, stop_rvc: bool = True) -> None:
                            counts["shutdown"] += 1
                            if not stop_rvc:
                                raise AssertionError("expected stop_rvc=True")
                            self._temp_files.shutdown(timeout=0.2)

                    with patch.object(
                        ab, "resolve_rvc_python", return_value=Path(rvc["python_path"])
                    ), patch.object(
                        ab, "check_rvc_health", return_value=RVCHealthReport(ok=True)
                    ), patch.object(ab, "TTSEngine", FakeTTS), patch.object(
                        ab, "_naturalness_output_dir", return_value=out_parent
                    ):
                        code = ab.run_execute(cfg)
                    self.assertEqual(code, 1)
                    self.assertEqual(counts["save"], 0)
                    self.assertEqual(counts["wav"], 0)
                    self.assertEqual(counts["rvc"], 0)
                    self.assertEqual(counts["shutdown"], 1)
                    # 旧文件仍在（未能安全清除）——不得被当作成功产物报告
                    self.assertTrue(stale_base.is_file())
                    self.assertTrue(stale_rvc.is_file())


class SaveToFileContractTests(unittest.TestCase):
    def _engine(self, *, fallback: bool, tmp: str) -> TTSEngine:
        policy = SafetyPolicy(
            {"permissions": {"cloud_tts": True, "file_delete": True}}
        )
        policy.register_file_delete_dir(tmp)
        with patch.object(TTSEngine, "prepare_rvc"), patch.object(
            TTSEngine, "_initialize_engine"
        ):
            engine = TTSEngine(
                engine="edge-tts",
                pipeline_enabled=False,
                rvc_config={},
                safety_policy=policy,
                warm_rvc_on_init=False,
                fallback_enabled=fallback,
                temp_file_manager=TempFileManager(
                    safety_policy=policy, default_dir=Path(tmp)
                ),
            )
        engine.engine_type = "edge-tts"
        return engine

    def test_edge_exception_cleans_partial_and_reraises(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self._engine(fallback=False, tmp=tmp)
            out = Path(tmp) / "partial.mp3"

            class BoomComm:
                def __init__(self, *a, **k):
                    pass

                async def save(self, path: str) -> None:
                    Path(path).write_bytes(b"")
                    raise RuntimeError("ClientConnectorError")

            with patch("edge_tts.Communicate", BoomComm):
                with self.assertRaises(RuntimeError):
                    engine.save_to_file("hi", str(out))
            self.assertFalse(out.exists())
            engine.shutdown(stop_rvc=False)

    def test_permission_fallback_success_and_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            # 成功 fallback
            engine = self._engine(fallback=True, tmp=tmp)
            out = Path(tmp) / "ok.mp3"
            engine._require_cloud_tts = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[method-assign]
                PermissionError("deny")
            )
            engine._ensure_pyttsx3_fallback = lambda: True  # type: ignore[method-assign]

            class Py:
                def save_to_file(self, text, filename):
                    Path(filename).write_bytes(b"py")

                def runAndWait(self):
                    return None

            engine._pyttsx3 = Py()
            engine.save_to_file("hi", str(out))
            self.assertTrue(out.is_file() and out.stat().st_size > 0)

            # fallback 也失败 → 清理并抛出
            out2 = Path(tmp) / "bad.mp3"
            out2.write_bytes(b"x")

            class BoomPy:
                def save_to_file(self, text, filename):
                    Path(filename).write_bytes(b"")
                    raise OSError("py fail")

                def runAndWait(self):
                    return None

            engine._pyttsx3 = BoomPy()
            with self.assertRaises(OSError):
                engine.save_to_file("hi", str(out2))
            self.assertFalse(out2.exists())
            engine.shutdown(stop_rvc=False)


if __name__ == "__main__":
    unittest.main()
