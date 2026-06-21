"""RVC 音色转换门面

将 edge-tts 生成的音频经 RVC 模型转换为指定角色音色（如 Nahida）。
"""
import logging
import os
import tempfile
from typing import Any, Dict, Optional

from src.core.paths import create_app_temp_file
from src.core.safety import SafetyPolicy
from src.voice.rvc.infer import run_rvc_infer
from src.voice.rvc.postprocess import polish_rvc_wav


class RVCConverter:
    """edge-tts 音频 → RVC 目标音色的转换器。"""

    def __init__(self, rvc_config: Dict[str, Any], safety_policy: Optional[SafetyPolicy] = None):
        """
        初始化 RVC 转换器。

        Args:
            rvc_config: RVC 配置字典（``model_path``、``index_path``、``device`` 等）。
            safety_policy: 安全策略；子进程推理时使用。
        """
        self.rvc_config = rvc_config or {}
        self.safety_policy = safety_policy
        self.logger = logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        """是否已配置 ``model_path`` 从而启用 RVC。"""
        return bool(self.rvc_config.get("model_path"))

    def convert_file(self, input_audio: str) -> Optional[str]:
        """
        将输入音频转换为目标音色并写入临时 wav。

        Args:
            input_audio: 输入音频路径（通常为 edge-tts 生成的 wav）。

        Returns:
            成功时返回输出 wav 临时路径；失败或未启用时返回 ``None``。
        """
        if not self.enabled:
            return None

        output_audio = create_app_temp_file(".wav")
        try:
            ok = run_rvc_infer(
                input_audio,
                output_audio,
                self.rvc_config,
                self.safety_policy,
            )
            if ok:
                try:
                    polish_rvc_wav(output_audio, self.rvc_config)
                except Exception as exc:
                    self.logger.warning("RVC 后处理跳过: %s", exc)
                return output_audio
            if os.path.exists(output_audio):
                os.remove(output_audio)
            return None
        except Exception as exc:
            self.logger.error("RVC 转换异常: %s", exc, exc_info=True)
            if os.path.exists(output_audio):
                os.remove(output_audio)
            return None
