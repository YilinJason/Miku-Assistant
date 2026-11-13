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

from pathlib import Path

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

from src.core.config import Config
from src.core.file_cleaner import FileCleaner
from src.core.logging_setup import setup_app_logging
from src.core.paths import PROJECT_ROOT, RUNTIME_DATA_DIRS, ensure_runtime_dirs
from src.core.companion import (
    CompanionSettings,
    build_companion_base_system,
    parse_companion_settings,
)
from src.core.resource_coord import run_coordinated_warmup, start_ollama_warmup
from src.core.resource_settings import ResourceSettings, parse_resource_settings
from src.core.router import IntentRouter
from src.core.router_factory import build_intent_router
from src.core.safety import (
    Operation,
    SafetyPolicy,
    TrustedLocalAsrProfile,
    TrustedLocalTtsProfile,
    TrustedRvcProfile,
    _require_bool,
)
from src.core.network_endpoint import authorize_model_endpoint, endpoint_audit_label
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
from src.voice.audio.temp_files import TempFileManager


def default_tts_engine_cls(**kwargs: Any) -> Any:
    """窄默认工厂：仅在调用时导入 ``TTSEngine`` 并构造实例。"""
    from src.voice.tts.engine import TTSEngine

    return TTSEngine(**kwargs)


def default_stt_engine_cls(**kwargs: Any) -> Any:
    """窄默认工厂：仅在调用时导入 ``STTEngine`` 并构造实例。"""
    from src.voice.stt import STTEngine

    return STTEngine(**kwargs)


_LAZY_MODULE_EXPORTS: dict[str, tuple[str, str]] = {
    "TTSEngine": ("src.voice.tts.engine", "TTSEngine"),
    "STTEngine": ("src.voice.stt", "STTEngine"),
}


def __getattr__(name: str) -> object:
    spec = _LAZY_MODULE_EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = spec
    import importlib

    module = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_MODULE_EXPORTS))


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
    companion_settings: Optional[CompanionSettings] = None
    voice_autoload_enabled: bool = False
    temp_file_manager: Optional[TempFileManager] = None
    tts_engine_cls: Optional[Callable[..., Any]] = field(default=None, repr=False)
    stt_engine_cls: Optional[Callable[..., Any]] = field(default=None, repr=False)
    model_memory_admission: Optional[Any] = None
    llm_warmup_owner: Optional[Any] = None


def _type_declares_close_warmup(cls: type) -> bool:
    """仅类型 MRO 上显式声明的 ``close_warmup``；不用实例 getattr 探测 Mock。"""
    for klass in cls.__mro__:
        if "close_warmup" in klass.__dict__:
            return callable(klass.__dict__["close_warmup"])
    return False


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
                from src.core.log_privacy import exception_log_label

                self._logger.error(
                    "装配回滚失败 resource=%s err=%s",
                    name,
                    exception_log_label(exc),
                    exc_info=True,
                )


