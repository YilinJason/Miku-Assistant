# ADR: Windows 模型冷启动主机内存 / commit 准入

## 状态

Accepted（2026-11-04，Cursor fake gate 后 STOP 待 Root）

## 背景

三模型并行冷启动可能耗尽物理 RAM 与系统 commit；需在 spawn 前 fail-closed，且不先跑 RVC CUDA 重探针。

## 决策

- ``src/core/host_memory.py``：Microsoft 完整 ``PERFORMANCE_INFORMATION``（Win64 104 字节；``PageSize`` offset 80；尾三 DWORD）；stdlib-only ``GetPerformanceInfo``。
- 构造失败：``raise ModelStartupMemoryError``（禁止保留 ``auto`` 占位设备）；``TTSEngine.prepare_rvc`` 仅 catch 该受控类型。
- 阈值（保守起点，非容量保证）：RVC 4096/14336 MiB；CosyVoice 8192/10240 MiB；Paraformer 4096/6144 MiB（物理/commit；commit 下限依据 2026-11-05 监督 diagnostic 冷启动观测 tree private≈4520 MiB，`tmp/asr_gate_infer_nov5o.json`，仍非峰值保证）。
- ``start``/一次性 infer/Cosy ``_start_worker_poll`` spawn 前复查；已 ``ready`` 不复检。
- 拒绝：日志 + 稳定 ``reason`` 标签；不吞普通异常；不伪 ready。

## 验证

Root scoped gate：``test_host_memory_admission`` + RVC/本地 TTS 相关回归（见交接）。

## 未改

LLM/默认模型/声学/budget/NativeJob/Root 探针/正式 memory SQLite。
