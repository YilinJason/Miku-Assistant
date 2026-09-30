"""
助手默认依赖装配。

将配置加载、日志、运行时目录、引擎创建与清理器启动集中于此，
与 ``MikuAssistant`` 业务编排分离。注入测试路径应使用
``MikuAssistant.from_dependencies``，不要调用本模块的默认装配。

装配失败时按登记顺序的逆序回收本层已成功创建的 TTS / 角色 / 清理器
（仅 ``stop_auto_cleanup``，不调用 ``cleanup_now``）。构造器抛错且未返回
对象时，其内部泄漏不在本模块回收范围内。

Round 2：``startup_mode`` 在装配前可知；``text``/``interactive`` 不构造
STT/TTS/AudioPlayer/RVC；``voice`` 同步构造；Ollama/RVC 预热按资源配置串行。
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

from src.core.config import Config
from src.core.file_cleaner import FileCleaner
from src.core.logging_setup import setup_app_logging
from src.core.paths import RUNTIME_DATA_DIRS, ensure_runtime_dirs, runtime_layout_summary
from src.core.resource_coord import run_coordinated_warmup
from src.core.resource_settings import ResourceSettings, parse_resource_settings
from src.core.router import IntentRouter
from src.core.router_factory import build_intent_router
from src.core.safety import SafetyPolicy
from src.core.startup_mode import (
    StartupMode,
    normalize_startup_mode,
    voice_stack_at_assemble,
)
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
    startup_mode: StartupMode = "interactive"
    resource_settings: Optional[ResourceSettings] = None
    voice_autoload_enabled: bool = False
    tts_engine_cls: Optional[Callable[..., Any]] = field(default=None, repr=False)
    stt_engine_cls: Optional[Callable[..., Any]] = field(default=None, repr=False)


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


def _callable_accepts_stop_rvc(shutdown: Callable[..., Any]) -> bool:
    """调用前判断是否接受 ``stop_rvc``；不得用运行时 TypeError 决定是否重试。"""
    try:
        signature = inspect.signature(shutdown)
    except (TypeError, ValueError):
        return False
    params = signature.parameters
    if "stop_rvc" in params:
        return True
    return any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _shutdown_engine(engine: Any, *, stop_rvc: bool = True) -> None:
    """
    尽力关闭引擎；忽略单项失败。单次回收最多调用一次 ``shutdown``。

    未发布且已有正式 TTS 时传 ``stop_rvc=False``，避免误停其 Worker；
    本轮唯一未发布 TTS 应传 ``stop_rvc=True`` 以回收可能已预热的 Worker。
    """
    if engine is None:
        return
    shutdown = getattr(engine, "shutdown", None)
    if not callable(shutdown):
        return
    try:
        if _callable_accepts_stop_rvc(shutdown):
            shutdown(stop_rvc=stop_rvc)
        else:
            shutdown()
    except Exception:
        logging.getLogger(__name__).debug(
            "voice engine shutdown during rollback failed", exc_info=True
        )


def build_tts_engine(
    config: Any,
    safety_policy: SafetyPolicy,
    *,
    tts_engine_cls: Callable[..., Any] = TTSEngine,
    warm_rvc_on_init: bool = False,
) -> Any:
    """仅构造 TTS（含可选 AudioPlayer/RVC 绑定），不预热 Worker。"""
    tts_config = config.get("voice.tts", {}) or {}
    return tts_engine_cls(
        engine=tts_config.get("engine", "edge-tts"),
        language=tts_config.get("language", "zh-CN"),
        rate=tts_config.get("rate", 150),
        volume=tts_config.get("volume", 0.8),
        voice=tts_config.get("voice", "zh-CN-XiaoxiaoNeural"),
        rvc_config=config.get_rvc_config() if hasattr(config, "get_rvc_config") else None,
        fallback_enabled=tts_config.get("fallback_enabled", True),
        fallback_engine=tts_config.get("fallback_engine", "pyttsx3"),
        pipeline_enabled=tts_config.get("pipeline_enabled", True),
        safety_policy=safety_policy,
        warm_rvc_on_init=warm_rvc_on_init,
    )


def build_stt_engine(
    config: Any,
    *,
    stt_engine_cls: Callable[..., Any] = STTEngine,
) -> Any:
    stt_config = config.get("voice.stt", {}) or {}
    return stt_engine_cls(
        engine=stt_config.get("engine", "speech_recognition"),
        language=stt_config.get("language", "zh-CN"),
        energy_threshold=stt_config.get("energy_threshold", 4000),
    )


def build_voice_engines(
    config: Any,
    safety_policy: SafetyPolicy,
    *,
    tts_engine_cls: Callable[..., Any] = TTSEngine,
    stt_engine_cls: Callable[..., Any] = STTEngine,
    warm_rvc_on_init: bool = False,
) -> Tuple[Any, Any]:
    """
    构造 TTS/STT；默认不在构造期预热 RVC。

    事务式：STT 失败时 exactly-once 关闭本轮已创建的 TTS，避免泄漏。
    """
    tts_engine = build_tts_engine(
        config,
        safety_policy,
        tts_engine_cls=tts_engine_cls,
        warm_rvc_on_init=warm_rvc_on_init,
    )
    try:
        stt_engine = build_stt_engine(config, stt_engine_cls=stt_engine_cls)
    except Exception:
        _shutdown_engine(tts_engine)
        raise
    return tts_engine, stt_engine


def assemble_default(
    config_path: Optional[str] = None,
    *,
    startup_mode: object = "interactive",
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
    warmup_events: Optional[List[str]] = None,
) -> AssembledParts:
    """
    按配置创建并连接默认运行时组件（含副作用）。

    ``startup_mode``：
    - ``text`` / ``interactive``：不构造 STT/TTS/RVC；可经助手懒加载。
    - ``voice``：装配期构造语音栈。

    ``memory.enabled=false``（默认）时不创建记忆库、不启动提取线程、不包装 LLM。
    启用时先校验 memory 配置，再创建 runtime，并在后续组件失败时逆序回滚。
    """
    mode = normalize_startup_mode(startup_mode)
    load_config = config_loader or Config
    setup_logging = logging_setup or setup_app_logging
    prepare_dirs = dir_preparer or ensure_runtime_dirs
    cleaner_factory = file_cleaner_from_config or FileCleaner.from_config
    build_router = router_builder or build_intent_router

    config = load_config(config_path)
    safety_policy = SafetyPolicy.from_app_config(config)

    # memory / resource 配置在任何设备工厂之前校验；未启用则零开销
    memory_settings = parse_memory_settings(config)
    memory_enabled = memory_settings.enabled
    resource_settings = parse_resource_settings(config)

    log_file = setup_logging(config)

    prepare_dirs()
    for directory in RUNTIME_DATA_DIRS:
        safety_policy.register_file_delete_dir(directory)

    logger = logging.getLogger("src.core.assembly")
    logger.info(runtime_layout_summary())
    logger.info(
        "装配启动模式=%s voice_at_assemble=%s resource.profile=%s",
        mode,
        voice_stack_at_assemble(mode),
        resource_settings.profile,
    )
    if log_file:
        logger.info("崩溃排查请查看日志文件: %s", log_file)
    logger.debug("安全策略: %s", safety_policy.summary())

    rollback = _RollbackRegistry(logger)
    memory_runtime = None
    tts_engine = None
    stt_engine = None

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

        if voice_stack_at_assemble(mode):
            # 构造期不预热 RVC；交由串行协调器，避免与 Ollama 并行抢 GPU
            tts_config = config.get("voice.tts", {}) or {}
            tts_engine = tts_engine_cls(
                engine=tts_config.get("engine", "edge-tts"),
                language=tts_config.get("language", "zh-CN"),
                rate=tts_config.get("rate", 150),
                volume=tts_config.get("volume", 0.8),
                voice=tts_config.get("voice", "zh-CN-XiaoxiaoNeural"),
                rvc_config=(
                    config.get_rvc_config()
                    if hasattr(config, "get_rvc_config")
                    else None
                ),
                fallback_enabled=tts_config.get("fallback_enabled", True),
                fallback_engine=tts_config.get("fallback_engine", "pyttsx3"),
                pipeline_enabled=tts_config.get("pipeline_enabled", True),
                safety_policy=safety_policy,
                warm_rvc_on_init=False,
            )
            rollback.register("tts_engine.shutdown", tts_engine.shutdown)
            logger.info("TTS 模块初始化成功（voice 模式）")

            stt_config = config.get("voice.stt", {}) or {}
            stt_engine = stt_engine_cls(
                engine=stt_config.get("engine", "speech_recognition"),
                language=stt_config.get("language", "zh-CN"),
                energy_threshold=stt_config.get("energy_threshold", 4000),
            )
            logger.info("STT 模块初始化成功（voice 模式）")
        else:
            logger.info(
                "跳过 TTS/STT/RVC 构造（startup_mode=%s）；首次进入语音时懒加载",
                mode,
            )

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

        # text：仅 Ollama；voice：串行 Ollama↔RVC；interactive 延迟到懒加载
        run_coordinated_warmup(
            llm_client=llm_client,
            tts_engine=tts_engine,
            settings=resource_settings,
            log=logger,
            events=warmup_events,
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
            startup_mode=mode,
            resource_settings=resource_settings,
            voice_autoload_enabled=not voice_stack_at_assemble(mode),
            tts_engine_cls=tts_engine_cls,
            stt_engine_cls=stt_engine_cls,
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
