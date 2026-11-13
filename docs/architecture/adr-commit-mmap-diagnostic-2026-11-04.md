# ADR: 诊断提交余量 + isolated Ollama mmap 控制变量（tmp）

## 状态

Accepted（2026-11-04，fake 后 STOP 待 Root 一次新 label 对照）

## 背景

三模型默认复测 FAIL；树 RSS ~2.6GB 而进程 PrivateUsage（commit charge）~17.7GB。不可用 ullAvailVirtual 代替系统提交余量。

## 决策

1. **Sampler**（`tmp/codex_windows_resource_sampler.py`）：`GetPerformanceInfo` → `commit_limit_mb` / `commit_total_mb` / `commit_available_mb`；失败为 `None`。
2. **Supervisor**：preflight `commit_available >= 12GiB`；循环 `< 4GiB` 或不可观测 → 与 RAM guard 同样 owner 停止；报告 `min_commit_available_mb`。
3. **`--ollama-mmap-control`**：仅 `isolated_ollama`；smoke 内 wrapper 对诊断 `OllamaClient` 设 `options.use_mmap=True`，保留 `keep_alive`；**不改**生产 `OllamaClient` 默认。

## 验证

- `tmp/codex_windows_resource_sampler_fake_2026_11_04.py`
- 监督 fake 增补 commit / mmap flags
- `tmp/codex_smoke_ollama_mmap_control_fake_2026_11_04.py`
- 原 Root6 + launcher4 + 监督18

## 未改

`src/core/safety.py`、生产配置、分页文件、驱动；**未跑**真实模型。
