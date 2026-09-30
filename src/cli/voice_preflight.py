"""
语音模式启动前检查 *preflight* 检查模块。

在进入持续语音会话前验证 STT、麦克风与 Ollama 服务/模型是否可用，
失败时打印结构化报告并阻止会话启动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from src.core.assistant import MikuAssistant


@dataclass
class VoicePreflightReport:
    """
    语音预检结果报告。

    汇总是否通过、错误与警告信息，供 CLI 打印给用户。
    """

    ok: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        """
        生成多行人类可读摘要。

        Returns:
            含状态、错误与警告列表的格式化字符串。
        """
        lines = ["语音模式检查:"]
        lines.append(f"  状态: {'通过' if self.ok else '未通过'}")
        for err in self.errors:
            lines.append(f"  [错误] {err}")
        for warn in self.warnings:
            lines.append(f"  [警告] {warn}")
        return "\n".join(lines)


def check_voice_preflight(assistant: MikuAssistant) -> VoicePreflightReport:
    """
    执行完整语音预检：语音栈懒加载 → STT → 麦克风 → Ollama 连接与模型。

    Args:
        assistant: 已初始化的助手实例。

    Returns:
        ``VoicePreflightReport``；任一硬性检查失败时 ``ok=False`` 并附带 ``errors``。
    """
    report = VoicePreflightReport()

    load = assistant.ensure_voice_stack()
    if not load.ok:
        report.errors.extend(load.errors)
        report.warnings.extend(load.warnings)
        return report
    report.warnings.extend(load.warnings)

    if not assistant.stt_engine or not assistant.stt_engine.is_ready():
        report.errors.append("语音识别(STT)未初始化，请安装 SpeechRecognition 和 PyAudio")
        return report

    mic_ok, mic_msg = assistant.stt_engine.check_microphone()
    if not mic_ok:
        report.errors.append(mic_msg or "无法访问麦克风")
        return report

    if assistant.llm_client:
        if not assistant.llm_client.check_connection():
            report.errors.append("Ollama 服务未运行，请先执行: ollama serve")
            return report
        if not assistant.llm_client.check_model_available():
            model = assistant.llm_client.model
            report.errors.append(
                f"Ollama 模型 '{model}' 不存在。"
                f"请运行 ollama list 查看可用模型，并修改 config/config.yaml 中的 ollama.model"
            )
            return report
    else:
        report.warnings.append("LLM 未初始化，语音对话将无法获得回复")

    report.ok = True
    return report


def ensure_voice_ready(assistant: MikuAssistant) -> bool:
    """
    预检并在失败时打印报告。

    Args:
        assistant: 已初始化的助手实例。

    Returns:
        预检通过为 ``True``；否则打印 ``summary()`` 并返回 ``False``。
    """
    report = check_voice_preflight(assistant)
    if report.ok:
        return True
    print(report.summary())
    return False
