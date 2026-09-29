"""长期记忆模块：领域契约、SQLite 持久层、应用服务与产品接线。

导入本包不得创建数据库、目录、线程或连接。
"""
from __future__ import annotations

from src.memory.aware_client import MemoryAwareLLMClient
from src.memory.context_format import (
    CONTEXT_HEADER,
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ENTRIES,
    format_memory_context,
)
from src.memory.errors import (
    MemoryDataCorruptError,
    MemoryError,
    MemoryExtractionError,
    MemoryExtractionResponseError,
    MemoryExtractionTransportError,
    MemoryNotFoundError,
    MemorySchemaIncompatibleError,
    MemoryStoreUnavailableError,
    MemoryValidationError,
)
from src.memory.extractor import MemoryExtractor
from src.memory.ingestion import MemoryIngestionService
from src.memory.lexical_recall import LexicalMemoryRecall, lexical_tokens
from src.memory.models import (
    MemoryCategory,
    MemoryRecord,
    MemorySource,
    MemoryWriteResult,
    NewMemory,
    normalize_dedupe_key,
)
from src.memory.ollama_extractor import OllamaMemoryExtractor
from src.memory.paths import default_memory_db_path
from src.memory.query import MemoryQueryService
from src.memory.runtime import MemoryRuntime, MemorySettings, parse_memory_settings
from src.memory.service_models import (
    ExtractedMemory,
    IngestionStopStatus,
    MemoryContext,
    MemoryRecallResult,
    MemoryTurnEvent,
    MemoryTurnOutcome,
    ScoredMemory,
)
from src.memory.sqlite_store import SCHEMA_VERSION, SqliteMemoryStore
from src.memory.store import MemoryStore

__all__ = [
    "CONTEXT_HEADER",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_ENTRIES",
    "SCHEMA_VERSION",
    "ExtractedMemory",
    "IngestionStopStatus",
    "LexicalMemoryRecall",
    "MemoryAwareLLMClient",
    "MemoryCategory",
    "MemoryContext",
    "MemoryDataCorruptError",
    "MemoryError",
    "MemoryExtractionError",
    "MemoryExtractionResponseError",
    "MemoryExtractionTransportError",
    "MemoryExtractor",
    "MemoryIngestionService",
    "MemoryNotFoundError",
    "MemoryQueryService",
    "MemoryRecallResult",
    "MemoryRecord",
    "MemoryRuntime",
    "MemorySchemaIncompatibleError",
    "MemorySettings",
    "MemorySource",
    "MemoryStore",
    "MemoryStoreUnavailableError",
    "MemoryTurnEvent",
    "MemoryTurnOutcome",
    "MemoryValidationError",
    "MemoryWriteResult",
    "NewMemory",
    "OllamaMemoryExtractor",
    "ScoredMemory",
    "SqliteMemoryStore",
    "default_memory_db_path",
    "format_memory_context",
    "lexical_tokens",
    "normalize_dedupe_key",
    "parse_memory_settings",
]
