"""
临时文件安全删除

TTS / RVC / STT 链路产生的临时音频在播放后清理。删除采用
「白名单目录 + 本进程 create 登记的精确文件身份」双条件；仅目录白名单不足。
延迟删除使用单一有界队列与一个后台 Worker，禁止一文件一线程无限增长。
Worker 等待均可被 shutdown 中断。
"""
from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple, Union

from src.core.paths import TMP_DIR
from src.core.safety import (
    FileIdentity,
    Operation,
    SafetyPolicy,
    TempFileShutdownStatus,
    capture_file_identity,
    identities_match,
)

DEFAULT_DELAYED_QUEUE_CAPACITY = 64
MAX_DELAYED_QUEUE_CAPACITY = 1024
DEFAULT_OWNED_CAPACITY = 128
MAX_OWNED_CAPACITY = 1024
MAX_IO_LEASE_OWNERS = 8
MAX_IO_PATHS_PER_LEASE = 2
# 前台同步删除：最多快速尝试次数，避免长时间卡住播放路径
FOREGROUND_REMOVE_ATTEMPTS = 2

@dataclass(frozen=True)
class _PendingDeleteRecord:
    identity: FileIdentity
    due_at: float
    remaining_attempts: int
    attempts_made: int = 0


class OwnedCapacityExceededError(RuntimeError):
    """登记所有权时已达硬上限；不创建文件、不修改外部路径。"""


