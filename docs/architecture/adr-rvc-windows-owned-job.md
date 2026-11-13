# ADR: Windows RVC persistent worker 解释器归属（命名 Job）

**状态**: 已实现（2026-11-02）  
**范围**: `RVC_WORKER` 在 Windows 上的子进程生命周期（非常驻 infer/probe 仍为后续切片）

## 问题

RVC 与 CosyVoice 相同，venv redirector PID 不等于实际 Py311 解释器。仅 terminate redirector 无法保证解释器树退出。

## 决策

1. **`SafetyPolicy.spawn_rvc_worker`** 在 Windows 上于封存校验后复用 `spawn_owned_voice_worker`（与本地 TTS 同一 Job 实现与环境字段）；`parent_pid` 必须等于 `os.getpid()`；未提供 `env` 时复制当前进程环境再写入 Job 字段。
2. **`scripts/rvc_worker.py`** 在 `__main__` 中、重型 import 之前调用 `worker_entry_join_owned_job`；`vc_params` 延迟到转换路径 import。
3. **`RVCWorkerClient`** 单例持续 reader（队列 1、背压、EOF sentinel）；`_OwnerBinding` 快照回收；`_spawn_lock` 为 `RLock`；`stop` 在 `start_inflight` 且无 `_proc` 时立即 `False`；idle owned 跳过阻塞 shutdown 写；`_stdin_writes_inflight` 保护 close；取消后校验 generation/epoch；管道线程 daemon 兜底且 join 失败则 `last_stop_exited=False`。

## 非目标

- 单次 `run_rvc_infer` / `run_rvc_probe` Job 归属（未做）
- 预热/预取切片（未做）
- RVC 环境隐私或线程 cap 新策略（未做）

## 主要代码

- `src/core/safety.py` → `spawn_rvc_worker`
- `src/core/owned_worker_job.py` → `spawn_owned_voice_worker`
- `scripts/rvc_worker.py` → `_early_join_owned_job`
- `src/voice/rvc/worker.py` → reader / stop / start 竞态
