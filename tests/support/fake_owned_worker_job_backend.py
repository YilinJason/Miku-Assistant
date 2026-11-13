"""离线测试用 Job 后端（非生产路径）。"""
from __future__ import annotations

import subprocess
from typing import Any, Optional


class FakeOwnedWorkerJobBackend:
    """可模拟 redirector 先退出而解释器仍活跃的 Job 后端。"""

    def __init__(self) -> None:
        self.created_names: list[str] = []
        self.opened_names: list[str] = []
        self.joined_names: list[str] = []
        self._next_handle = 1
        self._jobs: dict[int, dict[str, Any]] = {}
        self._redirector_for_job: dict[int, subprocess.Popen | None] = {}
        self.last_created_handle: int | None = None

    def attach_redirector(self, job_handle: int, proc: subprocess.Popen) -> None:
        self._redirector_for_job[job_handle] = proc

    def create_named_job(self, name: str) -> int:
        if name in self.created_names:
            raise OSError("job already exists")
        handle = self._next_handle
        self._next_handle += 1
        self.created_names.append(name)
        self._jobs[handle] = {"name": name, "active": 0, "terminated": False}
        self.last_created_handle = handle
        return handle

    def open_named_job(self, name: str) -> int:
        if name not in self.created_names:
            raise OSError("job missing")
        handle = self._next_handle
        self._next_handle += 1
        self.opened_names.append(name)
        self._jobs[handle] = {"name": name, "active": 1, "terminated": False}
        self.joined_names.append(name)
        return handle

    def set_kill_on_close(self, job_handle: int) -> None:
        if job_handle not in self._jobs:
            raise OSError("invalid job")

    def assign_current_process(self, job_handle: int) -> None:
        if job_handle not in self._jobs:
            raise OSError("invalid job")
        self._jobs[job_handle]["active"] = max(1, self._jobs[job_handle]["active"])

    def query_active_process_count(self, job_handle: int) -> Optional[int]:
        job = self._jobs.get(job_handle)
        if job is None:
            return None
        if job.get("terminated"):
            return 0
        if job.get("force_active"):
            return int(job["force_active"])
        redirector = self._redirector_for_job.get(job_handle)
        if redirector is not None and redirector.poll() is None:
            return 1
        return int(job.get("active", 0))

    def terminate_job(self, job_handle: int, exit_code: int) -> None:
        job = self._jobs.get(job_handle)
        if job is None:
            raise OSError("invalid job")
        job["terminated"] = True
        job["active"] = 0
        redirector = self._redirector_for_job.get(job_handle)
        if redirector is not None and redirector.poll() is None:
            try:
                redirector.terminate()
            except OSError:
                pass

    def close_handle(self, handle: int) -> None:
        self._jobs.pop(handle, None)
        self._redirector_for_job.pop(handle, None)
