# -*- coding: utf-8 -*-
"""
CosyVoice 生产 runtime worker（仅由 vendor CosyVoice Python + LOCAL_TTS_WORKER 启动）。

一次加载模型，JSON-line stdin/stdout；idle 超时完整退出释放锁与显存。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 协议行始终写入真实 stdout；第三方导入期间 sys.stdout 可能被重定向到 stderr。
_PROTOCOL_STDOUT = sys.stdout


def _early_join_owned_job() -> None:
    from src.core.owned_worker_job import worker_entry_join_owned_job

    worker_entry_join_owned_job()


def _emit(line: str) -> None:
    _PROTOCOL_STDOUT.write(line)
    _PROTOCOL_STDOUT.flush()


def _start_parent_watchdog(parent_pid: int) -> None:
    from src.core.parent_process import parent_process_is_alive

    if parent_pid <= 0:
        return

    def _watch() -> None:
        while parent_process_is_alive(parent_pid):
            time.sleep(0.5)
        os._exit(3)

    threading.Thread(target=_watch, name="CosyVoiceParentWatchdog", daemon=True).start()


def _apply_local_wetext(wetext_dir: Path, *, repo_root: Path) -> None:
    from src.voice.cosyvoice.local_modelscope_import import import_wetext_with_local_snapshot
    from src.voice.cosyvoice.worker_runtime import redirect_stdout_to_stderr

    with redirect_stdout_to_stderr():
        import_wetext_with_local_snapshot(wetext_dir, repo_root=repo_root)


def _ensure_cache_dirs(repo_root: Path, cache_root: Path) -> bool:
    try:
        from src.voice.cosyvoice.path_trust import assert_trusted_repo_path
        from src.voice.cosyvoice.worker_paths import validate_worker_lexical_layout

        if validate_worker_lexical_layout(repo_root) is not None:
            return False
        assert_trusted_repo_path(repo_root, cache_root, kind="dir")
        for sub in ("hf", "modelscope", "mpl"):
            path = cache_root / sub
            assert_trusted_repo_path(repo_root, path, kind="dir")
            path.mkdir(parents=True, exist_ok=True)
        return True
    except (OSError, PermissionError):
        return False


def _prepare_cosyvoice_paths(cosy_root: Path) -> None:
    if str(cosy_root) not in sys.path:
        sys.path.insert(0, str(cosy_root))
    matcha = cosy_root / "third_party" / "Matcha-TTS"
    if matcha.is_dir() and str(matcha) not in sys.path:
        sys.path.insert(0, str(matcha))


def _import_cosyvoice_automodel_class(
    cosy_root: Path,
    *,
    wetext_dir: Path | None = None,
    repo_root: Path | None = None,
) -> type:
    from src.core.paths import PROJECT_ROOT
    from src.voice.cosyvoice.local_modelscope_import import (
        modelscope_local_snapshot_import_scope,
    )
    from src.voice.cosyvoice.worker_paths import resolve_trusted_worker_paths
    from src.voice.cosyvoice.worker_runtime import redirect_stdout_to_stderr

    repo = Path(repo_root or PROJECT_ROOT)
    if wetext_dir is None:
        wetext_dir = resolve_trusted_worker_paths(repo)["wetext_dir"]
    _prepare_cosyvoice_paths(cosy_root)
    with redirect_stdout_to_stderr():
        with modelscope_local_snapshot_import_scope(
            wetext_root=wetext_dir,
            repo_root=repo,
        ):
            from cosyvoice.cli.cosyvoice import AutoModel

    return AutoModel


def cpu_checkpoint_staging_scope(model_dir: Path):
    from src.voice.cosyvoice.checkpoint_loading import (
        cpu_checkpoint_staging_scope as _scope,
    )

    return _scope(model_dir)


def redirect_stdout_to_stderr():
    from src.voice.cosyvoice.worker_runtime import redirect_stdout_to_stderr as _impl

    return _impl()


def _construct_automodel(automodel_cls: type, model_dir: Path) -> object:
    with redirect_stdout_to_stderr():
        with cpu_checkpoint_staging_scope(model_dir):
            return automodel_cls(model_dir=str(model_dir))


def run_worker_loop(**kwargs) -> None:
    from src.voice.cosyvoice.worker_runtime import run_worker_loop as _impl

    return _impl(**kwargs)


def run_resource_admission(settings):
    from src.voice.cosyvoice.worker_admission import run_resource_admission as _impl

    return _impl(settings)


def parse_worker_cli(argv):
    from src.voice.cosyvoice.worker_cli import parse_worker_cli as _impl

    return _impl(argv)


def resolve_trusted_worker_paths(repo_root: Path):
    from src.voice.cosyvoice.worker_paths import resolve_trusted_worker_paths as _impl

    return _impl(repo_root)


def verify_pinned_assets(**kwargs):
    from src.voice.cosyvoice.assets import verify_pinned_assets as _impl

    return _impl(**kwargs)


def try_acquire_model_lock(lock_path: Path) -> bool:
    from src.voice.cosyvoice.model_lock import try_acquire_model_lock as _impl

    return _impl(lock_path)


def release_model_lock() -> None:
    from src.voice.cosyvoice.model_lock import release_model_lock as _impl

    _impl()


def main() -> int:
    from src.core.paths import TMP_DIR
    from src.voice.cosyvoice.assets import RUNTIME_MODEL_LOCK_REL
    from src.voice.cosyvoice.path_trust import assert_trusted_repo_path
    from src.voice.cosyvoice.protocol import (
        ERR_ASSET_PIN,
        ERR_MODEL_LOCK_BUSY,
        ERR_OUTPUT_REJECTED,
        ERR_PROTOCOL,
        ERR_SYNTH_FAILED,
        StartupProgressReporter,
        build_error_message,
        build_ready_message,
    )
    from src.voice.cosyvoice.worker_admission import apply_thread_env
    from src.voice.cosyvoice.worker_runtime import StdinReaderFatal, stdin_reader_thread

    try:
        cli = parse_worker_cli(sys.argv[1:])
    except ValueError:
        _emit(build_error_message(ERR_PROTOCOL))
        return 2

    settings = cli.settings
    apply_thread_env(settings.cpu_threads)
    _start_parent_watchdog(cli.parent_pid)

    lock_path = _REPO_ROOT / RUNTIME_MODEL_LOCK_REL
    try:
        assert_trusted_repo_path(_REPO_ROOT, lock_path.parent, kind="dir")
        assert_trusted_repo_path(_REPO_ROOT, lock_path)
    except PermissionError:
        _emit(build_error_message(ERR_OUTPUT_REJECTED))
        return 2

    if not try_acquire_model_lock(lock_path):
        _emit(build_error_message(ERR_MODEL_LOCK_BUSY))
        return 2

    exit_code = 2
    progress = StartupProgressReporter(_emit)
    try:
        from src.voice.cosyvoice.worker_paths import validate_worker_lexical_layout

        if not progress.begin("layout"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2
        layout_err = validate_worker_lexical_layout(_REPO_ROOT)
        if not progress.end("layout"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2
        if layout_err is not None:
            _emit(build_error_message(layout_err))
            return 2

        if not progress.begin("resource_admission"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2
        admit_err = run_resource_admission(settings)
        if not progress.end("resource_admission"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2
        if admit_err is not None:
            _emit(build_error_message(admit_err))
            return 2

        if not progress.begin("asset_pin"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2
        try:
            paths = resolve_trusted_worker_paths(_REPO_ROOT)
        except PermissionError:
            progress.end("asset_pin")
            _emit(build_error_message(ERR_OUTPUT_REJECTED))
            return 2

        cosy_root = paths["cosy_root"]
        matcha_root = paths["matcha_root"]
        model_dir = paths["model_dir"]
        wetext_dir = paths["wetext_dir"]
        cache_root = paths["cache_root"]

        try:
            verify_pinned_assets(
                cosyvoice_root=cosy_root,
                matcha_root=matcha_root,
                wetext_root=wetext_dir,
                model_dir=model_dir,
            )
        except Exception:
            progress.end("asset_pin")
            _emit(build_error_message(ERR_ASSET_PIN))
            return 2
        if not progress.end("asset_pin"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2

        if not progress.begin("cache_dirs"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2
        if not _ensure_cache_dirs(_REPO_ROOT, cache_root):
            progress.end("cache_dirs")
            _emit(build_error_message(ERR_OUTPUT_REJECTED))
            return 2

        os.environ["PYTHONUTF8"] = "1"
        os.environ["HF_HOME"] = str(cache_root / "hf")
        os.environ["MODELSCOPE_CACHE"] = str(cache_root / "modelscope")
        os.environ["MPLCONFIGDIR"] = str(cache_root / "mpl")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        if not progress.end("cache_dirs"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2

        if not progress.begin("wetext_import"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2
        try:
            _apply_local_wetext(wetext_dir, repo_root=_REPO_ROOT)
        except Exception:
            progress.end("wetext_import")
            _emit(build_error_message(ERR_ASSET_PIN))
            return 2
        if not progress.end("wetext_import"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2

        t0 = time.perf_counter()
        if not progress.begin("cosyvoice_import"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2
        try:
            automodel_cls = _import_cosyvoice_automodel_class(
                cosy_root,
                wetext_dir=wetext_dir,
                repo_root=_REPO_ROOT,
            )
        except Exception:
            progress.end("cosyvoice_import")
            _emit(build_error_message(ERR_SYNTH_FAILED))
            return 2
        if not progress.end("cosyvoice_import"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2

        if not progress.begin("automodel_construct"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2
        try:
            model = _construct_automodel(automodel_cls, model_dir)
        except Exception:
            progress.end("automodel_construct")
            _emit(build_error_message(ERR_SYNTH_FAILED))
            return 2
        if not progress.end("automodel_construct"):
            _emit(build_error_message(ERR_PROTOCOL))
            return 2
        load_ms = (time.perf_counter() - t0) * 1000.0
        _emit(build_ready_message(load_ms))

        stdin_q: queue.Queue[str | None] = queue.Queue(maxsize=8)
        stdin_fatal = StdinReaderFatal()
        threading.Thread(
            target=stdin_reader_thread,
            args=(sys.stdin, stdin_q),
            kwargs={"fatal": stdin_fatal},
            name="CosyVoiceStdinReader",
            daemon=True,
        ).start()

        run_worker_loop(
            model=model,
            settings=settings,
            parent_pid=cli.parent_pid,
            repo_root=_REPO_ROOT,
            tmp_root=TMP_DIR,
            stdin_q=stdin_q,
            emit=_emit,
            stdin_fatal=stdin_fatal,
        )
        exit_code = 0
    finally:
        release_model_lock()
    return exit_code


if __name__ == "__main__":
    _early_join_owned_job()
    raise SystemExit(main())
