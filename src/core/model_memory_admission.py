"""应用级本地 Ollama 模型 POST 前内存准入（stdlib + 窄 HTTP metadata，无 import 副作用）。"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Final, Optional

import requests

from src.core.host_memory import HostMemorySnapshot, read_host_memory_snapshot
from src.core.network_endpoint import (
    ModelEndpointKind,
    SealedModelEndpoint,
    best_effort_close_response,
    enforce_no_redirect,
    reseal_model_endpoint,
)
from src.core.owned_thread import OwnedThreadTask

RESIDENT_REQUIRED_PHYSICAL_MB: Final[int] = 2048
RESIDENT_REQUIRED_COMMIT_MB: Final[int] = 4096
RESIDENT_EXTRA_PHYSICAL_MB: Final[int] = 2048
RESIDENT_EXTRA_COMMIT_MB: Final[int] = 2048

METADATA_FOREGROUND_CAP_SEC: Final[float] = 2.0
METADATA_HTTP_CONNECT_SEC: Final[float] = 0.5
METADATA_HTTP_READ_SEC: Final[float] = 0.5
METADATA_MAX_BODY_BYTES: Final[int] = 65536
METADATA_MAX_MODEL_ROWS: Final[int] = 512
_BYTES_MIB: Final[int] = 1024 * 1024
_MAX_FILE_BYTES_FOR_ESTIMATE: Final[int] = (1 << 62) // 3

REASON_RESOURCE_BUSY: Final[str] = "resource_busy"
REASON_HOST_UNOBSERVABLE: Final[str] = "host_memory_unobservable"
REASON_INSUFFICIENT: Final[str] = "model_memory_insufficient"
REASON_METADATA_FAILED: Final[str] = "model_metadata_failed"
REASON_METADATA_TIMEOUT: Final[str] = "model_metadata_timeout"
REASON_MODEL_UNKNOWN: Final[str] = "model_unknown"
REASON_CLOSING: Final[str] = "closing"

HINT_INSUFFICIENT: Final[str] = (
    "本机可用内存或提交余量不足，无法加载该模型；请关闭其它程序或选用更轻量的模型。"
)
HINT_UNOBSERVABLE: Final[str] = (
    "无法确认本机内存是否足够加载模型；请关闭其它程序或选用更轻量的模型。"
)
HINT_BUSY: Final[str] = "模型内存检测仍在进行，请稍后重试。"
HINT_CLOSING: Final[str] = "助手正在关闭，无法加载模型。"

_HttpGet = Callable[..., Any]


def _strict_non_negative_int_mb(value: object) -> bool:
    """``physical_available_mb`` / ``commit_available_mb`` 须为严格非负 ``int``（非 bool/float）。"""
    if isinstance(value, bool):
        return False
    if type(value) is not int:
        return False
    return value >= 0


def observable_host_memory_snapshot(
    snapshot: Optional[HostMemorySnapshot],
) -> Optional[HostMemorySnapshot]:
    """采样合法才返回；bool/float/NaN/Inf/负值/非 dataclass 视为不可观测。"""
    if snapshot is None:
        return None
    if not isinstance(snapshot, HostMemorySnapshot):
        return None
    if not _strict_non_negative_int_mb(snapshot.physical_available_mb):
        return None
    if not _strict_non_negative_int_mb(snapshot.commit_available_mb):
        return None
    return snapshot


def read_observable_host_memory_snapshot() -> Optional[HostMemorySnapshot]:
    return observable_host_memory_snapshot(read_host_memory_snapshot())


class ModelMemoryAdmissionError(Exception):
    """受控拒绝模型 POST（非连接失败）。"""

    def __init__(self, reason: str, user_message: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_message = user_message


@dataclass(frozen=True)
class _MetadataOutcome:
    ok: bool
    reason: str = ""
    file_bytes: Optional[int] = None
    resident: bool = False


class LocalModelMemoryAdmission:
    """
    共享 guard：loopback + Windows 时在模型 POST 前采样 RAM/commit 与 Ollama metadata。

    构造/导入零网络、零线程。每次 ``require_model_post`` 须传入实际 ``sealed_endpoint``；
    构造时的 endpoint 仅为未传参时的兼容 fallback。
    """

    __slots__ = (
        "_closing",
        "_default_endpoint",
        "_http_get",
        "_lock",
        "_owner",
        "_require_lock",
        "_safety_policy",
    )

    def __init__(
        self,
        sealed_endpoint: SealedModelEndpoint,
        *,
        safety_policy: Optional[Any] = None,
        http_get: Optional[_HttpGet] = None,
    ) -> None:
        self._default_endpoint = reseal_model_endpoint(
            safety_policy, sealed_endpoint, context="model_memory_admission"
        )
        self._safety_policy = safety_policy
        self._http_get = http_get or requests.get
        self._lock = threading.Lock()
        self._require_lock = threading.Lock()
        self._owner: Optional[OwnedThreadTask] = None
        self._closing = False

    @property
    def sealed_endpoint(self) -> SealedModelEndpoint:
        """默认 fallback 端点（非 memory override 的实际调用方应显式传参）。"""
        return self._default_endpoint

    def close(self, timeout: float = 2.0) -> bool:
        with self._lock:
            self._closing = True
            owner = self._owner
        if owner is None:
            return True
        owner.cancel()
        return owner.join(timeout=timeout) is True

    def require_model_post(
        self,
        model: str,
        *,
        call_timeout: float,
        sealed_endpoint: Optional[SealedModelEndpoint] = None,
    ) -> None:
        self._raise_if_closing()
        if not isinstance(model, str) or not model.strip():
            raise ModelMemoryAdmissionError(
                REASON_MODEL_UNKNOWN, HINT_UNOBSERVABLE
            )
        if isinstance(call_timeout, bool) or not isinstance(
            call_timeout, (int, float)
        ):
            raise ValueError("call_timeout must be a finite positive number")
        timeout_f = float(call_timeout)
        if not math.isfinite(timeout_f) or timeout_f <= 0:
            raise ValueError("call_timeout must be a finite positive number")

        endpoint = self._resolve_endpoint(sealed_endpoint)
        if endpoint.kind is not ModelEndpointKind.LOOPBACK:
            self._raise_if_closing()
            return
        if sys.platform != "win32":
            self._raise_if_closing()
            return

        if not self._begin_require():
            self._raise_if_closing()
            raise ModelMemoryAdmissionError(REASON_RESOURCE_BUSY, HINT_BUSY)

        try:
            self._require_loopback(model.strip(), timeout_f, endpoint)
        finally:
            self._end_require()

    def _resolve_endpoint(
        self, sealed_endpoint: Optional[SealedModelEndpoint]
    ) -> SealedModelEndpoint:
        candidate = (
            self._default_endpoint
            if sealed_endpoint is None
            else sealed_endpoint
        )
        return reseal_model_endpoint(
            self._safety_policy,
            candidate,
            context="model_memory_admission_require",
        )

    def _raise_if_closing(self) -> None:
        if self._closing:
            raise ModelMemoryAdmissionError(REASON_CLOSING, HINT_CLOSING)

    def _begin_require(self) -> bool:
        if not self._require_lock.acquire(blocking=False):
            return False
        if self._closing:
            self._require_lock.release()
            return False
        return True

    def _end_require(self) -> None:
        self._require_lock.release()

    def _remaining_deadline(self, deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _deadline_expired(self, deadline: float) -> bool:
        return time.monotonic() >= deadline

    def _raise_if_closing_or_deadline(self, deadline: float) -> None:
        self._raise_if_closing()
        if self._deadline_expired(deadline):
            raise ModelMemoryAdmissionError(
                REASON_METADATA_TIMEOUT, HINT_BUSY
            )

    def _require_loopback(
        self,
        model: str,
        timeout_f: float,
        endpoint: SealedModelEndpoint,
    ) -> None:
        deadline = time.monotonic() + min(METADATA_FOREGROUND_CAP_SEC, timeout_f)
        normalized = normalize_model_name(model)

        pre = read_observable_host_memory_snapshot()
        if pre is None:
            raise ModelMemoryAdmissionError(
                REASON_HOST_UNOBSERVABLE, HINT_UNOBSERVABLE
            )
        if (
            pre.physical_available_mb < RESIDENT_REQUIRED_PHYSICAL_MB
            or pre.commit_available_mb < RESIDENT_REQUIRED_COMMIT_MB
        ):
            raise ModelMemoryAdmissionError(REASON_INSUFFICIENT, HINT_INSUFFICIENT)

        self._raise_if_closing_or_deadline(deadline)

        with self._lock:
            self._raise_if_closing()
            prior = self._owner
            if prior is not None and not prior.snapshot().quiet:
                raise ModelMemoryAdmissionError(REASON_RESOURCE_BUSY, HINT_BUSY)

        outcome_holder: dict[str, Any] = {}
        worker_endpoint = endpoint

        def _worker(cancel: threading.Event) -> None:
            outcome_holder["value"] = self._collect_metadata(
                cancel,
                sealed_endpoint=worker_endpoint,
                normalized_model=normalized,
            )

        new_owner = OwnedThreadTask(_worker, name="ModelMemoryMeta")
        with self._lock:
            self._raise_if_closing()
            prior = self._owner
            if prior is not None and not prior.snapshot().quiet:
                raise ModelMemoryAdmissionError(REASON_RESOURCE_BUSY, HINT_BUSY)
            self._owner = new_owner
            owner = new_owner

        try:
            owner.start()
        except BaseException as exc:
            owner.cancel()
            if not isinstance(exc, Exception):
                raise
            raise ModelMemoryAdmissionError(
                REASON_METADATA_FAILED, HINT_UNOBSERVABLE
            ) from exc

        remaining = self._remaining_deadline(deadline)
        if remaining <= 0.0:
            owner.cancel()
            raise ModelMemoryAdmissionError(
                REASON_METADATA_TIMEOUT, HINT_BUSY
            )

        quiet = owner.join(timeout=remaining)
        if not quiet or self._deadline_expired(deadline):
            owner.cancel()
            raise ModelMemoryAdmissionError(
                REASON_METADATA_TIMEOUT, HINT_BUSY
            )

        worker_err = owner.error
        if worker_err is not None:
            if not isinstance(worker_err, Exception):
                raise worker_err
            raise ModelMemoryAdmissionError(
                REASON_METADATA_FAILED, HINT_UNOBSERVABLE
            )

        meta = outcome_holder.get("value")
        if not isinstance(meta, _MetadataOutcome) or not meta.ok:
            reason = (
                meta.reason
                if isinstance(meta, _MetadataOutcome)
                else REASON_METADATA_FAILED
            )
            if reason == REASON_MODEL_UNKNOWN:
                raise ModelMemoryAdmissionError(
                    REASON_MODEL_UNKNOWN, HINT_UNOBSERVABLE
                )
            if reason == REASON_INSUFFICIENT:
                raise ModelMemoryAdmissionError(
                    REASON_INSUFFICIENT, HINT_INSUFFICIENT
                )
            raise ModelMemoryAdmissionError(
                REASON_METADATA_FAILED, HINT_UNOBSERVABLE
            )

        self._raise_if_closing_or_deadline(deadline)

        post = read_observable_host_memory_snapshot()
        if post is None:
            raise ModelMemoryAdmissionError(
                REASON_HOST_UNOBSERVABLE, HINT_UNOBSERVABLE
            )
        if meta.resident:
            req_phys = RESIDENT_REQUIRED_PHYSICAL_MB
            req_commit = RESIDENT_REQUIRED_COMMIT_MB
        else:
            file_bytes = meta.file_bytes
            if file_bytes is None or file_bytes <= 0:
                raise ModelMemoryAdmissionError(
                    REASON_METADATA_FAILED, HINT_UNOBSERVABLE
                )
            req_phys = _cold_required_physical_mb(file_bytes)
            req_commit = _cold_required_commit_mb(file_bytes)
        if (
            post.physical_available_mb < req_phys
            or post.commit_available_mb < req_commit
        ):
            raise ModelMemoryAdmissionError(REASON_INSUFFICIENT, HINT_INSUFFICIENT)

        self._raise_if_closing_or_deadline(deadline)

    def _collect_metadata(
        self,
        cancel: threading.Event,
        *,
        sealed_endpoint: SealedModelEndpoint,
        normalized_model: str,
    ) -> _MetadataOutcome:
        ps_url = sealed_endpoint.api_url("/api/ps")
        tags_url = sealed_endpoint.api_url("/api/tags")
        try:
            ps_body = self._get_json(cancel, ps_url)
            if cancel.is_set():
                return _MetadataOutcome(False, REASON_METADATA_FAILED)
            resident_size = _resident_size_from_ps(ps_body, normalized_model)
            if resident_size is not None:
                return _MetadataOutcome(
                    True, resident=True, file_bytes=resident_size
                )
            if cancel.is_set():
                return _MetadataOutcome(False, REASON_METADATA_FAILED)
            tags_body = self._get_json(cancel, tags_url)
            if cancel.is_set():
                return _MetadataOutcome(False, REASON_METADATA_FAILED)
            file_bytes = _file_size_from_tags(tags_body, normalized_model)
            if file_bytes is None:
                return _MetadataOutcome(False, REASON_MODEL_UNKNOWN)
            return _MetadataOutcome(True, file_bytes=file_bytes, resident=False)
        except _MetadataParseError as exc:
            return _MetadataOutcome(False, exc.reason)

    def _get_json(self, cancel: threading.Event, url: str) -> Any:
        if cancel.is_set():
            raise _MetadataParseError(REASON_METADATA_FAILED)
        response = self._http_get(
            url,
            timeout=(METADATA_HTTP_CONNECT_SEC, METADATA_HTTP_READ_SEC),
            allow_redirects=False,
            stream=True,
        )
        try:
            enforce_no_redirect(response)
            if response.status_code >= 400:
                raise _MetadataParseError(REASON_METADATA_FAILED)
            raw = _read_bounded_body(response, cancel)
            if cancel.is_set():
                raise _MetadataParseError(REASON_METADATA_FAILED)
            return json.loads(raw.decode("utf-8"))
        finally:
            best_effort_close_response(response)


def normalize_model_name(raw: str) -> str:
    """末路径分量缺 tag 时补 ``:latest``；保持大小写。"""
    text = raw.strip()
    if not text:
        raise ValueError("model name must be non-empty")
    base = text.split("/")[-1]
    if ":" not in base:
        return f"{text}:latest"
    return text


def payload_exempt_from_model_admission(payload: dict[str, Any]) -> bool:
    """``keep_alive`` 为数值/字符串 ``0`` 且 ``messages=[]``；bool 不算零。"""
    keep = payload.get("keep_alive")
    if isinstance(keep, bool):
        return False
    if keep not in (0, "0", 0.0):
        return False
    messages = payload.get("messages")
    return messages == []


def _cold_required_physical_mb(file_bytes: int) -> int:
    numer = 3 * file_bytes
    denom = 2 * _BYTES_MIB
    mib = (numer + denom - 1) // denom
    return mib + RESIDENT_EXTRA_PHYSICAL_MB


def _cold_required_commit_mb(file_bytes: int) -> int:
    numer = 2 * file_bytes
    denom = _BYTES_MIB
    mib = (numer + denom - 1) // denom
    return mib + RESIDENT_EXTRA_COMMIT_MB


class _MetadataParseError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _positive_int_size(
    value: object, *, max_bytes: int = _MAX_FILE_BYTES_FOR_ESTIMATE
) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > max_bytes:
        return None
    return value


def _item_model_name(item: dict[str, Any]) -> Optional[str]:
    has_model = "model" in item
    has_name = "name" in item
    model_val = item.get("model")
    name_val = item.get("name")
    if has_model and has_name:
        if not isinstance(model_val, str) or not isinstance(name_val, str):
            raise _MetadataParseError(REASON_METADATA_FAILED)
        norm_m = normalize_model_name(model_val)
        norm_n = normalize_model_name(name_val)
        if norm_m != norm_n:
            raise _MetadataParseError(REASON_METADATA_FAILED)
        return norm_m
    if isinstance(model_val, str):
        return normalize_model_name(model_val)
    if isinstance(name_val, str):
        return normalize_model_name(name_val)
    return None


def _resident_size_from_ps(body: Any, normalized_model: str) -> Optional[int]:
    if not isinstance(body, dict):
        raise _MetadataParseError(REASON_METADATA_FAILED)
    if "models" not in body:
        raise _MetadataParseError(REASON_METADATA_FAILED)
    models = body.get("models")
    if models is None:
        raise _MetadataParseError(REASON_METADATA_FAILED)
    if not isinstance(models, list):
        raise _MetadataParseError(REASON_METADATA_FAILED)
    if len(models) > METADATA_MAX_MODEL_ROWS:
        raise _MetadataParseError(REASON_METADATA_FAILED)
    if len(models) == 0:
        return None
    for item in models:
        if not isinstance(item, dict):
            raise _MetadataParseError(REASON_METADATA_FAILED)
        name = _item_model_name(item)
        if name is None:
            raise _MetadataParseError(REASON_METADATA_FAILED)
        if name != normalized_model:
            continue
        size = _positive_int_size(item.get("size"))
        if size is None:
            raise _MetadataParseError(REASON_METADATA_FAILED)
        return size
    return None


def _file_size_from_tags(body: Any, normalized_model: str) -> Optional[int]:
    if not isinstance(body, dict):
        raise _MetadataParseError(REASON_METADATA_FAILED)
    models = body.get("models")
    if not isinstance(models, list):
        raise _MetadataParseError(REASON_METADATA_FAILED)
    if len(models) > METADATA_MAX_MODEL_ROWS:
        raise _MetadataParseError(REASON_METADATA_FAILED)
    for item in models:
        if not isinstance(item, dict):
            raise _MetadataParseError(REASON_METADATA_FAILED)
        name = _item_model_name(item)
        if name is None:
            raise _MetadataParseError(REASON_METADATA_FAILED)
        if name != normalized_model:
            continue
        size = _positive_int_size(item.get("size"))
        if size is None:
            raise _MetadataParseError(REASON_METADATA_FAILED)
        return size
    return None


def _read_bounded_body(response: Any, cancel: threading.Event) -> bytes:
    chunks: list[bytes] = []
    total = 0
    iter_content = getattr(response, "iter_content", None)
    if not callable(iter_content):
        content = getattr(response, "content", b"")
        if not isinstance(content, (bytes, bytearray)):
            raise _MetadataParseError(REASON_METADATA_FAILED)
        if len(content) > METADATA_MAX_BODY_BYTES:
            raise _MetadataParseError(REASON_METADATA_FAILED)
        return bytes(content)
    for chunk in iter_content(4096):
        if cancel.is_set():
            raise _MetadataParseError(REASON_METADATA_FAILED)
        if not chunk:
            continue
        if not isinstance(chunk, (bytes, bytearray)):
            raise _MetadataParseError(REASON_METADATA_FAILED)
        total += len(chunk)
        if total > METADATA_MAX_BODY_BYTES:
            raise _MetadataParseError(REASON_METADATA_FAILED)
        chunks.append(bytes(chunk))
    return b"".join(chunks)
