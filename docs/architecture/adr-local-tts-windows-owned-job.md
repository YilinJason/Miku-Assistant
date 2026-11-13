# ADR: Windows 本地 TTS runtime 解释器归属（命名 Job）

**状态**: 已实现（2026-10-18）  
**范围**: CosyVoice `LOCAL_TTS_WORKER` 在 Windows 上的子进程生命周期与资源安全

## 问题

venv 启动器（redirector）的 PID 与实际加载模型的 Python 解释器 PID 不同。仅对 redirector `terminate()`/`wait()` 并在 stdout EOF 后判定退出，无法保证真实解释器已结束；实测可出现 redirector 已退出而解释器仍驻留 GPU/RAM。

## 决策

1. **父进程**在通过 `SafetyPolicy.spawn_local_tts_worker` 完成既有封存校验后，创建**会话内唯一命名** Job（`Local\MikuLocalTts_{parent_pid}_{uuid32}`），设置 `KILL_ON_JOB_CLOSE`，禁止 breakaway；将 Job 名写入内部环境字段（不继承 ambient、不允许 caller 覆盖）。
2. **子进程**在 runtime worker `__main__` 中、**任何** `src.voice`/torch/numpy 导入之前，按环境打开该 Job 并将 **当前进程** 自加入，随后立即关闭子侧 Job 句柄。
3. 父侧返回 **Popen 兼容适配器**：`poll`/`wait` 仅在 redirector 已退出 **且** `ActiveProcesses==0` 时返回；查询失败视为未完成（fail-closed）。`terminate`/`kill` 终止 Job 与 redirector。
4. **自定义 `popen_fn` 不得隐式降级为测试 fake 后端**；生产路径始终默认 Win32 原生 containment，测试须显式注入 `owned_worker_job_backend`（见 `tests/support/`）。
5. **`CreateJobObject` 命名冲突**：若返回有效句柄且 `ERROR_ALREADY_EXISTS`，仅关闭本次打开的句柄并拒绝，不修改既有 Job。
6. **句柄关闭失败**不报告终端完成；`CloseHandle` 失败时保留句柄与未完成状态以便重试。`spawn` 在 `BaseException` 路径对有界 `kill`/`wait`/`close` 尽力清理；若仍无法确认 quiescent，将活 `OwnedWorkerProcess` 附到原异常（`_miku_owned_worker_process`，保持原异常类型）供调用方接管，不得先丢弃 owner 再等待。
7. **CosyVoice 客户端**在 `spawn_local_tts_worker` 抛出带 pending owner 的 **任意** `BaseException`（含 `PermissionError`）时，先 `take_owned_worker_from_exception` 再按既有语义处理；`PermissionError` 仍记录并 `return False`，其它控制流异常原样上抛。纳入 `_proc` + `stop_failed`，直至 `shutdown` 有界回收。
8. **`OwnedWorkerProcess` 句柄状态转换**由短 `RLock` 串行化；`wait` 睡眠在锁外。无 redirector 时仍查询 Job：`ActiveProcesses==0` 且成功 `close` 则返回固定失败码（2）；关闭失败或仍有活跃进程则 `poll` 为 `None` 以便 client `shutdown` 重试。
9. **`spawn` 在 `_bind_job_handle` 前中断**：未绑定的 `create_named_job` 句柄先纳入已分配的 owner，再走与 post-Popen 相同的有界清理/异常交接；不得因 `CloseHandle` 失败直接丢弃句柄。
10. **`spawn` 在 `_bind_job_handle` 成功但局部 `orphan_handle` 尚未清零时中断**：清理路径重绑若 owner 已持有同句柄，仅丢弃局部 orphan 标记，不得关闭仍由 owner 持有的句柄（避免「句柄已关、owner 仍记为打开」的陈旧状态）。
11. **有界清理的 quiescence 查询失败**：`_is_spawn_quiescent()` 在 cleanup 末尾抛错视为无法确认退出（等同 cleanup 未完成），保留原异常对象并附 pending owner，不得用查询异常替换原 spawn 异常。

## 非目标 / 限制

- 不是 OS 级沙箱；不替代 SafetyPolicy 路径/资产封存。
- 非 Windows 路径不变；无 Job 环境直接启动 runtime worker 在 Windows 上 **exit 2**（fail-closed）。
- 不使用 taskkill、进程名猜测、全局 Job 复用或 `ERROR_ALREADY_EXISTS` 附着。

## 竞态说明

若 redirector 在子进程自加入前退出，父侧关闭 Job 会导致子进程 `OpenJobObject` 失败并提前退出（无模型加载）。子加入后父进程退出会触发 `KILL_ON_JOB_CLOSE` 清理解释器树。

## 主要代码

- `src/core/owned_worker_job.py`
- `src/core/safety.py` → `spawn_local_tts_worker`
- `scripts/cosyvoice_runtime_worker.py` → `_early_join_owned_job`
