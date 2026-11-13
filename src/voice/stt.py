"""
语音转文字（STT）模块
支持多种STT引擎：speech_recognition（Google 在线）, whisper（本地）, paraformer（本地 worker）
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
from typing import Any, Optional

from src.voice.recognition import (
    RecognitionProvider,
    RecognitionResult,
    RecognitionStatus,
)


class _CombinedCancelView:
    """只读 OR：任一底层 Event 置位则 is_set 为 True。"""

    __slots__ = ("_events",)

    def __init__(self, *events: Optional[threading.Event]) -> None:
        self._events = tuple(e for e in events if e is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


class STTEngine:
    """STT引擎封装类"""

    def __init__(
        self,
        engine: str = "paraformer",
        language: str = "zh-CN",
        energy_threshold: int = 4000,
        *,
        provider: Optional[RecognitionProvider] = None,
        safety_policy: Optional[Any] = None,
        temp_file_manager: Optional[Any] = None,
        paraformer_settings: Optional[Any] = None,
        input_audio_settings: Optional[Any] = None,
    ):
        """
        初始化STT引擎

        Args:
            engine: STT引擎类型 ('speech_recognition', 'whisper', 'paraformer')
            language: 语言代码（Google 用 BCP-47；Whisper 取主语言如 zh）
            energy_threshold: 能量阈值（用于语音检测；校准后可能被覆盖）
            provider: 可选可注入 provider；传入时不初始化默认引擎或设备。
            safety_policy: 可选安全策略（与装配共享）；无 temp_file_manager 时用于创建管理器。
            temp_file_manager: 共享临时文件所有权管理器；Whisper 临时 WAV 走统一清理。
        """
        self.engine_type = engine
        self.language = language
        self.energy_threshold = energy_threshold
        self.logger = logging.getLogger(__name__)
        self._provider = provider
        self._recognizer = None
        self._model = None
        self.safety_policy = safety_policy
        self._owns_temp_files = temp_file_manager is None and safety_policy is not None
        self._temp_files = temp_file_manager
        self._paraformer_client = None
        self._shutdown_complete = False
        self._permanently_closed = False
        self._recording = False
        self._engine_lock = threading.Lock()
        self._shutdown_cancel = threading.Event()
        self._mic_device_count: Optional[int] = None
        self._mic_identity_logged = False
        self._input_audio_backend_logged = False
        self._silero_vad: Any = None
        self._silero_vad_lock = threading.Lock()
        self._silero_vad_creating = False
        self._silero_vad_retiring: Any = None
        self._silero_vad_close_target: Any = None
        self._continuous_capture_lock = threading.Lock()
        self._continuous_capture: Any = None
        self._continuous_capture_stop_failed = False
        self._continuous_capture_epoch = 0
        if input_audio_settings is None and provider is None and engine == "paraformer":
            from src.voice.input_audio import InputAudioSettings

            input_audio_settings = InputAudioSettings.from_mapping(None)
        if input_audio_settings is not None:
            from src.voice.input_audio import InputAudioSettings

            if not isinstance(input_audio_settings, InputAudioSettings):
                raise TypeError(
                    "input_audio_settings must be InputAudioSettings or None"
                )
        self._input_audio_settings = input_audio_settings
        if paraformer_settings is not None:
            self._paraformer_settings = paraformer_settings
        elif engine == "paraformer":
            from src.voice.paraformer.settings import ParaformerSettings

            self._paraformer_settings = ParaformerSettings()
        else:
            self._paraformer_settings = None
        if self._temp_files is None and safety_policy is not None:
            from src.voice.audio.temp_files import TempFileManager

            self._temp_files = TempFileManager(safety_policy=safety_policy)
        if provider is None:
            self._initialize_engine()

    def _cloud_stt_allowed(self) -> bool:
        from src.core.safety import Operation, SafetyPolicy

        policy = self.safety_policy
        if policy is None:
            # 无策略：与裸 SafetyPolicy() 一致，拒绝云 STT
            return False
        if isinstance(policy, SafetyPolicy):
            return policy.is_allowed(Operation.CLOUD_STT)
        is_allowed = getattr(policy, "is_allowed", None)
        if callable(is_allowed):
            return bool(is_allowed(Operation.CLOUD_STT))
        return False

    def _require_cloud_stt(self, context: str = "cloud_stt") -> None:
        from src.core.safety import Operation, SafetyPolicy

        policy = self.safety_policy
        if policy is None:
            policy = SafetyPolicy()
            self.safety_policy = policy
        policy.require(Operation.CLOUD_STT, context)

    def _initialize_engine(self) -> None:
        """按 ``engine_type`` 加载 speech_recognition 或 whisper 后端。"""
        try:
            if self.engine_type == "speech_recognition":
                import speech_recognition as sr

                inner = sr.Recognizer()
                self._recognizer = self._finalize_recognizer(inner)

            elif self.engine_type == "whisper":
                try:
                    import whisper

                    self._model = whisper.load_model("base")
                    # 录音仍依赖 speech_recognition 的 Microphone
                    import speech_recognition as sr

                    inner = sr.Recognizer()
                    self._recognizer = self._finalize_recognizer(inner)
                    self.logger.info("Whisper引擎初始化成功（本地识别）")
                except ImportError:
                    if not self._cloud_stt_allowed():
                        self.logger.warning(
                            "Whisper未安装，且 cloud_stt 未允许，不回退 Google；"
                            "安装: pip install openai-whisper 或在配置中开启 cloud_stt"
                        )
                        self._recognizer = None
                        self._model = None
                        return
                    self.logger.warning(
                        "Whisper未安装，cloud_stt 已允许，回退到 speech_recognition/Google；"
                        "安装: pip install openai-whisper"
                    )
                    import speech_recognition as sr

                    self.engine_type = "speech_recognition"
                    inner = sr.Recognizer()
                    self._recognizer = self._finalize_recognizer(inner)

            elif self.engine_type == "paraformer":
                import speech_recognition as sr

                inner = sr.Recognizer()
                self._recognizer = self._finalize_recognizer(inner)
                self.logger.info(
                    "Paraformer 引擎已登记（本地 worker；首次识别时冷启动）"
                )

        except ImportError as e:
            from src.core.log_privacy import exception_log_label

            self.logger.error(
                "STT引擎导入失败 err=%s", exception_log_label(e)
            )
            self._recognizer = None
        except Exception as e:
            from src.core.log_privacy import exception_log_label

            self.logger.error(
                "STT引擎初始化失败 err=%s", exception_log_label(e)
            )
            self._recognizer = None

    def _finalize_recognizer(self, inner: Any) -> Any:
        from src.voice.input_audio import InputAudioSettings, create_bounded_recognizer

        settings = self._input_audio_settings
        if (
            self.engine_type == "paraformer"
            and isinstance(settings, InputAudioSettings)
            and settings.enabled
        ):
            recognizer = create_bounded_recognizer(
                settings,
                initial_energy=self.energy_threshold,
            )
        else:
            recognizer = inner
        self._apply_recognizer_defaults(recognizer)
        if self.engine_type == "speech_recognition" and recognizer is inner:
            self.logger.info("speech_recognition引擎初始化成功（Google 在线识别）")
        return recognizer

    def _paraformer_input_guard_enabled(self) -> bool:
        from src.voice.input_audio import InputAudioSettings

        settings = self._input_audio_settings
        return (
            self.engine_type == "paraformer"
            and isinstance(settings, InputAudioSettings)
            and settings.enabled
        )

    def _calibration_threshold_floor(self) -> float:
        from src.voice.input_audio import InputAudioSettings

        settings = self._input_audio_settings
        if (
            self.engine_type == "paraformer"
            and isinstance(settings, InputAudioSettings)
            and settings.enabled
        ):
            return float(settings.energy_floor)
        return max(300.0, float(self.energy_threshold) * 0.5)

    def input_audio_readiness_detail(self) -> tuple[bool, str]:
        """Paraformer 麦克风声学 guard 就绪（不打开麦克风、不 spawn worker）。"""
        if self._provider is not None:
            return True, ""
        if not self._paraformer_input_guard_enabled():
            return True, ""
        from src.voice.input_audio import resolve_acoustic_input_readiness

        return resolve_acoustic_input_readiness(self._input_audio_settings)

    def _log_input_audio_backend_once(self) -> None:
        from src.voice.input_audio import (
            INPUT_AUDIO_BACKEND_SILERO,
            InputAudioSettings,
        )

        settings = self._input_audio_settings
        if not isinstance(settings, InputAudioSettings):
            return
        if not self._paraformer_input_guard_enabled():
            return
        if self._input_audio_backend_logged:
            return
        self._input_audio_backend_logged = True
        idx = settings.microphone_device_index
        mic_label = f"device_index={idx}" if idx is not None else "default"
        if settings.backend == INPUT_AUDIO_BACKEND_SILERO:
            from src.voice.silero_vad import EXPECTED_ASSET_SHA256

            self.logger.info(
                "STT 声学准入 backend=silero_onnx threshold=%.2f "
                "model=silero_vad.onnx sha256_prefix=%s mic=%s",
                settings.speech_probability_threshold,
                EXPECTED_ASSET_SHA256[:16],
                mic_label,
            )
        else:
            self.logger.info(
                "STT 声学准入 backend=webrtc vad_mode=%d mic=%s",
                settings.vad_mode,
                mic_label,
            )

    def _ensure_silero_vad(self) -> tuple[Any, Optional[str]]:
        from src.voice.input_audio import INPUT_AUDIO_BACKEND_SILERO, InputAudioSettings

        settings = self._input_audio_settings
        if not isinstance(settings, InputAudioSettings):
            return None, "silero_unavailable"
        if settings.backend != INPUT_AUDIO_BACKEND_SILERO:
            return None, "silero_unavailable"

        with self._silero_vad_lock:
            if self._shutdown_cancel.is_set() or self._permanently_closed:
                return None, "closed"
            existing = self._silero_vad
            if existing is not None:
                if getattr(existing, "closed", False):
                    return None, "closed"
                return existing, None
            if self._silero_vad_creating:
                return None, "busy"
            self._silero_vad_creating = True

        try:
            from src.voice.silero_vad import SileroOnnxVAD

            candidate = SileroOnnxVAD()
        except BaseException:
            with self._silero_vad_lock:
                self._silero_vad_creating = False
            raise

        retire_after: Any = None
        publish = False
        result: tuple[Any, Optional[str]] = (None, "closed")

        with self._silero_vad_lock:
            self._silero_vad_creating = False
            if self._shutdown_cancel.is_set() or self._permanently_closed:
                self._register_silero_retire_candidate(candidate)
                retire_after = candidate
                result = (None, "closed")
            elif self._silero_vad is not None:
                self._register_silero_retire_candidate(candidate)
                retire_after = candidate
                existing = self._silero_vad
                if getattr(existing, "closed", False):
                    result = (None, "closed")
                else:
                    result = (existing, None)
            else:
                self._silero_vad = candidate
                publish = True
                result = (candidate, None)

        if retire_after is not None:
            self._finish_claimed_silero_close(retire_after)

        if publish:
            self._log_input_audio_backend_once()
            warm_err = self._warm_silero_vad_instance(candidate)
            if warm_err is not None:
                with self._silero_vad_lock:
                    if self._silero_vad is candidate:
                        self._silero_vad = None
                    self._register_silero_retire_candidate(candidate)
                self._finish_claimed_silero_close(candidate)
                return None, warm_err
        return result

    def _warm_silero_vad_instance(self, vad: Any) -> Optional[str]:
        """
        在采集线程启动前加载 ORT 会话。

        若把首次 ``InferenceSession`` 放在采集 worker 里，Windows 上 ORT 可能长时间
        占用 GIL，导致主线程 ``Event.wait(timeout=…)`` 超时后仍无法返回，界面假死。
        """
        score = getattr(vad, "score_pcm", None)
        if not callable(score):
            return None
        from src.voice.input_segments import FRAME_BYTES

        self.logger.info("STT Silero VAD 正在加载推理会话…")
        try:
            result = score(
                b"\x00" * FRAME_BYTES,
                cancel_event=self._shutdown_cancel,
            )
        except Exception as exc:
            from src.core.log_privacy import exception_log_label

            self.logger.error(
                "STT Silero VAD 预热失败 err=%s",
                exception_log_label(exc),
            )
            return "vad_warm_failed"
        if getattr(result, "ok", False):
            self.logger.info("STT Silero VAD 推理会话已就绪")
            return None
        reason = getattr(result, "reason", None) or "vad_warm_failed"
        self.logger.error("STT Silero VAD 预热未就绪 reason=%s", reason)
        return str(reason)

    def _register_silero_retire_candidate(self, candidate: Any) -> None:
        self._silero_vad_retiring = candidate
        self._silero_vad_close_target = candidate

    def _close_silero_vad_reference(self, vad: Any) -> bool:
        try:
            outcome = vad.close(timeout_sec=2.0)
        except Exception:
            return False
        return outcome is True

    def _finish_claimed_silero_close(self, target: Any) -> bool:
        succeeded = False
        try:
            succeeded = self._close_silero_vad_reference(target)
        finally:
            with self._silero_vad_lock:
                if self._silero_vad_close_target is target:
                    self._silero_vad_close_target = None
                if succeeded is True:
                    if self._silero_vad_retiring is target:
                        self._silero_vad_retiring = None
                    if self._silero_vad is target:
                        self._silero_vad = None
        return succeeded is True

    def _silero_vad_shutdown_pending(self) -> bool:
        with self._silero_vad_lock:
            if self._silero_vad_creating:
                return True
            if self._silero_vad_close_target is not None:
                return True
            if self._silero_vad_retiring is not None:
                return True
            return self._silero_vad is not None

    def _try_begin_claimed_silero_close(self, target: Any) -> bool:
        with self._silero_vad_lock:
            if self._silero_vad_close_target is not None:
                return False
            self._silero_vad_close_target = target
            if (
                self._silero_vad_retiring is None
                and self._silero_vad is not target
            ):
                self._silero_vad_retiring = target
            return True

    def _shutdown_owned_silero_vad(self) -> bool:
        with self._silero_vad_lock:
            if self._silero_vad_creating:
                self._shutdown_complete = False
                return False
            if self._silero_vad_close_target is not None:
                self._shutdown_complete = False
                return False
            retiring = self._silero_vad_retiring
            published = self._silero_vad

        if retiring is not None:
            if not self._try_begin_claimed_silero_close(retiring):
                self._shutdown_complete = False
                return False
            if not self._finish_claimed_silero_close(retiring):
                self._shutdown_complete = False
                return False

        if published is None:
            return True

        if not self._try_begin_claimed_silero_close(published):
            self._shutdown_complete = False
            return False
        if not self._finish_claimed_silero_close(published):
            self._shutdown_complete = False
            return False
        return True

    def _silero_vad_quiescent_for_permanent_close(self) -> bool:
        return not self._silero_vad_shutdown_pending()

    def _run_mic_device_preflight(
        self,
        sr_module: Any,
        *,
        enumerate_devices: bool,
    ) -> Optional[str]:
        from src.voice.input_audio import InputAudioSettings

        settings = self._input_audio_settings
        if not isinstance(settings, InputAudioSettings):
            return None
        if self._mic_device_count is None:
            try:
                names = sr_module.Microphone.list_microphone_names()
            except Exception:
                return "无法枚举麦克风设备"
            if not names:
                return "未检测到麦克风设备"
            self._mic_device_count = len(names)
            if not self._mic_identity_logged:
                idx = settings.microphone_device_index
                label = f"device_index={idx}" if idx is not None else "default"
                self.logger.info("STT 输入设备已选择 (%s, count=%d)", label, len(names))
                self._mic_identity_logged = True
        from src.voice.input_audio import validate_microphone_device_index

        assert self._mic_device_count is not None
        return validate_microphone_device_index(
            settings.microphone_device_index,
            list_device_count=lambda: self._mic_device_count or 0,
        )

    def _open_microphone(self, sr_module: Any) -> Any:
        from src.voice.input_audio import InputAudioSettings

        err = self._run_mic_device_preflight(sr_module, enumerate_devices=False)
        if err:
            raise OSError(err)
        settings = self._input_audio_settings
        idx = (
            settings.microphone_device_index
            if isinstance(settings, InputAudioSettings)
            else None
        )
        if idx is None:
            return sr_module.Microphone()
        return sr_module.Microphone(device_index=idx)

    def _apply_recognizer_defaults(self, recognizer: Any) -> None:
        """降低误切句：动态能量 + 稍长停顿判定句末。"""
        recognizer.energy_threshold = self.energy_threshold
        recognizer.dynamic_energy_threshold = True
        # 默认约 0.8；1.5 秒停顿才切句，避免换气被当成说完。
        recognizer.pause_threshold = 1.5
        recognizer.non_speaking_duration = 0.5

    def is_ready(self) -> bool:
        """检查 STT 引擎是否已成功初始化。"""
        if self._provider is not None:
            try:
                return bool(self._provider.is_ready())
            except Exception as exc:
                from src.core.log_privacy import exception_log_label

                self.logger.error(
                    "STT provider 就绪检查失败 err=%s",
                    exception_log_label(exc),
                )
                return False
        if self.engine_type == "speech_recognition":
            return self._recognizer is not None
        if self.engine_type == "whisper":
            return self._model is not None and self._recognizer is not None
        if self.engine_type == "paraformer":
            if self._shutdown_complete:
                return False
            return self._paraformer_runtime_ready()
        return False

    def _paraformer_runtime_ready(self) -> bool:
        """Paraformer 就绪：麦克风后端 + 本地 ASR 权限 + 已封存固定 profile（不加载 torch/模型）。"""
        if self._shutdown_complete or self._permanently_closed:
            return False
        if self._shutdown_cancel.is_set():
            return False
        if self._recognizer is None:
            return False
        if not self._local_asr_allowed():
            return False
        policy = self.safety_policy
        if policy is None:
            return False
        from src.core.safety import SafetyPolicy

        if isinstance(policy, SafetyPolicy):
            if not policy.local_asr_profile_sealed():
                return False
        else:
            sealed = getattr(policy, "local_asr_profile_sealed", None)
            if callable(sealed):
                try:
                    if sealed() is not True:
                        return False
                except Exception:
                    return False
            else:
                return False
        if self._paraformer_input_guard_enabled():
            from src.voice.input_audio import resolve_acoustic_input_readiness

            ready, _hint = resolve_acoustic_input_readiness(self._input_audio_settings)
            if not ready:
                return False
        return True

    def _continuous_capture_wanted(self) -> bool:
        if self._provider is not None:
            return False
        if self.engine_type != "paraformer":
            return False
        from src.voice.input_audio import (
            CAPTURE_MODE_CONTINUOUS,
            INPUT_AUDIO_BACKEND_SILERO,
            InputAudioSettings,
        )

        settings = self._input_audio_settings
        if not isinstance(settings, InputAudioSettings):
            return False
        if not settings.enabled:
            return False
        if settings.capture_mode != CAPTURE_MODE_CONTINUOUS:
            return False
        if settings.backend != INPUT_AUDIO_BACKEND_SILERO:
            return False
        return True

    def _bump_continuous_capture_epoch(self) -> int:
        with self._continuous_capture_lock:
            self._continuous_capture_epoch += 1
            return self._continuous_capture_epoch

    def _continuous_capture_consuming(self) -> bool:
        """是否应走已拥有的连续采集（非配置谓词）。"""
        with self._continuous_capture_lock:
            failed = self._continuous_capture_stop_failed
            capture = self._continuous_capture
        if failed:
            return True
        if capture is None:
            return False
        status = capture.status()
        if status.running:
            return True
        if status.pending_cleanup or not status.complete:
            return True
        return False

    def _continuous_capture_shutdown_pending(self) -> bool:
        with self._continuous_capture_lock:
            failed = self._continuous_capture_stop_failed
            capture = self._continuous_capture
        if failed:
            return True
        if capture is None:
            return False
        status = capture.status()
        return bool(
            status.running
            or status.pending_cleanup
            or not status.complete
        )

    def _close_continuous_capture_identity(self, capture: Any) -> bool:
        """关闭指定控制器；仅身份匹配且 ``close is True`` 时清除当前槽位。"""
        try:
            closed = capture.close() is True
        except Exception:
            closed = False
        with self._continuous_capture_lock:
            if self._continuous_capture is not capture:
                return closed
            if not closed:
                self._continuous_capture_stop_failed = True
                return False
            self._continuous_capture = None
            self._continuous_capture_stop_failed = False
        return True

    def _close_owned_continuous_capture(self) -> bool:
        with self._continuous_capture_lock:
            capture = self._continuous_capture
        if capture is None:
            with self._continuous_capture_lock:
                self._continuous_capture_stop_failed = False
            return True
        return self._close_continuous_capture_identity(capture)

    def _shutdown_continuous_capture(self) -> bool:
        return self._close_owned_continuous_capture()

    def _continuous_capture_epoch_matches(self, epoch: int) -> bool:
        with self._continuous_capture_lock:
            return epoch == self._continuous_capture_epoch

    def start_continuous_input(self) -> tuple[bool, str]:
        if not self._continuous_capture_wanted():
            return True, ""
        if self._permanently_closed or self._shutdown_cancel.is_set():
            return False, "STT 已关闭"
        with self._continuous_capture_lock:
            epoch = self._continuous_capture_epoch
            if self._continuous_capture_stop_failed:
                return False, "capture_stop_pending"
            capture = self._continuous_capture
            if capture is not None:
                status = capture.status()
                if status.running:
                    return True, ""
                if not status.complete:
                    return False, "capture_start_pending"
        if not self._paraformer_runtime_ready():
            from src.voice.paraformer.client import ParaformerWorkerClient

            return False, ParaformerWorkerClient.setup_hint()
        silero_vad, err = self._ensure_silero_vad()
        if not self._continuous_capture_epoch_matches(epoch):
            return False, "capture_session_cancelled"
        if self._permanently_closed or self._shutdown_cancel.is_set():
            return False, "STT 已关闭"
        if silero_vad is None:
            hint = err or "silero_unavailable"
            if hint == "closed":
                return False, "STT 已关闭"
            if hint == "busy":
                return False, "STT 正忙"
            return False, "Silero VAD 不可用"
        publish_ref: Any
        owns_start = False
        with self._continuous_capture_lock:
            if epoch != self._continuous_capture_epoch:
                return False, "capture_session_cancelled"
            if self._continuous_capture_stop_failed:
                return False, "capture_stop_pending"
            if self._permanently_closed or self._shutdown_cancel.is_set():
                return False, "STT 已关闭"
            capture = self._continuous_capture
            if capture is not None and capture.status().complete:
                capture = None
                self._continuous_capture = None
            if capture is not None:
                status = capture.status()
                if status.running:
                    return True, ""
                if not status.complete:
                    return False, "capture_start_pending"
            if capture is None:
                from src.voice.input_capture import ContinuousInputCapture

                capture = ContinuousInputCapture(
                    settings=self._input_audio_settings,
                    silero_vad_owner=silero_vad,
                )
                self._continuous_capture = capture
                owns_start = True
            publish_ref = capture
        if not owns_start:
            return True, ""
        if not self._continuous_capture_epoch_matches(epoch):
            self._close_continuous_capture_identity(publish_ref)
            return False, "capture_session_cancelled"
        if self._permanently_closed or self._shutdown_cancel.is_set():
            self._close_continuous_capture_identity(publish_ref)
            return False, "STT 已关闭"
        self.logger.info("连续采集线程启动中…")
        ok, reason = publish_ref.start()
        if (
            not self._continuous_capture_epoch_matches(epoch)
            or self._permanently_closed
            or self._shutdown_cancel.is_set()
        ):
            self._close_continuous_capture_identity(publish_ref)
            if not self._continuous_capture_epoch_matches(epoch):
                return False, "capture_session_cancelled"
            return False, "STT 已关闭"
        if not ok:
            self.logger.error(
                "连续采集启动失败 reason=%s",
                reason or "capture_start_failed",
            )
            self._close_continuous_capture_identity(publish_ref)
            return False, reason or "capture_start_failed"
        self.logger.info("连续采集已启动，等待语音片段")
        return True, ""

    def stop_continuous_input(self) -> bool:
        self._bump_continuous_capture_epoch()
        return self._close_owned_continuous_capture()

    def _recognize_via_continuous_capture(
        self,
        *,
        timeout: Optional[float],
        phrase_time_limit: Optional[float],
        cancel_event: Optional[threading.Event],
    ) -> RecognitionResult:
        if cancel_event is not None and cancel_event.is_set():
            return RecognitionResult.cancelled("识别开始前已取消")
        if self._permanently_closed or self._shutdown_cancel.is_set():
            return RecognitionResult.failure(RecognitionStatus.NOT_READY, "STT 已关闭")
        with self._continuous_capture_lock:
            capture = self._continuous_capture
        if capture is None:
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "连续采集未启动"
            )
        status = capture.status()
        if not status.running:
            if status.pending_cleanup or self._continuous_capture_stop_failed:
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY, "连续采集不可用"
                )
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "连续采集未运行"
            )
        limits = self._coerce_listen_limits(
            timeout,
            phrase_time_limit,
            continuous=True,
        )
        if isinstance(limits, RecognitionResult):
            return limits
        wait_timeout, _phrase = limits
        if wait_timeout is None:
            wait_timeout = 0.8
        combined_cancel = _CombinedCancelView(
            cancel_event,
            self._shutdown_cancel,
            capture.stop_cancel_view,
        )
        segment, reason = capture.read_segment(
            wait_timeout,
            cancel_event=combined_cancel,
        )
        if combined_cancel.is_set():
            return RecognitionResult.cancelled("识别已取消")
        if self._shutdown_cancel.is_set():
            return RecognitionResult.failure(RecognitionStatus.NOT_READY, "STT 已关闭")
        if segment is None:
            if reason == "cancelled":
                return RecognitionResult.cancelled("识别已取消")
            if reason == "timeout":
                return RecognitionResult.failure(
                    RecognitionStatus.SILENCE_TIMEOUT, "未检测到语音片段"
                )
            if reason == "not_ready":
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY, "连续采集未就绪"
                )
            if reason and (
                reason.startswith("device")
                or reason in ("overflow_limit", "frame_geometry")
            ):
                return RecognitionResult.failure(
                    RecognitionStatus.DEVICE_ERROR, reason
                )
            if reason:
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY, reason
                )
            return RecognitionResult.failure(
                RecognitionStatus.SILENCE_TIMEOUT, "未检测到语音片段"
            )
        if not self._paraformer_runtime_ready():
            from src.voice.paraformer.client import ParaformerWorkerClient

            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY,
                ParaformerWorkerClient.setup_hint(),
            )
        with self._continuous_capture_lock:
            if capture is not self._continuous_capture:
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY, "连续采集会话已结束"
                )
            live_status = capture.status()
            if (
                segment.stream_generation != live_status.generation
                or segment.session_id != live_status.session_id
            ):
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY, "连续采集代际已过期"
                )
        if combined_cancel.is_set():
            return RecognitionResult.cancelled("识别已取消")
        pcm = segment.pcm
        cap_start = segment.capture_start_mono
        cap_end = segment.capture_end_mono
        gen_before = segment.stream_generation
        session_before = segment.session_id
        result = self._transcribe_paraformer_pcm(
            pcm,
            cancel_event=combined_cancel,
            apply_acoustic_guard=False,
        )
        if combined_cancel.is_set() or self._shutdown_cancel.is_set():
            return RecognitionResult.cancelled("识别已取消")
        with self._continuous_capture_lock:
            current = self._continuous_capture
            if current is not capture:
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY, "连续采集会话已结束"
                )
            live = current.status()
            if (
                gen_before != live.generation
                or session_before != live.session_id
            ):
                return RecognitionResult.cancelled("识别已取消")
        if result.status is RecognitionStatus.SUCCESS:
            if combined_cancel.is_set() or self._shutdown_cancel.is_set():
                return RecognitionResult.cancelled("识别已取消")
            return RecognitionResult.success(
                result.text,
                capture_start_mono=cap_start,
                capture_end_mono=cap_end,
            )
        return result

    def shutdown(self) -> bool:
        """关闭 STT 资源；仅 ``True`` 表示可永久丢弃引擎。"""
        if self._permanently_closed:
            if self._continuous_capture_shutdown_pending():
                self._shutdown_complete = False
                return False
            return True
        self._bump_continuous_capture_epoch()
        self._shutdown_cancel.set()
        if not self._shutdown_continuous_capture():
            self._shutdown_complete = False
            return False
        if not self._shutdown_owned_silero_vad():
            self._shutdown_complete = False
            return False
        if self._recording:
            client = self._paraformer_client
            if client is not None and self.engine_type == "paraformer":
                settings = self._paraformer_settings
                budget = getattr(settings, "close_budget_sec", 2.0) if settings else 2.0
                try:
                    client.shutdown(budget_sec=min(float(budget), 2.0))
                except Exception:
                    pass
            self._shutdown_complete = False
            return False
        if self._provider is not None:
            closer = getattr(self._provider, "shutdown", None)
            if not callable(closer):
                self._permanently_closed = True
                self._shutdown_complete = True
                return True
            try:
                result = closer()
            except Exception:
                self._shutdown_complete = False
                return False
            if result is not True:
                self._shutdown_complete = False
                return False
            self._permanently_closed = True
            self._shutdown_complete = True
            return True
        if self.engine_type != "paraformer":
            if self._owns_temp_files and self._temp_files is not None:
                if not self._shutdown_owned_temp_files():
                    return False
            self._permanently_closed = True
            self._shutdown_complete = True
            return True
        client = self._paraformer_client
        if client is None:
            if self._owns_temp_files and self._temp_files is not None:
                if not self._shutdown_owned_temp_files():
                    return False
            if not self._silero_vad_quiescent_for_permanent_close():
                self._shutdown_complete = False
                return False
            self._permanently_closed = True
            self._shutdown_complete = True
            return True
        settings = self._paraformer_settings
        budget = getattr(settings, "close_budget_sec", 2.0) if settings else 2.0
        try:
            ok = client.shutdown(budget_sec=budget)
        except Exception:
            ok = False
        if ok is not True:
            self._shutdown_complete = False
            return False
        self._paraformer_client = None
        if not self._shutdown_owned_temp_files():
            return False
        self._permanently_closed = True
        self._shutdown_complete = True
        return True

    def _shutdown_owned_temp_files(self) -> bool:
        if not self._owns_temp_files or self._temp_files is None:
            return True
        try:
            status = self._temp_files.shutdown(timeout=2.0)
        except Exception:
            self._shutdown_complete = False
            return False
        if not getattr(status, "complete", False):
            self._shutdown_complete = False
            return False
        return True

    def get_shutdown_status(self) -> dict:
        if self._continuous_capture_shutdown_pending():
            worker_alive = False
            client = self._paraformer_client
            if client is not None:
                try:
                    worker_alive = bool(client.shutdown_status().worker_alive)
                except Exception:
                    worker_alive = False
            return {
                "complete": False,
                "worker_alive": worker_alive,
                "capture_pending": True,
            }
        if self._silero_vad_shutdown_pending():
            client = self._paraformer_client
            worker_alive = False
            if client is not None:
                try:
                    worker_alive = bool(client.shutdown_status().worker_alive)
                except Exception:
                    worker_alive = False
            return {"complete": False, "worker_alive": worker_alive}
        if self._permanently_closed and self._paraformer_client is None:
            if not self._shutdown_complete:
                return {"complete": False, "worker_alive": False}
            return {"complete": True, "worker_alive": False}
        client = self._paraformer_client
        if client is None:
            complete = (
                self._shutdown_complete
                and not self._silero_vad_shutdown_pending()
            )
            return {"complete": complete, "worker_alive": False}
        status = client.shutdown_status()
        complete = (
            self._shutdown_complete
            and status.complete
            and not self._silero_vad_shutdown_pending()
        )
        return {
            "complete": complete,
            "worker_alive": status.worker_alive,
        }

    def is_shutdown_complete(self) -> bool:
        if self._continuous_capture_shutdown_pending():
            return False
        if self._silero_vad_shutdown_pending():
            return False
        if not self._permanently_closed:
            return False
        if self._paraformer_client is not None:
            return False
        return self._shutdown_complete

    def check_microphone(self) -> tuple[bool, str]:
        """检查麦克风是否可用。"""
        if self._provider is not None:
            try:
                return self._provider.check_microphone()
            except Exception as exc:
                from src.core.log_privacy import exception_log_label

                self.logger.error(
                    "STT provider 麦克风检查失败 err=%s",
                    exception_log_label(exc),
                )
                return False, f"麦克风检查失败: {exc}"
        try:
            import speech_recognition as sr

            err = self._run_mic_device_preflight(sr, enumerate_devices=True)
            if err:
                return False, err
            with self._open_microphone(sr) as source:
                pass
            return True, ""
        except OSError as exc:
            return False, f"麦克风访问失败: {exc}"
        except Exception as exc:
            return False, f"麦克风检查失败: {exc}"

    def recognize(
        self,
        timeout: Optional[float] = None,
        phrase_time_limit: Optional[float] = None,
        continuous: bool = False,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> RecognitionResult:
        """监听并返回结构化识别结果；本入口不直接打印 UI 提示。"""
        if cancel_event is not None and cancel_event.is_set():
            return RecognitionResult.cancelled("识别开始前已取消")
        if self._permanently_closed or self._shutdown_cancel.is_set():
            return RecognitionResult.failure(RecognitionStatus.NOT_READY, "STT 已关闭")

        if not self._engine_lock.acquire(blocking=False):
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, "STT 正忙"
            )
        try:
            if continuous and self._continuous_capture_consuming():
                return self._recognize_via_continuous_capture(
                    timeout=timeout,
                    phrase_time_limit=phrase_time_limit,
                    cancel_event=cancel_event,
                )
            if self._provider is not None:
                try:
                    result = self._provider.recognize(
                        timeout=timeout,
                        phrase_time_limit=phrase_time_limit,
                        continuous=continuous,
                        cancel_event=cancel_event,
                    )
                    result = RecognitionResult.coerce(result)
                    if self._shutdown_cancel.is_set() or (
                        cancel_event is not None and cancel_event.is_set()
                    ):
                        return RecognitionResult.cancelled("识别完成时已取消")
                    if (
                        result.status is RecognitionStatus.SUCCESS
                        and self._shutdown_cancel.is_set()
                    ):
                        return RecognitionResult.failure(
                            RecognitionStatus.NOT_READY, "STT 已关闭"
                        )
                    return result
                except Exception as exc:
                    from src.core.log_privacy import exception_log_label

                    self.logger.error(
                        "STT provider 识别失败 err=%s",
                        exception_log_label(exc),
                        exc_info=True,
                    )
                    return RecognitionResult.failure(
                        RecognitionStatus.INTERNAL_ERROR, str(exc)
                    )

            self._recording = True
            try:
                return self._recognize_with_microphone(
                    timeout=timeout,
                    phrase_time_limit=phrase_time_limit,
                    continuous=continuous,
                    cancel_event=cancel_event,
                )
            finally:
                self._recording = False
        finally:
            self._engine_lock.release()

    def _coerce_listen_limits(
        self,
        timeout: Optional[float],
        phrase_time_limit: Optional[float],
        *,
        continuous: bool,
    ) -> tuple[Optional[float], Optional[float]] | RecognitionResult:
        def _positive_finite(name: str, value: Optional[float]) -> Optional[float] | RecognitionResult:
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY, f"{name} 参数无效"
                )
            number = float(value)
            if not math.isfinite(number) or number <= 0.0:
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY, f"{name} 参数无效"
                )
            return number

        coerced_timeout = _positive_finite("timeout", timeout)
        if isinstance(coerced_timeout, RecognitionResult):
            return coerced_timeout
        coerced_phrase = _positive_finite("phrase_time_limit", phrase_time_limit)
        if isinstance(coerced_phrase, RecognitionResult):
            return coerced_phrase
        if self.engine_type == "paraformer":
            from src.voice.paraformer.settings import MAX_UTTERANCE_SEC

            if coerced_phrase is None or coerced_phrase > float(MAX_UTTERANCE_SEC):
                coerced_phrase = float(MAX_UTTERANCE_SEC)
        return coerced_timeout, coerced_phrase

    def _effective_paraformer_listen_phrase_limit(
        self,
        source: Any,
        phrase_time_limit: Optional[float],
    ) -> Optional[float]:
        if self.engine_type != "paraformer" or self._recognizer is None:
            return phrase_time_limit
        from src.voice.input_audio import compute_paraformer_listen_phrase_limit_sec

        return compute_paraformer_listen_phrase_limit_sec(
            source,
            self._recognizer,
            phrase_time_limit,
        )

    @staticmethod
    def _paraformer_pcm_geometry_failure(reason: str) -> RecognitionResult:
        if reason == "pcm_empty":
            return RecognitionResult.failure(
                RecognitionStatus.UNRECOGNIZED, "音频为空"
            )
        if reason == "pcm_misaligned":
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, "PCM 字节未对齐"
            )
        if reason == "pcm_oversized":
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, "PCM 超过协议上限"
            )
        if reason == "pcm_zero":
            return RecognitionResult.failure(
                RecognitionStatus.UNRECOGNIZED, "音频无效"
            )
        if reason == "pcm_invalid_type":
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, "PCM 类型无效"
            )
        return RecognitionResult.failure(
            RecognitionStatus.INTERNAL_ERROR, f"PCM 无效 ({reason})"
        )

    def _recognize_with_microphone(
        self,
        *,
        timeout: Optional[float],
        phrase_time_limit: Optional[float],
        continuous: bool,
        cancel_event: Optional[threading.Event],
    ) -> RecognitionResult:
        if self._permanently_closed or self._shutdown_cancel.is_set():
            return RecognitionResult.failure(RecognitionStatus.NOT_READY, "STT 已关闭")

        if not self._recognizer:
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "STT 引擎未初始化"
            )

        if self.engine_type == "whisper" and self._model is None:
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "Whisper 模型未加载"
            )

        limits = self._coerce_listen_limits(
            timeout, phrase_time_limit, continuous=continuous
        )
        if isinstance(limits, RecognitionResult):
            return limits
        timeout, phrase_time_limit = limits

        # Paraformer：打开麦克风前检查权限/封存 profile（不启动 worker、不上云）
        if self.engine_type == "paraformer":
            if not self._paraformer_runtime_ready():
                from src.voice.paraformer.client import ParaformerWorkerClient

                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY,
                    ParaformerWorkerClient.setup_hint(),
                )

        # Google 云识别：打开麦克风前强制检查；拒绝时不录音、不调用网络
        if self.engine_type == "speech_recognition":
            try:
                self._require_cloud_stt("recognize_google")
            except PermissionError:
                self.logger.warning("cloud_stt 未允许，拒绝打开麦克风与 Google 识别")
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY,
                    "cloud_stt 未允许，无法使用 Google 在线识别",
                )

        try:
            import speech_recognition as sr

            try:
                if cancel_event is not None and cancel_event.is_set():
                    return RecognitionResult.cancelled("打开麦克风前已取消")
                if self._shutdown_cancel.is_set():
                    return RecognitionResult.failure(
                        RecognitionStatus.NOT_READY, "STT 已关闭"
                    )
                mic = self._open_microphone(sr)
                with mic as source:
                    if cancel_event is not None and cancel_event.is_set():
                        return RecognitionResult.cancelled("打开麦克风前已取消")
                    if self._shutdown_cancel.is_set():
                        return RecognitionResult.failure(
                            RecognitionStatus.NOT_READY, "STT 已关闭"
                        )
                    if not getattr(self, "_noise_adjusted", False):
                        sync_cal = self.engine_type == "paraformer"
                        self._calibrate_ambient_noise(
                            source, continuous=continuous, sync=sync_cal
                        )
                    if cancel_event is not None and cancel_event.is_set():
                        return RecognitionResult.cancelled("开始录音前已取消")

                    if not continuous:
                        self.logger.info("请说话...")
                    elif not getattr(self, "_first_listen", False):
                        self.logger.info("开始持续监听...")
                        self._first_listen = True

                    if continuous:
                        listen_timeout = timeout if timeout is not None else 10
                        listen_phrase_limit = (
                            phrase_time_limit if phrase_time_limit is not None else 12
                        )
                    else:
                        listen_timeout = timeout if timeout is not None else 5
                        listen_phrase_limit = phrase_time_limit

                    if self.engine_type == "paraformer":
                        listen_phrase_limit = self._effective_paraformer_listen_phrase_limit(
                            source, listen_phrase_limit
                        )

                    if continuous:
                        self.logger.info(
                            "正在监听麦克风 (超时=%s秒)...", listen_timeout
                        )
                    else:
                        self.logger.debug(
                            "开始监听 (timeout=%s, phrase_limit=%s)",
                            listen_timeout,
                            listen_phrase_limit,
                        )

                    try:
                        audio = self._recognizer.listen(
                            source,
                            timeout=listen_timeout,
                            phrase_time_limit=listen_phrase_limit,
                        )
                    except sr.WaitTimeoutError:
                        if not continuous:
                            self.logger.warning("录音超时")
                        return RecognitionResult.failure(
                            RecognitionStatus.SILENCE_TIMEOUT, "录音等待超时"
                        )

                    duration_s = len(audio.frame_data) / float(
                        audio.sample_rate * audio.sample_width
                    )
                    self.logger.debug(
                        "检测到音频活动，录音完成 (约 %.1f 秒)", duration_s
                    )
                    if duration_s < 0.4 and not self._paraformer_input_guard_enabled():
                        self.logger.info("录音过短，已忽略")
                        return RecognitionResult.failure(
                            RecognitionStatus.UNRECOGNIZED, "录音过短"
                        )

            except OSError as exc:
                from src.core.log_privacy import exception_log_label

                self.logger.error(
                    "麦克风访问失败 err=%s", exception_log_label(exc)
                )
                return RecognitionResult.failure(
                    RecognitionStatus.DEVICE_ERROR, str(exc)
                )

            if cancel_event is not None and cancel_event.is_set():
                return RecognitionResult.cancelled("录音完成后已取消")
            if self._shutdown_cancel.is_set():
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY, "STT 已关闭"
                )

            self.logger.info("正在识别语音 (engine=%s)...", self.engine_type)
            if self.engine_type == "paraformer":
                return self._recognize_paraformer_result(
                    audio,
                    cancel_event=cancel_event,
                    apply_acoustic_guard=True,
                )
            if self.engine_type == "whisper":
                return self._recognize_whisper_result(audio)
            return self._recognize_google_result(audio)

        except Exception as exc:
            from src.core.log_privacy import exception_log_label

            self.logger.error(
                "语音识别失败 err=%s",
                exception_log_label(exc),
                exc_info=True,
            )
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, str(exc)
            )

    def listen(
        self,
        timeout: Optional[float] = None,
        phrase_time_limit: Optional[float] = None,
        continuous: bool = False,
    ) -> str:
        """兼容字符串入口：成功返回文本，所有非成功状态返回空字符串。"""
        result = self.recognize(
            timeout=timeout,
            phrase_time_limit=phrase_time_limit,
            continuous=continuous,
        )
        if result.status is RecognitionStatus.SUCCESS:
            return result.text
        self._notify_legacy_result(result, continuous=continuous)
        return ""

    def _local_asr_allowed(self) -> bool:
        from src.core.safety import Operation, SafetyPolicy

        policy = self.safety_policy
        if policy is None:
            return False
        if isinstance(policy, SafetyPolicy):
            return policy.is_allowed(Operation.LOCAL_ASR_WORKER)
        is_allowed = getattr(policy, "is_allowed", None)
        if callable(is_allowed):
            return bool(is_allowed(Operation.LOCAL_ASR_WORKER))
        return False

    def _ensure_paraformer_client(self) -> bool:
        if self._permanently_closed or self._shutdown_cancel.is_set():
            return False
        if self._paraformer_client is not None:
            return True
        if not self._local_asr_allowed():
            return False
        if self.safety_policy is None:
            return False
        from src.voice.paraformer.client import ParaformerWorkerClient

        self._paraformer_client = ParaformerWorkerClient(
            self._paraformer_settings,
            self.safety_policy,
        )
        return True

    def _recognize_paraformer_result(
        self,
        audio: Any,
        *,
        cancel_event: Optional[threading.Event] = None,
        apply_acoustic_guard: bool = False,
    ) -> RecognitionResult:
        """直接 AudioData 入口（诊断/包装器）；默认不跑麦克风声学 guard。"""
        if cancel_event is not None and cancel_event.is_set():
            return RecognitionResult.cancelled("识别完成后已取消")
        if self._permanently_closed or self._shutdown_cancel.is_set():
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "STT 已关闭"
            )
        try:
            pcm = audio.get_raw_data(convert_rate=16000, convert_width=2)
        except Exception as exc:
            from src.core.log_privacy import exception_log_label

            self.logger.error(
                "Paraformer PCM 转换失败 err=%s", exception_log_label(exc)
            )
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, "音频格式转换失败"
            )
        return self._transcribe_paraformer_pcm(
            pcm,
            cancel_event=cancel_event,
            apply_acoustic_guard=apply_acoustic_guard,
        )

    def _transcribe_paraformer_pcm(
        self,
        pcm: bytes,
        *,
        cancel_event: Optional[threading.Event] = None,
        apply_acoustic_guard: bool = False,
    ) -> RecognitionResult:
        from src.voice.input_audio import validate_paraformer_pcm_bytes

        geom = validate_paraformer_pcm_bytes(pcm)
        if geom is not None:
            return self._paraformer_pcm_geometry_failure(geom)
        if cancel_event is not None and cancel_event.is_set():
            return RecognitionResult.cancelled("识别完成后已取消")
        if self._permanently_closed or self._shutdown_cancel.is_set():
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "STT 已关闭"
            )
        if apply_acoustic_guard and self._paraformer_input_guard_enabled():
            from src.voice.input_audio import (
                INPUT_AUDIO_BACKEND_SILERO,
                InputAudioSettings,
                acoustic_guard_not_ready_hint,
                analyze_utterance_pcm,
            )
            from src.voice.noise_suppression import (
                NOISE_SUPPRESSION_WEBRTC,
                suppress_utterance_pcm,
            )

            silero_vad = None
            settings = self._input_audio_settings
            pcm_for_guard = pcm
            combined_cancel = _CombinedCancelView(
                cancel_event,
                self._shutdown_cancel,
            )
            if (
                isinstance(settings, InputAudioSettings)
                and settings.noise_suppression == NOISE_SUPPRESSION_WEBRTC
            ):
                ns_result = suppress_utterance_pcm(
                    pcm,
                    cancel_event=combined_cancel,
                )
                if cancel_event is not None and cancel_event.is_set():
                    return RecognitionResult.cancelled("识别已取消")
                if self._shutdown_cancel.is_set():
                    return RecognitionResult.failure(
                        RecognitionStatus.NOT_READY,
                        "STT 已关闭",
                    )
                if not ns_result.ok:
                    if ns_result.reason == "cancelled":
                        return RecognitionResult.cancelled("识别已取消")
                    hint = acoustic_guard_not_ready_hint(
                        settings,
                        reason=ns_result.reason,
                    )
                    return RecognitionResult.failure(
                        RecognitionStatus.NOT_READY,
                        hint,
                    )
                pcm_for_guard = ns_result.pcm
            if (
                isinstance(settings, InputAudioSettings)
                and settings.backend == INPUT_AUDIO_BACKEND_SILERO
            ):
                silero_vad, err = self._ensure_silero_vad()
                if silero_vad is None:
                    if err == "closed":
                        return RecognitionResult.failure(
                            RecognitionStatus.NOT_READY,
                            "STT 已关闭",
                        )
                    if err == "busy":
                        return RecognitionResult.failure(
                            RecognitionStatus.INTERNAL_ERROR,
                            "STT 正忙",
                        )
                    hint = acoustic_guard_not_ready_hint(
                        settings, reason="silero_unavailable"
                    )
                    return RecognitionResult.failure(
                        RecognitionStatus.NOT_READY,
                        hint,
                    )
            else:
                self._log_input_audio_backend_once()

            verdict = analyze_utterance_pcm(
                pcm_for_guard,
                self._input_audio_settings,
                cancel_event=combined_cancel,
                silero_vad=silero_vad,
            )
            if cancel_event is not None and cancel_event.is_set():
                return RecognitionResult.cancelled("识别已取消")
            if self._shutdown_cancel.is_set():
                return RecognitionResult.failure(
                    RecognitionStatus.NOT_READY, "STT 已关闭"
                )
            if not verdict.accepted:
                self.logger.debug(
                    "声学准入拒绝 reason=%s voiced_ms=%d ratio=%.3f frames=%d",
                    verdict.reason,
                    verdict.voiced_ms,
                    verdict.voiced_ratio,
                    verdict.frames_analyzed,
                )
                if verdict.reason == "cancelled":
                    return RecognitionResult.cancelled("识别已取消")
                if verdict.reason in (
                    "vad_unavailable",
                    "vad_native_error",
                    "silero_unavailable",
                ):
                    hint = acoustic_guard_not_ready_hint(
                        self._input_audio_settings,
                        reason=verdict.reason,
                    )
                    return RecognitionResult.failure(
                        RecognitionStatus.NOT_READY,
                        hint,
                    )
                return RecognitionResult.failure(
                    RecognitionStatus.UNRECOGNIZED,
                    f"未检测到足够人声 ({verdict.reason})",
                )
            pcm = pcm_for_guard
        if cancel_event is not None and cancel_event.is_set():
            return RecognitionResult.cancelled("识别已取消")
        if self._shutdown_cancel.is_set():
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "STT 已关闭"
            )
        if not self._ensure_paraformer_client():
            from src.voice.paraformer.client import ParaformerWorkerClient

            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY,
                ParaformerWorkerClient.setup_hint(),
            )
        client = self._paraformer_client
        if client is None:
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "Paraformer 客户端不可用"
            )
        outcome = client.transcribe(
            pcm,
            cancel_event=_CombinedCancelView(cancel_event, self._shutdown_cancel),
        )
        if self._shutdown_cancel.is_set() or (
            cancel_event is not None and cancel_event.is_set()
        ):
            return RecognitionResult.cancelled("识别已取消")
        if outcome.ok and outcome.text:
            if self._shutdown_cancel.is_set() or (
                cancel_event is not None and cancel_event.is_set()
            ):
                return RecognitionResult.cancelled("识别已取消")
            self.logger.info("语音识别成功 chars=%d", len(outcome.text))
            return RecognitionResult.success(outcome.text)
        if outcome.empty:
            self.logger.info("语音识别无文本，已忽略")
            return RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED)
        if outcome.error_label == "cancelled":
            return RecognitionResult.cancelled("识别已取消")
        if outcome.error_label == "permission":
            from src.voice.paraformer.client import ParaformerWorkerClient

            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY,
                ParaformerWorkerClient.setup_hint(),
            )
        if outcome.error_label in ("startup", "asset", "not_ready"):
            from src.voice.paraformer.client import ParaformerWorkerClient

            return RecognitionResult.failure(
                RecognitionStatus.SERVICE_ERROR,
                ParaformerWorkerClient.setup_hint(),
            )
        return RecognitionResult.failure(
            RecognitionStatus.SERVICE_ERROR, "本地识别暂时不可用"
        )

    def _recognize_google_result(self, audio: Any) -> RecognitionResult:
        import speech_recognition as sr

        try:
            self._require_cloud_stt("recognize_google")
        except PermissionError:
            self.logger.warning("cloud_stt 未允许，拒绝调用 Google 识别")
            return RecognitionResult.failure(
                RecognitionStatus.SERVICE_ERROR,
                "cloud_stt 未允许，无法使用 Google 在线识别",
            )

        try:
            text = self._recognizer.recognize_google(audio, language=self.language)
            if not isinstance(text, str) or not text.strip():
                self.logger.info("未能识别语音内容，请再说一次")
                return RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED)
            self.logger.info("语音识别成功 chars=%d", len(text))
            return RecognitionResult.success(text)
        except sr.UnknownValueError:
            self.logger.info("未能识别语音内容，请再说一次")
            return RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED)
        except sr.RequestError as exc:
            from src.core.log_privacy import exception_log_label

            self.logger.error(
                "识别服务错误 err=%s", exception_log_label(exc)
            )
            return RecognitionResult.failure(
                RecognitionStatus.SERVICE_ERROR, str(exc)
            )

    def _recognize_whisper_result(self, audio: Any) -> RecognitionResult:
        if self._model is None:
            self.logger.error("Whisper 模型未加载")
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "Whisper 模型未加载"
            )

        path = ""
        try:
            if self._temp_files is not None:
                path = self._temp_files.create(".wav")
            else:
                fd, path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
            with open(path, "wb") as file_obj:
                file_obj.write(audio.get_wav_data())

            # language: zh-CN → zh
            lang = (self.language or "zh").split("-")[0].split("_")[0]
            result = self._model.transcribe(path, language=lang, fp16=False)
            text = (result.get("text") or "").strip()
            if not text:
                return RecognitionResult.failure(RecognitionStatus.UNRECOGNIZED)
            self.logger.info("语音识别成功 chars=%d", len(text))
            return RecognitionResult.success(text)
        except Exception as exc:
            self.logger.error("Whisper 识别失败: %s", type(exc).__name__, exc_info=True)
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, str(exc)
            )
        finally:
            if path:
                if self._temp_files is not None:
                    try:
                        self._temp_files.remove(path)
                    except Exception:
                        pass
                elif os.path.isfile(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def _notify_legacy_result(
        self, result: RecognitionResult, *, continuous: bool
    ) -> None:
        """只为旧 ``listen`` 调用方保留原有持续模式提示。"""
        if not continuous:
            if result.status is RecognitionStatus.UNRECOGNIZED:
                self.logger.warning("无法识别音频")
            return
        if result.status is RecognitionStatus.UNRECOGNIZED:
            self._notify_unrecognized()
        elif result.status is RecognitionStatus.SERVICE_ERROR:
            print(f"\n[语音模式] 识别服务不可用: {result.error_detail or '未知错误'}\n")
        elif result.status is RecognitionStatus.DEVICE_ERROR:
            if not getattr(self, "_mic_error_reported", False):
                self._mic_error_reported = True
                print(f"\n[语音模式] 麦克风不可用: {result.error_detail or '未知错误'}\n")
        elif result.status is RecognitionStatus.INTERNAL_ERROR:
            print(f"\n[语音模式] 识别出错: {result.error_detail or '未知错误'}\n")

    def _calibrate_ambient_noise(
        self, source, *, continuous: bool, sync: bool = False
    ) -> None:
        """首次监听前短时校准环境噪音；失败不阻断监听。"""
        if continuous:
            self.logger.info("持续模式：首次校准环境噪音…")
        else:
            self.logger.info("正在调整环境噪音...")

        if sync:
            try:
                self._recognizer.adjust_for_ambient_noise(source, duration=0.5)
            except Exception as exc:
                from src.core.log_privacy import exception_log_label

                self.logger.warning(
                    "环境噪音调整失败，使用默认设置 err=%s",
                    exception_log_label(exc),
                )
            else:
                adjusted = float(self._recognizer.energy_threshold)
                floor = self._calibration_threshold_floor()
                if adjusted < floor:
                    self._recognizer.energy_threshold = floor
                    adjusted = floor
                self.logger.info(
                    "环境噪音调整完成 (energy_threshold=%.0f)",
                    adjusted,
                )
            self._noise_adjusted = True
            return

        noise_adjusted = [False]
        error_occurred = [None]

        def adjust_noise() -> None:
            try:
                # 0.5s 比原先 0.3s 更稳，仍保持较短以免拖慢首句
                self._recognizer.adjust_for_ambient_noise(source, duration=0.5)
                noise_adjusted[0] = True
            except Exception as exc:
                error_occurred[0] = exc

        adjust_thread = threading.Thread(target=adjust_noise, daemon=True)
        adjust_thread.start()
        adjust_thread.join(timeout=2.0)

        if adjust_thread.is_alive():
            self.logger.warning("环境噪音调整超时，跳过此步骤")
        elif error_occurred[0]:
            from src.core.log_privacy import exception_log_label

            self.logger.warning(
                "环境噪音调整失败，使用默认设置 err=%s",
                exception_log_label(error_occurred[0]),
            )
        else:
            adjusted = float(self._recognizer.energy_threshold)
            floor = self._calibration_threshold_floor()
            if adjusted < floor:
                self._recognizer.energy_threshold = floor
                adjusted = floor
            self.logger.info(
                "环境噪音调整完成 (energy_threshold=%.0f)",
                adjusted,
            )

        self._noise_adjusted = True

    def _notify_unrecognized(self) -> None:
        """持续模式下节流提示，避免刷屏。"""
        import time

        now = time.monotonic()
        last = getattr(self, "_last_unrecognized_notify", 0.0)
        if now - last < 3.0:
            return
        self._last_unrecognized_notify = now
        print("\n[语音模式] 没听清，请再说一次…\n")
