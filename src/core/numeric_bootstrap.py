# -*- coding: utf-8 -*-
"""主进程启动前固定数值库线程预算（stdlib + 受控 env_modify）。"""
from __future__ import annotations

from src.core.safety import Operation, SafetyPolicy, safe_setenv

_THREAD_ENV_KEYS = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def apply_main_numeric_thread_budget() -> None:
    """
    将当前进程的 BLAS/OpenMP 线程数固定为 1。

    仅用于 ``main.py`` 脚本入口或嵌入方在导入 numpy/ORT 前显式调用。
    内部创建一次性 SafetyPolicy(env_modify=True)，不得外泄或复用于助手运行时。
    """
    policy = SafetyPolicy({"permissions": {Operation.ENV_MODIFY.value: True}})
    for key in _THREAD_ENV_KEYS:
        safe_setenv(policy, key, "1", context="main_numeric_thread_budget")


def thread_env_keys() -> tuple[str, ...]:
    """固定四键名（供测试断言）。"""
    return _THREAD_ENV_KEYS
