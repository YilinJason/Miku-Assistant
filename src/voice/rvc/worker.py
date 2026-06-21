"""
RVC 常驻 Worker 客户端

通过 stdin/stdout JSON 与 ``scripts/rvc_worker.py`` 通信，模型仅加载一次以加速连续朗读。
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

from src.core.paths import PROJECT_ROOT
from src.voice.rvc.device import resolve_rvc_device
from src.voice.rvc.infer import RVC_INFER_SCRIPT, resolve_rvc_python
from src.voice.timing import log_elapsed

logger = logging.getLogger(__name__)

RVC_WORKER_SCRIPT = PROJECT_ROOT / "scripts" / "rvc_worker.py"
_WORKER_START_TIMEOUT = 300.0


def _read_json_line(stream: TextIO, timeout: float = 60.0) -> Optional[dict]:
    """从 Worker stdout 读取一行 JSON，跳过空行与非 JSON 输出。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line or not line.startswith("{"):
            logger.debug("Worker 跳过非协议行: %s", line[:120])
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Worker 跳过无效 JSON: %s", line[:120])
            continue
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
        self._ready = False
        self._device = resolve_rvc_device(rvc_config)
        self._load_ms: Optional[float] = None

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
        if self.ready:
            return True

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

    def convert(self, input_path: str, output_path: str, timeout: float = 300) -> bool:
        """
        向 Worker 发送单段转换请求并等待结果。

        Args:
            input_path: 输入音频路径。
            output_path: 输出 wav 路径。
            timeout: 保留参数（当前由 Worker 脚本内部控制超时）。

        Returns:
            转换成功返回 ``True``。
        """
        if not self.start():
            return False

        req = {
            "input": input_path,
            "output": output_path,
            "pitch": int(self.rvc_config.get("pitch", 0)),
            "index_rate": float(self.rvc_config.get("index_rate", 0.55)),
        }

        with self._lock:
            if not self.ready or self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
                return False
            try:
                with log_elapsed("RVC Worker 推理"):
                    self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                    self._proc.stdin.flush()
                    payload = _read_json_line(self._proc.stdout, timeout=timeout)
                if not payload:
                    self._ready = False
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
                if self._proc.poll() is not None:
                    self._ready = False
                return False
            except Exception as exc:
                logger.error("RVC Worker 通信异常: %s", exc, exc_info=True)
                self._ready = False
                return False

    def stop(self) -> None:
        """发送 shutdown 命令并等待或强制终止 Worker 子进程。"""
        proc = self._proc
        self._proc = None
        self._ready = False
        if not proc:
            return
        try:
            if proc.stdin and proc.poll() is None:
                proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        logger.info("RVC Worker 已停止")

    def _log_stderr_and_stop(self, reason: str) -> None:
        """
        记录 Worker 启动失败原因与 stderr 片段，并停止子进程。

        Args:
            reason: 简短失败原因描述。
        """
        proc = self._proc
        stderr = ""
        if proc and proc.stderr:
            try:
                stderr = proc.stderr.read(800)
            except Exception:
                pass
        logger.error("RVC Worker 启动失败: %s%s", reason, f" | {stderr}" if stderr else "")
        self.stop()