class TempFileManager:
    """带文件身份所有权登记、重试与安全策略检查的临时文件清理器。"""

    def __init__(
        self,
        safety_policy: Optional[SafetyPolicy] = None,
        *,
        delayed_queue_capacity: int = DEFAULT_DELAYED_QUEUE_CAPACITY,
        owned_capacity: int = DEFAULT_OWNED_CAPACITY,
        default_dir: Optional[Union[str, Path]] = None,
    ):
        """
        初始化临时文件管理器。

        Args:
            safety_policy: 安全策略；删除路径须在白名单内且具备 ``FILE_DELETE`` 权限。
            delayed_queue_capacity: 延迟删除队列容量；满时保留精确待删意图由 Worker 推进。
            owned_capacity: 同时登记的所有权条目硬上限（1..1024）。
            default_dir: ``create`` 默认目录；默认 ``data/tmp``。
        """
        if (
            not isinstance(delayed_queue_capacity, int)
            or isinstance(delayed_queue_capacity, bool)
            or delayed_queue_capacity < 1
        ):
            raise ValueError("delayed_queue_capacity must be a positive int")
        if delayed_queue_capacity > MAX_DELAYED_QUEUE_CAPACITY:
            raise ValueError(
                f"delayed_queue_capacity must be <= {MAX_DELAYED_QUEUE_CAPACITY}"
            )
        if (
            not isinstance(owned_capacity, int)
            or isinstance(owned_capacity, bool)
            or owned_capacity < 1
            or owned_capacity > MAX_OWNED_CAPACITY
        ):
            raise ValueError(
                f"owned_capacity must be a strict int in 1..{MAX_OWNED_CAPACITY}"
            )

        self.logger = logging.getLogger(__name__)
        self.safety_policy = safety_policy or SafetyPolicy()
        self._default_dir = Path(default_dir) if default_dir is not None else TMP_DIR
        self._owned_capacity = int(owned_capacity)
        # 管理器默认根目录必须可删；登记幂等，不触发扫盘
        self.safety_policy.register_file_delete_dir(self._default_dir)
        # path -> 创建时捕获的文件身份（不只是路径字符串）
        self._owned: Dict[str, FileIdentity] = {}
        self._pending_cleanup: Dict[str, _PendingDeleteRecord] = {}
        self._io_pin_ref: Dict[str, int] = {}
        self._io_leases: Dict[str, FrozenSet[str]] = {}
        self._lock = threading.RLock()
        self._delayed_queue: queue.Queue[
            Tuple[str, FileIdentity, float, int]
        ] = queue.Queue(maxsize=delayed_queue_capacity)
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self._worker_creator_pending = False
        self._worker_start_unknown = False
        self._worker_start_attempted = False
        self._worker_entered = threading.Event()
        self._worker_terminal = threading.Event()
        self._worker_wakeup = threading.Event()
        self._inflight_paths: Set[str] = set()
        self._queued_paths: Set[str] = set()
        self._retry_exhausted: Set[str] = set()
        self._delete_claims: Dict[str, FileIdentity] = {}
        self._stop = threading.Event()
        self._shutdown_done = False
        self._inflight = 0

    @staticmethod
    def _is_file_locked_error(exc: BaseException) -> bool:
        if isinstance(exc, PermissionError):
            return True
        if isinstance(exc, OSError):
            if getattr(exc, "winerror", None) == 32:
                return True
            if getattr(exc, "errno", None) in (13, 32):
                return True
        return False

    def _normalize_key(self, file_path: Union[str, Path]) -> Optional[str]:
        try:
            return str(Path(file_path).resolve())
        except OSError:
            return None

    def owned_count(self) -> int:
        """当前仍登记的所有权条目数（可观察契约）。"""
        with self._lock:
            return len(self._owned)

    def pending_cleanup_count(self) -> int:
        """仍待删除的 owned 意图数（含 pin 等待、队列满、删除失败）。"""
        with self._lock:
            return len(self._pending_cleanup)

    def delayed_worker_alive(self) -> bool:
        """延迟删除 Worker 线程是否仍存活（如实观察 OS 线程）。"""
        with self._worker_lock:
            worker = self._worker
            return worker is not None and worker.is_alive()

    def inflight_count(self) -> int:
        """Worker 正在处理的项数（含锁外删除 claim）。"""
        with self._lock:
            return int(self._inflight) + len(self._delete_claims)

    def _worker_observable_alive(self) -> bool:
        with self._worker_lock:
            worker = self._worker
            return worker is not None and worker.is_alive()

    def _worker_is_quiet(self) -> bool:
        with self._worker_lock:
            if self._worker_creator_pending:
                return False
            worker = self._worker
            if worker is not None and worker.is_alive():
                return False
            if worker is not None and (
                self._worker_start_unknown
                or (
                    self._worker_start_attempted
                    and not self._worker_terminal.is_set()
                )
            ):
                return False
        with self._lock:
            return int(self._inflight) == 0 and len(self._delete_claims) == 0

    def shutdown_status(self) -> TempFileShutdownStatus:
        """当前关闭可观察状态快照。"""
        with self._lock:
            owned = len(self._owned)
            inflight = int(self._inflight) + len(self._delete_claims)
            pending_cleanup = len(self._pending_cleanup)
            io_leases = self.active_io_lease_count()
        pending = self._delayed_queue.qsize()
        with self._worker_lock:
            worker_creator_pending = self._worker_creator_pending
            worker_start_unknown = self._worker_start_unknown
        alive = self._worker_observable_alive()
        worker_quiet = self._worker_is_quiet()
        complete = (
            owned == 0
            and pending == 0
            and pending_cleanup == 0
            and inflight == 0
            and io_leases == 0
            and worker_quiet
        )
        return TempFileShutdownStatus(
            complete=complete,
            owned=owned,
            pending=pending,
            inflight=inflight,
            worker_alive=alive,
            active_io_leases=io_leases,
            pending_cleanup=pending_cleanup,
            worker_start_unknown=worker_start_unknown,
            worker_creator_pending=worker_creator_pending,
        )

    def _reserved_resource_keys_locked(self) -> Set[str]:
        keys = set(self._owned.keys())
        keys.update(self._delete_claims.keys())
        keys.update(self._inflight_paths)
        return keys

    def _revoke_owned_identity_only_locked(self, key: Optional[str]) -> None:
        if key is None:
            return
        self._owned.pop(key, None)
        self._pending_cleanup.pop(key, None)
        self._retry_exhausted.discard(key)
        self._queued_paths.discard(key)

    def _release_delete_claim_locked(
        self, key: str, identity: FileIdentity
    ) -> None:
        current = self._delete_claims.get(key)
        if current is not None and identities_match(current, identity):
            self._delete_claims.pop(key, None)
            self._worker_wakeup.set()

    def _revoke_locked(self, key: Optional[str]) -> None:
        if key is not None:
            self._revoke_owned_identity_only_locked(key)
            self._delete_claims.pop(key, None)
            self._inflight_paths.discard(key)

    def _path_delete_busy_locked(self, key: str) -> bool:
        return key in self._inflight_paths or key in self._delete_claims

    def _claim_delete_slot_locked(
        self, key: str, identity: FileIdentity
    ) -> bool:
        if key in self._delete_claims:
            return False
        reserved = self._reserved_resource_keys_locked()
        if key not in reserved and len(reserved) >= self._owned_capacity:
            return False
        self._delete_claims[key] = identity
        return True

    def _commit_delete_if_identity_locked(
        self, key: str, identity: FileIdentity
    ) -> bool:
        """删除已在锁外完成；仅在身份仍匹配时撤销登记。"""
        registered = self._owned.get(key)
        if registered is None or not identities_match(registered, identity):
            return False
        if self._is_io_pinned_locked(key):
            return False
        self._revoke_locked(key)
        return True

    def _perform_os_remove_outside_lock(
        self, file_path: str, key: str, identity: FileIdentity
    ) -> Optional[OSError]:
        delete_done = False
        try:
            try:
                os.remove(file_path)
                delete_done = True
            except OSError as exc:
                with self._lock:
                    self._release_delete_claim_locked(key, identity)
                return exc
        except BaseException:
            with self._lock:
                self._release_delete_claim_locked(key, identity)
                self._record_failed_delete_intent_locked(key, identity)
            raise
        with self._lock:
            self._release_delete_claim_locked(key, identity)
            if self._commit_delete_if_identity_locked(key, identity):
                self._retry_exhausted.discard(key)
        return None

    def _owned_slot_available_locked(self, key: Optional[str] = None) -> bool:
        if key is not None and key in self._owned:
            return True
        reserved = self._reserved_resource_keys_locked()
        if key is not None and key in reserved:
            return True
        return len(reserved) < self._owned_capacity

    def _capture_or_revoke(
        self, key: str, *, label: str = "文件"
    ) -> Optional[FileIdentity]:
        """复核身份；缺失/替换/链接时撤销登记并返回 None。"""
        expected = self._owned.get(key)
        if expected is None:
            return None
        try:
            actual = capture_file_identity(key)
        except PermissionError:
            if self._path_delete_busy_locked(key):
                return None
            self._revoke_owned_identity_only_locked(key)
            return None
        if not identities_match(expected, actual):
            self.logger.warning("临时文件身份已变化，撤销所有权: %s", label)
            if self._path_delete_busy_locked(key):
                return None
            self._revoke_owned_identity_only_locked(key)
            return None
        return expected

    def create(self, suffix: str, *, directory: Optional[Union[str, Path]] = None) -> str:
        """
        在白名单目录原子创建临时文件并立即登记文件身份。

        ``directory`` 不得扩展白名单：须已是构造期登记的受信根或其子路径。
        授权判定在任何 mkdir/touch 之前完成。
        """
        with self._lock:
            if self._stop.is_set() or self._shutdown_done:
                raise RuntimeError("TempFileManager 已关闭，不能登记新文件")
            if not self._owned_slot_available_locked():
                raise OwnedCapacityExceededError(
                    f"owned 登记已达上限 {self._owned_capacity}"
                )
            root = Path(directory) if directory is not None else self._default_dir
            try:
                resolved_root = root.resolve(strict=False)
            except OSError as exc:
                raise PermissionError("临时文件目录无法解析") from exc
            probe = resolved_root / f".miku_probe_{uuid.uuid4().hex}"
            if not self.safety_policy.is_path_deletable(probe):
                raise PermissionError("临时文件目录不在删除白名单内或未授权删除")

            resolved_root.mkdir(parents=True, exist_ok=True)
            if not suffix.startswith("."):
                suffix = f".{suffix}"
            path = resolved_root / f"miku_{uuid.uuid4().hex}{suffix}"
            path.touch(exist_ok=False)
            try:
                identity = capture_file_identity(path)
            except PermissionError:
                try:
                    path.unlink()
                except OSError:
                    pass
                raise PermissionError("拒绝登记 symlink/junction 或非普通文件")
            key = identity.path
            if not self.safety_policy.is_path_deletable(key):
                try:
                    path.unlink()
                except OSError:
                    pass
                raise PermissionError("创建后路径越界，已撤销")
            self._owned[key] = identity
            return key

    def adopt_existing(self, file_path: Union[str, Path]) -> str:
        """
        将白名单内已存在的普通文件登记为本管理器所有，供受控 ``remove``。

        不扩大白名单；拒绝 symlink/junction/目录/不可解析路径。
        """
        with self._lock:
            if self._stop.is_set() or self._shutdown_done:
                raise RuntimeError("TempFileManager 已关闭，不能登记新文件")
            key = self._normalize_key(file_path)
            if key is None:
                raise PermissionError("路径无法解析，拒绝登记")
            if self._path_delete_busy_locked(key):
                raise RuntimeError("路径删除进行中，拒绝 adopt")
            if key in self._owned:
                if self._capture_or_revoke(key, label="adopt") is not None:
                    return key
            if not self._owned_slot_available_locked(key):
                raise OwnedCapacityExceededError(
                    f"owned 登记已达上限 {self._owned_capacity}"
                )
            if not self.safety_policy.is_path_deletable(key):
                raise PermissionError("路径不在删除白名单内或未授权删除")
            if not os.path.isfile(key):
                raise PermissionError("仅可登记已存在的普通文件")
            try:
                identity = capture_file_identity(key)
            except PermissionError as exc:
                raise PermissionError("拒绝登记 symlink/junction 或非普通文件") from exc
            self._owned[key] = identity
            return key

    def is_owned(self, file_path: Union[str, Path]) -> bool:
        key = self._normalize_key(file_path)
        if key is None:
            return False
        with self._lock:
            return self._capture_or_revoke(key, label="is_owned") is not None

    def assert_owned_regular_file(self, file_path: Union[str, Path], *, label: str) -> str:
        """断言路径为本管理器登记且身份未变的普通文件（拒绝缺失/替换/链接/越界）。"""
        key = self._normalize_key(file_path)
        if key is None:
            raise PermissionError(f"RVC 子进程能力拒绝: {label} 路径无法解析")
        with self._lock:
            if key not in self._owned:
                raise PermissionError(f"RVC 子进程能力拒绝: {label} 未登记")
            expected = self._capture_or_revoke(key, label=label)
            if expected is None:
                raise PermissionError(
                    f"RVC 子进程能力拒绝: {label} 缺失或文件身份已变化"
                )
            if not self.safety_policy.is_path_deletable(key):
                self._revoke_locked(key)
                raise PermissionError(f"RVC 子进程能力拒绝: {label} 不在白名单")
            return key

    def assert_rvc_io_paths(self, input_path: str, output_path: str) -> None:
        """一次性推理：输入/输出均须为本管理器当前拥有且身份未变的精确普通文件。"""
        self.assert_owned_regular_file(input_path, label="输入")
        self.assert_owned_regular_file(output_path, label="输出")

    def active_io_lease_count(self) -> int:
        """当前仍有效的 IO pin 引用总数（路径级 refcount 之和）。"""
        with self._lock:
            return sum(int(v) for v in self._io_pin_ref.values() if v > 0)

    def io_lease_active(self, token: str) -> bool:
        with self._lock:
            return token in self._io_leases

    def pin_io_paths(
        self, paths: Tuple[str, str] | Tuple[str, ...]
    ) -> str:
        """
        为单次 RVC infer 等场景 pin 已登记路径；最多 ``MAX_IO_PATHS_PER_LEASE`` 条。

        Returns:
            不透明 lease token；须 ``release_io_lease`` 显式释放。
        """
        if len(paths) < 1 or len(paths) > MAX_IO_PATHS_PER_LEASE:
            raise ValueError("pin_io_paths path count out of range")
        token = uuid.uuid4().hex
        with self._lock:
            if self._stop.is_set() or self._shutdown_done:
                raise RuntimeError("TempFileManager 已关闭，不能 pin IO")
            if len(self._io_leases) >= MAX_IO_LEASE_OWNERS:
                raise RuntimeError("IO lease owner 数量已达上限")
            keys: list[str] = []
            for raw in paths:
                key = self._normalize_key(raw)
                if key is None:
                    raise PermissionError("IO pin 路径无法解析")
                if key not in self._owned:
                    raise PermissionError("RVC 子进程能力拒绝: IO pin 未登记")
                if self._path_delete_busy_locked(key):
                    raise PermissionError(
                        "RVC 子进程能力拒绝: IO pin 路径删除进行中"
                    )
                expected = self._capture_or_revoke(key, label="IO pin")
                if expected is None:
                    raise PermissionError(
                        "RVC 子进程能力拒绝: IO pin 缺失或文件身份已变化"
                    )
                if not self.safety_policy.is_path_deletable(key):
                    self._revoke_locked(key)
                    raise PermissionError("RVC 子进程能力拒绝: IO pin 不在白名单")
                keys.append(key)
            frozen = frozenset(keys)
            self._io_leases[token] = frozen
            for key in frozen:
                self._io_pin_ref[key] = int(self._io_pin_ref.get(key, 0)) + 1
        return token

    def release_io_lease(self, token: str) -> None:
        with self._lock:
            paths = self._io_leases.pop(token, None)
            if paths is None:
                return
            for key in paths:
                count = int(self._io_pin_ref.get(key, 0)) - 1
                if count <= 0:
                    self._io_pin_ref.pop(key, None)
                else:
                    self._io_pin_ref[key] = count
            self._pump_pending_into_queue_locked()
            self._worker_wakeup.set()

    def _is_io_pinned_locked(self, key: str) -> bool:
        return int(self._io_pin_ref.get(key, 0)) > 0

    def _can_delete_locked(
        self, file_path: str, *, expected: Optional[FileIdentity] = None
    ) -> bool:
        """双条件：白名单 + 所有权身份一致；并拒绝目录/symlink/替换物。"""
        if not self.safety_policy.is_path_deletable(file_path):
            return False
        key = self._normalize_key(file_path)
        if key is None or key not in self._owned:
            return False
        registered = self._owned[key]
        if expected is not None and expected != registered:
            return False
        try:
            actual = capture_file_identity(key)
        except PermissionError:
            if not self._path_delete_busy_locked(key):
                self._revoke_owned_identity_only_locked(key)
            return False
        if not identities_match(registered, actual):
            if not self._path_delete_busy_locked(key):
                self._revoke_owned_identity_only_locked(key)
            return False
        if self._is_io_pinned_locked(key):
            return False
        return True

    def _delete_task_matches_owned_locked(
        self, key: str, identity: FileIdentity
    ) -> bool:
        registered = self._owned.get(key)
        if registered is None:
            return False
        return identities_match(registered, identity)

    @staticmethod
    def _validate_remove_delayed_params(delay: float, max_retries: int) -> None:
        if isinstance(delay, bool) or isinstance(max_retries, bool):
            raise ValueError("delay and max_retries must not be bool")
        if not isinstance(max_retries, int):
            raise ValueError("max_retries must be a strict int")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if not isinstance(delay, (int, float)):
            raise ValueError("delay must be a finite non-negative number")
        delay_f = float(delay)
        if not math.isfinite(delay_f) or delay_f < 0.0:
            raise ValueError("delay must be a finite non-negative number")

    def _note_pending_cleanup_locked(
        self,
        key: str,
        identity: FileIdentity,
        delay: float,
        max_retries: int,
        *,
        refresh_exhausted: bool = False,
    ) -> None:
        if not self._delete_task_matches_owned_locked(key, identity):
            stored = self._pending_cleanup.get(key)
            if stored is not None and not self._delete_task_matches_owned_locked(
                key, stored.identity
            ):
                self._pending_cleanup.pop(key, None)
            return
        if key in self._pending_cleanup and not refresh_exhausted:
            return
        if (
            key not in self._pending_cleanup
            and len(self._pending_cleanup) >= self._owned_capacity
        ):
            return
        if refresh_exhausted:
            self._retry_exhausted.discard(key)
        now = time.monotonic()
        budget = max(1, int(max_retries))
        self._pending_cleanup[key] = _PendingDeleteRecord(
            identity=identity,
            due_at=now + float(delay),
            remaining_attempts=budget,
            attempts_made=0,
        )
        self._worker_wakeup.set()

    def _record_failed_delete_intent_locked(
        self,
        key: str,
        identity: FileIdentity,
        *,
        max_retries: int = 0,
    ) -> None:
        """合法删除已接受但失败/取消：保留同身份 pending 并禁止 pump 自动重试。"""
        self._note_pending_cleanup_locked(
            key, identity, 0.0, int(max_retries), refresh_exhausted=True
        )
        self._retry_exhausted.add(key)

    def _should_start_worker_for_pending_locked(self, key: str) -> bool:
        """合法非耗尽 pending 接受后须在锁外确保 worker（与是否已入 ready 无关）。"""
        if self._stop.is_set() or self._shutdown_done:
            return False
        if key in self._retry_exhausted:
            return False
        record = self._pending_cleanup.get(key)
        if record is None:
            return False
        return self._delete_task_matches_owned_locked(key, record.identity)

    def _enqueue_delayed_locked(
        self,
        file_path: str,
        identity: FileIdentity,
        delay: float,
        max_retries: int,
        *,
        refresh_exhausted: bool = False,
    ) -> bool:
        """在已持有 ``_lock`` 时登记并入队；返回是否需要在锁外 ``_ensure_worker``。"""
        key = self._normalize_key(file_path)
        if key is None:
            return False
        self._note_pending_cleanup_locked(
            key,
            identity,
            delay,
            max_retries,
            refresh_exhausted=refresh_exhausted,
        )
        if self._stop.is_set():
            return False
        self._pump_pending_into_queue_locked()
        self._worker_wakeup.set()
        return self._should_start_worker_for_pending_locked(key)

    def remove(self, file_path: str, max_retries: int = 8, delay: float = 0.5) -> None:
        """
        删除单个已登记且身份未变的临时文件（exactly-once / 幂等）。

        前台路径仅做很少量快速尝试；遇占用立即转入有界延迟队列。
        路径不存在或身份已变：仅撤销登记，绝不删除替代物。
        """
        eff_delay = 3.0
        eff_retries = 5
        if not file_path:
            return

        start_worker = False
        claimed_key: Optional[str] = None
        claimed_ident: Optional[FileIdentity] = None
        with self._lock:
            key = self._normalize_key(file_path)
            if key is None:
                return

            if self._path_delete_busy_locked(key):
                return

            if not os.path.exists(file_path):
                self._revoke_owned_identity_only_locked(key)
                return

            expected = self._owned.get(key)
            if expected is None:
                self.logger.warning("未登记或不安全路径，跳过删除")
                return

            if not self.safety_policy.is_allowed(Operation.FILE_DELETE):
                self.logger.debug("文件删除未授权，跳过")
                return

            if self._is_io_pinned_locked(key):
                self._note_pending_cleanup_locked(
                    key,
                    expected,
                    0.0,
                    eff_retries,
                    refresh_exhausted=key in self._retry_exhausted,
                )
                start_worker = True
                self._worker_wakeup.set()
            elif not self._can_delete_locked(file_path, expected=expected):
                self.logger.warning("路径不安全或文件身份已变化，跳过删除")
                return
            else:
                if key in self._retry_exhausted:
                    self._retry_exhausted.discard(key)
                if not self._claim_delete_slot_locked(key, expected):
                    return
                claimed_key = key
                claimed_ident = expected
        if claimed_key is None or claimed_ident is None:
            if start_worker:
                self._ensure_worker()
                self._worker_wakeup.set()
            return
        try:
            for _attempt in range(FOREGROUND_REMOVE_ATTEMPTS):
                with self._lock:
                    if not self._can_delete_locked(
                        file_path, expected=claimed_ident
                    ):
                        return
                remove_exc = self._perform_os_remove_outside_lock(
                    file_path, claimed_key, claimed_ident
                )
                if remove_exc is None:
                    self.logger.debug("成功删除临时文件")
                    return
                if self._is_file_locked_error(remove_exc):
                    self.logger.debug("临时文件仍被占用，稍后重试")
                    with self._lock:
                        start_worker = self._enqueue_delayed_locked(
                            file_path, claimed_ident, eff_delay, eff_retries
                        )
                    break
                with self._lock:
                    self._record_failed_delete_intent_locked(
                        claimed_key, claimed_ident
                    )
                self.logger.warning(
                    "删除临时文件失败: %s", type(remove_exc).__name__
                )
                return
        finally:
            with self._lock:
                self._release_delete_claim_locked(claimed_key, claimed_ident)
        if start_worker:
            self._ensure_worker()
            self._worker_wakeup.set()

    def remove_delayed(
        self, file_path: str, delay: float = 3.0, max_retries: int = 5
    ) -> None:
        """
        将已登记且身份未变的文件放入单一有界延迟删除队列（单一 Worker）。
        """
        self._validate_remove_delayed_params(delay, max_retries)
        if not file_path or self._stop.is_set():
            return
        start_worker = False
        with self._lock:
            key = self._normalize_key(file_path)
            if key is None:
                return
            if self._path_delete_busy_locked(key):
                return
            if not os.path.exists(file_path):
                if not self._path_delete_busy_locked(key):
                    self._revoke_owned_identity_only_locked(key)
                return
            expected = self._owned.get(key)
            if expected is None:
                self.logger.warning("未登记或不安全路径，跳过延迟删除")
                return
            if not self.safety_policy.is_allowed(Operation.FILE_DELETE):
                self.logger.debug("文件删除未授权，跳过")
                return
            if self._is_io_pinned_locked(key):
                self._note_pending_cleanup_locked(
                    key,
                    expected,
                    float(delay),
                    int(max_retries),
                    refresh_exhausted=key in self._retry_exhausted,
                )
                start_worker = True
                self._worker_wakeup.set()
            elif not self._can_delete_locked(file_path, expected=expected):
                self.logger.warning("未登记或不安全路径，跳过延迟删除")
                return
            else:
                start_worker = self._enqueue_delayed_locked(
                    file_path,
                    expected,
                    delay,
                    max_retries,
                    refresh_exhausted=key in self._retry_exhausted,
                )
        if start_worker:
            self._ensure_worker()
            self._worker_wakeup.set()

    def remove_many(self, *paths: str) -> None:
        for path in paths:
            if path:
                self.remove(path)

    def pending_delayed(self) -> int:
        """当前延迟队列中的待处理项数。"""
        return self._delayed_queue.qsize()

    def shutdown(self, timeout: float = 2.0) -> TempFileShutdownStatus:
        """
        有界关闭：停止新登记，打断延迟等待，在总预算内尽力清理。

        deadline 到达后立即停止继续取队列项；未处理项保留为 pending/owned，
        返回 ``complete=False`` 供再次 shutdown 重试。单次不可中断的 OS 删除
        允许小幅越界，但不得在 deadline 后继续批量工作。
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            self._shutdown_done = True
            self._stop.set()
        self._worker_wakeup.set()

        self._join_worker_outside_registry(deadline)

        while time.monotonic() < deadline:
            try:
                file_path, identity, _delay, _retries = self._delayed_queue.get_nowait()
            except queue.Empty:
                break
            key_norm: Optional[str] = None
            item_identity: Optional[FileIdentity] = None
            claimed = False
            try:
                with self._lock:
                    key_norm = self._normalize_key(file_path)
                    item_identity = identity
                    claimed = (
                        key_norm is not None
                        and key_norm in self._owned
                        and self._can_delete_locked(file_path, expected=identity)
                        and self._claim_delete_slot_locked(key_norm, identity)
                    )
                if claimed and key_norm is not None and item_identity is not None:
                    self._perform_os_remove_outside_lock(
                        file_path, key_norm, item_identity
                    )
            finally:
                with self._lock:
                    if (
                        claimed
                        and key_norm is not None
                        and item_identity is not None
                    ):
                        self._release_delete_claim_locked(
                            key_norm, item_identity
                        )
                try:
                    self._delayed_queue.task_done()
                except ValueError:
                    pass

        with self._lock:
            owned_keys = list(self._owned.keys())
        for key in owned_keys:
            if time.monotonic() >= deadline:
                break
            self.remove(key)

        self._join_worker_outside_registry(deadline)
        return self.shutdown_status()

    def _worker_slot_taken_locked(self) -> bool:
        """调用方必须已持有 ``_worker_lock``。"""
        if self._worker_creator_pending or self._worker_start_unknown:
            return True
        if self._worker_start_attempted and not self._worker_entered.is_set():
            return True
        worker = self._worker
        if worker is not None and worker.is_alive():
            return True
        if self._worker_start_attempted and worker is not None:
            return True
        return False

    def _maybe_reclaim_worker_slot_locked(self) -> None:
        """在 ``_worker_lock`` 内：仅当 creator 已结束且线程已终态且 not alive 时摘除 owner。"""
        if self._worker_creator_pending:
            return
        worker = self._worker
        if worker is None:
            return
        if worker.is_alive():
            return
        if not self._worker_terminal.is_set():
            return
        if self._worker_start_unknown:
            self._worker_start_unknown = False
        self._worker = None
        self._worker_start_attempted = False

    def _ensure_worker(self) -> None:
        if self._shutdown_done or self._stop.is_set():
            return
        worker: Optional[threading.Thread] = None
        with self._worker_lock:
            if self._shutdown_done or self._stop.is_set():
                return
            self._maybe_reclaim_worker_slot_locked()
            if self._worker_slot_taken_locked():
                return
            try:
                worker = threading.Thread(
                    target=self._delayed_worker_loop,
                    name="TempFileDelayedDelete",
                    daemon=True,
                )
            except BaseException:
                self._worker = None
                self._worker_start_attempted = False
                self._worker_creator_pending = False
                self._worker_start_unknown = False
                raise
            self._worker = worker
            self._worker_creator_pending = True
            self._worker_start_attempted = True
            self._worker_entered.clear()
            self._worker_terminal.clear()
        assert worker is not None
        try:
            worker.start()
        except BaseException as exc:
            with self._worker_lock:
                self._worker_start_unknown = True
                self._worker_creator_pending = False
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return
        with self._worker_lock:
            self._worker_creator_pending = False

    def _join_worker_outside_registry(self, deadline: float) -> None:
        rem = deadline - time.monotonic()
        if rem <= 0:
            return
        with self._worker_lock:
            worker = self._worker
        if worker is None:
            return
        if worker.is_alive():
            worker.join(timeout=rem)
        with self._worker_lock:
            self._maybe_reclaim_worker_slot_locked()

    def _defer_ready_item_locked(self, key: str) -> None:
        """暂不能处理：清本条 ready 标记，保留 pending，唤醒 worker。"""
        self._queued_paths.discard(key)
        self._worker_wakeup.set()

    def _assess_dequeued_ready_item_locked(
        self, key: str, identity: FileIdentity
    ) -> str:
        """返回 ``proceed`` | ``defer`` | ``stale``（调用方已持有 ``_lock``）。"""
        if not self._delete_task_matches_owned_locked(key, identity):
            return "stale"
        if key in self._retry_exhausted:
            self._defer_ready_item_locked(key)
            return "defer"
        record = self._pending_cleanup.get(key)
        if record is None or not identities_match(record.identity, identity):
            return "stale"
        now = time.monotonic()
        if record.due_at > now:
            self._defer_ready_item_locked(key)
            return "defer"
        if self._is_io_pinned_locked(key):
            self._defer_ready_item_locked(key)
            return "defer"
        if key in self._delete_claims:
            self._defer_ready_item_locked(key)
            return "defer"
        return "proceed"

    def _worker_idle_wait_timeout_locked(self) -> Optional[float]:
        """最近可处理 pending 的等待秒数；None 表示无 pending 时可 Event 等待。"""
        now = time.monotonic()
        next_due: Optional[float] = None
        has_pending = bool(self._pending_cleanup)
        for key, record in self._pending_cleanup.items():
            if key in self._retry_exhausted:
                continue
            if not self._delete_task_matches_owned_locked(key, record.identity):
                continue
            blocked = (
                key in self._inflight_paths
                or self._is_io_pinned_locked(key)
                or key in self._queued_paths
                or key in self._delete_claims
            )
            if record.due_at <= now:
                if not blocked:
                    return 0.0
                continue
            if blocked:
                continue
            if next_due is None or record.due_at < next_due:
                next_due = record.due_at
        if next_due is not None:
            return max(0.0, next_due - now)
        if has_pending:
            return 0.5
        return None

    def _apply_locked_delete_backoff_locked(
        self, key: str, identity: FileIdentity
    ) -> None:
        record = self._pending_cleanup.get(key)
        if record is None or not identities_match(record.identity, identity):
            return
        attempts = record.attempts_made + 1
        remaining = record.remaining_attempts - 1
        if remaining <= 0:
            self._record_failed_delete_intent_locked(
                key, identity, max_retries=record.remaining_attempts
            )
            return
        now = time.monotonic()
        self._pending_cleanup[key] = _PendingDeleteRecord(
            identity=record.identity,
            due_at=now + 2.0 * attempts,
            remaining_attempts=remaining,
            attempts_made=attempts,
        )
        self._worker_wakeup.set()

    def _requeue_locked(
        self, file_path: str, identity: FileIdentity, max_retries: int
    ) -> None:
        """stop 时保留 pending 意图，仅清除 ready 标记并尝试 pump。"""
        _ = max_retries
        key = self._normalize_key(file_path)
        if key is None:
            return
        if key in self._retry_exhausted:
            return
        if not self._delete_task_matches_owned_locked(key, identity):
            self._pending_cleanup.pop(key, None)
            return
        if key not in self._pending_cleanup:
            self._note_pending_cleanup_locked(key, identity, 0.0, 1)
        self._queued_paths.discard(key)
        self._pump_pending_into_queue_locked()

    def _pump_pending_into_queue_locked(self) -> None:
        now = time.monotonic()
        indexed: List[Tuple[int, str, _PendingDeleteRecord]] = [
            (idx, key, record)
            for idx, (key, record) in enumerate(self._pending_cleanup.items())
        ]
        indexed.sort(key=lambda item: (item[2].due_at, item[0]))
        for _idx, key, record in indexed:
            if not self._delete_task_matches_owned_locked(key, record.identity):
                self._pending_cleanup.pop(key, None)
                continue
            if self._stop.is_set():
                return
            if key in self._retry_exhausted:
                continue
            if record.due_at > now:
                continue
            if key in self._inflight_paths or self._is_io_pinned_locked(key):
                continue
            if key in self._queued_paths:
                continue
            if key in self._delete_claims:
                continue
            try:
                self._delayed_queue.put_nowait(
                    (
                        record.identity.path,
                        record.identity,
                        0.0,
                        record.remaining_attempts,
                    )
                )
                self._queued_paths.add(key)
            except queue.Full:
                return

    def _clear_pending_for_obsolete_queue_item_locked(
        self, key: str, identity: FileIdentity
    ) -> None:
        """丢弃本条过期队列项自身的 pending，不撤当前 owned 下较新身份的意图。"""
        stored = self._pending_cleanup.get(key)
        if stored is None or not identities_match(stored.identity, identity):
            return
        registered = self._owned.get(key)
        if registered is not None and not identities_match(registered, identity):
            return
        self._pending_cleanup.pop(key, None)

    def _delayed_worker_loop(self) -> None:
        self._worker_entered.set()
        try:
            if self._stop.is_set():
                with self._worker_lock:
                    self._worker_terminal.set()
                    self._maybe_reclaim_worker_slot_locked()
                return
            while not self._stop.is_set():
                got_item = False
                file_path = ""
                identity: Optional[FileIdentity] = None
                max_retries = 0
                key: Optional[str] = None
                try:
                    file_path, identity, _delay, max_retries = (
                        self._delayed_queue.get_nowait()
                    )
                    got_item = True
                except queue.Empty:
                    self._worker_wakeup.clear()
                    with self._lock:
                        self._pump_pending_into_queue_locked()
                    try:
                        file_path, identity, _delay, max_retries = (
                            self._delayed_queue.get_nowait()
                        )
                        got_item = True
                    except queue.Empty:
                        if self._stop.is_set():
                            break
                        with self._lock:
                            idle = self._worker_idle_wait_timeout_locked()
                            has_pending = bool(self._pending_cleanup)
                        if idle is None and not has_pending:
                            self._worker_wakeup.wait()
                        else:
                            wait_for = 0.5 if idle is None else min(0.5, idle)
                            self._worker_wakeup.wait(timeout=wait_for)
                        self._worker_wakeup.clear()
                        continue
                key = self._normalize_key(file_path) if file_path else None
                processing_item = False
                item_action = "stale"
                try:
                    with self._lock:
                        if key is None or identity is None:
                            item_action = "stale"
                        else:
                            item_action = self._assess_dequeued_ready_item_locked(
                                key, identity
                            )
                            if item_action == "stale":
                                self._clear_pending_for_obsolete_queue_item_locked(
                                    key, identity
                                )
                            elif item_action == "proceed":
                                self._queued_paths.discard(key)
                                self._inflight += 1
                                self._inflight_paths.add(key)
                                processing_item = True
                    if item_action in ("stale", "defer"):
                        continue
                    assert identity is not None and key is not None
                    if self._stop.is_set():
                        with self._lock:
                            self._requeue_locked(file_path, identity, max_retries)
                        return
                    if not file_path or not os.path.exists(file_path):
                        with self._lock:
                            self._revoke_owned_identity_only_locked(key)
                        continue
                    with self._lock:
                        if not self._can_delete_locked(
                            file_path, expected=identity
                        ):
                            self._defer_ready_item_locked(key)
                            processing_item = False
                            self._inflight = max(0, self._inflight - 1)
                            self._inflight_paths.discard(key)
                            continue
                        if not self._claim_delete_slot_locked(key, identity):
                            self._defer_ready_item_locked(key)
                            processing_item = False
                            self._inflight = max(0, self._inflight - 1)
                            self._inflight_paths.discard(key)
                            continue
                    remove_exc = self._perform_os_remove_outside_lock(
                        file_path, key, identity
                    )
                    if remove_exc is None:
                        self.logger.debug("延迟删除成功")
                    elif self._is_file_locked_error(remove_exc):
                        with self._lock:
                            self._apply_locked_delete_backoff_locked(key, identity)
                        self.logger.debug("临时文件仍被占用，稍后重试")
                    elif os.path.exists(file_path):
                        with self._lock:
                            self._record_failed_delete_intent_locked(
                                key, identity, max_retries=int(max_retries)
                            )
                        self.logger.debug(
                            "延迟删除仍失败，保留 owned 待显式重试"
                        )
                finally:
                    with self._lock:
                        if processing_item:
                            if key is not None and identity is not None:
                                self._release_delete_claim_locked(key, identity)
                            self._inflight = max(0, self._inflight - 1)
                            if key is not None:
                                self._inflight_paths.discard(key)
                    if got_item:
                        self._delayed_queue.task_done()
        finally:
            with self._worker_lock:
                self._worker_terminal.set()
                self._maybe_reclaim_worker_slot_locked()
