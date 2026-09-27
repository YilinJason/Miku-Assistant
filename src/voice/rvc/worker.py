"""
RVC 常驻 Worker 客户端

通过 stdin/stdout JSON 与 ``scripts/rvc_worker.py`` 通信，模型仅加载一次以加速连续朗读。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

from src.core.paths import PROJECT_ROOT
from src.voice.rvc.device import resolve_rvc_device
from src.voice.rvc.infer import RVC_INFER_SCRIPT, resolve_rvc_python
from src.voice.timing import log_elapsed

logger = logging.getLogger(__name__)

RVC_WORKER_SCRIPT = PROJECT_ROOT / "scripts" / "rvc_worker.py"
_WORKER_START_TIMEOUT = 60.0


def _read_json_line(stream: TextIO, timeout: float = 60.0) -> Optional[dict]:
    """
    有界读取一条 Worker JSON。

    ``TextIO.readline`` 对管道是阻塞调用，原先包在 deadline 循环中并不能真正
    超时。这里由 daemon reader 执行阻塞读取，调用线程只对结果队列有限等待；
    超时后调用方会终止对应 Worker，使 reader 随管道 EOF 退出。
    """
    result: queue.Queue[Optional[dict]] = queue.Queue(maxsize=1)

    def _read() -> None:
        payload: Optional[dict] = None
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                line = line.strip()
                if not line or not line.startswith("{"):
                    logger.debug("Worker 跳过非协议行: %s", line[:120])
                    continue
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    logger.debug("Worker 跳过无效 JSON: %s", line[:120])
        except Exception:
            logger.debug("读取 Worker 协议管道失败", exc_info=True)
        try:
            result.put_nowait(payload)
        except queue.Full:
            pass

    threading.Thread(target=_read, name="RVCWorkerStdout", daemon=True).start()
    try:
        return result.get(timeout=max(0.0, timeout))
    except queue.Empty:
        return None


class RVCWorkerClient:
    """管理 ``scripts/rvc_worker.py`` 子进程，模型只加载一次。"""

    def __init__(self, rvc_config: Dict[str, Any]):
        """
        初始化 Worker 客户端（尚未启动子进程）。

        Args:
            rvc_config: RVC 配置字典（``model_path``、``index_path``、``pitch`` 等）。
        """
        self.rvc_config = dict(rvc_config)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._operation_state_lock = threading.Lock()
        self._operation_epoch = 0
        self._ready = False
        self._device = resolve_rvc_device(rvc_config)
        self._load_ms: Optional[float] = None
        self._converting = threading.Event()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_thread: Optional[threading.Thread] = None

    @property
    def ready(self) -> bool:
        """Worker 子进程已就绪且仍在运行。"""
        return self._ready and self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        """
        启动 Worker 子进程并等待就绪 JSON 行。

        Returns:
            启动并就绪返回 ``True``；缺少环境或模型加载失败返回 ``False``。
        """
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        """在 ``_lock`` 持有期间启动 Worker，避免取消后与下一轮并发重建。"""
        if self.ready:
            return True
        if self._proc is not None:
            self.stop()
        self._stderr_tail.clear()

        python = resolve_rvc_python(self.rvc_config)
        if not python.is_file() or not RVC_WORKER_SCRIPT.is_file():
            logger.error("RVC Worker 启动失败：缺少 Python 环境或 scripts/rvc_worker.py")
            return False

        model_path = self.rvc_config.get("model_path", "")
        if not model_path:
            logger.error("RVC Worker 启动失败：未配置 model_path")
            return False

        cmd = [
            str(python),
            "-u",
            str(RVC_WORKER_SCRIPT),
            "--model", model_path,
            "--device", self._device,
            "--pitch", str(int(self.rvc_config.get("pitch", 0))),
            "--index-rate", str(float(self.rvc_config.get("index_rate", 0.55))),
            "--protect", str(float(self.rvc_config.get("protect", 0.5))),
            "--filter-radius", str(int(self.rvc_config.get("filter_radius", 3))),
            "--rms-mix-rate", str(float(self.rvc_config.get("rms_mix_rate", 0.75))),
            "--f0-method", str(self.rvc_config.get("f0_method", "rmvpe")),
            "--parent-pid", str(os.getpid()),
        ]
        index_path = self.rvc_config.get("index_path")
        if index_path:
            cmd.extend(["--index", index_path])

        try:
            with log_elapsed("RVC Worker 启动+模型加载"):
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    bufsize=1,
                )
                self._start_stderr_drain(self._proc)
                assert self._proc.stdout is not None
                payload = _read_json_line(self._proc.stdout, timeout=_WORKER_START_TIMEOUT)
                if not payload:
                    self._log_stderr_and_stop("Worker 无就绪响应")
                    return False

                if not payload.get("ready"):
                    self._log_stderr_and_stop(payload.get("error", "Worker 就绪失败"))
                    return False

                self._load_ms = float(payload.get("load_ms", 0))
                self._device = payload.get("device", self._device)
                self._ready = True
                logger.info(
                    "[TTS计时] RVC Worker 已就绪 device=%s load=%.0f ms",
                    self._device,
                    self._load_ms,
                )
                return True
        except Exception as exc:
            logger.error("RVC Worker 启动异常: %s", exc, exc_info=True)
            self.stop()
            return False

    @property
    def converting(self) -> bool:
        """当前是否正在启动/预热/转换一项可被插话终止的 RVC 工作。"""
        return self._converting.is_set()

    def _operation_epoch_snapshot(self) -> int:
        with self._operation_state_lock:
            return self._operation_epoch

    def _begin_operation(self, request_epoch: int) -> bool:
        with self._operation_state_lock:
            if request_epoch != self._operation_epoch:
                return False
            self._converting.set()
            return True

    def _operation_is_current(self, request_epoch: int) -> bool:
        with self._operation_state_lock:
            return request_epoch == self._operation_epoch

    def interrupt_current(self) -> bool:
        """使尚未发送的旧任务失效；仅在任务活跃时终止子进程。"""
        with self._operation_state_lock:
            self._operation_epoch += 1
            active = self._converting.is_set()
        if active:
            self.stop()
        return active

    def warm_up(self, timeout: float = 45.0) -> bool:
        """执行一次不播放的短音频推理，消化首段 CUDA/声学路径冷启动。"""
        request_epoch = self._operation_epoch_snapshot()
        with self._lock:
            if not self._begin_operation(request_epoch):
                return False
            try:
                if not self._start_locked():
                    return False
                if not self._operation_is_current(request_epoch):
                    self.stop()
                    return False
                proc = self._proc
                if proc is None or proc.stdin is None or proc.stdout is None:
                    return False
                with log_elapsed("RVC Worker 首次推理预热"):
                    proc.stdin.write(json.dumps({"cmd": "warmup"}) + "\n")
                    proc.stdin.flush()
                    payload = _read_json_line(proc.stdout, timeout=timeout)
                if not payload:
                    self._ready = False
                    self._terminate_process(proc)
                    self._close_process_streams(proc)
                    return False
                if payload.get("ok") and payload.get("warmup"):
                    logger.info(
                        "[TTS计时] RVC Worker 预热 infer=%s ms total=%s ms",
                        payload.get("infer_ms"),
                        payload.get("total_ms"),
                    )
                    return True
                logger.warning("RVC Worker 首次推理预热失败: %s", payload.get("error"))
                return False
            finally:
                self._converting.clear()

    def convert(self, input_path: str, output_path: str, timeout: float = 45.0) -> bool:
        """
        向 Worker 发送单段转换请求并等待结果。

        Args:
            input_path: 输入音频路径。
            output_path: 输出 wav 路径。
            timeout: 等待本段 Worker 响应的最长秒数；超时会终止并回收 Worker。

        Returns:
            转换成功返回 ``True``。
        """
        req = {
            "input": input_path,
            "output": output_path,
            "pitch": int(self.rvc_config.get("pitch", 0)),
            "index_rate": float(self.rvc_config.get("index_rate", 0.55)),
        }

        request_epoch = self._operation_epoch_snapshot()
        with self._lock:
            if not self._begin_operation(request_epoch):
                return False
            if not self._start_locked():
                self._converting.clear()
                return False
            if not self._operation_is_current(request_epoch):
                self.stop()
                self._converting.clear()
                return False
            if not self.ready or self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
                self._converting.clear()
                return False
            proc = self._proc
            try:
                with log_elapsed("RVC Worker 推理"):
                    proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                    payload = _read_json_line(proc.stdout, timeout=timeout)
                if not payload:
                    self._ready = False
                    self._terminate_process(proc)
                    self._close_process_streams(proc)
                    return False
                if payload.get("ok"):
                    infer_ms = payload.get("infer_ms")
                    total_ms = payload.get("total_ms")
                    logger.info(
                        "[TTS计时] RVC Worker 单段 infer=%s ms total=%s ms",
                        infer_ms,
                        total_ms,
                    )
                    return True
                logger.error("RVC Worker 转换失败: %s", payload.get("error"))
                if proc.poll() is not None:
                    self._ready = False
                return False
            except Exception as exc:
                logger.error("RVC Worker 通信异常: %s", exc, exc_info=True)
                self._ready = False
                self._terminate_process(proc)
                self._close_process_streams(proc)
                return False
            finally:
                self._converting.clear()

    def stop(self) -> None:
        """发送 shutdown 命令并等待或强制终止 Worker 子进程。"""
        proc = self._proc
        self._proc = None
        self._ready = False
        if not proc:
            return
        active = self._converting.is_set()
        try:
            if not active and proc.stdin and proc.poll() is None:
                proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            if active:
                # 正在推理的 Worker 不会读取 shutdown；直接有界终止，避免插话
                # 固定多等两秒。
                self._terminate_process(proc)
            else:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._terminate_process(proc)
        finally:
            self._close_process_streams(proc)
            thread = self._stderr_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=0.5)
            self._stderr_thread = None
        logger.info("RVC Worker 已停止")

    def _start_stderr_drain(self, proc: subprocess.Popen) -> None:
        """持续排空 stderr，避免第三方库日志填满管道后阻塞推理进程。"""
        stream = proc.stderr
        if stream is None:
            return

        def _drain() -> None:
            try:
                for line in stream:
                    message = line.strip()
                    if message:
                        self._stderr_tail.append(message)
                        logger.debug("RVC Worker stderr: %s", message[:300])
            except Exception:
                logger.debug("排空 RVC Worker stderr 时结束", exc_info=True)

        self._stderr_thread = threading.Thread(
            target=_drain, name="RVCWorkerStderr", daemon=True
        )
        self._stderr_thread.start()

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        """有界终止 Worker，并回收进程句柄。"""
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                logger.error("RVC Worker 强制终止后仍未退出")

    @staticmethod
    def _close_process_streams(proc: subprocess.Popen) -> None:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def _log_stderr_and_stop(self, reason: str) -> None:
        """
        记录 Worker 启动失败原因与 stderr 片段，并停止子进程。

        Args:
            reason: 简短失败原因描述。
        """
        stderr = " | ".join(self._stderr_tail)[-800:]
        logger.error("RVC Worker 启动失败: %s%s", reason, f" | {stderr}" if stderr else "")
        self.stop()
