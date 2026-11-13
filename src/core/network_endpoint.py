# -*- coding: utf-8 -*-
"""
模型端点 URL 规范化与分类（无 DNS、无探测）。

主聊天、memory extractor、diagnostic 共用本模块，避免规则漂移。
"""
from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# 允许追加的固定 API 路径（正文/模型输出不得改变 host）
_ALLOWED_API_PATHS = frozenset(
    {
        "/api/chat",
        "/api/ps",
        "/api/tags",
    }
)


class ModelEndpointKind(str, Enum):
    LOOPBACK = "loopback"
    REMOTE_HTTPS = "remote_https"
    REMOTE_HTTP = "remote_http"


@dataclass(frozen=True)
class SealedModelEndpoint:
    """构造期封存的模型根 URL；请求只能追加固定 API path。"""

    base_url: str
    kind: ModelEndpointKind

    def api_url(self, path: str) -> str:
        if path not in _ALLOWED_API_PATHS:
            raise PermissionError(f"模型端点拒绝: 非法 API 路径 {path}")
        return f"{self.base_url}{path}"


def _is_loopback_host(hostname: str) -> bool:
    """仅字符串/字面量判断；不做 DNS。"""
    host = (hostname or "").strip().lower()
    if host == "localhost":
        return True
    # 去掉 IPv6 方括号
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def classify_and_normalize_model_url(raw: str) -> tuple[str, ModelEndpointKind]:
    """
    规范化并分类模型 base_url。

    允许 http/https；拒绝 userinfo、query、fragment、非根 path、缺 hostname、非法 port。
    不做 DNS。返回 ``(normalized_base_url_without_trailing_slash, kind)``。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("模型端点 URL 必须为非空字符串")
    text = raw.strip()
    try:
        parsed = urlparse(text)
    except Exception as exc:
        raise ValueError(f"模型端点 URL 无法解析: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError("模型端点 URL scheme 必须为 http 或 https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("模型端点 URL 不得含 userinfo")
    if parsed.query:
        raise ValueError("模型端点 URL 不得含 query")
    if parsed.fragment:
        raise ValueError("模型端点 URL 不得含 fragment")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("模型端点 URL 缺少 hostname")

    # path 必须为空或仅 "/"
    path = parsed.path or ""
    if path not in ("", "/"):
        raise ValueError("模型端点 URL path 必须为根路径")

    # port：urlparse 非法端口会抛；显式空端口（host:）必须拒绝
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("模型端点 URL 端口非法") from exc

    netloc_raw = parsed.netloc or ""
    # 显式端口分隔符但端口为空：localhost: / 127.0.0.1: / [::1]:
    # 合法 IPv6 字面量为 [addr] 或 [addr]:port，不得仅因内部冒号误拒
    if netloc_raw.endswith(":"):
        raise ValueError("模型端点 URL 端口非法")

    # 重建规范化 URL（小写 scheme；localhost 小写；IPv6 带方括号；无尾斜杠）
    try:
        ip = ipaddress.ip_address(hostname)
        if isinstance(ip, ipaddress.IPv6Address):
            host_part = f"[{hostname}]"
        else:
            host_part = hostname
    except ValueError:
        host_part = "localhost" if hostname.lower() == "localhost" else hostname

    if port is not None:
        netloc = f"{host_part}:{port}"
    else:
        netloc = host_part

    normalized = urlunparse((scheme, netloc, "", "", "", "")).rstrip("/")
    if _is_loopback_host(hostname):
        kind = ModelEndpointKind.LOOPBACK
    elif scheme == "https":
        kind = ModelEndpointKind.REMOTE_HTTPS
    else:
        kind = ModelEndpointKind.REMOTE_HTTP
    return normalized, kind


def reseal_model_endpoint(
    policy: Optional[object],
    candidate: SealedModelEndpoint,
    *,
    context: str = "model_endpoint",
) -> SealedModelEndpoint:
    """
    不信任调用方提供的 ``kind``：仅用 ``base_url`` 重新分类并授权。

    若候选声称的 kind 与重算结果不一致，一律拒绝（防伪造 loopback）。
    """
    sealed = authorize_model_endpoint(policy, candidate.base_url, context=context)
    if candidate.kind is not sealed.kind or candidate.base_url != sealed.base_url:
        logger.warning(
            "模型端点拒绝: sealed_endpoint 与重算结果不一致 (%s)", context
        )
        raise PermissionError(
            f"模型端点拒绝: sealed_endpoint 与重算结果不一致 ({context})"
        )
    return sealed


def authorize_model_endpoint(
    policy: Optional[object],
    raw_url: str,
    *,
    context: str = "model_endpoint",
) -> SealedModelEndpoint:
    """
    按 SafetyPolicy 许可封存端点。

    无策略时仅允许 loopback；远端必须显式策略与权限。
    """
    normalized, kind = classify_and_normalize_model_url(raw_url)
    if kind is ModelEndpointKind.LOOPBACK:
        return SealedModelEndpoint(base_url=normalized, kind=kind)

    if policy is None:
        logger.warning("模型端点拒绝: 缺少 SafetyPolicy 且非 loopback (%s)", context)
        raise PermissionError(
            f"模型端点拒绝: 缺少 SafetyPolicy 且非 loopback ({context})"
        )

    # duck-type SafetyPolicy
    require = getattr(policy, "require", None)
    if require is None:
        raise PermissionError("模型端点拒绝: 无效策略对象")

    from src.core.safety import Operation

    if kind is ModelEndpointKind.REMOTE_HTTPS:
        require(Operation.REMOTE_MODEL_ENDPOINT, context)
    elif kind is ModelEndpointKind.REMOTE_HTTP:
        require(Operation.REMOTE_MODEL_ENDPOINT, context)
        require(Operation.INSECURE_REMOTE_MODEL_HTTP, context)
    else:
        raise PermissionError("模型端点拒绝: 未知分类")
    return SealedModelEndpoint(base_url=normalized, kind=kind)


def endpoint_audit_label(endpoint: SealedModelEndpoint) -> str:
    """启动摘要用分类标签；不含完整 URL。"""
    return endpoint.kind.value


def reject_redirect_status(status_code: int) -> None:
    """3xx 一律视为 HTTP 错误，禁止跟随 redirect。"""
    if 300 <= int(status_code) < 400:
        raise PermissionError(
            f"模型端点拒绝: HTTP redirect {status_code}（禁止跟随）"
        )


def best_effort_close_response(response: object) -> None:
    """关闭 HTTP 响应；失败静默，不得覆盖业务/安全异常。"""
    closer = getattr(response, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        return


def enforce_no_redirect(response: object) -> None:
    """
    若响应为 3xx：先 best-effort close，再抛 redirect 拒绝。

    ``close`` 失败不得覆盖原始 PermissionError；成功路径不关闭（由调用方持有）。
    """
    status = getattr(response, "status_code", None)
    if status is None:
        return
    code = int(status)
    if 300 <= code < 400:
        best_effort_close_response(response)
        reject_redirect_status(code)
