# -*- coding: utf-8 -*-
"""
语音自然度 / RVC 人工 A/B 入口（非离线 unittest 门禁）。

默认无参数 = dry-run：只打印计划、有效 prosody 与隐私提示；零网络、零 RVC、零文件创建。
显式 ``--execute`` 才调用 edge 网络 / RVC / 写音频。
``--suite {core,extended}`` 选择固定样本集（默认 core）。

用法：
    py test_rvc_conversion.py                         # dry-run（core）
    py test_rvc_conversion.py --suite extended        # dry-run（extended）
    py test_rvc_conversion.py --execute               # 真实合成 core
    py test_rvc_conversion.py --execute --suite extended

不得被 unittest discovery 导入为测试用例；本文件不以 test_ 函数供 discovery 收集。
"""
from __future__ import annotations

import argparse
import io
import logging
import shutil
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

project_root = Path(__file__).parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.config import Config
from src.core.paths import TEST_DIR
from src.voice.naturalness_helpers import (
    build_execute_safety_policy,
    path_lexists as _path_lexists,
    purge_sample_artifacts as _purge_sample_artifacts,
    require_nonempty_audio as _require_nonempty_audio,
)
from src.voice.prosody import EdgeProsodySettings
from src.voice.rvc.health_check import check_rvc_health
from src.voice.rvc.infer import resolve_rvc_python
from src.voice.tts import TTSEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

SampleCase = Tuple[str, str]

# 固定非敏感样本：陈述 / 疑问 / 感叹 / 省略舒缓
CORE_SAMPLE_CASES: Tuple[SampleCase, ...] = (
    ("neutral", "今天天气不错，适合出去走走。"),
    ("question", "你今天想喝点什么？"),
    ("exclamation", "太好了！我们马上开始吧！"),
    ("ellipsis", "嗯……"),
)

