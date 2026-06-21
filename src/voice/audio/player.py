"""
音频文件播放

跨平台播放 TTS / RVC 生成的本地音频，优先 pygame，其次 playsound 或系统命令。
"""
import logging
import platform
import time
from typing import Optional

from src.core.safety import Operation, SafetyPolicy, safe_os_system


class AudioPlayer:
    """跨平台音频播放器，优先 pygame。"""

    def __init__(self, volume: float = 0.8, safety_policy: Optional[SafetyPolicy] = None):
        """
        初始化播放器并自动检测可用后端。

        Args:
            volume: 播放音量，0.0–1.0（仅 pygame 后端生效）。
            safety_policy: 安全策略；系统命令播放需 ``os_shell`` 权限。
        """
        self.volume = volume
        self.safety_policy = safety_policy or SafetyPolicy()
        self.logger = logging.getLogger(__name__)
        self._backend = self._detect_backend()

    def _detect_backend(self) -> str:
        """检测并返回播放后端标识：``pygame`` / ``playsound`` / ``system`` / ``none``。"""
        try:
            import pygame
            pygame.mixer.init()
            self.logger.info("使用 pygame 播放音频")
            return "pygame"
        except ImportError:
            pass

        try:
            import playsound  # noqa: F401
            self.logger.info("使用 playsound 播放音频")
            return "playsound"
        except ImportError:
            pass

        if self.safety_policy.is_allowed(Operation.OS_SHELL):
            self.logger.info("使用系统命令播放音频")
            return "system"

        self.logger.warning("os_shell 未授权且无 pygame/playsound，音频可能无法播放")
        return "none"

    def play(self, audio_file: str) -> None:
        """
        阻塞式播放本地音频文件直至结束。

        Args:
            audio_file: 音频文件路径。

        Raises:
            FileNotFoundError: 文件不存在。
            RuntimeError: 无可用播放后端。
        """
        import os
        if not os.path.exists(audio_file):
            raise FileNotFoundError(f"音频文件不存在: {audio_file}")

        self.logger.debug("播放音频文件: %s", audio_file)

        if self._backend == "pygame":
            import pygame
            pygame.mixer.music.load(audio_file)
            pygame.mixer.music.set_volume(self.volume)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.1)
            self._release_pygame_music()
        elif self._backend == "playsound":
            import playsound
            playsound.playsound(audio_file, block=True)
        elif self._backend == "system":
            if platform.system() == "Windows":
                safe_os_system(
                    self.safety_policy,
                    f'start /min "" "{audio_file}"',
                    context="audio_playback",
                )
                time.sleep(2)
            else:
                safe_os_system(
                    self.safety_policy,
                    f"mpg123 {audio_file}",
                    context="audio_playback",
                )
        else:
            raise RuntimeError("无可用音频播放方式（请安装 pygame 或授权 os_shell）")

    @staticmethod
    def _release_pygame_music() -> None:
        """
        播放结束后释放 pygame 音乐文件句柄。

        Windows 上若不释放，后续无法删除临时 wav 文件。
        """
        try:
            import pygame
            unload = getattr(pygame.mixer.music, "unload", None)
            if callable(unload):
                unload()
            else:
                pygame.mixer.music.stop()
        except Exception:
            pass
