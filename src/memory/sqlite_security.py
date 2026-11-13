# -*- coding: utf-8 -*-
"""
SQLite 记忆库路径、sidecar 与连接硬化（Round 3C2）。

标准库 sqlite3 不提供静态加密；本模块只阻止可避免的路径重定向、
扩展/ATTACH 扩大与临时落盘面，不声称抵御同用户/管理员/镜像读取。
"""
from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple

from src.memory.errors import MemoryStoreUnavailableError, MemoryValidationError

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
_WIN_ILLEGAL_CHARS = frozenset('<>:"/\\|?*')
_ALLOWED_SUFFIXES = frozenset({".sqlite3", ".sqlite", ".db"})
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

DEFAULT_BUSY_TIMEOUT_SEC = 5.0
_MAX_BUSY_TIMEOUT_SEC = 5.0

# 测试可注入：在路径校验通过后、sqlite3.connect 前调用
_test_pre_connect_hook: Optional[Callable[[Path], None]] = None


@dataclass(frozen=True)
class TrustedDbParent:
    path: Path
    identity: Tuple[int, int]
    resolved: Path


@dataclass(frozen=True)
class OpenedMemoryDb:
    conn: sqlite3.Connection
    path: Path
    parent: TrustedDbParent
    db_identity: Tuple[int, int]
    db_resolved: Path
    readonly: bool


def file_identity(st: os.stat_result) -> Tuple[int, int]:
    return (int(st.st_dev), int(st.st_ino))


