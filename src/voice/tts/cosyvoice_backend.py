# -*- coding: utf-8 -*-
"""CosyVoice 本地 TTS 窄适配器（切片 C1）：顺序分句、owned WAV、client 生命周期。"""
from __future__ import annotations

import logging
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, List, Optional, Set, Tuple

from src.core.log_privacy import exception_log_label
from src.core.paths import TMP_DIR
from src.core.safety import Operation, SafetyPolicy
from src.voice.audio.preparation_rollback import request_owned_paths_rollback
from src.voice.audio.temp_files import TempFileManager
from src.voice.cosyvoice.client import CosyVoiceShutdownStatus, CosyVoiceWorkerClient
from src.voice.cosyvoice.settings import CosyVoiceSettings, parse_cosyvoice_settings
from src.voice.language import split_for_speech
from src.voice.rvc.converter import RVCConverter

COSYVOICE_STYLE_ID = "warm_companion"
MAX_TOTAL_SPEECH_CHARS = 3200
MAX_SPEECH_SEGMENTS = 32
MAX_SEGMENT_CHARS_CAP = 120
_INFLIGHT_POLL_SEC = 0.02
MAX_RETAINED_OUTPUTS = 2
_WORKER_RETIRE_TIMEOUT_SEC = 2.0


class CosyVoiceSpeechLimitError(ValueError):
    """投影后文本超出 CosyVoice 顺序播报硬上限。"""


class CosyVoiceSpeechError(RuntimeError):
    """顺序播报中某段合成或准备失败（非用户打断）。"""


def resolve_cosyvoice_settings_from_engine_config(
    cosyvoice_config: Optional[dict],
) -> CosyVoiceSettings:
    """
    将 ``TTSEngine(cosyvoice_config=...)`` 映射为严格 ``CosyVoiceSettings``。

    唯一契约：``None`` 或 ``voice.tts.cosyvoice`` 内层字段映射（C2 传入 inner dict）。
    """
    if cosyvoice_config is None:
        mapping = {"engine": "cosyvoice"}
    elif not isinstance(cosyvoice_config, dict):
        raise ValueError("cosyvoice_config must be None or voice.tts.cosyvoice mapping")
    elif cosyvoice_config.get("engine") is not None or "cosyvoice" in cosyvoice_config:
        raise ValueError(
            "cosyvoice_config must be voice.tts.cosyvoice inner mapping only"
        )
    else:
        mapping = {"engine": "cosyvoice", "cosyvoice": cosyvoice_config}
    settings = parse_cosyvoice_settings(mapping)
    if settings is None:
        raise ValueError("cosyvoice 引擎缺少有效 voice.tts.cosyvoice 配置")
    return settings


def _segment_char_limit(settings: CosyVoiceSettings) -> int:
    return min(MAX_SEGMENT_CHARS_CAP, settings.max_text_chars)


def _hard_split(chunk: str, max_len: int) -> List[str]:
    if len(chunk) <= max_len:
        return [chunk]
    return [chunk[i : i + max_len] for i in range(0, len(chunk), max_len)]


def _segment_discarded_by_cosy_normalizer(segment: str) -> bool:
    """
    CosyVoice frontend ``text_normalize`` drops ``is_only_punctuation`` inputs.

    Omit matching planned segments so worker inference is not invoked for text
    the vendor would discard anyway (P/S categories and whitespace only).
    """
    if not segment or segment.isspace():
        return True
    for ch in segment:
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if not cat:
            return False
        if cat[0] in ("P", "S"):
            continue
        return False
    return True


def plan_cosyvoice_segments(text: str, settings: CosyVoiceSettings) -> List[str]:
    """
    顺序播报分句计划：自然句界/逗号优先，无标点长句硬切，不丢字。

    Raises:
        CosyVoiceSpeechLimitError: 超出 3200 字或 32 段上限。
    """
    if not text:
        return []
    if len(text) > MAX_TOTAL_SPEECH_CHARS:
        raise CosyVoiceSpeechLimitError(
            f"CosyVoice 播报拒绝: 投影后文本超过 {MAX_TOTAL_SPEECH_CHARS} 字符"
        )
    max_seg = _segment_char_limit(settings)
    rough = split_for_speech(text, max_chars=max_seg)
    segments: List[str] = []
    for part in rough:
        segments.extend(_hard_split(part, max_seg))
    if len(segments) > MAX_SPEECH_SEGMENTS:
        raise CosyVoiceSpeechLimitError(
            f"CosyVoice 播报拒绝: 分句数超过 {MAX_SPEECH_SEGMENTS} 段"
        )
    return [
        segment
        for segment in segments
        if not _segment_discarded_by_cosy_normalizer(segment)
    ]


