"""
TTS 引擎门面：edge-tts + RVC 为主，pyttsx3 为离线应急备用

合成流程：文本清理 → edge-tts → MP3 转 WAV → 可选 RVC → 本地播放。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from queue import Empty, Queue
from typing import List, Optional, Set, Tuple

from src.core.paths import create_app_temp_file
from src.core.safety import SafetyPolicy
from src.voice.audio.player import AudioPlayer
from src.voice.audio.temp_files import TempFileManager
from src.voice.language import detect_edge_voice, sanitize_for_speech, split_for_speech
from src.voice.rvc.audio_prep import mp3_to_rvc_wav, warm_audio_preprocessing
from src.voice.rvc.converter import RVCConverter
from src.voice.timing import StageTimer, log_elapsed

# 已废弃的 engine 值，启动时自动映射为 edge-tts
_LEGACY_ENGINES = frozenset({"gtts", "rvc"})


class TTSEngine:
    """文字转语音：默认 edge-tts（可选 RVC 后处理），失败时可回退 pyttsx3"""

    def __init__(
        self,
        engine: str = "edge-tts",
        language: str = "zh-CN",
        rate: int = 150,
        volume: float = 0.8,
        voice: Optional[str] = None,
        rvc_config: Optional[dict] = None,
        fallback_enabled: bool = True,
        fallback_engine: str = "pyttsx3",
        pipeline_enabled: bool = True,
        safety_policy: Optional[SafetyPolicy] = None,
        temp_file_manager: Optional[TempFileManager] = None,
    ):
        """
        初始化 TTS 引擎。

        Args:
            engine: 主引擎，``edge-tts`` 或 ``pyttsx3``；已废弃的 ``gtts``/``rvc`` 会自动映射。
            language: 语言代码（如 ``zh-CN``），供 pyttsx3 与 STT 对齐。
            rate: 语速（pyttsx3 使用）。
            volume: 播放音量 0.0–1.0。
            voice: edge-tts 音色名；``None`` 时用默认中文女声。
            rvc_config: RVC 配置字典；缺 ``model_path`` 时不启用。
            fallback_enabled: 主引擎失败时是否尝试备用引擎。
            fallback_engine: 备用引擎名（通常 ``pyttsx3``）。
            pipeline_enabled: 是否启用分句流水线播报。
            safety_policy: 安全策略；影响临时文件删除与播放后端。
            temp_file_manager: 可选外部临时文件管理器；传入时本引擎不拥有其生命周期。
        """
        if engine in _LEGACY_ENGINES:
            logging.getLogger(__name__).warning(
                "voice.tts.engine=%s 已废弃，已自动改用 edge-tts", engine
            )
            engine = "edge-tts"

        if engine not in ("edge-tts", "pyttsx3"):
            raise ValueError(
                f"不支持的 TTS 引擎: {engine}（可选: edge-tts, pyttsx3）"
            )
        if fallback_engine != "pyttsx3":
            raise ValueError(f"不支持的应急引擎: {fallback_engine}（目前仅支持 pyttsx3）")

        self.engine_type = engine
        self.language = language
        self.rate = rate
        self.volume = volume
        self.voice = voice or "zh-CN-XiaoxiaoNeural"
        self.fallback_enabled = fallback_enabled
        self.fallback_engine = fallback_engine
        self.pipeline_enabled = pipeline_enabled
        self._rvc_config = dict(rvc_config or {})
        self.logger = logging.getLogger(__name__)
        self.safety_policy = safety_policy or SafetyPolicy()

        self._pyttsx3 = None
        self._player = AudioPlayer(volume=volume, safety_policy=self.safety_policy)
        self._owns_temp_files = temp_file_manager is None
        self._temp_files = temp_file_manager or TempFileManager(
            safety_policy=self.safety_policy
        )
        self._temp_files_shutdown_done = False
        self._rvc = (
            RVCConverter(self._rvc_config, safety_policy=self.safety_policy)
            if self._rvc_config.get("model_path")
            else None
        )
        self._rvc_ready = False
        self._rvc_last_error: Optional[str] = None
        self._speak_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._interrupt_event = threading.Event()
        self._interrupt_epoch_lock = threading.Lock()
        self._interrupt_epoch = 0

        if self._rvc and self._rvc.enabled:
            from src.voice.rvc.health_check import check_rvc_health

            report = check_rvc_health(self._rvc_config)
            self._rvc_ready = report.ok
            if not report.ok:
                self._rvc_last_error = report.summary()
                self.logger.error("%s", self._rvc_last_error)
            else:
                self.logger.info("RVC Nahida 模型与环境检查通过")
                if self._rvc_config.get("prewarm_audio_processing", False):
                    try:
                        with log_elapsed("MP3→WAV 重采样预热"):
                            warm_audio_preprocessing(
                                int(self._rvc_config.get("input_sample_rate", 40000))
                            )
                    except Exception as exc:
                        self.logger.warning("音频重采样预热失败，真实请求仍可继续: %s", exc)
                if self._rvc_config.get("worker_enabled", True):
                    from src.voice.rvc.infer import warm_rvc_worker

                    warm_rvc_worker(self._rvc_config)

        self._initialize_engine()

    def _initialize_engine(self) -> None:
        """按 ``engine_type`` 初始化 edge-tts 或 pyttsx3。"""
        if self.engine_type == "pyttsx3":
            if not self._init_pyttsx3():
                self.logger.error("pyttsx3 初始化失败，TTS 不可用")
            return

        try:
            import edge_tts  # noqa: F401

            self.logger.info("edge-tts 引擎准备就绪 (voice: %s)", self.voice)
        except ImportError as exc:
            self.logger.error("edge-tts 未安装: %s", exc)
            self._activate_fallback_on_init_failure()
        except Exception as exc:
            self.logger.error("edge-tts 初始化失败: %s", exc)
            self._activate_fallback_on_init_failure()

    def _activate_fallback_on_init_failure(self) -> None:
        """主引擎初始化失败且允许回退时切换到 pyttsx3。"""
        if not self.fallback_enabled:
            return
        if self._init_pyttsx3():
            self.logger.warning("已切换到应急引擎 pyttsx3（离线 Windows 语音）")
            self.engine_type = "pyttsx3"
        else:
            self.logger.error("应急引擎 pyttsx3 也不可用，TTS 已禁用")

    def _init_pyttsx3(self) -> bool:
        """初始化 pyttsx3 并设置语速、音量与中文语音。Returns: 成功 True。"""
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("rate", self.rate)
            engine.setProperty("volume", self.volume)
            for v in engine.getProperty("voices"):
                if "chinese" in v.name.lower() or "zh" in v.id.lower():
                    engine.setProperty("voice", v.id)
                    break
            self._pyttsx3 = engine
            self.logger.info("pyttsx3 引擎初始化成功")
            return True
        except ImportError:
            self.logger.error("pyttsx3 未安装，请运行: pip install -r requirements-optional.txt")
            return False
        except Exception as exc:
            self.logger.error("pyttsx3 初始化失败: %s", exc)
            return False

    def _ensure_pyttsx3_fallback(self) -> bool:
        """确保 pyttsx3 实例可用（懒初始化）。Returns: 可用 True。"""
        if self._pyttsx3:
            return True
        return self._init_pyttsx3()

    def speak(self, text: str, async_mode: bool = False) -> None:
        """
        朗读文本（阻塞式，内部串行加锁）。

        Args:
            text: 待朗读文本；会先经 ``sanitize_for_speech`` 清理。
            async_mode: 仅 pyttsx3 时有效，是否非阻塞 ``startLoop``。
        """
        request_epoch = self._interrupt_epoch_snapshot()
        if not text:
            return
        if self._shutdown_event.is_set():
            return
        text = sanitize_for_speech(text)
        if not text:
            self.logger.debug("清理表情后无可朗读内容，跳过 TTS")
            return

        with self._speak_lock:
            if self._shutdown_event.is_set():
                return
            # 排队中的旧轮若在等待锁时已被 interrupt，不能清除较新的打断信号。
            epoch_lock = self._ensure_interrupt_epoch_lock()
            with epoch_lock:
                if request_epoch != self._interrupt_epoch:
                    return
                self._interrupt_event.clear()
            try:
                if self.engine_type == "pyttsx3":
                    self._speak_pyttsx3(text, async_mode)
                    return

                sentences = split_for_speech(text) if self.pipeline_enabled else [text]
                if len(sentences) > 1:
                    self.logger.info("[TTS] 分句流水线: %d 段", len(sentences))
                    self._speak_pipeline(sentences)
                else:
                    self._speak_single(text)
            except Exception as exc:
                self.logger.error("TTS 朗读失败: %s", exc, exc_info=True)
                if (
                    not self._interrupted()
                    and self.engine_type == "edge-tts"
                    and self.fallback_enabled
                ):
                    self.logger.warning("尝试使用应急引擎 pyttsx3 朗读")
                    if self._ensure_pyttsx3_fallback():
                        self._speak_pyttsx3(text, async_mode)

    def _speak_pyttsx3(self, text: str, async_mode: bool) -> None:
        """使用 pyttsx3 直接朗读（不经 edge-tts / RVC）。"""
        if not self._pyttsx3 and not self._init_pyttsx3():
            self.logger.warning("pyttsx3 引擎未初始化")
            return
        with log_elapsed("pyttsx3 朗读"):
            self._pyttsx3.say(text)
            if async_mode:
                self._pyttsx3.startLoop(False)
            else:
                self._pyttsx3.runAndWait()

    def _speak_single(self, text: str) -> None:
        """单段文本：合成 → 播放 → 清理临时文件。"""
        timer = StageTimer("单段朗读")
        play_path, temp_paths = self._prepare_playable_audio(text, timer)
        timer.mark("合成完成")
        if not play_path:
            timer.finish()
            self._temp_files.remove_many(*temp_paths)
            return
        with log_elapsed("播放"):
            self._player.play(play_path)
        timer.mark("播放完成")
        timer.finish()
        self._temp_files.remove_many(*temp_paths)

    def _speak_pipeline(self, sentences: List[str]) -> None:
        """分句流水线：后台线程预合成，主线程顺序播放。"""
        timer = StageTimer("分句流水线")
        ready_queue: Queue = Queue()
        temp_registry: Set[str] = set()
        errors: List[Exception] = []

        def _prepare() -> None:
            try:
                for idx, sentence in enumerate(sentences, start=1):
                    if self._interrupted():
                        break
                    label = f"第{idx}/{len(sentences)}句"
                    try:
                        play_path, temp_paths = self._prepare_playable_audio(sentence, None, label=label)
                        temp_registry.update(temp_paths)
                        if play_path and not self._interrupted():
                            ready_queue.put(play_path)
                    except Exception as exc:
                        errors.append(exc)
                        self.logger.error("分句合成失败 (%s): %s", label, exc, exc_info=True)
            finally:
                ready_queue.put(None)

        prep_thread = threading.Thread(target=_prepare, daemon=True)
        prep_thread.start()

        played = 0
        while True:
            try:
                play_path = ready_queue.get(timeout=0.1)
            except Empty:
                if self._interrupted():
                    break
                continue
            if play_path is None:
                break
            if self._interrupted():
                break
            with log_elapsed(f"播放第{played + 1}段"):
                self._player.play(play_path)
            played += 1
            timer.mark(f"第{played}段播放完")

        prep_thread.join(timeout=2.0 if self._interrupted() else 600.0)
        if prep_thread.is_alive():
            self.logger.warning("TTS 合成线程在插话后仍未退出，将由后台完成并交给临时文件清理器回收")
        timer.finish()
        # 合成线程仍可能正在写入/使用登记文件；此时交给周期清理，避免并发删除。
        if not prep_thread.is_alive():
            self._temp_files.remove_many(*temp_registry)

        if errors and played == 0:
            raise errors[0]

    def _prepare_playable_audio(
        self,
        text: str,
        timer: Optional[StageTimer] = None,
        label: str = "",
    ) -> Tuple[Optional[str], Set[str]]:
        """
        生成可播放音频路径并登记待删临时文件。

        Returns:
            ``(播放路径, 临时文件路径集合)``；合成失败时播放路径为 ``None``。
        """
        temp_paths: Set[str] = set()
        prefix = f"{label} " if label else ""

        if self._interrupted():
            return None, temp_paths

        with log_elapsed(f"{prefix}edge-tts 生成"):
            mp3_path = asyncio.run(self._edge_tts_to_mp3(text))
        temp_paths.add(mp3_path)
        if self._interrupted():
            return None, temp_paths
        if timer:
            timer.mark(f"{prefix}edge-tts")

        with log_elapsed(f"{prefix}MP3→WAV"):
            wav_path = self._mp3_to_wav(mp3_path, self._rvc_config)
        if wav_path != mp3_path:
            temp_paths.add(wav_path)
        if timer:
            timer.mark(f"{prefix}转WAV")
        if self._interrupted():
            return None, temp_paths

        play_path = mp3_path
        if self._rvc and self._rvc.enabled:
            if not self._rvc_ready:
                self.logger.error(
                    "RVC 未就绪，将播放默认 edge-tts 音频。"
                    "修复: powershell -ExecutionPolicy Bypass -File scripts/setup_voice_env.ps1"
                )
            else:
                self.logger.info("%s开始 RVC 音色转换...", prefix)
            with log_elapsed(f"{prefix}RVC 转换"):
                rvc_path = self._rvc.convert_file(wav_path) if self._rvc_ready else None
            if rvc_path:
                play_path = rvc_path
                temp_paths.add(rvc_path)
            elif self._rvc_ready:
                self.logger.error("%sRVC 转换失败，回退到默认 edge-tts 音频", prefix)
            if timer:
                timer.mark(f"{prefix}RVC")

        if self._interrupted():
            return None, temp_paths

        return play_path, temp_paths

    def _interrupted(self) -> bool:
        shutdown = getattr(self, "_shutdown_event", None)
        interrupted = getattr(self, "_interrupt_event", None)
        return bool(
            (shutdown is not None and shutdown.is_set())
            or (interrupted is not None and interrupted.is_set())
        )

    def _ensure_interrupt_epoch_lock(self) -> threading.Lock:
        """兼容少量用 ``object.__new__`` 构造的旧测试替身。"""
        lock = getattr(self, "_interrupt_epoch_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._interrupt_epoch_lock = lock
            self._interrupt_epoch = int(getattr(self, "_interrupt_epoch", 0))
        return lock

    def _interrupt_epoch_snapshot(self) -> int:
        lock = self._ensure_interrupt_epoch_lock()
        with lock:
            return int(getattr(self, "_interrupt_epoch", 0))

    def _signal_interrupt(self) -> None:
        interrupt_event = getattr(self, "_interrupt_event", None)
        if interrupt_event is None:
            interrupt_event = threading.Event()
            self._interrupt_event = interrupt_event
        lock = self._ensure_interrupt_epoch_lock()
        with lock:
            self._interrupt_epoch += 1
            interrupt_event.set()

    def interrupt(self) -> None:
        """用户插话：停止当前播放/合成，实例仍可服务下一轮。"""
        self._signal_interrupt()
        self._player.stop_current()
        if self._pyttsx3 is not None:
            try:
                self._pyttsx3.stop()
            except Exception:
                pass
        # 活跃的跨进程推理必须终止才能及时打断；空闲 Worker 保留模型，
        # 避免每次插话后的下一轮重新加载。
        from src.voice.rvc.infer import interrupt_rvc_worker

        interrupt_rvc_worker()

    async def _edge_tts_to_mp3(self, text: str) -> str:
        """调用 edge-tts 将文本保存为临时 mp3。Returns: mp3 文件路径。"""
        import edge_tts

        voice = detect_edge_voice(text, self.voice)
        mp3_path = create_app_temp_file(".mp3")
        await edge_tts.Communicate(text, voice).save(mp3_path)
        return mp3_path

    @staticmethod
    def _mp3_to_wav(mp3_path: str, rvc_config: Optional[dict] = None) -> str:
        """MP3 转 WAV；启用 RVC 时重采样到配置采样率（默认 40000Hz）。"""
        if not mp3_path.endswith(".mp3"):
            return mp3_path
        wav_path = mp3_path.replace(".mp3", ".wav")
        mp3_to_rvc_wav(mp3_path, wav_path, rvc_config)
        return wav_path

    def apply_rvc_model(self, input_audio: str) -> Optional[str]:
        """
        对已有音频文件执行 RVC 转换。

        Args:
            input_audio: 输入音频路径。

        Returns:
            成功时返回输出路径；未配置 RVC 或失败时返回 ``None``。
        """
        if self._rvc:
            return self._rvc.convert_file(input_audio)
        return None

    def shutdown(self) -> None:
        """停止新合成、当前 pygame 播放与 RVC Worker；回收自有 TempFileManager。可重复调用。"""
        self._shutdown_event.set()
        self._signal_interrupt()
        self._player.stop()
        from src.voice.rvc.infer import stop_rvc_worker

        stop_rvc_worker()
        owns = getattr(self, "_owns_temp_files", True)
        done = getattr(self, "_temp_files_shutdown_done", False)
        temp_files = getattr(self, "_temp_files", None)
        if owns and not done and temp_files is not None:
            self._temp_files_shutdown_done = True
            temp_files.shutdown()

    def save_to_file(self, text: str, filename: str) -> None:
        """
        将文本合成为音频并保存到指定文件。

        Args:
            text: 待合成文本。
            filename: 输出文件路径（格式由引擎决定，edge-tts 通常为 mp3）。
        """
        try:
            if self.engine_type == "pyttsx3":
                if not self._pyttsx3 and not self._init_pyttsx3():
                    return
                self._pyttsx3.save_to_file(text, filename)
                self._pyttsx3.runAndWait()
            else:
                async def _save():
                    import edge_tts

                    voice = detect_edge_voice(text, self.voice)
                    await edge_tts.Communicate(text, voice).save(filename)

                asyncio.run(_save())
        except Exception as exc:
            self.logger.error("保存 TTS 文件失败: %s", exc)
