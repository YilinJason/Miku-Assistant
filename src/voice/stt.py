"""
语音转文字（STT）模块
支持多种STT引擎：speech_recognition（Google 在线）, whisper（本地）
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import Any, Optional

from src.voice.recognition import (
    RecognitionProvider,
    RecognitionResult,
    RecognitionStatus,
)


class STTEngine:
    """STT引擎封装类"""

    def __init__(
        self,
        engine: str = "speech_recognition",
        language: str = "zh-CN",
        energy_threshold: int = 4000,
        *,
        provider: Optional[RecognitionProvider] = None,
    ):
        """
        初始化STT引擎

        Args:
            engine: STT引擎类型 ('speech_recognition', 'whisper')
            language: 语言代码（Google 用 BCP-47；Whisper 取主语言如 zh）
            energy_threshold: 能量阈值（用于语音检测；校准后可能被覆盖）
            provider: 可选可注入 provider；传入时不初始化默认引擎或设备。
        """
        self.engine_type = engine
        self.language = language
        self.energy_threshold = energy_threshold
        self.logger = logging.getLogger(__name__)
        self._provider = provider
        self._recognizer = None
        self._model = None
        if provider is None:
            self._initialize_engine()

    def _initialize_engine(self) -> None:
        """按 ``engine_type`` 加载 speech_recognition 或 whisper 后端。"""
        try:
            if self.engine_type == "speech_recognition":
                import speech_recognition as sr

                self._recognizer = sr.Recognizer()
                self._apply_recognizer_defaults(self._recognizer)
                self.logger.info("speech_recognition引擎初始化成功（Google 在线识别）")

            elif self.engine_type == "whisper":
                try:
                    import whisper

                    self._model = whisper.load_model("base")
                    # 录音仍依赖 speech_recognition 的 Microphone
                    import speech_recognition as sr

                    self._recognizer = sr.Recognizer()
                    self._apply_recognizer_defaults(self._recognizer)
                    self.logger.info("Whisper引擎初始化成功（本地识别）")
                except ImportError:
                    self.logger.warning(
                        "Whisper未安装，回退到 speech_recognition/Google；"
                        "安装: pip install openai-whisper"
                    )
                    import speech_recognition as sr

                    self._recognizer = sr.Recognizer()
                    self._apply_recognizer_defaults(self._recognizer)
                    self.engine_type = "speech_recognition"

        except ImportError as e:
            self.logger.error("STT引擎导入失败: %s", e)
            self._recognizer = None
        except Exception as e:
            self.logger.error("STT引擎初始化失败: %s", e)
            self._recognizer = None

    def _apply_recognizer_defaults(self, recognizer: Any) -> None:
        """降低误切句：动态能量 + 稍长停顿判定句末。"""
        recognizer.energy_threshold = self.energy_threshold
        recognizer.dynamic_energy_threshold = True
        # 默认约 0.8；略增可减少话没说完就被截断送去识别
        recognizer.pause_threshold = 1.0
        recognizer.non_speaking_duration = 0.5

    def is_ready(self) -> bool:
        """检查 STT 引擎是否已成功初始化。"""
        if self._provider is not None:
            try:
                return bool(self._provider.is_ready())
            except Exception as exc:
                self.logger.error("STT provider 就绪检查失败: %s", exc)
                return False
        if self.engine_type == "speech_recognition":
            return self._recognizer is not None
        if self.engine_type == "whisper":
            return self._model is not None and self._recognizer is not None
        return False

    def check_microphone(self) -> tuple[bool, str]:
        """检查麦克风是否可用。"""
        if self._provider is not None:
            try:
                return self._provider.check_microphone()
            except Exception as exc:
                self.logger.error("STT provider 麦克风检查失败: %s", exc)
                return False, f"麦克风检查失败: {exc}"
        try:
            import speech_recognition as sr

            names = sr.Microphone.list_microphone_names()
            if not names:
                return False, "未检测到麦克风设备"
            with sr.Microphone() as source:
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

        if self._provider is not None:
            try:
                result = self._provider.recognize(
                    timeout=timeout,
                    phrase_time_limit=phrase_time_limit,
                    continuous=continuous,
                    cancel_event=cancel_event,
                )
                result = RecognitionResult.coerce(result)
                if cancel_event is not None and cancel_event.is_set():
                    return RecognitionResult.cancelled("识别完成时已取消")
                return result
            except Exception as exc:
                self.logger.error("STT provider 识别失败: %s", exc, exc_info=True)
                return RecognitionResult.failure(
                    RecognitionStatus.INTERNAL_ERROR, str(exc)
                )

        if not self._recognizer:
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "STT 引擎未初始化"
            )

        if self.engine_type == "whisper" and self._model is None:
            return RecognitionResult.failure(
                RecognitionStatus.NOT_READY, "Whisper 模型未加载"
            )

        try:
            import speech_recognition as sr

            try:
                if cancel_event is not None and cancel_event.is_set():
                    return RecognitionResult.cancelled("打开麦克风前已取消")
                mic = sr.Microphone()
                with mic as source:
                    if cancel_event is not None and cancel_event.is_set():
                        return RecognitionResult.cancelled("打开麦克风前已取消")
                    if not getattr(self, "_noise_adjusted", False):
                        self._calibrate_ambient_noise(source, continuous=continuous)
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
                    self.logger.info("检测到语音，录音完成 (约 %.1f 秒)", duration_s)
                    if duration_s < 0.4:
                        self.logger.info("录音过短，已忽略")
                        return RecognitionResult.failure(
                            RecognitionStatus.UNRECOGNIZED, "录音过短"
                        )

            except OSError as exc:
                self.logger.error("麦克风访问失败: %s", exc)
                return RecognitionResult.failure(
                    RecognitionStatus.DEVICE_ERROR, str(exc)
                )

            if cancel_event is not None and cancel_event.is_set():
                return RecognitionResult.cancelled("录音完成后已取消")

            self.logger.info("正在识别语音 (engine=%s)...", self.engine_type)
            if self.engine_type == "whisper":
                return self._recognize_whisper_result(audio)
            return self._recognize_google_result(audio)

        except Exception as exc:
            self.logger.error("语音识别失败: %s", exc, exc_info=True)
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

    def _recognize_google_result(self, audio: Any) -> RecognitionResult:
        import speech_recognition as sr

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
            self.logger.error("识别服务错误: %s", exc)
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
            self.logger.error("Whisper 识别失败: %s", exc, exc_info=True)
            return RecognitionResult.failure(
                RecognitionStatus.INTERNAL_ERROR, str(exc)
            )
        finally:
            if path and os.path.isfile(path):
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

    def _calibrate_ambient_noise(self, source, *, continuous: bool) -> None:
        """首次监听前短时校准环境噪音；失败不阻断监听。"""
        if continuous:
            self.logger.info("持续模式：首次校准环境噪音…")
        else:
            self.logger.info("正在调整环境噪音...")

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
            self.logger.warning(
                "环境噪音调整失败，使用默认设置: %s", error_occurred[0]
            )
        else:
            self.logger.info(
                "环境噪音调整完成 (energy_threshold=%.0f)",
                self._recognizer.energy_threshold,
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
