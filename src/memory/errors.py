"""长期记忆相关异常；不吞掉 KeyboardInterrupt / SystemExit。"""
from __future__ import annotations


class MemoryError(Exception):
    """长期记忆子系统错误基类。"""


class MemoryValidationError(MemoryError, ValueError):
    """领域不变量或输入校验失败。"""


class MemoryNotFoundError(MemoryError):
    """按 ID 更新/删除时目标不存在。"""


class MemoryStoreUnavailableError(MemoryError):
    """存储已关闭、路径不可用或底层 I/O 失败。"""


class MemoryDataCorruptError(MemoryError):
    """已持久化行无法解码为合法领域对象。"""


class MemorySchemaIncompatibleError(MemoryError):
    """数据库 schema 版本高于当前支持版本；非破坏性拒绝。"""

    def __init__(self, message: str, *, found_version: int, supported_version: int) -> None:
        super().__init__(message)
        self.found_version = found_version
        self.supported_version = supported_version


class MemoryExtractionError(MemoryError):
    """无历史 Ollama 提取适配器失败基类（交由 ingestion 失败开放）。"""


class MemoryExtractionTransportError(MemoryExtractionError):
    """HTTP/连接/超时等传输层失败。"""

    def __init__(self, message: str, *, error_kind: str = "transport") -> None:
        super().__init__(message)
        self.error_kind = error_kind


class MemoryExtractionResponseError(MemoryExtractionError):
    """响应 JSON/结构/字段不合法。"""

    def __init__(self, message: str, *, error_kind: str = "response") -> None:
        super().__init__(message)
        self.error_kind = error_kind
