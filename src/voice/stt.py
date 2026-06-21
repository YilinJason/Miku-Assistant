"""
语音转文字（STT）模块
支持多种STT引擎：speech_recognition, whisper
"""
import logging
from typing import Optional


class STTEngine:
    """STT引擎封装类"""
    
    def __init__(self, engine: str = "speech_recognition", 
                 language: str = "zh-CN",
                 energy_threshold: int = 4000):
        """
        初始化STT引擎
        
        Args:
            engine: STT引擎类型 ('speech_recognition', 'whisper')
            language: 语言代码
            energy_threshold: 能量阈值（用于语音检测，仅speech_recognition）
        """
        self.engine_type = engine
        self.language = language
        self.energy_threshold = energy_threshold
        self.logger = logging.getLogger(__name__)
        self._recognizer = None
        self._initialize_engine()
    
    def _initialize_engine(self):
        """按 ``engine_type`` 加载 speech_recognition 或 whisper 后端。"""
        try:
            if self.engine_type == "speech_recognition":
                import speech_recognition as sr
                self._recognizer = sr.Recognizer()
                self._recognizer.energy_threshold = self.energy_threshold
                self.logger.info("speech_recognition引擎初始化成功")
                
            elif self.engine_type == "whisper":
                try:
                    import whisper
                    self._model = whisper.load_model("base")
                    self.logger.info("Whisper引擎初始化成功")
                except ImportError:
                    self.logger.warning("Whisper未安装，回退到speech_recognition")
                    import speech_recognition as sr
                    self._recognizer = sr.Recognizer()
                    self.engine_type = "speech_recognition"
                    
        except ImportError as e:
            self.logger.error(f"STT引擎导入失败: {e}")
            self._recognizer = None
        except Exception as e:
            self.logger.error(f"STT引擎初始化失败: {e}")
            self._recognizer = None

    def is_ready(self) -> bool:
        """
        检查 STT 引擎是否已成功初始化。

        Returns:
            引擎可用返回 ``True``，否则 ``False``。
        """
        if self.engine_type == "speech_recognition":
            return self._recognizer is not None
        if self.engine_type == "whisper":
            return hasattr(self, "_model") and self._model is not None
        return False

    def check_microphone(self) -> tuple[bool, str]:
        """
        检查麦克风是否可用。

        Returns:
            ``(True, "")`` 表示可用；``(False, 原因)`` 表示不可用。
        """
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
    
    def listen(self, timeout: Optional[float] = None, 
               phrase_time_limit: Optional[float] = None,
               continuous: bool = False) -> str:
        """
        监听并识别语音
        
        Args:
            timeout: 超时时间（秒），None表示无限等待（用于持续监听）
            phrase_time_limit: 短语时间限制（秒），None表示不限制
            continuous: 是否持续监听模式（语音模式下使用）
            
        Returns:
            识别出的文本，失败返回空字符串
        """
        try:
            if self.engine_type == "speech_recognition":
                if not self._recognizer:
                    return ""
                
                import speech_recognition as sr
                import threading
                import time
                
                # 检查麦克风是否可用
                try:
                    mic = sr.Microphone()
                    with mic as source:
                        # 只在第一次或非持续模式下调整环境噪音
                        # 持续模式下跳过环境噪音调整，直接开始监听
                        if not continuous:
                            if not hasattr(self, '_noise_adjusted'):
                                self.logger.info("正在调整环境噪音...")
                                
                                # 使用线程和超时机制，避免卡住
                                noise_adjusted = [False]
                                error_occurred = [None]
                                
                                def adjust_noise():
                                    try:
                                        self._recognizer.adjust_for_ambient_noise(source, duration=0.2)
                                        noise_adjusted[0] = True
                                    except Exception as e:
                                        error_occurred[0] = e
                                
                                # 在单独线程中执行，设置超时
                                adjust_thread = threading.Thread(target=adjust_noise, daemon=True)
                                adjust_thread.start()
                                adjust_thread.join(timeout=1.5)  # 最多等待1.5秒
                                
                                if adjust_thread.is_alive():
                                    self.logger.warning("环境噪音调整超时，跳过此步骤")
                                elif error_occurred[0]:
                                    self.logger.warning(f"环境噪音调整失败，使用默认设置: {error_occurred[0]}")
                                else:
                                    self.logger.info("环境噪音调整完成")
                                
                                self._noise_adjusted = True
                        else:
                            # 持续模式下，完全跳过环境噪音调整，直接开始监听
                            # 这样可以避免卡住，并且持续模式下不需要频繁调整
                            if not hasattr(self, '_noise_adjusted'):
                                self.logger.info("持续模式：跳过环境噪音调整，直接开始监听")
                                self._noise_adjusted = True
                        
                        if not continuous:
                            self.logger.info("请说话...")
                        else:
                            # 持续模式下也显示提示
                            if not hasattr(self, '_first_listen') or not self._first_listen:
                                self.logger.info("开始持续监听...")
                                self._first_listen = True
                        
                        try:
                            # 持续监听模式下，设置合理的超时时间（10秒），避免无限等待
                            # 超时后会返回空字符串，继续下一轮监听
                            if continuous:
                                listen_timeout = timeout if timeout is not None else 10  # 持续模式：10秒超时
                                listen_phrase_limit = phrase_time_limit if phrase_time_limit is not None else 10
                            else:
                                listen_timeout = timeout if timeout is not None else 5
                                listen_phrase_limit = phrase_time_limit if phrase_time_limit is not None else None
                            
                            if continuous:
                                self.logger.info(f"正在监听麦克风 (超时={listen_timeout}秒)...")
                            else:
                                self.logger.debug(f"开始监听 (timeout={listen_timeout}, phrase_limit={listen_phrase_limit})")
                            
                            audio = self._recognizer.listen(
                                source, 
                                timeout=listen_timeout,
                                phrase_time_limit=listen_phrase_limit
                            )
                            self.logger.info("检测到语音，录音完成")
                        except sr.WaitTimeoutError:
                            if continuous:
                                # 持续模式下超时是正常的，静默返回
                                return ""
                            self.logger.warning("录音超时")
                            return ""
                            
                except OSError as e:
                    self.logger.error(f"麦克风访问失败: {e}")
                    if continuous:
                        if not getattr(self, "_mic_error_reported", False):
                            self._mic_error_reported = True
                            print(f"\n[语音模式] 麦克风不可用: {e}\n")
                        return ""
                    raise
                
                try:
                    text = self._recognizer.recognize_google(
                        audio, 
                        language=self.language
                    )
                    self.logger.info(f"识别结果: {text}")
                    return text
                except sr.UnknownValueError:
                    if continuous:
                        # 持续模式下识别失败是正常的，静默返回
                        return ""
                    self.logger.warning("无法识别音频")
                    return ""
                except sr.RequestError as e:
                    self.logger.error(f"识别服务错误: {e}")
                    return ""
                    
        except Exception as e:
            if continuous:
                # 持续模式下错误也静默处理
                return ""
            self.logger.error(f"语音识别失败: {e}")
            return ""

