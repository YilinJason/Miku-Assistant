"""
角色模型扩展接口模块。

预留用于未来接入 Unity 或其他 Live2D/3D 角色模型的消息与音频通道；
当前实现为占位逻辑，按 ``model_type`` 记录日志。
"""
import logging
from typing import Optional, Dict, Any
from pathlib import Path


class CharacterInterface:
    """
    角色模型统一接口。

    根据 ``model_type`` 初始化对应后端（待实现），
    并向角色侧转发助手回复文本及可选 TTS 音频路径。
    """

    def __init__(self, model_type: Optional[str] = None,
                 config: Dict[str, Any] = None):
        """
        初始化角色模型接口并尝试连接后端。

        Args:
            model_type: 模型类型标识，如 ``unity``、``other``；``None`` 表示仅占位。
            config: 后端特定配置字典（路径、端口等）。
        """
        self.model_type = model_type
        self.config = config or {}
        self.logger = logging.getLogger(__name__)
        self._initialized = False
        self._initialize()

    def _initialize(self) -> None:
        """
        按 ``model_type`` 执行后端初始化。

        失败时将 ``_initialized`` 置为 ``False`` 并记录错误日志。
        """
        try:
            if self.model_type == "unity":
                self.logger.info("Unity角色模型接口已准备（待实现）")
            elif self.model_type == "other":
                self.logger.info("其他角色模型接口已准备（待实现）")
            else:
                self.logger.info("角色模型接口已初始化（未指定类型）")

            self._initialized = True

        except Exception as e:
            from src.core.log_privacy import exception_log_label

            self.logger.error("角色模型初始化失败 err=%s", exception_log_label(e))
            self._initialized = False

    def send_message(self, message: str, audio_file: Optional[str] = None) -> bool:
        """
        向角色模型发送助手回复（及可选同步音频）。

        Args:
            message: 要展示的文本内容。
            audio_file: TTS 生成的音频文件路径；后端可驱动口型或播放。

        Returns:
            发送成功为 ``True``；未初始化或未知类型为 ``False``。
        """
        if not self._initialized:
            self.logger.warning("角色模型未初始化")
            return False

        try:
            msg_chars = len(message or "")
            has_audio = bool(audio_file)
            if self.model_type == "unity":
                self.logger.info(
                    "发送消息到Unity chars=%d has_audio=%s",
                    msg_chars,
                    has_audio,
                )
                return True

            elif self.model_type == "other":
                self.logger.info(
                    "发送消息到角色模型 chars=%d has_audio=%s",
                    msg_chars,
                    has_audio,
                )
                return True

            else:
                self.logger.warning("未指定角色模型类型")
                return False

        except Exception as e:
            from src.core.log_privacy import exception_log_label

            self.logger.error("发送消息失败 err=%s", exception_log_label(e))
            return False

    def close(self) -> None:
        """关闭与角色后端的连接并释放资源。"""
        if self._initialized:
            try:
                self.logger.info("角色模型接口已关闭")
            except Exception as e:
                from src.core.log_privacy import exception_log_label

                self.logger.error("关闭接口失败 err=%s", exception_log_label(e))
            finally:
                self._initialized = False