def validate_busy_timeout_sec(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryValidationError("busy timeout must be a finite number of seconds")
    number = float(value)
    if number != number or number == float("inf") or number == float("-inf"):
        raise MemoryValidationError("busy timeout must be finite")
    if number < 0 or number > _MAX_BUSY_TIMEOUT_SEC:
        raise MemoryValidationError(
            f"busy timeout must be in 0..{_MAX_BUSY_TIMEOUT_SEC:g} seconds"
        )
    return number


def validate_sqlite_db_file_name(file_name: Any) -> str:
    """仅允许安全单文件名，后缀限定为 .sqlite3 / .sqlite / .db。"""
    if not isinstance(file_name, str) or not file_name:
        raise MemoryValidationError("sqlite db file name must be non-empty str")
    name = file_name
    for ch in name:
        if ord(ch) < 32:
            raise MemoryValidationError("sqlite db file name must not contain controls")
        if ch in _WIN_ILLEGAL_CHARS:
            raise MemoryValidationError("sqlite db file name has illegal characters")
    if name != name.strip():
        raise MemoryValidationError("sqlite db file name must not have edge whitespace")
    if name.endswith(".") or name.endswith(" "):
        raise MemoryValidationError("sqlite db file name must not end with dot/space")
    if name in (".", ".."):
        raise MemoryValidationError("sqlite db file name is illegal")
    try:
        as_path = Path(name)
    except Exception as exc:
        raise MemoryValidationError("sqlite db file name is illegal") from exc
    if as_path.is_absolute() or bool(as_path.drive):
        raise MemoryValidationError("sqlite db file name must not be absolute")
    if as_path.name != name or len(as_path.parts) != 1:
        raise MemoryValidationError("sqlite db file name must be a single component")
    suffix = as_path.suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise MemoryValidationError(
            "sqlite db file name suffix must be .sqlite3, .sqlite, or .db"
        )
    first = name.split(".", 1)[0].upper()
    if first in _WIN_RESERVED or name.upper() in _WIN_RESERVED:
        raise MemoryValidationError("sqlite db file name is a Windows reserved device")
    return name


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
        raise MemoryValidationError("sqlite path cannot be probed") from exc


def _path_is_dir_nofollow(path: Path) -> bool:
    try:
        return bool(path.is_dir(follow_symlinks=False))
    except TypeError:
        return path.is_dir()


def _path_is_file_nofollow(path: Path) -> bool:
    try:
        return bool(path.is_file(follow_symlinks=False))
    except TypeError:
        return path.is_file()


def _assert_existing_dir_ordinary(path: Path) -> os.stat_result:
    if _is_reparse_or_link(path):
        raise MemoryValidationError(
            "sqlite path component must not be symlink/junction/reparse"
        )
    if not _path_exists_nofollow(path):
        raise MemoryValidationError("sqlite path component does not exist")
    if not _path_is_dir_nofollow(path):
        raise MemoryValidationError("sqlite path component must be an ordinary directory")
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise MemoryValidationError("sqlite path component cannot be statted") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise MemoryValidationError("sqlite path component must be an ordinary directory")
    return st


def _assert_ancestor_chain_ordinary(path: Path) -> None:
    """从 path 走到根：每个已存在组件必须是普通目录且非 reparse。"""
    cur = Path(path)
    while True:
        if _path_exists_nofollow(cur) or cur.is_symlink() or _is_reparse_or_link(cur):
            _assert_existing_dir_ordinary(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent


def bind_trusted_db_parent(parent: Path) -> TrustedDbParent:
    """父目录须已存在、为普通目录且非 reparse；绑定 (st_dev, st_ino) 与 resolve。"""
    path = Path(parent)
    _assert_ancestor_chain_ordinary(path)
    st = _assert_existing_dir_ordinary(path)
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise MemoryValidationError("sqlite parent cannot be resolved") from exc
    return TrustedDbParent(path=path, identity=file_identity(st), resolved=resolved)


def ensure_parent_directory(db_path: Path) -> TrustedDbParent:
    """
    确保数据库父目录存在并绑定身份。

    从最近已存在祖先起逐段创建；每个既有/新建组件必须是普通目录且非 reparse。
    禁止未经祖先验证的 ``mkdir(parents=True)``。
    """
    parent = Path(db_path).parent
    if not parent.parts:
        raise MemoryValidationError("sqlite parent is invalid")

    # 收集需要创建的缺失段（自叶向根）
    missing: list[Path] = []
    cur = parent
    while True:
        if _is_reparse_or_link(cur):
            raise MemoryValidationError(
                "sqlite path ancestor must not be symlink/junction/reparse"
            )
        if _path_exists_nofollow(cur):
            _assert_existing_dir_ordinary(cur)
            _assert_ancestor_chain_ordinary(cur)
            break
        missing.append(cur)
        nxt = cur.parent
        if nxt == cur:
            raise MemoryValidationError("sqlite path has no existing ordinary ancestor")
        cur = nxt

    for segment in reversed(missing):
        # 段的父此时必须已是已验证普通目录
        try:
            segment.mkdir(exist_ok=False)
        except FileExistsError:
            # 竞态下已存在：仍须验证为普通目录
            pass
        except OSError as exc:
            raise MemoryStoreUnavailableError("failed to create sqlite parent") from exc
        _assert_existing_dir_ordinary(segment)

    return bind_trusted_db_parent(parent)


def assert_safe_existing_db_file(path: Path) -> os.stat_result:
    if _is_reparse_or_link(path):
        raise MemoryValidationError("sqlite db must not be symlink/junction/reparse")
    if _path_is_dir_nofollow(path):
        raise MemoryValidationError("sqlite db path must not be a directory")
    if not _path_is_file_nofollow(path):
        raise MemoryValidationError("sqlite db path must be an ordinary file")
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise MemoryValidationError("sqlite db cannot be statted") from exc
    if not stat.S_ISREG(st.st_mode):
        raise MemoryValidationError("sqlite db path must be an ordinary file")
    if int(getattr(st, "st_nlink", 1) or 1) != 1:
        raise MemoryValidationError("sqlite db must not be multiply hard-linked")
    if _is_reparse_or_link(path):
        raise MemoryValidationError("sqlite db must not be symlink/junction/reparse")
    return st


def assert_safe_db_path_for_open(db_path: Path, *, must_exist: bool) -> None:
    """打开前校验路径与 basename；不调用 sqlite3.connect。"""
    path = Path(db_path)
    validate_sqlite_db_file_name(path.name)
    if _is_reparse_or_link(path):
        raise MemoryValidationError("sqlite db must not be symlink/junction/reparse")
    exists = _path_exists_nofollow(path) or path.is_symlink()
    if must_exist:
        if not exists:
            raise FileNotFoundError("sqlite database does not exist")
        assert_safe_existing_db_file(path)
    elif exists:
        assert_safe_existing_db_file(path)
    assert_safe_sidecars(path)


def assert_safe_sidecars(db_path: Path) -> None:
    """已存在 sidecar 须为同父目录下普通单链接文件；不安全则拒绝（不删除）。"""
    path = Path(db_path)
    parent = path.parent
    for suffix in _SIDECAR_SUFFIXES:
        side = Path(str(path) + suffix)
        if not (_path_exists_nofollow(side) or side.is_symlink()):
            continue
        if side.parent != parent and side.parent.resolve() != parent.resolve():
            raise MemoryValidationError("sqlite sidecar must share db parent")
        if _is_reparse_or_link(side):
            raise MemoryValidationError("sqlite sidecar must not be reparse/link")
        if _path_is_dir_nofollow(side):
            raise MemoryValidationError("sqlite sidecar must not be a directory")
        if not _path_is_file_nofollow(side):
            raise MemoryValidationError("sqlite sidecar must be an ordinary file")
        try:
            st = os.stat(side, follow_symlinks=False)
        except OSError as exc:
            raise MemoryValidationError("sqlite sidecar cannot be statted") from exc
        if not stat.S_ISREG(st.st_mode):
            raise MemoryValidationError("sqlite sidecar must be an ordinary file")
        if int(getattr(st, "st_nlink", 1) or 1) != 1:
            raise MemoryValidationError("sqlite sidecar must not be multiply hard-linked")


def revalidate_db_location(
    db_path: Path,
    parent: TrustedDbParent,
    *,
    expected_db_identity: Optional[Tuple[int, int]] = None,
) -> Tuple[int, int]:
    """
    写事务 / VACUUM / 打开后复验父目录与数据库文件身份。

    运行时路径/身份失败统一映射为 ``MemoryStoreUnavailableError``。
    返回当前数据库文件 ``(st_dev, st_ino)``。
    """
    try:
        path = Path(db_path)
        validate_sqlite_db_file_name(path.name)
        current_parent = bind_trusted_db_parent(path.parent)
        if current_parent.identity != parent.identity:
            raise MemoryStoreUnavailableError("sqlite parent identity changed")
        if current_parent.resolved != parent.resolved:
            raise MemoryStoreUnavailableError("sqlite parent resolution changed")
        if _is_reparse_or_link(path):
            raise MemoryStoreUnavailableError("sqlite db became a reparse/link")
        if not _path_exists_nofollow(path):
            raise MemoryStoreUnavailableError("sqlite db path disappeared")
        st = assert_safe_existing_db_file(path)
        identity = file_identity(st)
        if expected_db_identity is not None and identity != expected_db_identity:
            raise MemoryStoreUnavailableError("sqlite db file identity changed")
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise MemoryStoreUnavailableError("sqlite db cannot be resolved") from exc
        if resolved.parent != parent.resolved:
            raise MemoryStoreUnavailableError("sqlite db escaped trusted parent")
        if resolved.name != path.name:
            raise MemoryStoreUnavailableError("sqlite db basename mismatch")
        assert_safe_sidecars(path)
        return identity
    except MemoryValidationError as exc:
        raise MemoryStoreUnavailableError("sqlite db location rejected") from exc


def database_path_fingerprint(db_path: Path) -> str:
    """
    计划绑定用路径指纹：父目录身份 + 安全文件名；不把绝对路径写入计划。
    """
    import hashlib

    path = Path(db_path)
    name = validate_sqlite_db_file_name(path.name)
    parent = bind_trusted_db_parent(path.parent)
    payload = f"{parent.identity[0]}:{parent.identity[1]}:{parent.resolved.as_posix()}:{name}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_pragma_value(conn: sqlite3.Connection, pragma: str, expected: Any) -> None:
    row = conn.execute(f"PRAGMA {pragma}").fetchone()
    actual = row[0] if row is not None else None
    if isinstance(expected, str):
        if str(actual).lower() != expected.lower():
            raise MemoryStoreUnavailableError(
                f"sqlite pragma {pragma} did not take effect"
            )
    else:
        if actual != expected:
            raise MemoryStoreUnavailableError(
                f"sqlite pragma {pragma} did not take effect"
            )


def harden_connection(
    conn: sqlite3.Connection,
    *,
    readonly: bool,
    busy_timeout_sec: float,
) -> None:
    """对已打开连接施加可验证 PRAGMA / 能力限制。"""
    timeout = validate_busy_timeout_sec(busy_timeout_sec)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA temp_store=MEMORY")
        if not readonly:
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
            if mode is None or str(mode[0]).lower() != "delete":
                raise MemoryStoreUnavailableError("sqlite journal_mode=DELETE required")
            conn.execute("PRAGMA secure_delete=ON")
            _require_pragma_value(conn, "secure_delete", 1)
        else:
            conn.execute("PRAGMA query_only=ON")
            _require_pragma_value(conn, "query_only", 1)

        _require_pragma_value(conn, "foreign_keys", 1)
        _require_pragma_value(conn, "trusted_schema", 0)
        _require_pragma_value(conn, "temp_store", 2)

        enable = getattr(conn, "enable_load_extension", None)
        if callable(enable):
            try:
                enable(False)
            except sqlite3.Error as exc:
                raise MemoryStoreUnavailableError(
                    "failed to disable sqlite extension loading"
                ) from exc

        setlimit = getattr(conn, "setlimit", None)
        limit_const = getattr(sqlite3, "SQLITE_LIMIT_ATTACHED", None)
        if callable(setlimit) and limit_const is not None:
            try:
                setlimit(limit_const, 0)
            except (sqlite3.Error, ValueError, OverflowError) as exc:
                raise MemoryStoreUnavailableError(
                    "failed to set SQLITE_LIMIT_ATTACHED=0"
                ) from exc
    except MemoryStoreUnavailableError:
        raise
    except sqlite3.Error as exc:
        raise MemoryStoreUnavailableError("failed to harden sqlite connection") from exc


def exclusive_precreate_db_file(path: Path) -> os.stat_result:
    """
    在仍受信任的父目录内 exclusive 创建空库文件。

    不覆盖、不跟随已有链接；创建后复验为普通单链接文件。
    """
    target = Path(path)
    validate_sqlite_db_file_name(target.name)
    if target.exists() or target.is_symlink() or _is_reparse_or_link(target):
        raise MemoryValidationError("sqlite db precreate target must not already exist")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(str(target), flags, 0o600)
    except FileExistsError as exc:
        raise MemoryValidationError("sqlite db precreate target already exists") from exc
    except OSError as exc:
        raise MemoryStoreUnavailableError("failed to precreate sqlite db file") from exc
    try:
        os.close(fd)
    except OSError:
        pass
    return assert_safe_existing_db_file(target)


def _sqlite_uri(path: Path, *, mode: str) -> str:
    """构造不隐式建库的 URI；mode 为 ro / rw（勿用 rwc）。"""
    if mode not in ("ro", "rw"):
        raise MemoryValidationError("sqlite uri mode must be ro or rw")
    return path.resolve().as_uri() + f"?mode={mode}&cache=private"


def _invoke_pre_connect_hook(path: Path) -> None:
    hook = _test_pre_connect_hook
    if callable(hook):
        hook(path)


def connect_memory_db(
    db_path: Path | str,
    *,
    readonly: bool = False,
    busy_timeout_sec: float = DEFAULT_BUSY_TIMEOUT_SEC,
    check_same_thread: bool = False,
) -> OpenedMemoryDb:
    """
    集中打开记忆库连接：

    路径校验 →（新库 exclusive 预建）→ hook → **connect 前复验** →
    URI ``mode=rw|ro&cache=private``（不自动建库）→ 复验 → harden → 再复验。
    """
    path = Path(db_path)
    timeout = validate_busy_timeout_sec(busy_timeout_sec)
    validate_sqlite_db_file_name(path.name)

    prebound_identity: Optional[Tuple[int, int]] = None
    if readonly:
        assert_safe_db_path_for_open(path, must_exist=True)
        parent = bind_trusted_db_parent(path.parent)
        prebound_identity = file_identity(assert_safe_existing_db_file(path))
    else:
        parent = ensure_parent_directory(path)
        exists = _path_exists_nofollow(path) or path.is_symlink()
        if exists:
            assert_safe_db_path_for_open(path, must_exist=True)
            prebound_identity = file_identity(assert_safe_existing_db_file(path))
        else:
            # F6：hook/connect 前在可信父目录 exclusive 预建；禁止 SQLite rwc 自动建库
            exclusive_precreate_db_file(path)
            assert_safe_sidecars(path)
            prebound_identity = file_identity(assert_safe_existing_db_file(path))

    _invoke_pre_connect_hook(path)

    conn: Optional[sqlite3.Connection] = None
    try:
        # F6：父目录换 junction 等竞态在 connect 前失败，外部不会出现空库文件
        revalidate_db_location(
            path, parent, expected_db_identity=prebound_identity
        )

        mode = "ro" if readonly else "rw"
        uri = _sqlite_uri(path, mode=mode)
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=timeout,
            check_same_thread=check_same_thread,
        )
        conn.row_factory = sqlite3.Row
        db_identity = revalidate_db_location(
            path, parent, expected_db_identity=prebound_identity
        )
        try:
            db_resolved = path.resolve()
        except OSError as exc:
            raise MemoryStoreUnavailableError("sqlite db cannot be resolved") from exc
        harden_connection(conn, readonly=readonly, busy_timeout_sec=timeout)
        db_identity = revalidate_db_location(
            path, parent, expected_db_identity=db_identity
        )
        opened = OpenedMemoryDb(
            conn=conn,
            path=path,
            parent=parent,
            db_identity=db_identity,
            db_resolved=db_resolved,
            readonly=readonly,
        )
        conn = None
        return opened
    except (MemoryValidationError, MemoryStoreUnavailableError, FileNotFoundError):
        raise
    except (sqlite3.Error, OSError) as exc:
        raise MemoryStoreUnavailableError("failed to open memory database") from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def read_pragma(conn: sqlite3.Connection, name: str) -> Any:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row is not None else None


def attach_should_be_rejected(conn: sqlite3.Connection, attach_path: Path | str) -> bool:
    """行为探测：ATTACH 应失败。返回 True 表示已被拒绝。"""
    target = Path(attach_path)
    try:
        conn.execute(f"ATTACH DATABASE ? AS probe_attach", (str(target),))
    except sqlite3.Error:
        return True
    try:
        conn.execute("DETACH DATABASE probe_attach")
    except sqlite3.Error:
        pass
    return False
