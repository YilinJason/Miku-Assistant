# ADR: Windows 单次 RVC infer/probe 归属（owned run + IO lease）

**状态**: STOP — Root 统一验收（active 票据 + `_retiring` 独占）  
**范围**: `run_rvc_infer` / `run_rvc_probe` 在 Windows 上的 Job 归属、有界捕获、policy 单记录与 infer IO pin；不含预热/预取/candidate。

## 问题

`subprocess.run` 超时后 redirector 可能已退出而 Py311 解释器仍存活；失败路径上 TempFileManager 可能在 worker 仍写 IO 时删除路径。

## 决策

1. **`src/core/owned_rvc_run.py`**：原子 `_ensure_previous_reclaimed`；`finally` 仅 `reserved_gen` 匹配时 `_end_execute(gen)`；`public retire` pending 清理持 `_retiring` 票据；取消不得成功；`owned.kill` 锁外。
2. **`SafetyPolicy.run_rvc_*`**（win32）：校验与 lease 后走 owned run；probe 预算 30s、infer 90s；`sanitize_run_kwargs` 拒绝 `executable`/`preexec_fn`/`args`/`env`/`cwd` 覆盖。
3. **`scripts/rvc_infer.py`** 与固定 probe `-c` 片段：重型 import 前 `worker_entry_join_owned_job`。
4. **`TempFileManager.pin_io_paths`**：单次 infer 输入/输出 pin（每 lease ≤2 路径，全局 ≤8 lease）；`shutdown.complete` 要求 `active_io_leases==0`。
5. **`TtsShutdownStatus.rvc_subprocess_complete`** + Engine/App 关闭时 `retire_rvc_runs` 聚合。

## 未覆盖（不得宣称已闭环）

- 持久 Worker convert 取消未知路径的 pin（机制已存在，本轮未接入 worker 热路径）
- 真实三模型共存 / 无模型 Py311 树验收（Root 职责）

## 主要代码

- `src/core/owned_rvc_run.py`
- `src/core/safety.py` → `run_rvc_infer` / `run_rvc_probe` / `retire_rvc_runs`
- `src/voice/audio/temp_files.py` → `pin_io_paths`
- `src/core/shutdown_status.py` → `rvc_subprocess_complete`
- `tests/test_rvc_owned_run.py`
