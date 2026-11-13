"""RVC 音色转换门面

将 edge-tts 生成的音频经 RVC 模型转换为指定角色音色（如 Nahida）。
临时输出经统一 ``TempFileManager`` 所有权清理，不直接 ``os.remove``。
"""
import logging
from typing import Any, Dict, Optional

from src.core.log_privacy import exception_log_label
from src.core.safety import SafetyPolicy
from src.voice.audio.preparation_rollback import request_owned_paths_rollback
from src.voice.audio.temp_files import TempFileManager
from src.voice.rvc.infer import run_rvc_infer
from src.voice.rvc.postprocess import polish_rvc_wav


class RVCConverter:
    """edge-tts 音频 → RVC 目标音色的转换器。"""

    def __init__(
        self,
        rvc_config: Dict[str, Any],
        safety_policy: Optional[SafetyPolicy] = None,
        temp_file_manager: Optional[TempFileManager] = None,
    ):
        """
        初始化 RVC 转换器。

        Args:
            rvc_config: RVC 配置字典（``model_path``、``index_path``、``device`` 等）。
            safety_policy: 安全策略；子进程推理时使用。
            temp_file_manager: 共享临时文件所有权管理器。
        """
        self.rvc_config = rvc_config or {}
        self.safety_policy = safety_policy  # 可为 None：convert 时拒绝，不新建放行策略
        self._owns_temp_files = temp_file_manager is None
        if temp_file_manager is not None:
            self._temp_files = temp_file_manager
        elif safety_policy is not None:
            self._temp_files = TempFileManager(safety_policy=safety_policy)
        else:
            self._temp_files = None
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
        if self.safety_policy is None or self._temp_files is None:
            self.logger.error("RVC 转换拒绝: 缺少显式 SafetyPolicy 或 TempFileManager")
            return None

        output_audio = ""
        try:
            output_audio = self._temp_files.create(".wav")
        except Exception as exc:
            self.logger.error(
                "RVC 输出临时文件创建失败 err=%s",
                exception_log_label(exc),
            )
            return None

        try:
            ok = run_rvc_infer(
                input_audio,
                output_audio,
                self.rvc_config,
                self.safety_policy,
                temp_file_manager=self._temp_files,
            )
        except Exception as exc:
            self.logger.error(
                "RVC 转换异常 err=%s",
                exception_log_label(exc),
            )
            request_owned_paths_rollback(
                self._temp_files, output_audio, primary=exc
            )
            return None
        except BaseException as exc:
            request_owned_paths_rollback(
                self._temp_files, output_audio, primary=exc
            )
            raise

        if not ok:
            request_owned_paths_rollback(self._temp_files, output_audio)
            return None

        try:
            polish_rvc_wav(output_audio, self.rvc_config)
        except Exception as exc:
            self.logger.warning(
                "RVC 后处理跳过 err=%s",
                exception_log_label(exc),
            )
        except BaseException as exc:
            request_owned_paths_rollback(
                self._temp_files, output_audio, primary=exc
            )
            raise
        return output_audio