def _callable_accepts_kw(fn: Callable[..., Any], name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    if name in sig.parameters:
        return True
    return any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
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


def _voice_tts_engine_name(config: Any) -> str:
    tts_cfg = config.get("voice.tts", {}) or {}
    return str(tts_cfg.get("engine", "edge-tts"))


def _voice_stt_engine_name(config: Any) -> str:
    stt_cfg = _coerce_voice_stt_mapping(config)
    return str(stt_cfg.get("engine", "paraformer"))


def _coerce_voice_stt_mapping(config: Any) -> dict[str, Any]:
    """``voice.stt`` 缺省为 ``{}``；``None`` 与缺键等价；其它错误形状显式失败。"""
    raw = config.get("voice.stt", None)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("voice.stt must be a mapping")
    return raw


def validate_assembly_voice_stt_config(config: Any) -> None:
    """在装配副作用前校验 ``voice.stt``（含 paraformer 内层与 input_audio）。"""
    stt_cfg = _coerce_voice_stt_mapping(config)
    from src.voice.input_audio import resolve_input_audio_settings_from_stt_config

    resolve_input_audio_settings_from_stt_config(stt_cfg)
    engine = _voice_stt_engine_name(config)
    if engine != "paraformer":
        return
    from src.voice.paraformer.settings import resolve_paraformer_settings_from_stt_config

    resolve_paraformer_settings_from_stt_config(stt_cfg)
    security = config.get("security", {}) or {}
    perms = security.get("permissions") or {}
    if "local_asr_worker" in perms:
        _require_bool("security.permissions.local_asr_worker", perms["local_asr_worker"])


def _local_asr_worker_enabled_for_assembly(config: Any) -> bool:
    security = config.get("security", {}) or {}
    perms = security.get("permissions") or {}
    if "local_asr_worker" not in perms:
        return False
    return bool(perms["local_asr_worker"])


def validate_assembly_voice_tts_config(config: Any) -> None:
    """
    在日志/目录/TFM 等装配副作用之前校验 ``voice.tts``。

    选定 ``cosyvoice`` 时严格解析内层 ``voice.tts.cosyvoice``；
    ``local_tts_worker`` 若出现则必须是 bool。
    """
    engine = _voice_tts_engine_name(config)
    if engine != "cosyvoice":
        return
    tts_cfg = config.get("voice.tts", {}) or {}
    inner = tts_cfg.get("cosyvoice")
    if inner is not None and not isinstance(inner, dict):
        raise ValueError("voice.tts.cosyvoice must be a mapping when engine is cosyvoice")
    from src.voice.tts.cosyvoice_backend import (
        resolve_cosyvoice_settings_from_engine_config,
    )

    resolve_cosyvoice_settings_from_engine_config(inner)
    security = config.get("security", {}) or {}
    perms = security.get("permissions") or {}
    if "local_tts_worker" in perms:
        _require_bool("security.permissions.local_tts_worker", perms["local_tts_worker"])


def _local_tts_worker_enabled_for_assembly(config: Any) -> bool:
    """与 ``SafetyPolicy.from_app_config`` 缺省一致：仅显式 true 才尝试封存本地 TTS。"""
    security = config.get("security", {}) or {}
    perms = security.get("permissions") or {}
    if "local_tts_worker" not in perms:
        return False
    return bool(perms["local_tts_worker"])


def _try_trusted_asr_profile_for_config(config: Any) -> Optional[TrustedLocalAsrProfile]:
    """若固定布局可用则返回 ASR profile；缺失资产时返回 None（不阻塞 text 启动）。"""
    if _voice_stt_engine_name(config) != "paraformer":
        return None
    if not _local_asr_worker_enabled_for_assembly(config):
        return None
    from src.voice.paraformer.assets import revalidate_fixed_layout_paths

    try:
        revalidate_fixed_layout_paths(PROJECT_ROOT)
    except PermissionError:
        return None
    return TrustedLocalAsrProfile(repo_root=PROJECT_ROOT)


def build_assembly_safety_policy(
    config: Any,
    *,
    trusted_rvc: Optional[TrustedRvcProfile] = None,
    startup_mode: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> SafetyPolicy:
    """
    构造装配期共享 ``SafetyPolicy``。

    CosyVoice TTS profile 与历史一致：``engine=cosyvoice`` 且权限开启时在构造期尝试封存。
    Paraformer ASR profile 在固定布局存在时同样于构造期封存（与 startup_mode 无关）；
    布局缺失时仅省略 ASR profile，text/interactive 仍可用；安装资产后需重启助手。
    """
    log = logger or logging.getLogger(__name__)
    trusted_local: Optional[TrustedLocalTtsProfile] = None
    trusted_asr: Optional[TrustedLocalAsrProfile] = None
    if _voice_tts_engine_name(config) == "cosyvoice" and _local_tts_worker_enabled_for_assembly(
        config
    ):
        trusted_local = TrustedLocalTtsProfile(repo_root=PROJECT_ROOT)
    trusted_asr = _try_trusted_asr_profile_for_config(config)
    try:
        return SafetyPolicy.from_app_config(
            config,
            trusted_rvc_profile=trusted_rvc,
            trusted_local_tts_profile=trusted_local,
            trusted_local_asr_profile=trusted_asr,
        )
    except PermissionError:
        if trusted_asr is not None and trusted_local is not None:
            log.debug(
                "装配 ASR+TTS profile 组合封存失败，回退仅 TTS tag=local_asr_profile_seal_fallback"
            )
            try:
                return SafetyPolicy.from_app_config(
                    config,
                    trusted_rvc_profile=trusted_rvc,
                    trusted_local_tts_profile=trusted_local,
                    trusted_local_asr_profile=None,
                )
            except PermissionError:
                pass
        if trusted_local is None and trusted_asr is None:
            raise
        log.debug(
            "装配本地语音 profile 封存跳过 tag=local_voice_profile_seal_fallback"
        )
        return SafetyPolicy.from_app_config(
            config,
            trusted_rvc_profile=trusted_rvc,
            trusted_local_tts_profile=None,
            trusted_local_asr_profile=None,
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


def _register_stt_engine_rollback(
    rollback: _RollbackRegistry,
    stt_engine: Any,
    *,
    logger: logging.Logger,
) -> None:
    """仅登记显式 ``shutdown``；遗留无资源适配器可省略。Paraformer 须报告 complete。"""
    shutdown = getattr(stt_engine, "shutdown", None)
    if not callable(shutdown):
        return
    engine_ref = stt_engine

    def _rollback_stt() -> None:
        from src.core.log_privacy import exception_log_label
        from src.core.voice_activation import stt_shutdown_reports_complete

        try:
            if _callable_accepts_stop_rvc(shutdown):
                result = shutdown(stop_rvc=False)
            else:
                result = shutdown()
        except Exception as exc:
            logger.error(
                "装配回滚 STT shutdown 失败 err=%s",
                exception_log_label(exc),
                exc_info=True,
            )
            return
        if getattr(engine_ref, "engine_type", None) == "paraformer":
            if result is not True or not stt_shutdown_reports_complete(engine_ref):
                logger.error(
                    "装配回滚 Paraformer STT shutdown 未 complete tag=stt_rollback_incomplete"
                )

    rollback.register("stt_engine.shutdown", _rollback_stt)


def build_tts_engine(
    config: Any,
    safety_policy: SafetyPolicy,
    *,
    tts_engine_cls: Callable[..., Any] = default_tts_engine_cls,
    warm_rvc_on_init: bool = False,
    temp_file_manager: Optional[TempFileManager] = None,
) -> Any:
    """仅构造 TTS（含可选 AudioPlayer/RVC 绑定），不预热 Worker。"""
    tts_config = config.get("voice.tts", {}) or {}
    engine = tts_config.get("engine", "edge-tts")
    kwargs: dict[str, Any] = {
        "engine": engine,
        "language": tts_config.get("language", "zh-CN"),
        "rate": tts_config.get("rate", 150),
        "volume": tts_config.get("volume", 0.8),
        "voice": tts_config.get("voice", "zh-CN-XiaoxiaoNeural"),
        "rvc_config": config.get_rvc_config() if hasattr(config, "get_rvc_config") else None,
        "fallback_enabled": tts_config.get("fallback_enabled", True),
        "fallback_engine": tts_config.get("fallback_engine", "pyttsx3"),
        "pipeline_enabled": tts_config.get("pipeline_enabled", True),
        "safety_policy": safety_policy,
        "temp_file_manager": temp_file_manager,
        "warm_rvc_on_init": warm_rvc_on_init,
        "edge_prosody": tts_config.get("edge_prosody"),
    }
    if engine == "cosyvoice":
        kwargs["cosyvoice_config"] = tts_config.get("cosyvoice")
    return tts_engine_cls(**kwargs)


def build_stt_engine(
    config: Any,
    *,
    stt_engine_cls: Callable[..., Any] = default_stt_engine_cls,
    safety_policy: Optional[SafetyPolicy] = None,
    temp_file_manager: Optional[TempFileManager] = None,
) -> Any:
    from src.voice.input_audio import (
        resolve_input_audio_settings_from_stt_config,
        validate_stt_energy_threshold_value,
    )

    stt_config = _coerce_voice_stt_mapping(config)
    engine = stt_config.get("engine", "paraformer")
    kwargs: dict[str, Any] = {
        "engine": engine,
        "language": stt_config.get("language", "zh-CN"),
        "energy_threshold": validate_stt_energy_threshold_value(
            stt_config.get("energy_threshold", 4000)
        ),
        "safety_policy": safety_policy,
        "temp_file_manager": temp_file_manager,
    }
    if engine == "paraformer":
        from src.voice.paraformer.settings import resolve_paraformer_settings_from_stt_config

        kwargs["paraformer_settings"] = resolve_paraformer_settings_from_stt_config(
            stt_config
        )

    kwargs["input_audio_settings"] = resolve_input_audio_settings_from_stt_config(
        stt_config
    )
    return stt_engine_cls(**kwargs)


def build_voice_engines(
    config: Any,
    safety_policy: SafetyPolicy,
    *,
    tts_engine_cls: Callable[..., Any] = default_tts_engine_cls,
    stt_engine_cls: Callable[..., Any] = default_stt_engine_cls,
    warm_rvc_on_init: bool = False,
    temp_file_manager: Optional[TempFileManager] = None,
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
        temp_file_manager=temp_file_manager,
    )
    try:
        stt_engine = build_stt_engine(
            config,
            stt_engine_cls=stt_engine_cls,
            safety_policy=safety_policy,
            temp_file_manager=temp_file_manager,
        )
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
    tts_engine_cls: Callable[..., Any] = default_tts_engine_cls,
    stt_engine_cls: Callable[..., Any] = default_stt_engine_cls,
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

    validate_assembly_voice_tts_config(config)
    validate_assembly_voice_stt_config(config)

    # 装配边界：构造前解析受信 RVC profile；无模型则保持未封存并安全拒绝
    trusted_rvc = None
    try:
        from src.voice.rvc.infer import resolve_rvc_python

        rvc_cfg = (
            config.get_rvc_config() if hasattr(config, "get_rvc_config") else {}
        ) or {}
        python = resolve_rvc_python(rvc_cfg)
        model = rvc_cfg.get("model_path")
        if model and Path(model).is_file() and python.is_file():
            index = rvc_cfg.get("index_path")
            trusted_rvc = TrustedRvcProfile(
                python_path=python,
                model_path=model,
                index_path=index if index and Path(index).is_file() else None,
            )
    except Exception:
        # 解析失败时保持未封存；正式策略创建后不可再补封
        trusted_rvc = None

    assembly_logger = logging.getLogger("src.core.assembly")
    safety_policy = build_assembly_safety_policy(
        config,
        trusted_rvc=trusted_rvc,
        startup_mode=mode,
        logger=assembly_logger,
    )

    # memory / resource / companion 配置在任何设备工厂之前校验；未启用则零开销
    memory_settings = parse_memory_settings(config)
    memory_enabled = memory_settings.enabled
    resource_settings = parse_resource_settings(config)
    companion_settings = parse_companion_settings(config)
    companion_base_system = build_companion_base_system(companion_settings)

    log_file = setup_logging(config)

    prepare_dirs()
    for directory in RUNTIME_DATA_DIRS:
        safety_policy.register_file_delete_dir(directory)

    logger = assembly_logger
    if trusted_rvc is None:
        logger.debug("未封存 RVC profile（无模型或解析失败），相关能力将 fail closed")

    temp_file_manager = TempFileManager(safety_policy=safety_policy)

    logger.info("运行时数据目录已就绪 dirs=logs,tmp,test")
    logger.info(
        "装配启动模式=%s voice_at_assemble=%s resource.profile=%s",
        mode,
        voice_stack_at_assemble(mode),
        resource_settings.profile,
    )
    if log_file:
        # 仅记相对文件名，不落绝对路径
        logger.info(
            "崩溃排查请查看日志文件 name=%s",
            Path(log_file).name if hasattr(log_file, "name") else "miku.log",
        )
    logger.debug(
        "安全策略权限摘要 keys=%s",
        len(safety_policy.summary()),
    )

    rollback = _RollbackRegistry(logger)
    rollback.register("temp_file_manager.shutdown", temp_file_manager.shutdown)
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
        # 模型端点在任何 HTTP/设备/线程前封存；无策略旁路仅 loopback
        ollama_url = config.get("ollama.base_url", "http://localhost:11434")
        sealed_ollama = authorize_model_endpoint(
            safety_policy, ollama_url, context="assemble_ollama"
        )
        from src.core.model_memory_admission import LocalModelMemoryAdmission

        model_memory_guard = LocalModelMemoryAdmission(
            sealed_ollama, safety_policy=safety_policy
        )
        rollback.register(
            "model_memory_admission.close", model_memory_guard.close
        )
        ollama_kwargs: dict[str, Any] = {
            "base_url": sealed_ollama.base_url,
            "model": config.get("ollama.model"),
            "timeout": config.get("ollama.timeout"),
            "keep_alive": config.get("ollama.keep_alive", "30m"),
            "max_stored_turns": max_stored_turns,
        }
        if _callable_accepts_kw(ollama_client_cls, "safety_policy"):
            ollama_kwargs["safety_policy"] = safety_policy
        if _callable_accepts_kw(ollama_client_cls, "sealed_endpoint"):
            ollama_kwargs["sealed_endpoint"] = sealed_ollama
        if _callable_accepts_kw(ollama_client_cls, "foreground_warmup_wait_sec"):
            ollama_kwargs["foreground_warmup_wait_sec"] = (
                resource_settings.foreground_warmup_wait_sec
            )
        if (
            companion_base_system is not None
            and _callable_accepts_kw(ollama_client_cls, "base_system")
        ):
            ollama_kwargs["base_system"] = companion_base_system
        if _callable_accepts_kw(ollama_client_cls, "model_memory_guard"):
            ollama_kwargs["model_memory_guard"] = model_memory_guard
        llm_client = ollama_client_cls(**ollama_kwargs)
        llm_warmup_owner: Optional[Any] = None
        if _type_declares_close_warmup(type(llm_client)):
            llm_warmup_owner = llm_client

            def _rollback_close_llm_warmup(
                _owner: Any = llm_warmup_owner,
            ) -> None:
                if _owner.close_warmup(timeout=2.0) is not True:
                    raise RuntimeError("llm_warmup_owner.close_warmup incomplete")

            rollback.register(
                "llm_warmup_owner.close_warmup", _rollback_close_llm_warmup
            )
        logger.info("LLM 模块初始化成功")

        tts_cfg = config.get("voice.tts", {}) or {}
        stt_cfg = config.get("voice.stt", {}) or {}
        logger.info(
            "网络出口摘要 endpoint_kind=%s remote_model=%s insecure_http=%s "
            "cloud_stt=%s cloud_tts=%s stt_engine=%s tts_engine=%s voice_at_assemble=%s",
            endpoint_audit_label(sealed_ollama),
            safety_policy.is_allowed(Operation.REMOTE_MODEL_ENDPOINT),
            safety_policy.is_allowed(Operation.INSECURE_REMOTE_MODEL_HTTP),
            safety_policy.is_allowed(Operation.CLOUD_STT),
            safety_policy.is_allowed(Operation.CLOUD_TTS),
            stt_cfg.get("engine", "paraformer"),
            tts_cfg.get("engine", "edge-tts"),
            voice_stack_at_assemble(mode),
        )

        if memory_enabled:
            # extractor 显式 override 独立授权；null 继承已封存主 Ollama URL
            raw_ext = config.get("memory.extractor.base_url")
            if raw_ext is None or raw_ext == "":
                sealed_extractor = sealed_ollama
            else:
                sealed_extractor = authorize_model_endpoint(
                    safety_policy, raw_ext, context="assemble_memory_extractor"
                )
            make_runtime = memory_runtime_factory or MemoryRuntime.create_from_settings
            runtime_kwargs: dict[str, Any] = {"logger": logger}
            if _callable_accepts_kw(make_runtime, "safety_policy"):
                runtime_kwargs["safety_policy"] = safety_policy
            if _callable_accepts_kw(make_runtime, "sealed_endpoint"):
                runtime_kwargs["sealed_endpoint"] = sealed_extractor
            if _callable_accepts_kw(make_runtime, "model_memory_guard"):
                runtime_kwargs["model_memory_guard"] = model_memory_guard
            memory_runtime = make_runtime(memory_settings, **runtime_kwargs)
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
            tts_engine, stt_engine = build_voice_engines(
                config,
                safety_policy,
                tts_engine_cls=tts_engine_cls,
                stt_engine_cls=stt_engine_cls,
                warm_rvc_on_init=False,
                temp_file_manager=temp_file_manager,
            )
            rollback.register("tts_engine.shutdown", tts_engine.shutdown)
            _register_stt_engine_rollback(rollback, stt_engine, logger=logger)
            logger.info("TTS 模块初始化成功（voice 模式）")
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

        # text：仅 Ollama；voice Edge+RVC：装配期 defer RVC；Cosy：App ensure
        engine_name = _voice_tts_engine_name(config)
        if engine_name == "cosyvoice":
            logger.info(
                "CosyVoice 装配期零模型预热（App ensure 再 prepare_local_voice）"
            )
        elif (
            tts_engine is not None
            and voice_stack_at_assemble(mode)
        ):
            from src.core.voice_activation import edge_rvc_warmup_required

            if edge_rvc_warmup_required(tts_engine, resource_settings):
                started = start_ollama_warmup(
                    llm_client,
                    settings=resource_settings,
                    log=logger,
                )
                if warmup_events is not None:
                    warmup_events.append("ollama_start" if started else "ollama_skip")
                    warmup_events.append("rvc_deferred_app_foreground")
                logger.info(
                    "voice 装配期 defer RVC 预热；App request 再串行 Ollama→RVC"
                )
            else:
                run_coordinated_warmup(
                    llm_client=llm_client,
                    tts_engine=tts_engine,
                    settings=resource_settings,
                    log=logger,
                    events=warmup_events,
                )
        elif tts_engine is None or engine_name != "cosyvoice":
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
            companion_settings=companion_settings,
            voice_autoload_enabled=not voice_stack_at_assemble(mode),
            temp_file_manager=temp_file_manager,
            tts_engine_cls=tts_engine_cls,
            stt_engine_cls=stt_engine_cls,
            model_memory_admission=model_memory_guard,
            llm_warmup_owner=llm_warmup_owner,
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
        from src.core.log_privacy import exception_log_label

        logger.error(
            "文件清理器构造失败 err=%s",
            exception_log_label(exc),
            exc_info=True,
        )
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
        from src.core.log_privacy import exception_log_label

        logger.error(
            "文件清理器启动失败 err=%s",
            exception_log_label(start_exc),
            exc_info=True,
        )
        # 阶段一：立即尝试停止（与致命失败回滚分开计数）
        try:
            file_cleaner.stop_auto_cleanup()
        except Exception as stop_exc:
            logger.error(
                "文件清理器启动失败且立即 stop_auto_cleanup 也失败，"
                "保留引用并由致命失败回滚再尝试一次 / 或交助手 shutdown err=%s",
                exception_log_label(stop_exc),
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
