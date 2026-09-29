"""
助手默认依赖装配。

将配置加载、日志、运行时目录、引擎创建与清理器启动集中于此，
与 ``MikuAssistant`` 业务编排分离。注入测试路径应使用
``MikuAssistant.from_dependencies``，不要调用本模块的默认装配。

装配失败时按登记顺序的逆序回收本层已成功创建的 TTS / 角色 / 清理器
（仅 ``stop_auto_cleanup``，不调用 ``cleanup_now``）。构造器抛错且未返回
对象时，其内部泄漏不在本模块回收范围内。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from src.core.config import Config
from src.core.file_cleaner import FileCleaner
from src.core.logging_setup import setup_app_logging
from src.core.paths import RUNTIME_DATA_DIRS, ensure_runtime_dirs, runtime_layout_summary
from src.core.router import IntentRouter
from src.core.router_factory import build_intent_router
from src.core.safety import SafetyPolicy
from src.extensions.character_interface import CharacterInterface
from src.llm.ollama_client import OllamaClient
from src.memory.runtime import MemoryRuntime, parse_memory_settings
from src.search.chrome_search import ChromeSearcher
from src.system.windows_control import WindowsController
from src.voice.stt import STTEngine
from src.voice.tts import TTSEngine


@dataclass
class AssembledParts:
    """默认装配产出的组件集合（供 ``MikuAssistant`` 绑定）。"""

    config: Any
    safety_policy: SafetyPolicy
    log_file: Any
    llm_client: Optional[Any]
    tts_engine: Optional[Any]
    stt_engine: Optional[Any]
    windows_controller: Optional[Any]
    chrome_searcher: Optional[Any]
    character_interface: Optional[Any]
    file_cleaner: Optional[Any]
    router: IntentRouter
    logger: Optional[Any] = None
    memory_runtime: Optional[Any] = None


class _RollbackRegistry:
    """登记装配期可回收动作；失败时逆序各执行至多一次。"""

    def __init__(self, logger: logging.Logger):
        self._logger = logger
        self._actions: List[Tuple[str, Callable[[], None]]] = []

    def register(self, name: str, action: Callable[[], None]) -> None:
        """资源创建成功后立即登记；装配成功返回前由 ``abandon`` 清空。"""
        self._actions.append((name, action))

    def abandon(self) -> None:
        """装配成功：生命周期交给助手，不执行回收。"""
        self._actions.clear()

    def rollback(self) -> None:
        """逆序回收；单项失败只记录，不中断其余项。"""
        while self._actions:
            name, action = self._actions.pop()
            try:
                action()
            except Exception as exc:
                self._logger.error(
                    "装配回滚失败 resource=%s error=%s",
                    name,
                    exc,
                    exc_info=True,
                )


def assemble_default(
    config_path: Optional[str] = None,
    *,
    config_loader: Optional[Callable[[Optional[str]], Any]] = None,
    logging_setup: Optional[Callable[[Any], Any]] = None,
    dir_preparer: Optional[Callable[[], None]] = None,
    ollama_client_cls: Callable[..., Any] = OllamaClient,
    tts_engine_cls: Callable[..., Any] = TTSEngine,
    stt_engine_cls: Callable[..., Any] = STTEngine,
    windows_controller_cls: Callable[..., Any] = WindowsController,
    chrome_searcher_cls: Callable[..., Any] = ChromeSearcher,
    character_interface_cls: Callable[..., Any] = CharacterInterface,
    file_cleaner_from_config: Optional[Callable[..., Any]] = None,
    router_builder: Optional[Callable[..., IntentRouter]] = None,
    start_cleaner: bool = True,
    memory_runtime_factory: Optional[Callable[..., Any]] = None,
) -> AssembledParts:
    """
    按配置创建并连接默认运行时组件（含副作用）。

    ``memory.enabled=false``（默认）时不创建记忆库、不启动提取线程、不包装 LLM。
    启用时先校验 memory 配置，再创建 runtime，并在后续组件失败时逆序回滚。
    """
    load_config = config_loader or Config
    setup_logging = logging_setup or setup_app_logging
    prepare_dirs = dir_preparer or ensure_runtime_dirs
    cleaner_factory = file_cleaner_from_config or FileCleaner.from_config
    build_router = router_builder or build_intent_router

    config = load_config(config_path)
    safety_policy = SafetyPolicy.from_app_config(config)

    # memory 配置在任何设备工厂之前校验；未启用则零开销
    memory_settings = parse_memory_settings(config)
    memory_enabled = memory_settings.enabled

    log_file = setup_logging(config)

    prepare_dirs()
    for directory in RUNTIME_DATA_DIRS:
        safety_policy.register_file_delete_dir(directory)

    logger = logging.getLogger("src.core.assembly")
    logger.info(runtime_layout_summary())
    if log_file:
        logger.info("崩溃排查请查看日志文件: %s", log_file)
    logger.debug("安全策略: %s", safety_policy.summary())

    rollback = _RollbackRegistry(logger)
    memory_runtime = None

    try:
        from src.llm.conversation_history import (
            DEFAULT_MAX_STORED_TURNS,
            require_max_stored_turns,
        )

        # 历史上限在任何设备副作用之前严格校验
        max_stored_turns = require_max_stored_turns(
            "ollama.history.max_stored_turns",
            config.get(
                "ollama.history.max_stored_turns", DEFAULT_MAX_STORED_TURNS
            ),
        )
        llm_client = ollama_client_cls(
            base_url=config.get("ollama.base_url"),
            model=config.get("ollama.model"),
            timeout=config.get("ollama.timeout"),
            keep_alive=config.get("ollama.keep_alive", "30m"),
            max_stored_turns=max_stored_turns,
        )
        logger.info("LLM 模块初始化成功")

        if memory_enabled:
            make_runtime = memory_runtime_factory or MemoryRuntime.create_from_settings
            memory_runtime = make_runtime(memory_settings, logger=logger)
            rollback.register("memory_runtime.close", memory_runtime.close)
            llm_client = memory_runtime.wrap_llm(llm_client)
            memory_runtime.start()
            logger.info("长期记忆模块已启用并启动 ingestion")
            if not memory_settings.retention_enabled:
                logger.warning(
                    "memory.retention.enabled=false：不会自动 prune；"
                    "请在 config 中显式开启 retention，或接受库体积只增不减"
                )
            else:
                logger.info(
                    "memory retention 已启用 max_records=%s interval_h=%s preserve_user_confirmed=%s",
                    memory_settings.retention_max_records,
                    memory_settings.retention_maintenance_interval_hours,
                    memory_settings.retention_preserve_user_confirmed,
                )

        tts_config = config.get("voice.tts", {}) or {}
        tts_engine = tts_engine_cls(
            engine=tts_config.get("engine", "edge-tts"),
            language=tts_config.get("language", "zh-CN"),
            rate=tts_config.get("rate", 150),
            volume=tts_config.get("volume", 0.8),
            voice=tts_config.get("voice", "zh-CN-XiaoxiaoNeural"),
            rvc_config=config.get_rvc_config(),
            fallback_enabled=tts_config.get("fallback_enabled", True),
            fallback_engine=tts_config.get("fallback_engine", "pyttsx3"),
            pipeline_enabled=tts_config.get("pipeline_enabled", True),
            safety_policy=safety_policy,
        )
        rollback.register("tts_engine.shutdown", tts_engine.shutdown)
        logger.info("TTS 模块初始化成功")

        stt_config = config.get("voice.stt", {}) or {}
        stt_engine = stt_engine_cls(
            engine=stt_config.get("engine", "speech_recognition"),
            language=stt_config.get("language", "zh-CN"),
            energy_threshold=stt_config.get("energy_threshold", 4000),
        )
        logger.info("STT 模块初始化成功")

        windows_controller = None
        if config.get("system.windows_control_enabled", False):
            windows_controller = windows_controller_cls(
                safety_policy=safety_policy,
                require_confirmation=config.get("system.require_confirmation", True),
            )
            logger.info("Windows 控制模块初始化成功")

        search_config = config.get("search", {}) or {}
        chrome_searcher = chrome_searcher_cls(
            chrome_path=search_config.get("chrome_path"),
            search_engine=search_config.get("search_engine", "google"),
            auto_open=search_config.get("auto_open", False),
            safety_policy=safety_policy,
        )
        logger.info("搜索模块初始化成功")

        character_interface = None
        ext_config = config.get("extensions.character_model", {}) or {}
        if ext_config.get("enabled", False):
            character_interface = character_interface_cls(
                model_type=ext_config.get("type"),
                config=ext_config.get("config", {}),
            )
            rollback.register("character_interface.close", character_interface.close)
            logger.info("角色模型扩展接口初始化成功")

        file_cleaner = _create_file_cleaner(
            logger=logger,
            cleaner_factory=cleaner_factory,
            config=config,
            safety_policy=safety_policy,
            start_cleaner=start_cleaner,
            rollback=rollback,
        )

        router = build_router(
            windows_controller=windows_controller,
            chrome_searcher=chrome_searcher,
            llm_client=llm_client,
            safety_policy=safety_policy,
        )

        parts = AssembledParts(
            config=config,
            safety_policy=safety_policy,
            log_file=log_file,
            llm_client=llm_client,
            tts_engine=tts_engine,
            stt_engine=stt_engine,
            windows_controller=windows_controller,
            chrome_searcher=chrome_searcher,
            character_interface=character_interface,
            file_cleaner=file_cleaner,
            router=router,
            logger=None,
            memory_runtime=memory_runtime,
        )
        if config.get("ollama.warmup_on_start", True):
            start_warmup = getattr(llm_client, "start_warmup", None)
            if callable(start_warmup):
                try:
                    started = start_warmup(
                        timeout=float(config.get("ollama.warmup_timeout", 120.0))
                    )
                    if started is not False:
                        logger.info("Ollama 模型后台预热已启动")
                except Exception as exc:
                    logger.warning(
                        "Ollama 模型后台预热启动失败（正式请求仍可继续）: %s",
                        exc,
                    )
        rollback.abandon()
        return parts
    except Exception:
        rollback.rollback()
        raise


def _create_file_cleaner(
    *,
    logger: logging.Logger,
    cleaner_factory: Callable[..., Any],
    config: Any,
    safety_policy: SafetyPolicy,
    start_cleaner: bool,
    rollback: _RollbackRegistry,
) -> Optional[Any]:
    """
    创建清理器；失败不阻断装配。

    - 构造失败：记录后返回 ``None``。
    - ``start_cleaner=False``：不启动，仍登记 ``stop_auto_cleanup`` 供致命回滚。
    - 启动失败：**阶段一**立即尝试 stop；成功则不交给助手且不登记；stop 也失败则
      保留引用并登记，供**阶段二**致命回滚再尝试一次（或成功装配后交助手 shutdown）。
    """
    try:
        file_cleaner = cleaner_factory(config, safety_policy=safety_policy)
    except Exception as exc:
        logger.error("文件清理器构造失败: %s", exc, exc_info=True)
        return None

    if file_cleaner is None:
        return None

    if not start_cleaner:
        # 未启动自动清理，仍登记 stop，以便后续致命失败时释放引用侧责任
        rollback.register(
            "file_cleaner.stop_auto_cleanup",
            file_cleaner.stop_auto_cleanup,
        )
        logger.info("文件清理器已创建（未启动自动清理）")
        return file_cleaner

    try:
        file_cleaner.start_auto_cleanup()
    except Exception as start_exc:
        logger.error("文件清理器启动失败: %s", start_exc, exc_info=True)
        # 阶段一：立即尝试停止（与致命失败回滚分开计数）
        try:
            file_cleaner.stop_auto_cleanup()
        except Exception as stop_exc:
            logger.error(
                "文件清理器启动失败且立即 stop_auto_cleanup 也失败，"
                "保留引用并由致命失败回滚再尝试一次 / 或交助手 shutdown: %s",
                stop_exc,
                exc_info=True,
            )
            rollback.register(
                "file_cleaner.stop_auto_cleanup",
                file_cleaner.stop_auto_cleanup,
            )
            return file_cleaner
        logger.warning(
            "文件清理器启动失败后已成功停止，不再交给助手重复回收"
        )
        return None

    rollback.register("file_cleaner.stop_auto_cleanup", file_cleaner.stop_auto_cleanup)
    logger.info("文件清理器初始化成功")
    return file_cleaner
