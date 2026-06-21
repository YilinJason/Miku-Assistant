"""
Miku 助手主类模块。

整合配置、安全策略、LLM/TTS/STT、搜索、Windows 控制、角色扩展与意图路由，
对外提供文本/语音输入处理及模式切换等统一入口。
"""
import logging
import threading
from typing import Optional

from src.core.config import Config
from src.core.file_cleaner import FileCleaner
from src.core.handlers import LLMIntentHandler, SearchIntentHandler, SystemIntentHandler
from src.core.logging_setup import setup_app_logging
from src.core.paths import RUNTIME_DATA_DIRS, ensure_runtime_dirs, runtime_layout_summary
from src.core.router import IntentRouter
from src.core.safety import SafetyPolicy
from src.extensions.character_interface import CharacterInterface
from src.llm.ollama_client import OllamaClient
from src.search.chrome_search import ChromeSearcher
from src.system.windows_control import WindowsController
from src.voice.stt import STTEngine
from src.voice.tts import TTSEngine


class MikuAssistant:
    """
    Miku 桌面助手主类。

    负责初始化各子模块、注册安全白名单目录、构建意图路由链，
    并协调文本/语音输入、TTS 播报与角色模型扩展。
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        创建助手实例并完成全部子模块初始化。

        Args:
            config_path: 可选的配置文件路径；为 ``None`` 时使用默认 ``config/config.yaml``。

        Raises:
            Exception: 任一核心模块（LLM、TTS、STT 等）初始化失败时向上抛出。
        """
        self.config = Config(config_path)
        self.safety_policy = SafetyPolicy.from_app_config(self.config)

        self.log_file = setup_app_logging(self.config)
        self.logger = logging.getLogger(__name__)
        self.logger.info(runtime_layout_summary())
        if self.log_file:
            self.logger.info("崩溃排查请查看日志文件: %s", self.log_file)
        self.logger.debug("安全策略: %s", self.safety_policy.summary())

        self.llm_client: Optional[OllamaClient] = None
        self.tts_engine: Optional[TTSEngine] = None
        self.stt_engine: Optional[STTEngine] = None
        self.windows_controller: Optional[WindowsController] = None
        self.chrome_searcher: Optional[ChromeSearcher] = None
        self.character_interface: Optional[CharacterInterface] = None
        self.file_cleaner: Optional[FileCleaner] = None
        self.router: Optional[IntentRouter] = None

        self._register_safe_directories()
        self._initialize_modules()
        self._initialize_router()
        self._initialize_file_cleaner()

    def _register_safe_directories(self) -> None:
        """将运行时数据目录注册到安全策略的文件删除白名单。"""
        ensure_runtime_dirs()
        for directory in RUNTIME_DATA_DIRS:
            self.safety_policy.register_file_delete_dir(directory)

    def _initialize_modules(self) -> None:
        """
        按配置依次初始化 LLM、TTS、STT、Windows 控制、搜索与角色扩展。

        Raises:
            Exception: 初始化过程中发生未捕获异常时记录日志并重新抛出。
        """
        try:
            self.llm_client = OllamaClient(
                base_url=self.config.get("ollama.base_url"),
                model=self.config.get("ollama.model"),
                timeout=self.config.get("ollama.timeout"),
            )
            self.logger.info("LLM 模块初始化成功")

            tts_config = self.config.get("voice.tts", {})
            self.tts_engine = TTSEngine(
                engine=tts_config.get("engine", "edge-tts"),
                language=tts_config.get("language", "zh-CN"),
                rate=tts_config.get("rate", 150),
                volume=tts_config.get("volume", 0.8),
                voice=tts_config.get("voice", "zh-CN-XiaoxiaoNeural"),
                rvc_config=self.config.get_rvc_config(),
                fallback_enabled=tts_config.get("fallback_enabled", True),
                fallback_engine=tts_config.get("fallback_engine", "pyttsx3"),
                pipeline_enabled=tts_config.get("pipeline_enabled", True),
                safety_policy=self.safety_policy,
            )
            self.logger.info("TTS 模块初始化成功")

            stt_config = self.config.get("voice.stt", {})
            self.stt_engine = STTEngine(
                engine=stt_config.get("engine", "speech_recognition"),
                language=stt_config.get("language", "zh-CN"),
                energy_threshold=stt_config.get("energy_threshold", 4000),
            )
            self.logger.info("STT 模块初始化成功")

            if self.config.get("system.windows_control_enabled", True):
                self.windows_controller = WindowsController(
                    safety_policy=self.safety_policy,
                    require_confirmation=self.config.get(
                        "system.require_confirmation", True
                    ),
                )
                self.logger.info("Windows 控制模块初始化成功")

            search_config = self.config.get("search", {})
            self.chrome_searcher = ChromeSearcher(
                chrome_path=search_config.get("chrome_path"),
                search_engine=search_config.get("search_engine", "google"),
                auto_open=search_config.get("auto_open", True),
                safety_policy=self.safety_policy,
            )
            self.logger.info("搜索模块初始化成功")

            ext_config = self.config.get("extensions.character_model", {})
            if ext_config.get("enabled", False):
                self.character_interface = CharacterInterface(
                    model_type=ext_config.get("type"),
                    config=ext_config.get("config", {}),
                )
                self.logger.info("角色模型扩展接口初始化成功")

        except Exception as exc:
            self.logger.error("模块初始化失败: %s", exc, exc_info=True)
            raise

    def _initialize_router(self) -> None:
        """构建意图路由链：系统 → 搜索 → LLM（兜底）。"""
        self.router = IntentRouter([
            SystemIntentHandler(self.windows_controller, self.safety_policy),
            SearchIntentHandler(self.chrome_searcher, self.safety_policy),
            LLMIntentHandler(self.llm_client, self.safety_policy),
        ])

    def _initialize_file_cleaner(self) -> None:
        """初始化定时文件清理器；失败时仅记录日志，不阻断助手启动。"""
        try:
            self.file_cleaner = FileCleaner.from_config(
                self.config,
                safety_policy=self.safety_policy,
            )
            self.file_cleaner.start_auto_cleanup()
            self.logger.info("文件清理器初始化成功")
        except Exception as exc:
            self.logger.error("文件清理器初始化失败: %s", exc, exc_info=True)

    @staticmethod
    def check_mode_switch(text: str) -> Optional[str]:
        """
        检测用户输入是否包含语音/文本模式切换指令。

        Args:
            text: 用户原始输入文本。

        Returns:
            ``"switch_to_text"``、``"switch_to_voice"`` 或 ``None``（无切换意图）。
        """
        if not text:
            return None
        text_lower = text.lower().strip()
        text_compact = text_lower.replace(" ", "")

        to_text = ("关闭语音模式", "退出语音模式", "关闭语音", "退出语音")
        to_voice = (
            "开启语音模式", "打开语音模式", "启动语音模式", "进入语音模式",
            "开启语音", "打开语音", "voice mode", "voice",
        )

        if any(k in text_compact for k in to_text):
            return "switch_to_text"
        if any(k in text_compact or k == text_lower for k in to_voice):
            return "switch_to_voice"
        return None

    @staticmethod
    def strip_assistant_prefix(text: str) -> str:
        """
        去除助手回复前缀（如 ``Miku: ``），便于 TTS 朗读。

        Args:
            text: 可能带前缀的回复文本。

        Returns:
            去除已知前缀后的文本；无匹配前缀则原样返回。
        """
        for prefix in ("Miku: ", "miku: "):
            if text.startswith(prefix):
                return text[len(prefix):]
        return text

    def process_text_input(self, text: str, use_tts: bool = None) -> str:
        """
        处理文本输入：路由意图、可选 TTS 播报、同步角色模型。

        Args:
            text: 用户输入文本。
            use_tts: 是否启用 TTS；为 ``None`` 时读取 ``assistant.auto_tts`` 配置。

        Returns:
            助手回复文本（可能来自系统/搜索/LLM 处理器）。
        """
        response = self.router.dispatch(text) if self.router else "路由器未初始化"

        if use_tts is None:
            use_tts = self.config.get("assistant.auto_tts", True)

        if use_tts and self.tts_engine:
            tts_text = self.strip_assistant_prefix(response)
            try:
                # 后台线程播报，避免阻塞 CLI 输入循环
                threading.Thread(
                    target=self.tts_engine.speak,
                    args=(tts_text,),
                    daemon=True,
                ).start()
            except Exception as exc:
                self.logger.error("启动音频播放线程失败: %s", exc)
                self.tts_engine.speak(tts_text)

        if self.character_interface and self.character_interface._initialized:
            self.character_interface.send_message(response)

        return response

    def process_voice_input(self, continuous: bool = False) -> str:
        """
        通过 STT 引擎采集并识别语音输入。

        Args:
            continuous: 是否持续监听模式（由 STT 引擎解释）。

        Returns:
            识别到的文本；STT 未初始化或识别失败时返回提示或空字符串。
        """
        if not self.stt_engine:
            return "STT 模块未初始化"
        try:
            return self.stt_engine.listen(continuous=continuous)
        except Exception as exc:
            self.logger.error("语音识别失败: %s", exc)
            return ""

    def speak(self, text: str) -> None:
        """
        直接调用 TTS 朗读文本（同步阻塞）。

        Args:
            text: 待朗读内容；会自动去除 ``Miku: `` 前缀。
        """
        if self.tts_engine:
            self.tts_engine.speak(self.strip_assistant_prefix(text))
        else:
            self.logger.warning("TTS 模块未初始化")

    def shutdown(self) -> None:
        """优雅关闭：停止 TTS、停止并执行末次清理、关闭角色模型连接。"""
        self.logger.info("正在关闭助手...")
        if self.tts_engine:
            self.tts_engine.shutdown()
        if self.file_cleaner:
            self.file_cleaner.stop_auto_cleanup()
            self.file_cleaner.cleanup_now()
        if self.character_interface:
            self.character_interface.close()
        self.logger.info("助手已关闭")
