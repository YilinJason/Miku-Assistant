"""SafetyPolicy 本地 TTS 行为测试 runner 绑定。"""
from __future__ import annotations

from typing import Any, Callable, Optional

import subprocess

from src.core.safety import SafetyPolicy

from tests.support.fake_owned_worker_job_backend import FakeOwnedWorkerJobBackend


def bind_fake_local_tts_runners(
    policy: SafetyPolicy,
    *,
    popen_fn: Optional[Callable[..., subprocess.Popen]] = None,
    run_fn: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    owned_worker_job_backend: FakeOwnedWorkerJobBackend | None = None,
) -> FakeOwnedWorkerJobBackend:
    backend = owned_worker_job_backend or FakeOwnedWorkerJobBackend()
    kwargs: dict[str, Any] = {"owned_worker_job_backend": backend}
    if popen_fn is not None:
        kwargs["popen_fn"] = popen_fn
    if run_fn is not None:
        kwargs["run_fn"] = run_fn
    policy.bind_process_runners(**kwargs)
    return backend


def bind_fake_rvc_runners(
    policy: SafetyPolicy,
    *,
    popen_fn: Optional[Callable[..., subprocess.Popen]] = None,
    run_fn: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    owned_worker_job_backend: FakeOwnedWorkerJobBackend | None = None,
) -> FakeOwnedWorkerJobBackend:
    """RVC owned-worker 行为测试 runner 绑定（与本地 TTS fake Job 相同）。"""
    return bind_fake_local_tts_runners(
        policy,
        popen_fn=popen_fn,
        run_fn=run_fn,
        owned_worker_job_backend=owned_worker_job_backend,
    )