# 扩展语气状态试听集（顺序与 label 固定）
EXTENDED_SAMPLE_CASES: Tuple[SampleCase, ...] = (
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

# 兼容旧引用：默认 = core
SAMPLE_CASES: Tuple[SampleCase, ...] = CORE_SAMPLE_CASES

SUITE_CASES: Mapping[str, Tuple[SampleCase, ...]] = {
    "core": CORE_SAMPLE_CASES,
    "extended": EXTENDED_SAMPLE_CASES,
}


def _suite_cases(suite: str) -> Tuple[SampleCase, ...]:
    try:
        return SUITE_CASES[suite]
    except KeyError as exc:
        raise ValueError(f"unsupported suite: {suite!r}") from exc


def _naturalness_output_dir(suite: str = "core") -> Path:
    """计划路径；dry-run 不创建目录。extended 为独立子目录，不覆盖 core。"""
    base = TEST_DIR / "voice-naturalness"
    if suite == "extended":
        return base / "extended"
    return base


def _load_prosody_settings(config: Config) -> EdgeProsodySettings:
    tts = config.get("voice.tts", {}) or {}
    return EdgeProsodySettings.from_mapping(tts.get("edge_prosody"))


def print_dry_run_plan(config: Config, *, suite: str = "core") -> int:
    """无参数入口：打印计划与有效 prosody，不做任何 I/O 副作用。"""
    print("=" * 60)
    print("语音自然度 A/B（dry-run）")
    print("=" * 60)
    print()
    print("本模式不调用 edge 网络、不启动 RVC、不创建音频文件、不播放。")
    print("真实试听请显式传入 --execute。")
    print("隐私：不打印用户对话；下列仅为代码内固定非敏感样本。")
    print()

    try:
        cases = _suite_cases(suite)
        settings = _load_prosody_settings(config)
    except ValueError as exc:
        print(f"配置非法（在任何网络/音频副作用前失败）: {exc}")
        return 1

    rvc = config.get_rvc_config() or {}
    out_dir = _naturalness_output_dir(suite)
    print(f"suite: {suite}（{len(cases)} 组）")
    print(f"计划输出目录: {out_dir}")
    print(
        f"RVC YAML: filter_radius={rvc.get('filter_radius')!r} "
        f"rms_mix_rate={rvc.get('rms_mix_rate')!r} "
        f"pitch={rvc.get('pitch')!r} index_rate={rvc.get('index_rate')!r}"
    )
    print(f"edge_prosody.enabled={settings.enabled}")
    print()
    print("样本与有效 Communicate kwargs：")
    for label, text in cases:
        prosody = settings.resolve_for_sentence(text)
        print(f"  [{label}] rate={prosody.rate} pitch={prosody.pitch} volume={prosody.volume}")
        print(f"           text={text!r}")
    print()
    suite_flag = "" if suite == "core" else f" --suite {suite}"
    print(f"提示: py test_rvc_conversion.py --execute{suite_flag}")
    return 0


def run_execute(config: Config, *, suite: str = "core") -> int:
    """真实 edge + RVC A/B；失败返回非零；资源在 finally 关闭。"""
    print("=" * 60)
    print("语音自然度 A/B（--execute）")
    print("=" * 60)
    print()

    tts_engine: Optional[TTSEngine] = None
    try:
        cases = _suite_cases(suite)
        settings = _load_prosody_settings(config)
        rvc_config = config.get_rvc_config()
        if not rvc_config:
            print("错误: 未找到 RVC 配置（voice.tts.rvc）")
            return 1

        # 先于 health / 网络 / mkdir / TTS：解析资产并一次性封存
        safety, effective_rvc, err = build_execute_safety_policy(config, rvc_config)
        if safety is None or effective_rvc is None:
            print(f"错误: {err}")
            return 1

        report = check_rvc_health(effective_rvc, safety_policy=safety)
        print(report.summary())
        print()
        if not report.ok:
            print("RVC 环境未就绪，请运行 scripts/setup_voice_env.ps1")
            return 1

        out_dir = _naturalness_output_dir(suite)
        print(f"suite: {suite}（{len(cases)} 组）")
        out_dir.mkdir(parents=True, exist_ok=True)
        # 输出目录须在删除白名单内，供样本旧文件 fail-closed 清理
        safety.register_file_delete_dir(out_dir)

        tts_config = config.get("voice.tts", {}) or {}
        tts_engine = TTSEngine(
            engine=tts_config.get("engine", "edge-tts"),
            language=tts_config.get("language", "zh-CN"),
            voice=tts_config.get("voice", "zh-CN-XiaoxiaoNeural"),
            rvc_config=effective_rvc,
            safety_policy=safety,
            edge_prosody=tts_config.get("edge_prosody"),
            warm_rvc_on_init=True,
        )
        temp_files = tts_engine._temp_files  # noqa: SLF001

        success = 0
        total = len(cases)
        for index, (label, text) in enumerate(cases, 1):
            stage = "purge"
            base_path = out_dir / f"{index:02d}_{label}_base.mp3"
            rvc_dest = out_dir / f"{index:02d}_{label}_rvc.wav"
            wav_path: Optional[str] = None
            try:
                prosody = settings.resolve_for_sentence(text)
                print(f"[{index}/{total}] {label}")
                print(
                    f"  prosody rate={prosody.rate} "
                    f"pitch={prosody.pitch} volume={prosody.volume}"
                )

                # 合成前同步清理；失败不得进入 Edge/转码/RVC
                stage = "purge"
                _purge_sample_artifacts(temp_files, base_path, rvc_dest)

                stage = "edge"
                tts_engine.save_to_file(text, str(base_path))
                _require_nonempty_audio(base_path, stage="edge_base")
                print(f"  base -> {base_path}")

                stage = "transcode"
                wav_path = tts_engine._mp3_to_wav(  # noqa: SLF001
                    str(base_path), effective_rvc
                )
                _require_nonempty_audio(Path(wav_path), stage="transcode_wav")

                stage = "rvc"
                rvc_path = tts_engine.apply_rvc_model(wav_path)
                if not rvc_path or not Path(rvc_path).is_file():
                    raise RuntimeError("rvc_failed")
                _require_nonempty_audio(Path(rvc_path), stage="rvc_out")
                shutil.copy2(rvc_path, rvc_dest)
                _require_nonempty_audio(rvc_dest, stage="rvc_dest")
                print(f"  rvc  -> {rvc_dest}")
                success += 1
                print()
            except Exception as exc:
                print(f"  失败 stage={stage} err={type(exc).__name__}")
                # 尽量清理本轮半成品；清理再失败不掩盖主故障（仍返回非零）
                if stage != "purge":
                    extras = [base_path, rvc_dest]
                    if wav_path:
                        extras.append(Path(wav_path))
                    try:
                        _purge_sample_artifacts(temp_files, *extras)
                    except Exception as purge_exc:
                        logging.getLogger(__name__).debug(
                            "失败后清理未完成 err=%s", type(purge_exc).__name__
                        )
                return 1

        print(f"完成: {success}/{total} 条 RVC 成功；未自动播放。")
        print(f"输出目录: {out_dir}")
        return 0 if success == total else 1
    except Exception as exc:
        print(f"执行失败: {type(exc).__name__}")
        return 1
    finally:
        if tts_engine is not None:
            try:
                tts_engine.shutdown(stop_rvc=True)
            except Exception:
                logging.getLogger(__name__).exception("TTS/RVC shutdown failed")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="语音自然度 edge 韵律 + RVC 人工 A/B（默认 dry-run）"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真实调用 edge 网络与 RVC 并写入 data/test/voice-naturalness/",
    )
    parser.add_argument(
        "--suite",
        choices=tuple(SUITE_CASES.keys()),
        default="core",
        help="固定样本集：core（默认四组）或 extended（十组语气状态）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        config = Config()
    except Exception as exc:
        print(f"配置加载失败: {exc}")
        return 1

    if not args.execute:
        return print_dry_run_plan(config, suite=args.suite)
    return run_execute(config, suite=args.suite)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(1)
