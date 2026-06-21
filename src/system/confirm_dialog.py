"""
系统电源操作二次确认弹窗（Windows 原生对话框）

在用户确认前拦截关机、重启、锁屏等高风险系统操作。
"""
import logging
import platform
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

# command -> (标题, 正文)
_ACTION_PROMPTS: Dict[str, Tuple[str, str]] = {
    "shutdown": (
        "确认关机",
        "Miku 助手收到关机请求。\n\n确定要在 60 秒后关闭电脑吗？\n\n点击「是」执行，点击「否」取消。",
    ),
    "restart": (
        "确认重启",
        "Miku 助手收到重启请求。\n\n确定要在 60 秒后重启电脑吗？\n\n点击「是」执行，点击「否」取消。",
    ),
    "lock": (
        "确认锁屏",
        "Miku 助手收到锁屏请求。\n\n确定要锁定屏幕吗？\n\n点击「是」执行，点击「否」取消。",
    ),
}


def confirm_system_action(command: str) -> bool:
    """
    弹出二次确认对话框。

    Args:
        command: 系统操作类型，``shutdown`` / ``restart`` / ``lock``。

    Returns:
        ``True`` 表示用户点击确认；``False`` 表示取消或无法显示对话框。
    """
    prompt = _ACTION_PROMPTS.get(command)
    if not prompt:
        logger.warning("未知系统命令，拒绝确认: %s", command)
        return False

    title, message = prompt

    if platform.system() != "Windows":
        logger.warning("非 Windows 系统，无法显示确认对话框")
        return False

    try:
        return _show_win32_message_box(title, message)
    except Exception as exc:
        logger.error("确认对话框显示失败: %s", exc)
        return False


def _show_win32_message_box(title: str, message: str) -> bool:
    """
    调用 Win32 ``MessageBoxW`` 显示是/否确认框。

    Args:
        title: 对话框标题。
        message: 对话框正文。

    Returns:
        用户点击「是」返回 ``True``，否则 ``False``。
    """
    import ctypes

    MB_YESNO = 0x04
    MB_ICONWARNING = 0x30
    MB_TOPMOST = 0x40000
    IDYES = 6

    result = ctypes.windll.user32.MessageBoxW(
        0,
        message,
        title,
        MB_YESNO | MB_ICONWARNING | MB_TOPMOST,
    )
    confirmed = result == IDYES
    logger.info("用户%s系统操作", "确认" if confirmed else "取消")
    return confirmed
