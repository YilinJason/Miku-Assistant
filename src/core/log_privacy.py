# -*- coding: utf-8 -*-
"""
持久日志隐私辅助：安全文件名、异常标签、文件 handler 边界。

本模块约束 ``logs/`` 落盘路径；不改变面向用户的 CLI 返回正文。
"""
from __future__ import annotations

import logging
import math
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

# Windows 保留设备名（按第一个 ``.`` 之前的组件判定，含多扩展名形式）
_WIN_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

# Windows 文件名非法字符（不含已单独处理的分隔符语义）
_WIN_ILLEGAL_CHARS = frozenset('<>:"/\\|?*')

_MAX_SIZE_MB = 50.0
_MAX_BACKUP_COUNT = 10


def _open_log_stream(
    path: str,
    mode: str,
    *,
    encoding: Optional[str] = None,
    errors: Optional[str] = None,
):
    """
    打开日志流。Windows 使用 FILE_SHARE_DELETE，使路径在已打开句柄下仍可被替换，
    从而由后续身份复验 fail closed（而非依赖独占锁掩盖竞态）。
    """
    if os.name == "nt":
        return _win_open_log_share_delete(
            path, mode, encoding=encoding, errors=errors
        )
    return open(path, mode, encoding=encoding, errors=errors)


def _win_open_log_share_delete(
    path: str,
    mode: str,
    *,
    encoding: Optional[str] = None,
    errors: Optional[str] = None,
):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_ALWAYS = 4
    FILE_ATTRIBUTE_NORMAL = 0x00000080

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.GetLastError(), "CreateFileW failed", path)

    flags = os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = msvcrt.open_osfhandle(int(handle), flags)
    try:
        if "b" in mode:
            stream = open(fd, "r+b", closefd=True)
        else:
            stream = open(
                fd,
                "r+",
                encoding=encoding,
                errors=errors,
                closefd=True,
                newline=None,
            )
        if "a" in mode:
            stream.seek(0, os.SEEK_END)
        return stream
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise


class LogBoundaryError(ValueError):
    """
    日志路径 / 身份 / reparse / 硬链接等安全边界失败。

    仅此异常可在 emit 中 fail-closed、关闭流并向调用方抛出。
    普通格式化 / 写入 ``ValueError`` 仍走 ``handleError()``。
    """


def require_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} 必须为 bool")
    return value


