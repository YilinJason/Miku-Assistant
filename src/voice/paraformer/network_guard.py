# -*- coding: utf-8 -*-
"""Worker 侧出站网络阻断（保留 socket 类型，拦截常见出站路径）。"""
from __future__ import annotations

import socket
from typing import Any, Callable, List, Tuple

_ORIG_CREATE_CONNECTION: Callable[..., socket.socket] | None = None
_ORIG_GETADDRINFO: Callable[..., List[Tuple]] | None = None
_ORIG_CONNECT: Any = None
_ORIG_CONNECT_EX: Any = None
_ORIG_SENDTO: Any = None
_INSTALLED = False


def _blocked(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("network_disabled")


def install_outbound_network_guard() -> None:
    global _ORIG_CREATE_CONNECTION, _ORIG_GETADDRINFO, _INSTALLED
    global _ORIG_CONNECT, _ORIG_CONNECT_EX, _ORIG_SENDTO
    if _INSTALLED:
        return
    _ORIG_CREATE_CONNECTION = socket.create_connection
    _ORIG_GETADDRINFO = socket.getaddrinfo
    _ORIG_CONNECT = socket.socket.connect
    _ORIG_CONNECT_EX = socket.socket.connect_ex
    _ORIG_SENDTO = socket.socket.sendto

    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.getaddrinfo = _blocked  # type: ignore[assignment]
    socket.socket.connect = _blocked  # type: ignore[assignment]
    socket.socket.connect_ex = _blocked  # type: ignore[assignment]
    socket.socket.sendto = _blocked  # type: ignore[assignment]
    _INSTALLED = True


def restore_outbound_network_guard() -> None:
    global _ORIG_CREATE_CONNECTION, _ORIG_GETADDRINFO, _INSTALLED
    global _ORIG_CONNECT, _ORIG_CONNECT_EX, _ORIG_SENDTO
    if not _INSTALLED:
        return
    if _ORIG_CREATE_CONNECTION is not None:
        socket.create_connection = _ORIG_CREATE_CONNECTION  # type: ignore[assignment]
    if _ORIG_GETADDRINFO is not None:
        socket.getaddrinfo = _ORIG_GETADDRINFO  # type: ignore[assignment]
    if _ORIG_CONNECT is not None:
        socket.socket.connect = _ORIG_CONNECT  # type: ignore[assignment]
    if _ORIG_CONNECT_EX is not None:
        socket.socket.connect_ex = _ORIG_CONNECT_EX  # type: ignore[assignment]
    if _ORIG_SENDTO is not None:
        socket.socket.sendto = _ORIG_SENDTO  # type: ignore[assignment]
    _INSTALLED = False
