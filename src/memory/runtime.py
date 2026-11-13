"""长期记忆运行时：配置校验、组件所有权与关闭。"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from src.memory.aware_client import MemoryAwareLLMClient
from src.memory.context_format import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ENTRIES,
    MAX_MAX_CHARS,
    MAX_MAX_ENTRIES,
)
from src.memory.errors import MemoryValidationError
from src.memory.ingestion import (
    DEFAULT_MAX_EXTRACTIONS_PER_TURN,
    DEFAULT_QUEUE_CAPACITY,
    MAX_MAX_EXTRACTIONS_PER_TURN,
    MAX_QUEUE_CAPACITY,
    MemoryIngestionService,
)
from src.memory.lexical_recall import DEFAULT_CANDIDATE_LIMIT, MAX_CANDIDATE_LIMIT
from src.memory.admission import ExtractionAdmissionGate
from src.memory.models import MemoryPruneResult
from src.memory.ollama_extractor import (
    DEFAULT_NUM_PREDICT,
    KeepAliveValue,
    MAX_NUM_PREDICT,
    OllamaMemoryExtractor,
    normalize_keep_alive,
    normalize_optional_num_ctx,
    require_extractor_positive_int,
)
from src.memory.paths import default_memory_db_path
from src.memory.query import MemoryQueryService
from src.memory.service_models import IngestionStopStatus
from src.memory.sqlite_store import SqliteMemoryStore
from src.memory.store import MemoryStore


def _require_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise MemoryValidationError(f"{name} must be bool")
    return value


def _require_positive_int(name: str, value: Any, *, hard_max: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MemoryValidationError(f"{name} must be a positive int")
    if value > hard_max:
        raise MemoryValidationError(f"{name} must be <= {hard_max}")
    return value


def _require_finite_number(name: str, value: Any, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryValidationError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise MemoryValidationError(f"{name} must be finite")
    if number < minimum:
        raise MemoryValidationError(f"{name} must be >= {minimum}")
    return number


def _default_disabled_settings() -> "MemorySettings":
    """未启用时的安全默认快照；不读损坏子配置，不创建资源。"""
    return MemorySettings(
        enabled=False,
        db_path=default_memory_db_path(),
        max_entries=DEFAULT_MAX_ENTRIES,
        max_chars=DEFAULT_MAX_CHARS,
        candidate_limit=DEFAULT_CANDIDATE_LIMIT,
        queue_capacity=DEFAULT_QUEUE_CAPACITY,
        max_extractions_per_turn=DEFAULT_MAX_EXTRACTIONS_PER_TURN,
        close_drain=True,
        close_timeout=5.0,
        extractor_base_url="http://localhost:11434",
        extractor_model="Miku_Assistant-emotion:latest",
        extractor_timeout=60.0,
        extractor_keep_alive="30m",
        extractor_num_ctx=None,
        extractor_num_predict=DEFAULT_NUM_PREDICT,
        retention_enabled=False,
        retention_max_records=2000,
        retention_max_age_days=365.0,
        retention_maintenance_interval_hours=24.0,
        retention_preserve_user_confirmed=True,
        retention_batch_limit=64,
    )


@dataclass(frozen=True)
class MemorySettings:
    """已校验的 memory 配置快照。"""

    enabled: bool
    db_path: Path
    max_entries: int
    max_chars: int
    candidate_limit: int
    queue_capacity: int
    max_extractions_per_turn: int
    close_drain: bool
    close_timeout: float
    extractor_base_url: str
    extractor_model: str
    extractor_timeout: float
    extractor_keep_alive: KeepAliveValue
    extractor_num_ctx: Optional[int] = None
    extractor_num_predict: int = DEFAULT_NUM_PREDICT
    retention_enabled: bool = False
    retention_max_records: int = 2000
    retention_max_age_days: float = 365.0
    retention_maintenance_interval_hours: float = 24.0
    retention_preserve_user_confirmed: bool = True
    retention_batch_limit: int = 64


def parse_memory_settings(config: Any) -> MemorySettings:
    """
    解析并校验 ``memory.*``。

    ``enabled=false`` 时只校验开关，返回安全默认快照，不因休眠子配置破坏启动。
    ``enabled=true`` 时严格类型校验（拒绝 bool/float/数字字符串冒充 int）。
    """
    raw_enabled = config.get("memory.enabled", False)
    if raw_enabled is None:
        raw_enabled = False
    enabled = _require_bool("memory.enabled", raw_enabled)
    if not enabled:
        return _default_disabled_settings()

    raw_path = config.get("memory.db_path")
    if raw_path is None or raw_path == "":
        db_path = default_memory_db_path()
    else:
        if not isinstance(raw_path, (str, Path)):
            raise MemoryValidationError("memory.db_path must be str or Path")
        resolve = getattr(config, "resolve_path", None)
        path_str = str(raw_path)
        if callable(resolve):
            resolved = resolve(path_str)
            db_path = Path(resolved if resolved else path_str)
        else:
            db_path = Path(path_str)

    max_entries = _require_positive_int(
        "memory.recall.max_entries",
        config.get("memory.recall.max_entries", DEFAULT_MAX_ENTRIES),
        hard_max=MAX_MAX_ENTRIES,
    )
    max_chars = _require_positive_int(
        "memory.recall.max_chars",
        config.get("memory.recall.max_chars", DEFAULT_MAX_CHARS),
        hard_max=MAX_MAX_CHARS,
    )
    candidate_limit = _require_positive_int(
        "memory.recall.candidate_limit",
        config.get("memory.recall.candidate_limit", DEFAULT_CANDIDATE_LIMIT),
        hard_max=MAX_CANDIDATE_LIMIT,
    )
    queue_capacity = _require_positive_int(
        "memory.ingestion.queue_capacity",
        config.get("memory.ingestion.queue_capacity", DEFAULT_QUEUE_CAPACITY),
        hard_max=MAX_QUEUE_CAPACITY,
    )
    max_extractions = _require_positive_int(
        "memory.ingestion.max_extractions_per_turn",
        config.get(
            "memory.ingestion.max_extractions_per_turn",
            DEFAULT_MAX_EXTRACTIONS_PER_TURN,
        ),
        hard_max=MAX_MAX_EXTRACTIONS_PER_TURN,
    )
    close_drain_raw = config.get("memory.ingestion.close_drain", True)
    if close_drain_raw is None:
        close_drain_raw = True
    close_drain = _require_bool("memory.ingestion.close_drain", close_drain_raw)
    close_timeout = _require_finite_number(
        "memory.ingestion.close_timeout",
        config.get("memory.ingestion.close_timeout", 5.0),
        minimum=0.0,
    )

    base_url = config.get("memory.extractor.base_url")
    if base_url is None or base_url == "":
        base_url = config.get("ollama.base_url", "http://localhost:11434")
    if not isinstance(base_url, str) or not base_url.strip():
        raise MemoryValidationError("memory.extractor.base_url must be non-empty str")

    model = config.get("memory.extractor.model")
    if model is None or model == "":
        model = config.get("ollama.model", "Miku_Assistant-emotion:latest")
    if not isinstance(model, str) or not model.strip():
        raise MemoryValidationError("memory.extractor.model must be non-empty str")

    timeout_raw = config.get("memory.extractor.timeout")
    if timeout_raw is None or timeout_raw == "":
        timeout_raw = config.get("ollama.timeout", 60)
    extractor_timeout = _require_finite_number(
        "memory.extractor.timeout", timeout_raw, minimum=0.001
    )

    keep_alive = config.get("memory.extractor.keep_alive")
    if keep_alive is None or keep_alive == "":
        keep_alive = config.get("ollama.keep_alive", "30m")
    try:
        keep_alive_norm = normalize_keep_alive(keep_alive)
    except MemoryValidationError as exc:
        raise MemoryValidationError(
            f"memory.extractor.keep_alive invalid: {exc}"
        ) from exc

    # null / 缺省 → 继承模型 context（不写入 options.num_ctx）
    # 注意：Config.get(key, default) 会把 YAML null 塌成 default，故此处不加 fallback 默认整数
    num_ctx_raw = config.get("memory.extractor.num_ctx")
    num_ctx = normalize_optional_num_ctx(
        "memory.extractor.num_ctx", num_ctx_raw
    )
    num_predict_raw = config.get(
        "memory.extractor.num_predict", DEFAULT_NUM_PREDICT
    )
    if num_predict_raw is None or num_predict_raw == "":
        num_predict_raw = DEFAULT_NUM_PREDICT
    num_predict = require_extractor_positive_int(
        "memory.extractor.num_predict",
        num_predict_raw,
        hard_max=MAX_NUM_PREDICT,
    )

    raw_ret_en = config.get("memory.retention.enabled", False)
    if raw_ret_en is None:
        raw_ret_en = False
    retention_enabled = _require_bool("memory.retention.enabled", raw_ret_en)
    retention_max_records = _require_positive_int(
        "memory.retention.max_records",
        config.get("memory.retention.max_records", 2000),
        hard_max=100_000,
    )
    retention_max_age = _require_finite_number(
        "memory.retention.max_age_days",
        config.get("memory.retention.max_age_days", 365.0),
        minimum=0.0,
    )
    retention_interval = _require_finite_number(
        "memory.retention.maintenance_interval_hours",
        config.get("memory.retention.maintenance_interval_hours", 24.0),
        minimum=0.0,
    )
    raw_preserve = config.get("memory.retention.preserve_user_confirmed", True)
    if raw_preserve is None:
        raw_preserve = True
    retention_preserve = _require_bool(
        "memory.retention.preserve_user_confirmed", raw_preserve
    )
    retention_batch = _require_positive_int(
        "memory.retention.batch_limit",
        config.get("memory.retention.batch_limit", 64),
        hard_max=1000,
    )

    return MemorySettings(
        enabled=True,
        db_path=db_path,
        max_entries=max_entries,
        max_chars=max_chars,
        candidate_limit=candidate_limit,
        queue_capacity=queue_capacity,
        max_extractions_per_turn=max_extractions,
        close_drain=close_drain,
        close_timeout=close_timeout,
        extractor_base_url=base_url.strip(),
        extractor_model=model.strip(),
        extractor_timeout=extractor_timeout,
        extractor_keep_alive=keep_alive_norm,
        extractor_num_ctx=num_ctx,
        extractor_num_predict=num_predict,
        retention_enabled=retention_enabled,
        retention_max_records=retention_max_records,
        retention_max_age_days=retention_max_age,
        retention_maintenance_interval_hours=retention_interval,
        retention_preserve_user_confirmed=retention_preserve,
        retention_batch_limit=retention_batch,
    )


class MemoryRuntime:
    """
    集中持有 store / query / ingestion，并拥有唯一关闭入口。

    注入路径可传入已有组件；默认装配路径 ``owns_store=True``。
    """

    def __init__(
        self,
        store: MemoryStore,
        query: MemoryQueryService,
        ingestion: MemoryIngestionService,
        *,
        settings: MemorySettings,
        owns_store: bool = True,
        admission_gate: Optional[ExtractionAdmissionGate] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.store = store
        self.query = query
        self.ingestion = ingestion
        self.settings = settings
        self._owns_store = owns_store
        self._admission_gate = admission_gate
        self._logger = logger or logging.getLogger(__name__)
        self._closed = False
        self._closing = False
        self._started = False
        self._ingestion_stopped = False
        self._store_closed = False
        self._last_stop_status: Optional[IngestionStopStatus] = None
        self._maintenance_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        # 串行化整个 close 事务；不得在持有 lifecycle_lock 时做阻塞 close
        self._close_gate = threading.Lock()
        self._last_maintenance_mono: Optional[float] = None
        self._maintenance_timer: Optional[threading.Timer] = None
        self._maintenance_generation = 0
        self._timer_factory = threading.Timer

    @property
    def owns_store(self) -> bool:
        return self._owns_store

    @property
    def admission_gate(self) -> Optional[ExtractionAdmissionGate]:
        return self._admission_gate

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def last_stop_status(self) -> Optional[IngestionStopStatus]:
        return self._last_stop_status

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise MemoryValidationError("cannot start a closed MemoryRuntime")
            if self._closing:
                raise MemoryValidationError("cannot start while MemoryRuntime is closing")
            if not self._started:
                self.ingestion.start()
                self._started = True
                self._ingestion_stopped = False
            # 幂等：任意时刻最多一个维护 Timer
            self._ensure_maintenance_timer_locked()

    def wrap_llm(self, llm_client: Any) -> MemoryAwareLLMClient:
        return MemoryAwareLLMClient(
            llm_client,
            self.query,
            self.ingestion,
            max_entries=self.settings.max_entries,
            max_chars=self.settings.max_chars,
            candidate_limit=self.settings.candidate_limit,
            admission_gate=self._admission_gate,
            logger=self._logger,
        )

    def wait_until_idle(self, timeout: float) -> bool:
        """代理 ingestion 有界空闲等待；供诊断等公共调用方使用。"""
        return self.ingestion.wait_until_idle(timeout)

    def maybe_run_maintenance(
        self,
        *,
        force: bool = False,
        now: Optional[datetime] = None,
    ) -> Optional[MemoryPruneResult]:
        """
        低干扰有界 prune；仅当 ingestion idle 且原子获得维护 lease 时运行。

        永不在此路径执行 VACUUM。``force=True`` 仍要求 idle + 维护 lease。
        """
        if self._closed or self._closing or not self.settings.retention_enabled:
            return None
        if not self._maintenance_lock.acquire(blocking=False):
            return None
        acquired_lease = False
        gate = self._admission_gate
        try:
            if self._closed or self._closing:
                return None
            if not force:
                interval_s = (
                    self.settings.retention_maintenance_interval_hours * 3600.0
                )
                if interval_s > 0 and self._last_maintenance_mono is not None:
                    elapsed = time.monotonic() - self._last_maintenance_mono
                    if elapsed < interval_s:
                        return None

            idle_fn = getattr(self.ingestion, "is_idle", None)
            if callable(idle_fn):
                if not idle_fn():
                    return None
            else:
                if not self.ingestion.wait_until_idle(0.0):
                    return None

            if gate is None:
                return None
            acquire = getattr(gate, "try_acquire_maintenance", None)
            release = getattr(gate, "release_maintenance", None)
            if not callable(acquire) or not callable(release):
                self._logger.warning(
                    "memory retention skipped: gate lacks maintenance lease API"
                )
                return None
            if not acquire():
                return None
            acquired_lease = True

            if callable(idle_fn) and not idle_fn():
                return None

            prune = getattr(self.store, "prune_for_retention", None)
            if not callable(prune):
                return None
            result = prune(
                max_records=self.settings.retention_max_records,
                max_age_days=self.settings.retention_max_age_days,
                preserve_user_confirmed=self.settings.retention_preserve_user_confirmed,
                batch_limit=self.settings.retention_batch_limit,
                now=now,
            )
            self._last_maintenance_mono = time.monotonic()
            if result is not None:
                if getattr(result, "deleted", 0):
                    self._logger.info(
                        "memory retention prune deleted=%s preserved=%s truncated=%s",
                        result.deleted,
                        result.preserved_user_confirmed,
                        result.truncated_batch,
                    )
                if getattr(result, "over_capacity_unresolvable", False):
                    self._logger.warning(
                        "memory retention over capacity but no deletable records "
                        "(protected USER_CONFIRMED dominates)"
                    )
            return result
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 — 失败开放
            self._logger.error(
                "memory retention maintenance failed err=%s", type(exc).__name__
            )
            return None
        finally:
            if acquired_lease and gate is not None:
                release_fn = getattr(gate, "release_maintenance", None)
                if callable(release_fn):
                    try:
                        release_fn()
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as release_exc:  # noqa: BLE001
                        self._logger.error(
                            "memory maintenance lease release failed err=%s",
                            type(release_exc).__name__,
                        )
            self._maintenance_lock.release()

    def vacuum_offline(self) -> None:
        """显式离线 VACUUM；禁止从用户请求热路径调用。"""
        vacuum = getattr(self.store, "vacuum_offline", None)
        if not callable(vacuum):
            raise MemoryValidationError("store does not support vacuum_offline")
        vacuum()

    def _ensure_maintenance_timer_locked(self) -> None:
        """调用方须持有 ``_lifecycle_lock``。"""
        if self._closed or self._closing or not self.settings.retention_enabled:
            return
        if self._maintenance_timer is not None:
            return
        interval_s = self.settings.retention_maintenance_interval_hours * 3600.0
        if interval_s <= 0:
            return
        generation = self._maintenance_generation
        timer = self._timer_factory(
            interval_s, self._maintenance_timer_tick, args=(generation,)
        )
        timer.daemon = True
        self._maintenance_timer = timer
        timer.start()

    def _schedule_maintenance_timer(self) -> None:
        with self._lifecycle_lock:
            self._ensure_maintenance_timer_locked()

    def _maintenance_timer_tick(self, generation: int) -> None:
        with self._lifecycle_lock:
            if (
                self._closed
                or self._closing
                or generation != self._maintenance_generation
            ):
                return
            # 回调开始时清空引用，避免与 close/cancel 竞态覆盖
            if self._maintenance_timer is threading.current_thread():
                self._maintenance_timer = None
            elif self._maintenance_timer is not None:
                # Timer 对象不是 Thread 本身；清除以便重排
                self._maintenance_timer = None
        try:
            self.maybe_run_maintenance()
        finally:
            with self._lifecycle_lock:
                if (
                    self._closed
                    or self._closing
                    or generation != self._maintenance_generation
                ):
                    return
                self._ensure_maintenance_timer_locked()

    def _cancel_maintenance_timer_locked(self) -> None:
        """调用方须持有 ``_lifecycle_lock``；bump generation 使旧回调失效。"""
        self._maintenance_generation += 1
        timer = self._maintenance_timer
        self._maintenance_timer = None
        if timer is None:
            return
        timer.cancel()
        # 不得 join 当前回调线程
        current = threading.current_thread()
        timer_thread = getattr(timer, "thread", None) or timer
        if timer_thread is not current and hasattr(timer, "join"):
            try:
                timer.join(timeout=1.0)
            except RuntimeError:
                pass

    def close(
        self,
        *,
        drain: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> IngestionStopStatus:
        # 超时校验在串行锁外先做，避免非法参数占住 close 通道
        drain_flag = self.settings.close_drain if drain is None else drain
        if timeout is None:
            timeout_f = self.settings.close_timeout
        else:
            timeout_f = _require_finite_number(
                "timeout", timeout, minimum=0.0
            )

        with self._close_gate:
            # 已最终关闭：返回真实上次状态，禁止凭空构造 drained=True
            if self._closed:
                if self._last_stop_status is None:
                    raise MemoryValidationError(
                        "MemoryRuntime marked closed without stop status"
                    )
                return self._last_stop_status

            with self._lifecycle_lock:
                self._closing = True
                self._cancel_maintenance_timer_locked()

            try:
                if not self._ingestion_stopped:
                    status = self.ingestion.close(
                        drain=drain_flag, timeout=timeout_f
                    )
                    self._last_stop_status = status
                    if status.worker_alive:
                        self._logger.error(
                            "memory runtime close incomplete "
                            "worker_alive=True pending=%s",
                            status.pending,
                        )
                        with self._lifecycle_lock:
                            self._closing = False
                        return status
                    self._ingestion_stopped = True
                else:
                    status = self._last_stop_status
                    if status is None:
                        raise MemoryValidationError(
                            "ingestion already stopped without recorded status"
                        )

                if self._owns_store and not self._store_closed:
                    closer = getattr(self.store, "close", None)
                    if callable(closer):
                        closer()
                    self._store_closed = True
                    self._owns_store = False

                with self._lifecycle_lock:
                    self._closed = True
                    self._closing = False
                    self._started = False
                return status
            except (KeyboardInterrupt, SystemExit):
                with self._lifecycle_lock:
                    self._closing = False
                raise
            except Exception as exc:
                self._logger.error(
                    "memory runtime close failed err=%s", type(exc).__name__
                )
                with self._lifecycle_lock:
                    self._closing = False
                raise

    @classmethod
    def create_from_settings(
        cls,
        settings: MemorySettings,
        *,
        store_factory: Optional[Callable[..., MemoryStore]] = None,
        extractor_factory: Optional[Callable[..., Any]] = None,
        ingestion_factory: Optional[Callable[..., MemoryIngestionService]] = None,
        query_factory: Optional[Callable[..., MemoryQueryService]] = None,
        admission_gate: Optional[ExtractionAdmissionGate] = None,
        logger: Optional[logging.Logger] = None,
        safety_policy: Optional[Any] = None,
        sealed_endpoint: Optional[Any] = None,
        model_memory_guard: Optional[Any] = None,
    ) -> "MemoryRuntime":
        from src.memory.admission import ForegroundAwareAdmissionGate

        log = logger or logging.getLogger(__name__)
        make_store = store_factory or SqliteMemoryStore
        make_extractor = extractor_factory or OllamaMemoryExtractor
        make_ingestion = ingestion_factory or MemoryIngestionService
        make_query = query_factory or MemoryQueryService
        gate = admission_gate if admission_gate is not None else ForegroundAwareAdmissionGate()
        store: Optional[MemoryStore] = None
        try:
            store = make_store(settings.db_path, logger=log)
            extractor_kwargs: dict[str, Any] = {
                "base_url": settings.extractor_base_url,
                "model": settings.extractor_model,
                "timeout": settings.extractor_timeout,
                "keep_alive": settings.extractor_keep_alive,
                "max_extractions": settings.max_extractions_per_turn,
                "num_ctx": settings.extractor_num_ctx,
                "num_predict": settings.extractor_num_predict,
                "logger": log,
            }
            # 工厂可忽略未知关键字；默认 OllamaMemoryExtractor 接受策略
            if extractor_factory is None:
                extractor_kwargs["safety_policy"] = safety_policy
                extractor_kwargs["sealed_endpoint"] = sealed_endpoint
                extractor_kwargs["model_memory_guard"] = model_memory_guard
            extractor = make_extractor(**extractor_kwargs)
            ingestion = make_ingestion(
                store,
                extractor,
                queue_capacity=settings.queue_capacity,
                max_extractions_per_turn=settings.max_extractions_per_turn,
                owns_store=False,
                admission_gate=gate,
                logger=log,
            )
            query = make_query(store, logger=log)
            return cls(
                store,
                query,
                ingestion,
                settings=settings,
                owns_store=True,
                admission_gate=gate,
                logger=log,
            )
        except BaseException:
            if store is not None:
                closer = getattr(store, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as cleanup_exc:
                        log.error(
                            "memory store cleanup after create failure err=%s",
                            type(cleanup_exc).__name__,
                        )
            raise
