"""
TTS 引擎门面：edge-tts + RVC 为主，pyttsx3 为离线应急备用

合成流程：文本清理 → edge-tts → MP3 转 WAV → 可选 RVC → 本地播放。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Optional, Set, Tuple

from src.voice.cosyvoice.client import CosyVoiceShutdownStatus

from src.core.log_privacy import exception_log_label
from src.core.safety import Operation, SafetyPolicy
from src.core.shutdown_status import (
    AGGREGATE_SHUTDOWN_STATUS_FIELDS,
    TtsShutdownStatus,
    engine_declares_aggregate_shutdown_status,
    shutdown_completion_flag,
    verify_aggregate_shutdown_status,
)
from src.voice.audio.player import AudioPlayer
from src.voice.audio.preparation_rollback import request_owned_paths_rollback
from src.voice.audio.temp_files import TempFileManager
from src.voice.language import detect_edge_voice, sanitize_for_speech, split_for_speech
from src.voice.prosody import EdgeProsodySettings
from src.voice.rvc.audio_prep import mp3_to_rvc_wav, warm_audio_preprocessing
from src.voice.rvc.converter import RVCConverter
from src.core.rvc_prepare_metadata import RVC_PREPARE_USER_WARNING, RvcPrepareState
from src.voice.timing import StageTimer, log_elapsed
from src.voice.tts.turn_stream_prefetch import (
    TurnStreamPrefetchSession,
    cosy_max_held_wav,
    edge_max_held_paths,
)

# 已废弃的 engine 值，启动时自动映射为 edge-tts
_LEGACY_ENGINES = frozenset({"gtts", "rvc"})


class _MergedLocalTtsCancel:
    """合并外部 cancel 与引擎 interrupt/shutdown；只读 is_set，不可 clear。"""

    __slots__ = ("_engine", "_external")

    def __init__(
        self,
        external: Optional[threading.Event],
        engine: "TTSEngine",
    ) -> None:
        self._external = external
        self._engine = engine

    def is_set(self) -> bool:
        ext = self._external
        if ext is not None and ext.is_set():
            return True
        shutdown = getattr(self._engine, "_shutdown_event", None)
        if shutdown is not None and shutdown.is_set():
            return True
        interrupted = getattr(self._engine, "_interrupt_event", None)
        return interrupted is not None and interrupted.is_set()


class TTSEngine:
    """文字转语音：默认 edge-tts（可选 RVC 后处理），失败时可回退 pyttsx3"""

    supports_turn_stream: bool = True

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
        warm_rvc_on_init: bool = True,
        edge_prosody: Optional[dict] = None,
        cosyvoice_config: Optional[dict] = None,
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
            warm_rvc_on_init: 为 ``False`` 时跳过构造期 RVC 健康检查/Worker 预热，
                由资源协调器稍后调用 ``prepare_rvc``（按模式串行，避免与 Ollama 抢 GPU）。
            edge_prosody: ``voice.tts.edge_prosody`` 映射；``None`` 或缺省为中性
                （enabled=false）。非法值在任何网络/音频/RVC 副作用前抛出。
            cosyvoice_config: ``voice.tts.cosyvoice`` 或等价映射；仅 ``engine=cosyvoice`` 使用。
        """
        # 先于任何音频/RVC 副作用校验句级韵律配置
        self._edge_prosody = EdgeProsodySettings.from_mapping(edge_prosody)

        if engine in _LEGACY_ENGINES:
            logging.getLogger(__name__).warning(
                "voice.tts.engine=%s 已废弃，已自动改用 edge-tts", engine
            )
            engine = "edge-tts"

        from src.voice.tts.cosyvoice_backend import (
            CosyVoiceTtsBackend,
            resolve_cosyvoice_settings_from_engine_config,
        )

        _cosy_settings = None
        if engine == "cosyvoice":
            _cosy_settings = resolve_cosyvoice_settings_from_engine_config(
                cosyvoice_config
            )
        elif engine not in ("edge-tts", "pyttsx3"):
            raise ValueError(
                f"不支持的 TTS 引擎: {engine}（可选: edge-tts, pyttsx3, cosyvoice）"
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

        if engine == "cosyvoice":
            self.safety_policy.require(
                Operation.LOCAL_TTS_WORKER, "TTSEngine cosyvoice"
            )
            self.safety_policy.assert_local_tts_launch_assets()

        self._pyttsx3 = None
        self._player = AudioPlayer(volume=volume, safety_policy=self.safety_policy)
        self._owns_temp_files = temp_file_manager is None
        if temp_file_manager is None:
            from src.core.paths import RUNTIME_DATA_DIRS

            for directory in RUNTIME_DATA_DIRS:
                self.safety_policy.register_file_delete_dir(directory)
        self._temp_files = temp_file_manager or TempFileManager(
            safety_policy=self.safety_policy
        )
        self._temp_files_shutdown_done = False
        self._rvc = (
            RVCConverter(
                self._rvc_config,
                safety_policy=self.safety_policy,
                temp_file_manager=self._temp_files,
            )
            if self._rvc_config.get("model_path")
            else None
        )
        self._rvc_ready = False
        self._rvc_last_error: Optional[str] = None
        self._rvc_prepare_done = False
        self._rvc_prepare_inflight = False
        self._rvc_prepare_attempt_completed = False
        self._speak_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._interrupt_event = threading.Event()
        self._interrupt_epoch_lock = threading.Lock()
        self._interrupt_epoch = 0
        self._cosy_backend: Optional[CosyVoiceTtsBackend] = None
        self._cosy_local_shutdown_status: Optional[CosyVoiceShutdownStatus] = None
        self._tts_shutdown_status: Optional[TtsShutdownStatus] = None
        self._turn_stream_session: Optional[TurnStreamPrefetchSession] = None
        self._turn_stream_session_lock = threading.Lock()
        if engine == "pyttsx3":
            self.supports_turn_stream = False
        elif engine == "cosyvoice":
            self.supports_turn_stream = True
        else:
            self.supports_turn_stream = True

        if engine == "cosyvoice":
            assert _cosy_settings is not None
            try:
                self._cosy_backend = CosyVoiceTtsBackend(
                    _cosy_settings,
                    self.safety_policy,
                    self._temp_files,
                )
            except Exception:
                if self._owns_temp_files:
                    try:
                        self._temp_files.shutdown()
                    except Exception:
                        self.logger.debug(
                            "CosyVoice 后端构造失败后的 TFM 回收跳过",
                            exc_info=True,
                        )
                raise
        else:
            if warm_rvc_on_init:
                self.prepare_rvc()
            self._initialize_engine()

    def get_rvc_prepare_state(self) -> RvcPrepareState:
        enabled = bool(self._rvc and self._rvc.enabled)
        attempt_completed = bool(getattr(self, "_rvc_prepare_attempt_completed", False))
        inflight = bool(getattr(self, "_rvc_prepare_inflight", False))
        warning = ""
        if enabled and attempt_completed and not self._rvc_ready:
            warning = RVC_PREPARE_USER_WARNING
        return RvcPrepareState(
            enabled=enabled,
            inflight=inflight,
            attempt_completed=attempt_completed,
            ready=bool(self._rvc_ready),
            user_warning=warning,
        )

    def prepare_rvc(
        self,
        *,
        cancel_event: Optional[threading.Event] = None,
        explicit_retry: bool = False,
    ) -> bool:
        """
        执行 RVC 健康检查与可选 Worker 预热（幂等）。

        装配期可设 ``warm_rvc_on_init=False``，由资源协调器在 Ollama 预热之后调用，
        避免默认并行抢占 GPU。
        """
        merged = _MergedLocalTtsCancel(cancel_event, self)
        if merged.is_set():
            return False
        if getattr(self, "_rvc_prepare_attempt_completed", False):
            return self._rvc_ready
        if getattr(self, "_rvc_prepare_inflight", False) and not explicit_retry:
            return self._rvc_ready
        if not getattr(self, "_rvc_prepare_inflight", False):
            self._rvc_prepare_inflight = True
        try:
            if merged.is_set():
                return False
            if not (self._rvc and self._rvc.enabled):
                self._rvc_ready = False
                return False

            from src.voice.rvc.health_check import check_rvc_health

            use_worker = bool(self._rvc_config.get("worker_enabled", True))
            report = check_rvc_health(
                self._rvc_config,
                safety_policy=self.safety_policy,
                probe_import=not use_worker,
            )
            if merged.is_set():
                return False
            if not report.ok:
                self._rvc_ready = False
                self._rvc_last_error = report.summary()
                self.logger.error(
                    "RVC 健康检查未通过 errors=%s warnings=%s "
                    "has_model=%s has_index=%s has_python=%s",
                    len(report.errors),
                    len(report.warnings),
                    bool(report.model_path),
                    bool(report.index_path),
                    bool(report.python_path),
                )
                return False

            self.logger.info("RVC Nahida 模型与环境检查通过")
            if self._rvc_config.get("prewarm_audio_processing", False):
                try:
                    with log_elapsed("MP3→WAV 重采样预热"):
                        warm_audio_preprocessing(
                            int(self._rvc_config.get("input_sample_rate", 40000))
                        )
                except Exception as exc:
                    self.logger.warning(
                        "音频重采样预热失败，真实请求仍可继续 err=%s",
                        exception_log_label(exc),
                    )
            if merged.is_set():
                return False
            if use_worker:
                from src.core.host_memory import (
                    ModelStartupMemoryError,
                    RVC_DISABLED_MEMORY_MESSAGE,
                    STARTUP_MEMORY_USER_HINT,
                )
                from src.voice.rvc.infer import warm_rvc_worker

                self._rvc_ready = False
                try:
                    worker_ok = warm_rvc_worker(
                        self._rvc_config,
                        safety_policy=self.safety_policy,
                        temp_file_manager=self._temp_files,
                        cancel_event=merged,
                        for_explicit_retry=explicit_retry,
                    )
                except ModelStartupMemoryError as exc:
                    self._rvc_ready = False
                    self._rvc_last_error = (
                        f"{RVC_DISABLED_MEMORY_MESSAGE} reason={exc.reason}。"
                        f"{STARTUP_MEMORY_USER_HINT}"
                    )
                    self.logger.error(
                        "RVC 因主机内存准入拒绝未启用 reason=%s",
                        exc.reason,
                    )
                    return False
                if merged.is_set():
                    self._rvc_ready = False
                    return False
                self._rvc_ready = bool(worker_ok)
            else:
                self._rvc_ready = True
            if merged.is_set():
                self._rvc_ready = False
                return False
            return self._rvc_ready
        finally:
            self._rvc_prepare_inflight = False
            self._rvc_prepare_attempt_completed = True
            self._rvc_prepare_done = True

    def retry_rvc_preparation(
        self,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        """
        显式重试失败的 RVC 准备：同 policy/config 下 idle 退休 Worker 后再 ``prepare_rvc``。

        与 speak/save 互斥（``speak_lock`` 非阻塞）；closing/inflight/非失败态不重试。
        """
        if self._shutdown_event.is_set():
            return False
        state = self.get_rvc_prepare_state()
        if state.inflight:
            return False
        if not (
            state.enabled
            and state.attempt_completed
            and not state.ready
        ):
            return False
        if not self._speak_lock.acquire(blocking=False):
            return False
        try:
            if self._shutdown_event.is_set():
                return False
            request_epoch = self._interrupt_epoch_snapshot()
            epoch_lock = self._ensure_interrupt_epoch_lock()
            with epoch_lock:
                if request_epoch != self._interrupt_epoch:
                    return False
                self._interrupt_event.clear()
            merged = _MergedLocalTtsCancel(cancel_event, self)
            self._rvc_prepare_inflight = True
            try:
                if merged.is_set():
                    return False
                from src.voice.rvc.infer import retire_rvc_worker_for_retry

                if not retire_rvc_worker_for_retry(
                    self._rvc_config,
                    self.safety_policy,
                ):
                    return False
                if merged.is_set():
                    return False
                self._rvc_prepare_attempt_completed = False
                self._rvc_prepare_done = False
                self._rvc_ready = False
                ok = self.prepare_rvc(cancel_event=merged, explicit_retry=True)
                if not ok:
                    self._rvc_prepare_attempt_completed = True
                    self._rvc_ready = False
                return ok
            finally:
                self._rvc_prepare_inflight = False
        finally:
            self._speak_lock.release()

    def prepare_local_tts(
        self,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        """
        显式加载 CosyVoice 本地 worker（零 Edge/RVC/播放副作用）。

        非 ``cosyvoice`` 引擎恒为 ``False`` 且不触碰本地 client。与 speak/save 互斥：
        ``speak_lock`` 忙时立即拒绝，不等待。
        """
        if self.engine_type != "cosyvoice":
            return False
        if self._shutdown_event.is_set():
            return False
        backend = getattr(self, "_cosy_backend", None)
        if backend is None:
            return False
        request_epoch = self._interrupt_epoch_snapshot()
        if not self._speak_lock.acquire(blocking=False):
            return False
        try:
            if self._shutdown_event.is_set():
                return False
            epoch_lock = self._ensure_interrupt_epoch_lock()
            with epoch_lock:
                if request_epoch != self._interrupt_epoch:
                    return False
                self._interrupt_event.clear()
            merged = _MergedLocalTtsCancel(cancel_event, self)
            started = time.monotonic()
            ok = backend.prepare(cancel_event=merged)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            self.logger.info(
                "CosyVoice 显式 prepare 完成 ok=%s elapsed_ms=%.3f",
                ok,
                elapsed_ms,
            )
            return ok
        finally:
            self._speak_lock.release()

    def _cloud_tts_allowed(self) -> bool:
        policy = getattr(self, "safety_policy", None)
        if policy is None:
            return False
        return bool(policy.is_allowed(Operation.CLOUD_TTS))

    def _require_cloud_tts(self, context: str = "edge-tts") -> None:
        policy = getattr(self, "safety_policy", None)
        if policy is None:
            policy = SafetyPolicy()
            self.safety_policy = policy
        policy.require(Operation.CLOUD_TTS, context)

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
            self.logger.error(
                "edge-tts 未安装 err=%s", exception_log_label(exc)
            )
            self._activate_fallback_on_init_failure()
        except Exception as exc:
            self.logger.error(
                "edge-tts 初始化失败 err=%s", exception_log_label(exc)
            )
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
            self.logger.error(
                "pyttsx3 初始化失败 err=%s", exception_log_label(exc)
            )
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
            self.logger.debug("朗读投影后无可播内容，跳过 TTS")
            return

        with self._speak_lock:
            if self._shutdown_event.is_set():
                return
            if not self._admit_turn_stream_work(request_epoch):
                return
            try:
                if self.engine_type == "pyttsx3":
                    self._speak_pyttsx3(text, async_mode)
                    return
                if self.engine_type == "cosyvoice":
                    self._speak_cosyvoice(text)
                    return

                if not self._cloud_tts_allowed():
                    self.logger.warning(
                        "cloud_tts 未允许，拒绝调用 edge-tts"
                    )
                    if self.fallback_enabled and self._ensure_pyttsx3_fallback():
                        self.logger.warning("已回退应急引擎 pyttsx3（无云 TTS）")
                        self._speak_pyttsx3(text, async_mode)
                    return

                sentences = split_for_speech(text) if self.pipeline_enabled else [text]
                if len(sentences) > 1:
                    self.logger.info("[TTS] 分句流水线: %d 段", len(sentences))
                    self._run_turn_stream(
                        sentences,
                        cancel=_MergedLocalTtsCancel(None, self),
                        on_chunk_done=lambda: None,
                    )
                else:
                    self._speak_single(text)
            except PermissionError as exc:
                self.logger.warning(
                    "TTS 云出口被拒绝 err=%s", exception_log_label(exc)
                )
                if (
                    not self._interrupted()
                    and self.engine_type == "edge-tts"
                    and self.fallback_enabled
                ):
                    if self._ensure_pyttsx3_fallback():
                        self._speak_pyttsx3(text, async_mode)
            except Exception as exc:
                self.logger.error(
                    "TTS 朗读失败 err=%s",
                    exception_log_label(exc),
                    exc_info=True,
                )
                if (
                    not self._interrupted()
                    and self.engine_type == "edge-tts"
                    and self.fallback_enabled
                ):
                    self.logger.warning("尝试使用应急引擎 pyttsx3 朗读")
                    if self._ensure_pyttsx3_fallback():
                        self._speak_pyttsx3(text, async_mode)

    def speak_stream(
        self,
        chunks: Iterable[str],
        *,
        cancel_event: Optional[threading.Event] = None,
        on_chunk_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """同步消费一轮原句；整轮占一次 ``_speak_lock``。"""
        if self.supports_turn_stream is not True:
            raise NotImplementedError("此 TTS 引擎不支持 speak_stream")
        request_epoch = self._interrupt_epoch_snapshot()
        if self._shutdown_event.is_set():
            return
        with self._speak_lock:
            if self._shutdown_event.is_set():
                return
            if cancel_event is not None and cancel_event.is_set():
                return
            if not self._prepare_turn_stream_entry(
                request_epoch,
                raise_on_busy=True,
                external_cancel=cancel_event,
            ):
                return
            merged = _MergedLocalTtsCancel(cancel_event, self)
            self._run_turn_stream(
                chunks,
                cancel=merged,
                on_chunk_done=on_chunk_done or (lambda: None),
            )

    def _admit_turn_stream_work(self, request_epoch: int) -> bool:
        """speak 等兼容路径：资源忙/epoch 过期均静默拒绝（epoch 与 clear 同锁段）。"""
        return self._prepare_turn_stream_entry(
            request_epoch, raise_on_busy=False
        )

    def _prepare_turn_stream_entry(
        self,
        request_epoch: int,
        *,
        raise_on_busy: bool,
        external_cancel: Optional[threading.Event] = None,
    ) -> bool:
        """旧 session 回收（锁外）→ pending/Cosy 栅栏 → epoch 确认与 interrupt 清除（同锁段）。"""
        if external_cancel is not None and external_cancel.is_set():
            return False
        try:
            self._require_recycled_turn_stream_session()
        except RuntimeError as exc:
            if raise_on_busy:
                raise RuntimeError(str(exc)) from exc
            return False
        try:
            self._assert_turn_stream_admission_resources()
        except RuntimeError as exc:
            if raise_on_busy:
                raise
            return False
        epoch_lock = self._ensure_interrupt_epoch_lock()
        with epoch_lock:
            if request_epoch != self._interrupt_epoch:
                return False
            if external_cancel is not None and external_cancel.is_set():
                return False
            self._interrupt_event.clear()
        return True

    def _assert_turn_stream_admission_resources(self) -> None:
        """新 owner/prepare 前：TFM pending 与 Cosy retained 须已清空。"""
        _, _, _, tfm_pending = self._turn_stream_io_bindings()
        if tfm_pending():
            raise RuntimeError(
                "TempFileManager 仍有 pending 清理，拒绝 turn stream 音频工作"
            )
        backend = getattr(self, "_cosy_backend", None)
        if backend is not None:
            reconcile = getattr(backend, "reconcile_released_outputs", None)
            if callable(reconcile):
                reconcile()
        retained = (
            getattr(backend, "retained_output_count", None)
            if backend is not None
            else None
        )
        if callable(retained) and retained() > 0:
            raise RuntimeError(
                "CosyVoice 仍有 retained 输出，拒绝 turn stream 音频工作"
            )

    def _turn_stream_io_bindings(
        self,
    ) -> Tuple[
        Callable[[str], None],
        Callable[[str], None],
        Callable[[str], bool],
        Callable[[], bool],
    ]:
        tfm = self._temp_files
        return (
            self._player.play,
            tfm.remove,
            tfm.is_owned,
            lambda: tfm.pending_cleanup_count() > 0,
        )

    def _turn_stream_prefetch_enabled(self) -> bool:
        return bool(self.pipeline_enabled)

    def _require_recycled_turn_stream_session(self) -> None:
        with self._turn_stream_session_lock:
            existing = self._turn_stream_session
        if existing is None:
            self._assert_turn_stream_admission_resources()
            return
        existing.cancel()
        _, _, _, tfm_pending = self._turn_stream_io_bindings()
        quiet = existing.finish(timeout=2.0, tfm_pending=tfm_pending)
        if not quiet or not existing.paths_released(tfm_pending):
            raise RuntimeError(
                "上一轮 turn stream 预取未 quiet 或路径未释放，拒绝新 session"
            )
        with self._turn_stream_session_lock:
            if self._turn_stream_session is existing:
                self._turn_stream_session = None

    def _begin_turn_stream_session(self) -> TurnStreamPrefetchSession:
        with self._turn_stream_session_lock:
            existing = self._turn_stream_session
            if existing is not None:
                _, _, _, tfm_pending = self._turn_stream_io_bindings()
                if not existing.snapshot().quiet or not existing.paths_released(
                    tfm_pending
                ):
                    raise RuntimeError(
                        "上一轮 turn stream 预取尚未 quiet/路径未释放，拒绝新 session"
                    )
                self._turn_stream_session = None
            max_paths = (
                cosy_max_held_wav()
                if self.engine_type == "cosyvoice"
                else edge_max_held_paths()
            )
            session = TurnStreamPrefetchSession(max_held_paths=max_paths)
            self._turn_stream_session = session
            return session

    def _end_turn_stream_session(self, session: TurnStreamPrefetchSession) -> None:
        _, _, _, tfm_pending = self._turn_stream_io_bindings()
        quiet = session.finish(timeout=2.0, tfm_pending=tfm_pending)
        released = session.paths_released(tfm_pending)
        with self._turn_stream_session_lock:
            if self._turn_stream_session is session and quiet and released:
                self._turn_stream_session = None
        if not quiet or not released:
            self.logger.warning(
                "Turn stream 预取 session 未 quiet 或路径未释放；保留 owner 供 shutdown 复核"
            )

    def _shutdown_turn_stream_session(self) -> bool:
        with self._turn_stream_session_lock:
            session = self._turn_stream_session
        if session is None:
            return True
        session.cancel()
        _, _, _, tfm_pending = self._turn_stream_io_bindings()
        quiet = session.finish(timeout=2.0, tfm_pending=tfm_pending)
        released = session.paths_released(tfm_pending)
        ok = quiet and released
        with self._turn_stream_session_lock:
            if ok and self._turn_stream_session is session:
                self._turn_stream_session = None
        return ok

    def _run_turn_stream(
        self,
        chunks: Iterable[str],
        *,
        cancel: _MergedLocalTtsCancel,
        on_chunk_done: Callable[[], None],
    ) -> None:
        session = self._begin_turn_stream_session()
        prefetch = self._turn_stream_prefetch_enabled()
        try:
            if self.engine_type == "cosyvoice":
                self._run_turn_stream_cosyvoice(
                    chunks,
                    cancel=cancel,
                    on_chunk_done=on_chunk_done,
                    session=session,
                    prefetch=prefetch,
                )
            elif self.engine_type == "pyttsx3":
                raise NotImplementedError("pyttsx3 不支持 speak_stream")
            else:
                self._run_turn_stream_edge(
                    chunks,
                    cancel=cancel,
                    on_chunk_done=on_chunk_done,
                    session=session,
                    prefetch=prefetch,
                )
        finally:
            self._end_turn_stream_session(session)

    def _run_turn_stream_edge(
        self,
        chunks: Iterable[str],
        *,
        cancel: _MergedLocalTtsCancel,
        on_chunk_done: Callable[[], None],
        session: TurnStreamPrefetchSession,
        prefetch: bool,
    ) -> None:
        if not self._cloud_tts_allowed():
            raise PermissionError("cloud_tts 未允许")

        def plan(chunk: str) -> List[str]:
            cleaned = sanitize_for_speech(chunk)
            if not cleaned:
                return []
            if self.pipeline_enabled:
                return split_for_speech(cleaned)
            return [cleaned]

        prep_cancel = session.prepare_cancel_view(cancel)

        def prepare(text: str) -> Tuple[Optional[str], Set[str]]:
            if prep_cancel.is_set():
                return None, set()
            return self._prepare_playable_audio(text, stream_cancel=prep_cancel)

        play_fn, cleanup_one, is_owned, tfm_pending = self._turn_stream_io_bindings()
        session.bind_prepare(prepare)
        session.run(
            chunks,
            cancel=cancel,
            prefetch=prefetch,
            plan_segments=plan,
            play=play_fn,
            cleanup_path=cleanup_one,
            is_owned=is_owned,
            interrupted=self._interrupted,
            on_chunk_done=on_chunk_done,
            tfm_pending=tfm_pending,
        )

    def _run_turn_stream_cosyvoice(
        self,
        chunks: Iterable[str],
        *,
        cancel: _MergedLocalTtsCancel,
        on_chunk_done: Callable[[], None],
        session: TurnStreamPrefetchSession,
        prefetch: bool,
    ) -> None:
        from src.voice.tts.cosyvoice_backend import plan_cosyvoice_segments

        backend = getattr(self, "_cosy_backend", None)
        if backend is None:
            raise RuntimeError("CosyVoice 后端未初始化")
        if not backend._enter_call():  # noqa: SLF001
            return
        try:

            def plan(chunk: str) -> List[str]:
                cleaned = sanitize_for_speech(chunk)
                if not cleaned:
                    return []
                return plan_cosyvoice_segments(cleaned, backend._settings)  # noqa: SLF001

            play_fn, cleanup_one, is_owned, tfm_pending = self._turn_stream_io_bindings()
            prep_cancel = session.prepare_cancel_view(cancel)

            def prepare(text: str) -> Tuple[Optional[str], Set[str]]:
                if prep_cancel.is_set():
                    return None, set()
                playable = backend.prepare_playable(
                    text,
                    rvc=self._rvc,
                    rvc_ready=self._rvc_ready,
                    cancel_event=prep_cancel,
                    interrupted=prep_cancel.is_set,
                )
                temp_paths = set(playable.temp_paths)
                return playable.play_path, temp_paths

            session.bind_prepare(prepare)
            session.run(
                chunks,
                cancel=cancel,
                prefetch=prefetch,
                plan_segments=plan,
                play=play_fn,
                cleanup_path=cleanup_one,
                is_owned=is_owned,
                interrupted=self._interrupted,
                on_chunk_done=on_chunk_done,
                tfm_pending=tfm_pending,
            )
        finally:
            backend._exit_call()  # noqa: SLF001

    def _speak_cosyvoice(self, text: str) -> None:
        """本地 CosyVoice 顺序分句播报；失败不自动回退 Edge/pyttsx3。"""
        backend = getattr(self, "_cosy_backend", None)
        if backend is None:
            self.logger.error("CosyVoice 后端未初始化")
            return
        try:
            backend.speak_sequential(
                text,
                play_fn=self._player.play,
                rvc=self._rvc,
                rvc_ready=self._rvc_ready,
                cancel_event=self._interrupt_event,
                interrupted=self._interrupted,
            )
        except Exception as exc:
            self.logger.error(
                "CosyVoice 朗读失败 err=%s",
                exception_log_label(exc),
                exc_info=True,
            )

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
        play_error: Optional[BaseException] = None
        try:
            if not play_path:
                timer.finish()
                return
            with log_elapsed("播放"):
                self._player.play(play_path)
            timer.mark("播放完成")
            timer.finish()
        except BaseException as exc:
            play_error = exc
            raise
        finally:
            try:
                self._temp_files.remove_many(*temp_paths)
            except Exception:
                self.logger.debug("单段临时文件清理失败", exc_info=True)
                if play_error is None:
                    raise

    def _speak_pipeline(self, sentences: List[str]) -> None:
        """分句流水线：有界 turn stream（替换旧无界 Queue / 600s join）。"""
        self._run_turn_stream(
            sentences,
            cancel=_MergedLocalTtsCancel(None, self),
            on_chunk_done=lambda: None,
        )

    def _prepare_playable_audio(
        self,
        text: str,
        timer: Optional[StageTimer] = None,
        label: str = "",
        *,
        stream_cancel: Optional[object] = None,
    ) -> Tuple[Optional[str], Set[str]]:
        """
        生成可播放音频路径并登记待删临时文件。

        Returns:
            ``(播放路径, 临时文件路径集合)``；合成失败时播放路径为 ``None``。
        """
        temp_paths: Set[str] = set()
        prefix = f"{label} " if label else ""

        def _cancelled() -> bool:
            sc = stream_cancel
            if sc is not None and getattr(sc, "is_set", None) and sc.is_set():
                return True
            return self._interrupted()

        try:
            if _cancelled():
                return None, temp_paths

            with log_elapsed(f"{prefix}edge-tts 生成"):
                mp3_path = asyncio.run(self._edge_tts_to_mp3(text))
            temp_paths.add(mp3_path)
            if _cancelled():
                return None, temp_paths
            if timer:
                timer.mark(f"{prefix}edge-tts")

            with log_elapsed(f"{prefix}MP3→WAV"):
                wav_path = self._mp3_to_wav(mp3_path, self._rvc_config)
            if wav_path != mp3_path:
                temp_paths.add(wav_path)
            if timer:
                timer.mark(f"{prefix}转WAV")
            if _cancelled():
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

            if _cancelled():
                return None, temp_paths

            return play_path, temp_paths
        except BaseException as exc:
            if temp_paths:
                request_owned_paths_rollback(
                    self._temp_files, *temp_paths, primary=exc
                )
            raise

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
        backend = getattr(self, "_cosy_backend", None)
        if backend is not None:
            backend.interrupt()

    def local_cosyvoice_shutdown_status(self) -> Optional[CosyVoiceShutdownStatus]:
        """只读观察本地 CosyVoice worker 关闭状态（供 C2 资源协调）。"""
        return self._cosy_local_shutdown_status

    def local_cosyvoice_worker_playback_ready(self) -> bool:
        """只读：Cosy worker 是否仍可播报（idle 退出后为 False，不启动子进程）。"""
        if self.engine_type != "cosyvoice":
            return True
        backend = getattr(self, "_cosy_backend", None)
        if backend is None:
            return False
        client = getattr(backend, "client", None)
        if client is None:
            return False
        reader = getattr(client, "worker_playback_ready_readonly", None)
        if not callable(reader):
            return False
        return bool(reader())

    def get_shutdown_status(self) -> TtsShutdownStatus:
        """只读聚合关闭状态；未 shutdown 或本轮未完成时 ``complete=False``。"""
        status = self._tts_shutdown_status
        if status is None:
            return TtsShutdownStatus(
                complete=False,
                local_tts_complete=False,
                persistent_rvc_complete=False,
                synthesis_idle=False,
                temp_files_complete=False,
                rvc_subprocess_complete=False,
            )
        return status

    def _synthesis_idle_snapshot(self) -> bool:
        """进程内合成/本地在途调用是否已退出；不等待 ``speak_lock``。"""
        with self._turn_stream_session_lock:
            session = self._turn_stream_session
        if session is not None:
            _, _, _, tfm_pending = self._turn_stream_io_bindings()
            if not session.snapshot().quiet or not session.paths_released(
                tfm_pending
            ):
                return False
        if not self._speak_lock.acquire(blocking=False):
            return False
        self._speak_lock.release()
        backend = getattr(self, "_cosy_backend", None)
        inflight = getattr(backend, "inflight_calls", None)
        if callable(inflight) and inflight() != 0:
            return False
        return True

    def _publish_shutdown_status(
        self,
        *,
        local_tts_complete: object,
        persistent_rvc_complete: object,
        rvc_subprocess_complete: object,
        synthesis_idle: object,
        temp_files_complete: object,
    ) -> None:
        local = shutdown_completion_flag(local_tts_complete)
        persistent = shutdown_completion_flag(persistent_rvc_complete)
        subprocess_rvc = shutdown_completion_flag(rvc_subprocess_complete)
        synthesis = shutdown_completion_flag(synthesis_idle)
        temp_files = shutdown_completion_flag(temp_files_complete)
        complete = (
            local
            and persistent
            and subprocess_rvc
            and synthesis
            and temp_files
        )
        self._tts_shutdown_status = TtsShutdownStatus(
            complete=complete,
            local_tts_complete=local,
            persistent_rvc_complete=persistent,
            rvc_subprocess_complete=subprocess_rvc,
            synthesis_idle=synthesis,
            temp_files_complete=temp_files,
        )

    def _make_edge_communicate(self, text: str, voice: str):
        """
        构造 ``edge_tts.Communicate``；句级 rate/pitch/volume 由本地 prosody 派生。

        不修改正文，不生成 SSML。
        """
        import edge_tts

        prosody = self._edge_prosody.resolve_for_sentence(text)
        return edge_tts.Communicate(
            text,
            voice,
            rate=prosody.rate,
            pitch=prosody.pitch,
            volume=prosody.volume,
        )

    async def _edge_tts_to_mp3(self, text: str) -> str:
        """调用 edge-tts 将文本保存为临时 mp3。Returns: mp3 文件路径。"""
        self._require_cloud_tts("edge-tts Communicate")
        voice = detect_edge_voice(text, self.voice)
        mp3_path = self._temp_files.create(".mp3")
        try:
            await self._make_edge_communicate(text, voice).save(mp3_path)
            return mp3_path
        except BaseException as exc:
            request_owned_paths_rollback(self._temp_files, mp3_path, primary=exc)
            raise

    def _mp3_to_wav(self, mp3_path: str, rvc_config: Optional[dict] = None) -> str:
        """MP3 转 WAV；启用 RVC 时重采样到配置采样率（默认 40000Hz）。"""
        if not mp3_path.endswith(".mp3"):
            return mp3_path
        wav_path = self._temp_files.create(".wav")
        try:
            mp3_to_rvc_wav(mp3_path, wav_path, rvc_config)
            return wav_path
        except BaseException as exc:
            request_owned_paths_rollback(self._temp_files, wav_path, primary=exc)
            raise

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

    def shutdown(self, *, stop_rvc: bool = True) -> None:
        """
        停止新合成与当前播放；回收自有 TempFileManager。可重复调用。

        ``stop_rvc=True``（默认）：本实例拥有可能已预热的全局 Worker 时停止它。
        ``stop_rvc=False``：仅当丢弃重复实例且正式 TTS 仍保留 Worker 时使用。
        自有 manager 仅在返回 ``complete=True`` 时标记完成；incomplete 可重试。
        """
        self._shutdown_event.set()
        self._publish_shutdown_status(
            local_tts_complete=False,
            persistent_rvc_complete=False,
            rvc_subprocess_complete=False,
            synthesis_idle=False,
            temp_files_complete=False,
        )
        self._signal_interrupt()
        self._player.stop()
        stream_session_quiet = self._shutdown_turn_stream_session()
        cosy_shutdown_complete = True
        backend = getattr(self, "_cosy_backend", None)
        if backend is not None:
            self._cosy_local_shutdown_status = backend.shutdown_local()
            cosy_shutdown_complete = shutdown_completion_flag(
                getattr(self._cosy_local_shutdown_status, "complete", None)
            )
        local_tts_complete = cosy_shutdown_complete
        persistent_rvc_complete = True
        if stop_rvc:
            from src.voice.rvc.infer import stop_rvc_worker

            persistent_rvc_complete = stop_rvc_worker() is True
        rvc_subprocess_complete = True
        policy = getattr(self, "safety_policy", None)
        if stop_rvc and policy is not None:
            rvc_subprocess_complete = policy.retire_rvc_runs(timeout=2.0) is True
            if rvc_subprocess_complete and not policy.rvc_runs_quiescent():
                rvc_subprocess_complete = False
        synthesis_idle = self._synthesis_idle_snapshot()
        if not stream_session_quiet:
            synthesis_idle = False
        owns = getattr(self, "_owns_temp_files", True)
        done = getattr(self, "_temp_files_shutdown_done", False)
        temp_files = getattr(self, "_temp_files", None)
        writers_ready = (
            local_tts_complete
            and persistent_rvc_complete
            and rvc_subprocess_complete
            and synthesis_idle
        )
        if (
            owns
            and not done
            and temp_files is not None
            and writers_ready
        ):
            try:
                status = temp_files.shutdown()
                if shutdown_completion_flag(getattr(status, "complete", None)):
                    self._temp_files_shutdown_done = True
                else:
                    self.logger.warning(
                        "TTS 自有 TempFileManager 关闭未完成 "
                        "owned=%s pending=%s inflight=%s worker_alive=%s（可再次 shutdown 重试）",
                        getattr(status, "owned", "?"),
                        getattr(status, "pending", "?"),
                        getattr(status, "inflight", "?"),
                        getattr(status, "worker_alive", "?"),
                    )
            except Exception:
                self.logger.error(
                    "TTS 自有 TempFileManager 关闭异常（不标记完成，可重试）",
                    exc_info=True,
                )
        elif owns and not done and temp_files is not None and not writers_ready:
            if not local_tts_complete:
                inflight = backend.inflight_calls() if backend is not None else 0
                retained = (
                    backend.retained_output_count() if backend is not None else 0
                )
                self.logger.warning(
                    "CosyVoice 本地关闭未完成，保留 TempFileManager 与 client 供重试 "
                    "inflight=%s retained=%s",
                    inflight,
                    retained,
                )
            elif not persistent_rvc_complete:
                self.logger.warning(
                    "持久 RVC Worker 关闭未完成，保留 TempFileManager 供重试"
                )
            elif not rvc_subprocess_complete:
                self.logger.warning(
                    "单次 RVC infer/probe 关闭未完成，保留 TempFileManager 供重试"
                )
            elif not synthesis_idle:
                self.logger.warning(
                    "合成或在途本地调用仍未结束，保留 TempFileManager 供重试"
                )
        if owns and not getattr(self, "_temp_files_shutdown_done", False):
            temp_files_complete = False
        else:
            temp_files_complete = True
        self._publish_shutdown_status(
            local_tts_complete=local_tts_complete,
            persistent_rvc_complete=persistent_rvc_complete,
            rvc_subprocess_complete=rvc_subprocess_complete,
            synthesis_idle=synthesis_idle,
            temp_files_complete=temp_files_complete,
        )

    def save_to_file(self, text: str, filename: str) -> None:
        """
        将文本合成为音频并保存到指定文件。

        Args:
            text: 待合成文本。
            filename: 输出文件路径（格式由引擎决定，edge-tts 通常为 mp3）。

        Raises:
            PermissionError: 云 TTS 被拒且无可用 fallback。
            Exception: edge/pyttsx3 合成失败（失败时尽量清理不完整目标文件后重新抛出）。
        """
        request_epoch = self._interrupt_epoch_snapshot()
        with self._speak_lock:
            if not self._prepare_turn_stream_entry(
                request_epoch, raise_on_busy=True
            ):
                if self._interrupted():
                    raise RuntimeError(
                        "save_to_file 已取消（interrupt epoch 已变更）"
                    )
                raise RuntimeError(
                    "turn stream epoch 已过期，拒绝 save_to_file"
                )
            self._save_to_file_locked(text, filename)

    def _save_to_file_locked(self, text: str, filename: str) -> None:
        if self.engine_type == "cosyvoice":
            self._save_to_file_cosyvoice(text, filename)
            return
        try:
            if self.engine_type == "pyttsx3":
                if not self._pyttsx3 and not self._init_pyttsx3():
                    raise RuntimeError("pyttsx3 不可用")
                self._pyttsx3.save_to_file(text, filename)
                self._pyttsx3.runAndWait()
            else:
                self._require_cloud_tts("edge-tts save_to_file")

                async def _save():
                    voice = detect_edge_voice(text, self.voice)
                    await self._make_edge_communicate(text, voice).save(filename)

                asyncio.run(_save())
        except PermissionError as exc:
            self.logger.warning(
                "保存 TTS 文件被拒绝 err=%s", exception_log_label(exc)
            )
            if self.engine_type == "cosyvoice":
                raise
            if self.fallback_enabled and self._ensure_pyttsx3_fallback():
                try:
                    self._pyttsx3.save_to_file(text, filename)
                    self._pyttsx3.runAndWait()
                    return
                except Exception as fallback_exc:
                    self.logger.error(
                        "保存 TTS fallback 失败 err=%s",
                        exception_log_label(fallback_exc),
                    )
                    self._cleanup_partial_save_target(filename)
                    raise
            self._cleanup_partial_save_target(filename)
            raise
        except Exception as exc:
            self.logger.error(
                "保存 TTS 文件失败 err=%s", exception_log_label(exc)
            )
            self._cleanup_partial_save_target(filename)
            raise

    def _save_to_file_cosyvoice(self, text: str, filename: str) -> None:
        """CosyVoice 保存：调用方已持 speak_lock 且完成 turn stream 准入。"""
        if self._shutdown_event.is_set():
            raise RuntimeError("TTS 已关闭")
        backend = getattr(self, "_cosy_backend", None)
        if backend is None:
            raise RuntimeError("CosyVoice 后端未初始化")
        cleaned = sanitize_for_speech(text)
        if not cleaned:
            raise ValueError("save_to_file 文本为空")
        backend.save_base_wav(
            cleaned,
            filename,
            cancel_event=self._interrupt_event,
        )

    def _cleanup_partial_save_target(self, filename: str) -> None:
        """edge/fallback 失败后尽量清理不完整目标；仅走白名单+所有权边界。"""
        if not filename:
            return
        path = Path(filename)
        try:
            if not path.is_file():
                return
            temp = getattr(self, "_temp_files", None)
            policy = getattr(self, "safety_policy", None)
            if temp is not None and temp.is_owned(filename):
                temp.remove(filename)
                return
            if policy is None or not policy.is_path_deletable(filename):
                return
            if temp is None:
                return
            try:
                key = temp.adopt_existing(filename)
            except (PermissionError, RuntimeError, OSError):
                return
            temp.remove(key)
        except Exception:
            self.logger.debug("部分保存目标清理跳过", exc_info=False)