@dataclass(frozen=True)
class CosyVoicePlayable:
    play_path: Optional[str]
    temp_paths: Tuple[str, ...]


class CosyVoiceTtsBackend:
    """本地 CosyVoice 合成 + 可选 RVC；一次只持有一段 owned WAV。"""

    def __init__(
        self,
        settings: CosyVoiceSettings,
        safety_policy: SafetyPolicy,
        temp_file_manager: TempFileManager,
        *,
        client: Optional[CosyVoiceWorkerClient] = None,
    ) -> None:
        if safety_policy is None:
            raise PermissionError("CosyVoiceTtsBackend 需要显式 SafetyPolicy")
        if temp_file_manager is None:
            raise PermissionError("CosyVoiceTtsBackend 需要 TempFileManager")
        self._settings = settings
        self._policy = safety_policy
        self._temp_files = temp_file_manager
        self._logger = logging.getLogger(__name__)
        self._client = client or CosyVoiceWorkerClient(
            settings, safety_policy, temp_file_manager
        )
        self._last_shutdown: Optional[CosyVoiceShutdownStatus] = None
        self._closing = False
        self._lifecycle_lock = threading.Lock()
        self._inflight_calls = 0
        self._retained_outputs: Set[str] = set()
        self._retained_revision = 0
        self._pending_worker_retire = False

    @property
    def client(self) -> CosyVoiceWorkerClient:
        return self._client

    def inflight_calls(self) -> int:
        with self._lifecycle_lock:
            return int(self._inflight_calls)

    def retained_output_count(self) -> int:
        with self._lifecycle_lock:
            return len(self._retained_outputs)

    def reconcile_released_outputs(self) -> None:
        """
        被动对账：从 retained 快照移除 TFM 已不再 owned 的条目。

        不调用 TFM.remove、不 retire/prepare/合成；``is_owned`` 异常原样传播。
        快照后 revision 变化则放弃本轮删除，避免并发新增被误清。
        """
        with self._lifecycle_lock:
            snapshot = frozenset(self._retained_outputs)
            revision = self._retained_revision
        if not snapshot:
            return
        released: Set[str] = set()
        for path in snapshot:
            if not self._temp_files.is_owned(path):
                released.add(path)
        if not released:
            return
        with self._lifecycle_lock:
            if self._retained_revision != revision:
                return
            self._retained_outputs.difference_update(released)
            self._retained_revision += 1

    def last_shutdown_status(self) -> Optional[CosyVoiceShutdownStatus]:
        return self._last_shutdown

    def interrupt(self) -> None:
        self._client.interrupt()

    def prepare(
        self,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        """显式预热本地 worker；走 C1 在途计数与待删 owner 两阶段 gate，零合成。"""
        if not self._enter_call():
            return False
        try:
            if not self._gate_synth_allocation():
                return False
            return self._client.prepare(cancel_event=cancel_event)
        finally:
            self._exit_call()

    def _enter_call(self) -> bool:
        with self._lifecycle_lock:
            if self._closing:
                return False
            self._inflight_calls += 1
            return True

    def _exit_call(self) -> None:
        with self._lifecycle_lock:
            self._inflight_calls = max(0, self._inflight_calls - 1)

    def _cleanup_retained_outputs(self) -> None:
        self._release_retained_outputs()

    def _release_retained_outputs(self) -> None:
        with self._lifecycle_lock:
            paths = list(self._retained_outputs)
        if not paths:
            return
        released: Set[str] = set()
        for path in paths:
            try:
                self._temp_files.remove(path)
            except Exception:
                self._logger.debug(
                    "CosyVoice 保留输出清理跳过 path=%s",
                    path,
                    exc_info=True,
                )
            if not self._temp_files.is_owned(path):
                released.add(path)
        if released:
            with self._lifecycle_lock:
                self._retained_outputs.difference_update(released)
                self._retained_revision += 1

    def _mark_failed_output(self, path: str) -> None:
        with self._lifecycle_lock:
            self._retained_outputs.add(path)
            self._retained_revision += 1
            self._pending_worker_retire = True

    def _track_deferred_owned_paths(self, paths: Tuple[str, ...]) -> None:
        """播放/异常清理后仍 owned 的本后端路径，纳入有界待删集合（不触发 worker 退休）。"""
        deferred: Set[str] = set()
        for path in paths:
            if path and self._temp_files.is_owned(path):
                deferred.add(path)
        if not deferred:
            return
        with self._lifecycle_lock:
            self._retained_outputs.update(deferred)
            self._retained_revision += 1

    def _retire_pending_worker_if_needed(self, timeout: float) -> bool:
        with self._lifecycle_lock:
            pending_retire = self._pending_worker_retire
        if not pending_retire:
            return True
        if self._client.stop_failed:
            return False
        retire = getattr(self._client, "retire_worker", None)
        if retire is None:
            return False
        status = retire(timeout=timeout)
        if not status.complete:
            return False
        with self._lifecycle_lock:
            self._pending_worker_retire = False
        return True

    def _gate_synth_allocation(self) -> bool:
        """待删 owned 路径存在时拒绝分配；失败路径须先 retire 成功再尝试 TFM 回收。"""
        if self._closing or self._client.stop_failed:
            return False
        if not self._retire_pending_worker_if_needed(_WORKER_RETIRE_TIMEOUT_SEC):
            return False
        self._release_retained_outputs()
        with self._lifecycle_lock:
            if self._retained_outputs:
                return False
        return True

    def _wait_inflight_until(self, deadline: float) -> bool:
        while time.monotonic() < deadline:
            if self.inflight_calls() == 0:
                return True
            time.sleep(_INFLIGHT_POLL_SEC)
        return self.inflight_calls() == 0

    def shutdown_local(self, timeout: float = 2.0) -> CosyVoiceShutdownStatus:
        """有界关闭本地 worker；client.shutdown 推进 epoch/终止，再在剩余 budget 内等在途并复核状态。"""
        self._closing = True
        deadline = time.monotonic() + max(0.0, float(timeout))
        remaining = max(0.0, deadline - time.monotonic())
        self._client.shutdown(timeout=remaining)
        inflight_ok = self._wait_inflight_until(deadline)
        remaining = max(0.0, deadline - time.monotonic())
        continue_shutdown = getattr(self._client, "continue_shutdown", None)
        if continue_shutdown is not None:
            status = continue_shutdown(timeout=remaining)
        else:
            status = self._client.shutdown(timeout=remaining)
        worker_alive = status.worker_alive
        reader_alive = status.reader_alive
        writer_alive = status.writer_alive
        complete = bool(
            status.complete
            and inflight_ok
            and self.inflight_calls() == 0
        )
        if complete:
            remaining = max(0.0, deadline - time.monotonic())
            if self._retire_pending_worker_if_needed(remaining):
                self._cleanup_retained_outputs()
        aggregate = CosyVoiceShutdownStatus(
            complete=complete,
            worker_alive=worker_alive,
            reader_alive=reader_alive,
            writer_alive=writer_alive,
        )
        self._last_shutdown = aggregate
        return aggregate

    def synthesize_owned_wav(
        self,
        text: str,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[str]:
        """在固定 tmp 下创建 owned WAV 并合成；失败返回 ``None`` 且保留输出路径。"""
        if self._closing or self._client.stop_failed:
            return None
        if not self._gate_synth_allocation():
            return None
        if len(text) > self._settings.max_text_chars:
            self._logger.error("CosyVoice 单段文本超过 max_text_chars")
            return None
        wav_path = self._temp_files.create(".wav", directory=TMP_DIR)
        try:
            ok = self._client.synthesize(
                text,
                style_id=COSYVOICE_STYLE_ID,
                output_path=wav_path,
                cancel_event=cancel_event,
            )
            if not ok:
                self._mark_failed_output(wav_path)
                return None
            return wav_path
        except BaseException:
            self._mark_failed_output(wav_path)
            raise

    def prepare_playable(
        self,
        text: str,
        *,
        rvc: Optional[RVCConverter],
        rvc_ready: bool,
        cancel_event: Optional[threading.Event],
        interrupted: Callable[[], bool],
    ) -> CosyVoicePlayable:
        temp_paths: Set[str] = set()
        if interrupted():
            return CosyVoicePlayable(None, ())
        wav_path: Optional[str] = None
        try:
            wav_path = self.synthesize_owned_wav(text, cancel_event=cancel_event)
            if not wav_path:
                return CosyVoicePlayable(None, ())
            temp_paths.add(wav_path)
            if interrupted():
                return CosyVoicePlayable(None, tuple(temp_paths))
            play_path = wav_path
            if rvc is not None and rvc.enabled:
                if not rvc_ready:
                    self._logger.error(
                        "RVC 未就绪，将播放 CosyVoice 基础本地音频（未经 RVC）"
                    )
                else:
                    self._logger.info("开始 RVC 音色转换（CosyVoice 基础本地音频）...")
                    try:
                        rvc_path = rvc.convert_file(wav_path)
                    except Exception as exc:
                        self._logger.error(
                            "RVC 转换失败，回退 CosyVoice 基础本地音频 err=%s",
                            exception_log_label(exc),
                        )
                        rvc_path = None
                    if rvc_path:
                        play_path = rvc_path
                        temp_paths.add(rvc_path)
                    elif rvc_ready:
                        self._logger.error(
                            "RVC 转换失败，回退 CosyVoice 基础本地音频"
                        )
            if interrupted():
                return CosyVoicePlayable(None, tuple(temp_paths))
            return CosyVoicePlayable(play_path, tuple(temp_paths))
        except BaseException as exc:
            if temp_paths:
                request_owned_paths_rollback(
                    self._temp_files, *temp_paths, primary=exc
                )
                self._track_deferred_owned_paths(tuple(temp_paths))
            elif wav_path:
                self._mark_failed_output(wav_path)
            raise

    def speak_sequential(
        self,
        text: str,
        *,
        play_fn: Callable[[str], None],
        rvc: Optional[RVCConverter],
        rvc_ready: bool,
        cancel_event: Optional[threading.Event],
        interrupted: Callable[[], bool],
    ) -> None:
        """
        顺序分句播报：句间无预取；每段播放后立即回收 owned 文件。

        句间存在合成+播放串行等待，不是流式无等待播报（C2 再评估有界预取）。
        """
        if not self._enter_call():
            return
        try:
            segments = plan_cosyvoice_segments(text, self._settings)
            for idx, segment in enumerate(segments, start=1):
                if interrupted():
                    break
                label = f"CosyVoice 第{idx}/{len(segments)}段"
                playable = self.prepare_playable(
                    segment,
                    rvc=rvc,
                    rvc_ready=rvc_ready,
                    cancel_event=cancel_event,
                    interrupted=interrupted,
                )
                try:
                    if interrupted():
                        break
                    if playable.play_path is None:
                        raise CosyVoiceSpeechError(
                            f"{label} 合成或准备失败，已中止本次播报"
                        )
                    play_fn(playable.play_path)
                finally:
                    if playable.temp_paths:
                        try:
                            self._temp_files.remove_many(*playable.temp_paths)
                        except Exception:
                            self._logger.debug(
                                "%s 临时文件清理失败", label, exc_info=True
                            )
                        self._track_deferred_owned_paths(playable.temp_paths)
        finally:
            self._exit_call()

    def save_base_wav(
        self,
        text: str,
        filename: str,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """
        将单段文本写入调用方已登记的固定 tmp ``.wav``（不经 RVC）。

        ``filename`` 为调用方原始路径字符串，不在派发前 canonicalize。

        Raises:
            ValueError / PermissionError: 目标或文本非法。
            RuntimeError: 合成失败或取消。
        """
        if self._closing:
            raise RuntimeError("CosyVoice save_to_file 已关闭")
        if not self._enter_call():
            raise RuntimeError("CosyVoice save_to_file 已关闭")
        try:
            cleaned = text.strip()
            if not cleaned:
                raise ValueError("save_to_file 文本为空")
            if len(cleaned) > self._settings.max_text_chars:
                raise ValueError(
                    f"save_to_file 文本超过 max_text_chars="
                    f"{self._settings.max_text_chars}"
                )
            if not str(filename).lower().endswith(".wav"):
                raise ValueError("CosyVoice save_to_file 仅支持 .wav")
            self._policy.require(Operation.LOCAL_TTS_WORKER, "CosyVoice save_to_file")
            key = self._temp_files.assert_owned_regular_file(
                filename, label="CosyVoice save_to_file 目标"
            )
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("CosyVoice save_to_file 已取消")
            if not self._gate_synth_allocation():
                raise RuntimeError("CosyVoice save_to_file 资源未恢复")
            ok = self._client.synthesize(
                cleaned,
                style_id=COSYVOICE_STYLE_ID,
                output_path=filename,
                cancel_event=cancel_event,
            )
            if not ok:
                self._mark_failed_output(key)
                raise RuntimeError("CosyVoice save_to_file 合成失败")
        finally:
            self._exit_call()
