# -*- coding: utf-8 -*-
"""Silero VAD CPU ONNX 窄适配器（Phase 1；未接入默认生产配置）。"""
from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Tuple

if TYPE_CHECKING:
    from src.voice.silero_vad_stream import SileroVadStreamContext

SAMPLE_RATE_HZ = 16000
BYTES_PER_SAMPLE = 2
MAX_PCM_BYTES = 480000
FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64
INPUT_SAMPLES = FRAME_SAMPLES + CONTEXT_SAMPLES

EXPECTED_ASSET_BYTES = 2327524
EXPECTED_ASSET_SHA256 = (
    "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
)
ASSET_RELATIVE_PATH = Path("vendor") / "silero-vad" / "silero_vad.onnx"

INPUT_NAME = "input"
STATE_NAME = "state"
SR_NAME = "sr"
OUTPUT_NAME = "output"
STATE_OUT_NAME = "stateN"

EXPECTED_INPUT_NAMES = frozenset({INPUT_NAME, STATE_NAME, SR_NAME})
EXPECTED_OUTPUT_NAMES = frozenset({OUTPUT_NAME, STATE_OUT_NAME})

CLOSE_LOCK_TIMEOUT_SEC = 2.0
MAX_CLOSE_TIMEOUT_SEC = 2.0
SCORE_LOCK_TIMEOUT_SEC = 0.0

_REPARSE_ATTR = 0x400

SessionFactory = Callable[[bytes], Any]


@dataclass(frozen=True)
class VadScores:
    """VAD 概率输出；``probabilities`` 为每 512 样本帧，``valid_samples`` 为帧内真实样本数。"""

    ok: bool
    reason: str
    probabilities: Tuple[float, ...] = ()
    valid_samples: Tuple[int, ...] = ()
    elapsed_ms: float = 0.0


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def canonical_silero_asset_path(repo_root: Optional[Path] = None) -> Path:
    root = (repo_root or default_repo_root()).resolve()
    return root / ASSET_RELATIVE_PATH


def resolve_silero_asset_path(repo_root: Optional[Path] = None) -> Path:
    """词法固定路径（不 follow symlink）。"""
    return canonical_silero_asset_path(repo_root)


def _stable_error(reason: str) -> VadScores:
    return VadScores(ok=False, reason=reason)


def _validate_pcm_bytes(pcm: Any) -> Optional[str]:
    if type(pcm) is not bytes:
        return "pcm_invalid_type"
    if not pcm:
        return "pcm_empty"
    if len(pcm) % BYTES_PER_SAMPLE != 0:
        return "pcm_misaligned"
    if len(pcm) > MAX_PCM_BYTES:
        return "pcm_oversized"
    return None


def _segment_is_untrusted_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junc = getattr(path, "is_junction", None)
        if callable(is_junc) and is_junc():
            return True
    except OSError:
        return True
    try:
        st = os.lstat(path)
        attrs = getattr(st, "st_file_attributes", 0) or 0
        if attrs & _REPARSE_ATTR:
            return True
    except OSError:
        pass
    return False


def _path_has_symlink_in_chain(path: Path) -> bool:
    anchor = Path(path.anchor)
    cursor = anchor
    for part in path.parts[len(anchor.parts) :]:
        cursor = cursor / part
        if _segment_is_untrusted_reparse(cursor):
            return True
    return False


def _validate_close_timeout_sec(value: Any) -> float:
    if type(value) is bool:
        raise ValueError("close timeout must be a finite number of seconds")
    try:
        budget = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "close timeout must be a finite number of seconds"
        ) from exc
    if not math.isfinite(budget):
        raise ValueError("close timeout must be a finite number of seconds")
    if budget < 0.0 or budget > MAX_CLOSE_TIMEOUT_SEC:
        raise ValueError("close timeout must be between 0 and 2 seconds")
    return budget