def require_logging_mapping(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or isinstance(raw, (str, bytes)):
        raise ValueError("assistant.logging 必须为 mapping")
    return dict(raw)


def validate_log_file_name(file_name: Any) -> str:
    """
    仅允许单个 Windows 安全文件名（无路径分量）。

    拒绝绝对路径、盘符、分隔符、非法字符、控制字符、``.``/``..``、
    尾随点/空格、以及首组件为保留设备名的多扩展形式。
    """
    if not isinstance(file_name, str) or not file_name:
        raise ValueError("assistant.logging.file_name 必须为非空字符串")
    name = file_name
    for ch in name:
        code = ord(ch)
        if code < 32:
            raise ValueError("assistant.logging.file_name 不得含控制字符")
        if ch in _WIN_ILLEGAL_CHARS:
            raise ValueError("assistant.logging.file_name 含 Windows 非法字符")
    if name != name.strip():
        raise ValueError("assistant.logging.file_name 不得含首尾空白")
    if name.endswith(".") or name.endswith(" "):
        raise ValueError("assistant.logging.file_name 不得以点或空格结尾")
    if name in (".", ".."):
        raise ValueError("assistant.logging.file_name 非法")
    try:
        as_path = Path(name)
    except Exception as exc:
        raise ValueError("assistant.logging.file_name 非法") from exc
    if as_path.is_absolute() or bool(as_path.drive):
        raise ValueError("assistant.logging.file_name 不得为绝对路径")
    if as_path.name != name:
        raise ValueError("assistant.logging.file_name 非法")

    first_component = name.split(".", 1)[0].upper()
    if first_component in _WIN_RESERVED or name.upper() in _WIN_RESERVED:
        raise ValueError("assistant.logging.file_name 为 Windows 保留设备名")
    return name


def validate_max_size_mb(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("assistant.logging.max_size_mb 必须为有限正数")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("assistant.logging.max_size_mb 必须为有限正数")
    if number > _MAX_SIZE_MB:
        raise ValueError(f"assistant.logging.max_size_mb 不得超过 {_MAX_SIZE_MB:g}")
    return number


def max_size_mb_to_bytes(max_size_mb: float) -> int:
    """
    将已校验的 MB 上限转为字节。

    上取整并保证至少 1 字节，避免极小正数变成 ``maxBytes=0``（禁用轮转）。
    """
    raw = math.ceil(float(max_size_mb) * 1024 * 1024)
    return max(1, int(raw))


def validate_backup_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("assistant.logging.backup_count 必须为 int")
    if value < 0 or value > _MAX_BACKUP_COUNT:
        raise ValueError(
            f"assistant.logging.backup_count 必须在 0..{_MAX_BACKUP_COUNT}"
        )
    return value


def _is_reparse_or_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junc = getattr(path, "is_junction", None)
        if callable(is_junc) and is_junc():
            return True
    except OSError:
        return True
    try:
        st = os.stat(path, follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0) or 0
        if attrs & 0x400:
            return True
    except OSError:
        pass
    return False


def _path_exists_nofollow(path: Path) -> bool:
    try:
        return bool(path.exists(follow_symlinks=False))
    except TypeError:
        return path.exists() or path.is_symlink()
    except OSError as exc:
        raise LogBoundaryError("日志路径无法探测") from exc


def _path_is_dir_nofollow(path: Path) -> bool:
    try:
        return bool(path.is_dir(follow_symlinks=False))
    except TypeError:
        return path.is_dir()


def file_identity(st: os.stat_result) -> Tuple[int, int]:
    """文件身份：(st_dev, st_ino)；用于打开后比对句柄与路径。"""
    return (int(st.st_dev), int(st.st_ino))


def validate_logs_dir(logs_dir: Path) -> Path:
    """
    校验日志根目录：必须存在且为普通目录，不得为 symlink/junction/reparse。

    在确认非链接身份之前**不得** ``resolve()``，以免跟丢链接身份。
    通过后返回 resolve 后的绝对路径供子路径比较。
    """
    path = Path(logs_dir)
    if not _path_exists_nofollow(path) and not path.is_symlink():
        raise LogBoundaryError("logs 目录不存在")
    if _is_reparse_or_link(path):
        raise LogBoundaryError("logs 目录不得为 symlink/junction")
    if not _path_is_dir_nofollow(path):
        raise LogBoundaryError("logs 必须为普通目录")
    try:
        return path.resolve()
    except OSError as exc:
        raise LogBoundaryError("logs 目录无法解析") from exc


def assert_safe_existing_log_file(path: Path) -> os.stat_result:
    """
    对已存在目标 fail-closed：拒绝 reparse/link/目录/非普通文件/多硬链接。
    """
    if _is_reparse_or_link(path):
        raise LogBoundaryError("日志目标不得为 symlink/junction")
    if _path_is_dir_nofollow(path):
        raise LogBoundaryError("日志目标不得为目录")
    if not path.is_file():
        raise LogBoundaryError("日志目标必须为普通文件")
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise LogBoundaryError("日志目标无法 stat") from exc
    nlink = int(getattr(st, "st_nlink", 1) or 1)
    if nlink != 1:
        raise LogBoundaryError("日志目标不得为多硬链接")
    if _is_reparse_or_link(path):
        raise LogBoundaryError("日志目标不得为 symlink/junction")
    return st


def resolve_log_file_path(logs_dir: Path, file_name: str) -> Path:
    """
    将已校验文件名解析为 ``logs_dir`` 的直接子文件。

    存在目标若为 symlink/junction/目录/非普通/多硬链接则 fail closed；不删除不覆盖。
    """
    logs_root = validate_logs_dir(logs_dir)
    candidate = logs_root / file_name
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise LogBoundaryError("日志路径无法解析") from exc
    if resolved.parent != logs_root:
        raise LogBoundaryError("日志文件必须位于 logs/ 直接子级")
    if resolved.name != file_name:
        raise LogBoundaryError("日志文件名解析后不一致")

    if (
        _path_exists_nofollow(candidate)
        or candidate.is_symlink()
        or _is_reparse_or_link(candidate)
    ):
        assert_safe_existing_log_file(candidate)
    return candidate


def exception_log_label(exc: BaseException) -> str:
    """异常安全标签：类型名 + 可选 HTTP status；禁止 str(exc)/URL/body。"""
    name = type(exc).__name__
    status = None
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
    if status is None and hasattr(exc, "status_code"):
        status = getattr(exc, "status_code", None)
    try:
        if status is not None:
            return f"{name} status={int(status)}"
    except (TypeError, ValueError):
        pass
    return name


def _bind_trusted_logs_root(
    logs_dir: Path,
) -> Tuple[Path, Tuple[int, int], Path]:
    """校验并绑定可信日志根：返回 (原始路径, 身份, resolve 后路径)。"""
    path = Path(logs_dir)
    if not _path_exists_nofollow(path) and not path.is_symlink():
        raise LogBoundaryError("logs 目录不存在")
    if _is_reparse_or_link(path):
        raise LogBoundaryError("logs 目录不得为 symlink/junction")
    if not _path_is_dir_nofollow(path):
        raise LogBoundaryError("logs 必须为普通目录")
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise LogBoundaryError("logs 目录无法 stat") from exc
    identity = file_identity(st)
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise LogBoundaryError("logs 目录无法解析") from exc
    return path, identity, resolved


class PrivacyRotatingFileHandler(RotatingFileHandler):
    """
    文件落盘 handler：默认不持久化 traceback；绑定可信日志根身份，
    每次打开/轮转前后复验父目录与文件身份。

    ``backupCount=0`` 时对已验证 fd 截断（有界单文件，不创建 ``.1``，禁止路径 ``open w``）。
    仅 ``LogBoundaryError`` 会 fail-closed 并向上抛出。
    """

    # 测试可注入：仅同步用，不改变生产截断/打开语义
    _test_pre_open_hook: Optional[Callable[[Path], None]] = None
    _test_pre_truncate_hook: Optional[Callable[[Path], None]] = None

    def __init__(
        self,
        filename: Any,
        *args: Any,
        include_tracebacks: bool = False,
        trusted_logs_dir: Optional[Path] = None,
        **kwargs: Any,
    ):
        self.include_tracebacks = bool(include_tracebacks)
        self._miku_app_owned = False
        file_path = Path(filename)
        logs_dir = (
            Path(trusted_logs_dir)
            if trusted_logs_dir is not None
            else file_path.parent
        )
        (
            self._trusted_logs_dir_path,
            self._trusted_logs_identity,
            self._trusted_logs_root_resolved,
        ) = _bind_trusted_logs_root(logs_dir)
        validate_log_file_name(file_path.name)
        # 禁止 maxBytes=0（禁用轮转）
        if "maxBytes" in kwargs:
            mb = int(kwargs["maxBytes"])
            if mb <= 0:
                raise LogBoundaryError("maxBytes 必须为正整数")
            kwargs["maxBytes"] = mb
        super().__init__(filename, *args, **kwargs)

    def _assert_trusted_logs_root(self) -> None:
        parent = Path(self.baseFilename).parent
        if _is_reparse_or_link(parent):
            raise LogBoundaryError("logs 目录不得为 symlink/junction")
        if not _path_exists_nofollow(parent) and not parent.is_symlink():
            raise LogBoundaryError("logs 目录不存在")
        if not _path_is_dir_nofollow(parent):
            raise LogBoundaryError("logs 必须为普通目录")
        try:
            st = os.stat(parent, follow_symlinks=False)
        except OSError as exc:
            raise LogBoundaryError("logs 目录无法 stat") from exc
        if file_identity(st) != self._trusted_logs_identity:
            raise LogBoundaryError("logs 目录身份已变化")
        try:
            resolved = parent.resolve()
        except OSError as exc:
            raise LogBoundaryError("logs 目录无法解析") from exc
        if resolved != self._trusted_logs_root_resolved:
            raise LogBoundaryError("logs 目录解析结果已变化")
        try:
            file_resolved = Path(self.baseFilename).resolve(strict=False)
        except OSError as exc:
            raise LogBoundaryError("日志路径无法解析") from exc
        if file_resolved.parent != self._trusted_logs_root_resolved:
            raise LogBoundaryError("日志文件必须位于 logs/ 直接子级")

    def _fail_closed_close_stream(self) -> None:
        stream = getattr(self, "stream", None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        self.stream = None

    def doRollover(self) -> None:
        """
        ``backupCount>0``：标准轮转；``backupCount==0``：对已验证 fd 截断（不按路径 open ``w``）。
        """
        if self.backupCount > 0:
            if self.stream:
                self.stream.close()
                self.stream = None
            # 标准多文件轮转前仍须复验根
            self._assert_trusted_logs_root()
            if os.path.exists(self.baseFilename):
                for i in range(self.backupCount - 1, 0, -1):
                    sfn = self.rotation_filename("%s.%d" % (self.baseFilename, i))
                    dfn = self.rotation_filename("%s.%d" % (self.baseFilename, i + 1))
                    if os.path.exists(sfn):
                        if os.path.exists(dfn):
                            os.remove(dfn)
                        os.rename(sfn, dfn)
                dfn = self.rotation_filename(self.baseFilename + ".1")
                if os.path.exists(dfn):
                    os.remove(dfn)
                self._assert_trusted_logs_root()
                path = Path(self.baseFilename)
                if (
                    _path_exists_nofollow(path)
                    or path.is_symlink()
                    or _is_reparse_or_link(path)
                ):
                    assert_safe_existing_log_file(path)
                os.rename(self.baseFilename, dfn)
            self.stream = self._open()
            return

        # backupCount==0：只能截断已通过 _open 全套校验的 fd，禁止路径 open("w")
        self._rollover_truncate_verified_fd()

    def _rollover_truncate_verified_fd(self) -> None:
        """
        单文件有界截断：在已验证身份的打开句柄上 seek/truncate。

        若截断前路径被换为外部硬链接，仅原安全 fd 可能被截断，随后 fail closed；
        绝不按路径二次 open 触碰换入对象。
        """
        if self.stream is None:
            # 边界失败必须发生在任何截断之前
            self.stream = self._open()

        stream = self.stream
        try:
            fd_stat_before = os.fstat(stream.fileno())
        except OSError:
            # 普通 I/O：关闭不确定流后原样抛出，由 emit → handleError 吸收
            self._fail_closed_close_stream()
            raise

        if int(getattr(fd_stat_before, "st_nlink", 1) or 1) != 1:
            self._fail_closed_close_stream()
            raise LogBoundaryError("日志目标不得为多硬链接")

        try:
            self._assert_trusted_logs_root()
            path = Path(self.baseFilename)
            path_stat_before = assert_safe_existing_log_file(path)
            if file_identity(path_stat_before) != file_identity(fd_stat_before):
                self._fail_closed_close_stream()
                raise LogBoundaryError("截断前路径与句柄身份不一致")
        except LogBoundaryError:
            self._fail_closed_close_stream()
            raise

        # 测试钩子：仅用于「安全打开并验证后、截断前」同步；不改变生产截断方式
        hook = getattr(self, "_test_pre_truncate_hook", None)
        if callable(hook):
            hook(Path(self.baseFilename))

        try:
            stream.seek(0)
            stream.truncate(0)
            stream.flush()
        except OSError:
            # 磁盘/文件系统短暂故障：关闭不确定流，保留普通 OSError（不得升为边界异常）
            self._fail_closed_close_stream()
            raise

        try:
            fd_stat_after = os.fstat(stream.fileno())
        except OSError:
            self._fail_closed_close_stream()
            raise

        if file_identity(fd_stat_after) != file_identity(fd_stat_before):
            self._fail_closed_close_stream()
            raise LogBoundaryError("截断后句柄身份已变化")
        if int(getattr(fd_stat_after, "st_nlink", 1) or 1) != 1:
            self._fail_closed_close_stream()
            raise LogBoundaryError("截断后日志目标不得为多硬链接")

        try:
            self._assert_trusted_logs_root()
            path_stat_after = assert_safe_existing_log_file(Path(self.baseFilename))
            if file_identity(path_stat_after) != file_identity(fd_stat_after):
                self._fail_closed_close_stream()
                raise LogBoundaryError("截断后路径与句柄身份不一致")
        except LogBoundaryError:
            self._fail_closed_close_stream()
            raise

    def _open(self):
        self._assert_trusted_logs_root()

        path = Path(self.baseFilename)
        path_stat_before: Optional[os.stat_result] = None
        if (
            _path_exists_nofollow(path)
            or path.is_symlink()
            or _is_reparse_or_link(path)
        ):
            path_stat_before = assert_safe_existing_log_file(path)

        hook = getattr(self, "_test_pre_open_hook", None)
        if callable(hook):
            hook(path)

        stream = _open_log_stream(
            self.baseFilename,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
        )
        try:
            fd_stat = os.fstat(stream.fileno())
        except OSError:
            try:
                stream.close()
            except Exception:
                pass
            raise LogBoundaryError("日志文件句柄无法 fstat") from None

        nlink = int(getattr(fd_stat, "st_nlink", 1) or 1)
        if nlink != 1:
            try:
                stream.close()
            except Exception:
                pass
            raise LogBoundaryError("日志目标不得为多硬链接")

        # 无论打开前是否存在：打开后重新校验当前路径，并比对路径/fd/打开前身份
        try:
            path_stat_after = assert_safe_existing_log_file(path)
        except LogBoundaryError:
            try:
                stream.close()
            except Exception:
                pass
            raise

        if file_identity(path_stat_after) != file_identity(fd_stat):
            try:
                stream.close()
            except Exception:
                pass
            raise LogBoundaryError("日志文件身份在打开后不一致")

        if path_stat_before is not None:
            if file_identity(path_stat_before) != file_identity(fd_stat):
                try:
                    stream.close()
                except Exception:
                    pass
                raise LogBoundaryError("日志文件身份在打开后不一致")
            if file_identity(path_stat_before) != file_identity(path_stat_after):
                try:
                    stream.close()
                except Exception:
                    pass
                raise LogBoundaryError("日志文件路径身份在打开后被替换")

        try:
            self._assert_trusted_logs_root()
        except LogBoundaryError:
            try:
                stream.close()
            except Exception:
                pass
            raise
        return stream

    def emit(self, record: logging.LogRecord) -> None:
        if not self.include_tracebacks and (
            record.exc_info or record.exc_text or record.stack_info
        ):
            record = logging.makeLogRecord(record.__dict__)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        try:
            self._assert_trusted_logs_root()
            if self.shouldRollover(record):
                self.doRollover()
            if self.stream is None:
                self.stream = self._open()
            msg = self.format(record)
            stream = self.stream
            stream.write(msg + self.terminator)
            self.flush()
        except LogBoundaryError:
            self._fail_closed_close_stream()
            raise
        except Exception:
            # 普通 ValueError / IO 等：标准 logging 契约，不永久关闭
            self.handleError(record)