def read_verified_silero_model_bytes(
    repo_root: Optional[Path] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """有界读取固定资产为不可变 bytes；每次调用重新哈希，无全局缓存。"""
    root = (repo_root or default_repo_root()).resolve()
    asset_path = canonical_silero_asset_path(root)
    try:
        asset_path.relative_to(root)
    except ValueError:
        return None, "asset_outside_repo"
    if _path_has_symlink_in_chain(asset_path):
        return None, "asset_symlink"
    if not asset_path.is_file():
        return None, "asset_missing"
    try:
        with asset_path.open("rb") as handle:
            payload = handle.read(EXPECTED_ASSET_BYTES + 1)
    except OSError:
        return None, "asset_unavailable"
    if len(payload) != EXPECTED_ASSET_BYTES:
        return None, "asset_size_mismatch"
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_ASSET_SHA256:
        return None, "asset_hash_mismatch"
    return payload, None


def _import_numpy() -> Tuple[Any, Optional[str]]:
    try:
        import numpy as np
    except ImportError:
        return None, "numpy_unavailable"
    except OSError:
        return None, "numpy_unavailable"
    return np, None


def _import_onnxruntime() -> Tuple[Any, Optional[str]]:
    try:
        import onnxruntime as ort
    except ImportError:
        return None, "ort_unavailable"
    except OSError:
        return None, "ort_unavailable"
    return ort, None


def _build_cpu_session_options(ort_module: Any) -> Any:
    options = ort_module.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort_module.ExecutionMode.ORT_SEQUENTIAL
    options.enable_profiling = False
    if hasattr(ort_module, "SessionLogSeverity"):
        try:
            options.log_severity_level = ort_module.SessionLogSeverity.ERROR
        except Exception:
            pass
    return options


def _graph_dim_compatible(graph_entry: Any, runtime_dim: int) -> bool:
    if graph_entry is None:
        return True
    if isinstance(graph_entry, str):
        return True
    if isinstance(graph_entry, bool):
        return False
    if not isinstance(graph_entry, int):
        return False
    if graph_entry <= 0:
        return False
    return graph_entry == runtime_dim


def _graph_shape_matches_runtime(
    shape: Any,
    *,
    runtime_dims: Tuple[int, ...],
) -> bool:
    if not isinstance(shape, (list, tuple)):
        return False
    if len(shape) != len(runtime_dims):
        return False
    for graph_entry, runtime_dim in zip(shape, runtime_dims):
        if not _graph_dim_compatible(graph_entry, runtime_dim):
            return False
    return True


def _validate_sr_graph_shape(shape: Any) -> bool:
    if not isinstance(shape, (list, tuple)):
        return False
    if len(shape) == 0:
        return True
    if len(shape) == 1:
        entry = shape[0]
        if entry is None or isinstance(entry, str):
            return True
        if isinstance(entry, bool):
            return False
        return isinstance(entry, int) and entry == 1
    return False


def _validate_session_io(session: Any) -> Optional[str]:
    try:
        inputs = list(session.get_inputs())
        outputs = list(session.get_outputs())
        providers = list(session.get_providers())
    except Exception:
        return "model_io_invalid"
    if {node.name for node in inputs} != EXPECTED_INPUT_NAMES:
        return "model_io_invalid"
    if {node.name for node in outputs} != EXPECTED_OUTPUT_NAMES:
        return "model_io_invalid"
    if providers != ["CPUExecutionProvider"]:
        return "model_provider_invalid"
    by_name = {node.name: node for node in inputs}
    input_node = by_name[INPUT_NAME]
    state_node = by_name[STATE_NAME]
    sr_node = by_name[SR_NAME]
    if getattr(input_node, "type", "") != "tensor(float)":
        return "model_io_invalid"
    if getattr(state_node, "type", "") != "tensor(float)":
        return "model_io_invalid"
    if getattr(sr_node, "type", "") != "tensor(int64)":
        return "model_io_invalid"
    if not _graph_shape_matches_runtime(
        list(getattr(input_node, "shape", [])),
        runtime_dims=(1, INPUT_SAMPLES),
    ):
        return "model_io_invalid"
    if not _graph_shape_matches_runtime(
        list(getattr(state_node, "shape", [])),
        runtime_dims=(2, 1, 128),
    ):
        return "model_io_invalid"
    if not _validate_sr_graph_shape(list(getattr(sr_node, "shape", []))):
        return "model_io_invalid"
    out_by_name = {node.name: node for node in outputs}
    if getattr(out_by_name[OUTPUT_NAME], "type", "") != "tensor(float)":
        return "model_io_invalid"
    if getattr(out_by_name[STATE_OUT_NAME], "type", "") != "tensor(float)":
        return "model_io_invalid"
    if not _graph_shape_matches_runtime(
        list(getattr(out_by_name[OUTPUT_NAME], "shape", [])),
        runtime_dims=(1, 1),
    ):
        return "model_io_invalid"
    if not _graph_shape_matches_runtime(
        list(getattr(out_by_name[STATE_OUT_NAME], "shape", [])),
        runtime_dims=(2, 1, 128),
    ):
        return "model_io_invalid"
    return None


def _create_cpu_inference_session(
    model_bytes: bytes,
    *,
    session_factory: Optional[SessionFactory] = None,
) -> Any:
    if session_factory is not None:
        return session_factory(model_bytes)
    ort, err = _import_onnxruntime()
    if ort is None:
        raise OSError(err or "ort_unavailable")
    options = _build_cpu_session_options(ort)
    return ort.InferenceSession(
        model_bytes,
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )


def _parse_probability_output(raw: Any) -> Tuple[Optional[float], Optional[str]]:
    np, err = _import_numpy()
    if np is None:
        return None, err
    if raw is None or not isinstance(raw, np.ndarray):
        return None, "probability_invalid"
    if raw.dtype != np.float32:
        return None, "probability_invalid"
    if raw.shape != (1, 1):
        return None, "probability_invalid"
    try:
        value = float(raw[0, 0])
    except (TypeError, ValueError, IndexError):
        return None, "probability_invalid"
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        return None, "probability_invalid"
    return value, None


def _run_silero_vad_frame(
    session: Any,
    np: Any,
    *,
    state: Any,
    context: Any,
    frame_samples: Any,
    cancel_event: Optional[threading.Event],
    reject: Callable[[], Optional[str]],
) -> Tuple[Optional[float], Any, Any, Optional[str]]:
    """单帧 512 样本推理；更新 ``state`` 与 64 样本 ``context``。"""
    reject_reason = reject()
    if reject_reason is not None:
        return None, state, context, reject_reason
    if frame_samples.shape[0] != FRAME_SAMPLES:
        return None, state, context, "internal_frame_error"
    model_input = np.concatenate([context, frame_samples])
    if model_input.shape[0] != INPUT_SAMPLES:
        return None, state, context, "internal_frame_error"
    ort_inputs = {
        INPUT_NAME: model_input.reshape(1, -1).astype(np.float32),
        STATE_NAME: state,
        SR_NAME: np.array(SAMPLE_RATE_HZ, dtype=np.int64),
    }
    try:
        outputs = session.run([OUTPUT_NAME, STATE_OUT_NAME], ort_inputs)
    except OSError:
        return None, state, context, "inference_native_error"
    except Exception:
        return None, state, context, "inference_failed"
    if len(outputs) != 2:
        return None, state, context, "inference_output_invalid"
    prob_value, prob_err = _parse_probability_output(outputs[0])
    if prob_err is not None:
        return None, state, context, prob_err
    new_state, state_err = _parse_state_output(outputs[1])
    if state_err is not None:
        return None, state, context, state_err
    reject_reason = reject()
    if reject_reason is not None:
        return None, state, context, reject_reason
    assert prob_value is not None
    new_context = frame_samples[-CONTEXT_SAMPLES:].copy()
    return prob_value, new_state, new_context, None


def _parse_state_output(raw: Any) -> Tuple[Optional[Any], Optional[str]]:
    np, err = _import_numpy()
    if np is None:
        return None, err
    if raw is None or not isinstance(raw, np.ndarray):
        return None, "state_invalid"
    if raw.dtype != np.float32:
        return None, "state_invalid"
    if raw.shape != (2, 1, 128):
        return None, "state_invalid"
    try:
        finite = bool(np.all(np.isfinite(raw)))
    except (TypeError, ValueError):
        return None, "state_invalid"
    if not finite:
        return None, "state_invalid"
    return raw, None


class SileroOnnxVAD:
    """单实例至多一个 ORT InferenceSession；verified bytes 懒加载。"""

    def __init__(
        self,
        *,
        repo_root: Optional[Path] = None,
        session_factory: Optional[SessionFactory] = None,
        close_lock_timeout_sec: float = CLOSE_LOCK_TIMEOUT_SEC,
    ) -> None:
        self._repo_root = repo_root or default_repo_root()
        self._session_factory = session_factory
        self._close_lock_timeout_sec = _validate_close_timeout_sec(
            close_lock_timeout_sec
        )
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._closed = False
        self._session: Any = None
        self._verified_model_bytes: Optional[bytes] = None
        self._load_error: Optional[str] = None
        self._active_ops = 0
        self._active_stream: Optional["SileroVadStreamContext"] = None

    @property
    def closed(self) -> bool:
        return self._closed

    def _reject_if_done(
        self,
        cancel_event: Optional[threading.Event],
    ) -> Optional[VadScores]:
        if cancel_event is not None and cancel_event.is_set():
            return _stable_error("cancelled")
        if self._closed:
            return _stable_error("closed")
        if self._closing.is_set():
            return _stable_error("closing")
        return None

    def close(self, *, timeout_sec: Optional[float] = None) -> bool:
        raw = (
            self._close_lock_timeout_sec
            if timeout_sec is None
            else timeout_sec
        )
        budget = _validate_close_timeout_sec(raw)
        self._closing.set()
        acquired = self._lock.acquire(timeout=budget)
        if not acquired:
            return False
        try:
            if self._active_ops > 0:
                return False
            if self._active_stream is not None:
                self._active_stream._owner_close_invalidate()
                self._active_stream = None
            self._closed = True
            self._session = None
            self._verified_model_bytes = None
            return True
        finally:
            self._lock.release()

    def _load_session_under_lock(
        self,
        cancel_event: Optional[threading.Event],
    ) -> Tuple[Any, Optional[str]]:
        early = self._reject_if_done(cancel_event)
        if early is not None:
            return None, early.reason
        if self._session is not None:
            return self._session, None
        if self._load_error is not None:
            return None, self._load_error
        model_bytes, asset_err = read_verified_silero_model_bytes(self._repo_root)
        if asset_err is not None:
            self._load_error = asset_err
            return None, asset_err
        assert model_bytes is not None
        _, np_err = _import_numpy()
        if np_err is not None:
            self._load_error = np_err
            return None, np_err
        _, ort_err = _import_onnxruntime()
        if ort_err is not None:
            self._load_error = ort_err
            return None, ort_err
        early = self._reject_if_done(cancel_event)
        if early is not None:
            return None, early.reason
        try:
            session = _create_cpu_inference_session(
                model_bytes,
                session_factory=self._session_factory,
            )
        except OSError:
            self._load_error = "model_load_failed"
            return None, "model_load_failed"
        except Exception:
            self._load_error = "model_load_failed"
            return None, "model_load_failed"
        early = self._reject_if_done(cancel_event)
        if early is not None:
            try:
                del session
            except Exception:
                pass
            return None, early.reason
        io_err = _validate_session_io(session)
        if io_err is not None:
            self._load_error = io_err
            try:
                del session
            except Exception:
                pass
            return None, io_err
        early = self._reject_if_done(cancel_event)
        if early is not None:
            try:
                del session
            except Exception:
                pass
            return None, early.reason
        self._verified_model_bytes = model_bytes
        self._session = session
        return self._session, None

    def score_pcm(
        self,
        pcm: bytes,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> VadScores:
        started = time.perf_counter()
        early = self._reject_if_done(cancel_event)
        if early is not None:
            return early
        pcm_err = _validate_pcm_bytes(pcm)
        if pcm_err is not None:
            return _stable_error(pcm_err)

        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return _stable_error("busy")

        try:
            self._active_ops += 1
            early = self._reject_if_done(cancel_event)
            if early is not None:
                return early
            session, load_reason = self._load_session_under_lock(cancel_event)
            if session is None:
                return _stable_error(load_reason or "model_unavailable")

            np, np_err = _import_numpy()
            if np is None:
                return _stable_error(np_err or "numpy_unavailable")

            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            state = np.zeros((2, 1, 128), dtype=np.float32)
            context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
            probabilities: list[float] = []
            valid_samples: list[int] = []
            offset = 0
            total = len(samples)

            def _reject_reason() -> Optional[str]:
                early = self._reject_if_done(cancel_event)
                return early.reason if early is not None else None

            while offset < total:
                end = min(offset + FRAME_SAMPLES, total)
                valid = end - offset
                frame = samples[offset:end]
                if len(frame) < FRAME_SAMPLES:
                    frame = np.pad(frame, (0, FRAME_SAMPLES - len(frame)))
                prob_value, state, context, err = _run_silero_vad_frame(
                    session,
                    np,
                    state=state,
                    context=context,
                    frame_samples=frame,
                    cancel_event=cancel_event,
                    reject=_reject_reason,
                )
                if err is not None:
                    return _stable_error(err)
                assert prob_value is not None
                probabilities.append(prob_value)
                valid_samples.append(int(valid))
                offset += FRAME_SAMPLES

            early = self._reject_if_done(cancel_event)
            if early is not None:
                return early

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return VadScores(
                ok=True,
                reason="ok",
                probabilities=tuple(probabilities),
                valid_samples=tuple(valid_samples),
                elapsed_ms=elapsed_ms,
            )
        finally:
            self._active_ops -= 1
            self._lock.release()

    def create_stream_context(
        self,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> Tuple[Optional["SileroVadStreamContext"], Optional[str]]:
        from src.voice.silero_vad_stream import SileroVadStreamContext

        early = self._reject_if_done(cancel_event)
        if early is not None:
            return None, early.reason
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return None, "busy"
        try:
            early = self._reject_if_done(cancel_event)
            if early is not None:
                return None, early.reason
            if self._active_stream is not None:
                return None, "stream_active"
            ctx = SileroVadStreamContext(self)
            self._active_stream = ctx
            return ctx, None
        finally:
            self._lock.release()

    def _close_stream_context(
        self,
        ctx: "SileroVadStreamContext",
        *,
        timeout_sec: Optional[float] = None,
    ) -> bool:
        ctx._closing.set()
        raw = (
            self._close_lock_timeout_sec
            if timeout_sec is None
            else timeout_sec
        )
        budget = _validate_close_timeout_sec(raw)
        acquired = self._lock.acquire(timeout=budget)
        if not acquired:
            return False
        try:
            if self._active_ops > 0:
                return False
            if self._active_stream is not ctx:
                return bool(ctx.closed)
            self._active_stream = None
            ctx._finalize_close()
            return True
        finally:
            self._lock.release()

    def _reset_stream_context(self, ctx: "SileroVadStreamContext") -> bool:
        if self._closed or self._closing.is_set():
            return False
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return False
        try:
            if self._active_ops > 0:
                return False
            if self._active_stream is not ctx:
                return False
            if self._closed or self._closing.is_set():
                return False
            if ctx.closed or ctx.invalidated or ctx.closing:
                return False
            ctx._state = None
            ctx._context = None
            ctx._invalidated = False
            return True
        finally:
            self._lock.release()

    def _process_owned_stream_frame(
        self,
        ctx: "SileroVadStreamContext",
        pcm: bytes,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> "SileroStreamFrameScores":
        from src.voice.silero_vad_stream import SileroStreamFrameScores

        started = time.perf_counter()
        early = self._reject_if_done(cancel_event)
        if early is not None:
            ctx.invalidate()
            return SileroStreamFrameScores(ok=False, reason=early.reason)
        if ctx.closed or ctx.invalidated or ctx.closing:
            return SileroStreamFrameScores(ok=False, reason="stream_invalid")
        if type(pcm) is not bytes or len(pcm) != FRAME_SAMPLES * BYTES_PER_SAMPLE:
            ctx.invalidate()
            return SileroStreamFrameScores(ok=False, reason="frame_geometry")

        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return SileroStreamFrameScores(ok=False, reason="busy")

        try:
            self._active_ops += 1
            if self._active_stream is not ctx:
                ctx.invalidate()
                return SileroStreamFrameScores(ok=False, reason="stream_invalid")
            early = self._reject_if_done(cancel_event)
            if early is not None:
                ctx.invalidate()
                return SileroStreamFrameScores(ok=False, reason=early.reason)
            if ctx.closing:
                ctx.invalidate()
                return SileroStreamFrameScores(ok=False, reason="closing")
            session, load_reason = self._load_session_under_lock(cancel_event)
            if session is None:
                ctx.invalidate()
                return SileroStreamFrameScores(
                    ok=False, reason=load_reason or "model_unavailable"
                )
            np, np_err = _import_numpy()
            if np is None:
                ctx.invalidate()
                return SileroStreamFrameScores(
                    ok=False, reason=np_err or "numpy_unavailable"
                )
            state, context = ctx._state_arrays(np)
            if state is None:
                ctx.invalidate()
                return SileroStreamFrameScores(ok=False, reason="stream_invalid")

            frame = (
                np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            )

            def _reject_reason() -> Optional[str]:
                if ctx.closing or ctx.invalidated or ctx.closed:
                    return "closing"
                early = self._reject_if_done(cancel_event)
                return early.reason if early is not None else None

            prob_value, state, context, err = _run_silero_vad_frame(
                session,
                np,
                state=state,
                context=context,
                frame_samples=frame,
                cancel_event=cancel_event,
                reject=_reject_reason,
            )
            if err is not None:
                ctx.invalidate()
                return SileroStreamFrameScores(ok=False, reason=err)
            if (
                ctx.closing
                or ctx.invalidated
                or ctx.closed
                or self._active_stream is not ctx
            ):
                ctx.invalidate()
                early = self._reject_if_done(cancel_event)
                reason = early.reason if early is not None else "closing"
                return SileroStreamFrameScores(ok=False, reason=reason)
            ctx._commit_state(state, context)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            assert prob_value is not None
            return SileroStreamFrameScores(
                ok=True,
                reason="ok",
                probability=prob_value,
                valid_samples=FRAME_SAMPLES,
                elapsed_ms=elapsed_ms,
            )
        finally:
            self._active_ops -= 1
            self._lock.release()


SILERO_READINESS_HINT = (
    "Silero CPU VAD 未就绪：请安装 requirements-input-audio.txt（onnxruntime/numpy），"
    "并将校验过的固定模型放入 vendor/silero-vad/silero_vad.onnx（见 docs/VOICE.md）"
)


def probe_silero_install_readiness(
    repo_root: Optional[Path] = None,
) -> Tuple[bool, str]:
    """轻量就绪：词法路径 + 文件大小 + lazy numpy/onnxruntime 导入；不创建 ORT Session。"""
    root = (repo_root or default_repo_root()).resolve()
    asset_path = canonical_silero_asset_path(root)
    try:
        asset_path.relative_to(root)
    except ValueError:
        return False, "Silero 模型路径无效"
    if _path_has_symlink_in_chain(asset_path):
        return False, "Silero 模型路径不可信（链接/reparse）"
    try:
        size = asset_path.stat().st_size
    except OSError:
        return False, "Silero 模型文件不可用"
    if size != EXPECTED_ASSET_BYTES:
        return False, (
            f"Silero 模型大小不匹配（期望 {EXPECTED_ASSET_BYTES} 字节，见 docs/VOICE.md）"
        )
    _, np_err = _import_numpy()
    if np_err is not None:
        return False, f"{SILERO_READINESS_HINT} ({np_err})"
    _, ort_err = _import_onnxruntime()
    if ort_err is not None:
        return False, f"{SILERO_READINESS_HINT} ({ort_err})"
    return True, ""
