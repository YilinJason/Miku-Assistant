# ADR: RVC 启动预检 — 避免重复 IMPORT_RVC 探针

## 状态

Accepted（2026-11-04，Cursor fake gate 后 STOP 待 Root）

## 背景

``prepare_rvc`` 在 Worker 路径上先跑完整 ``check_rvc_health``（含 ``IMPORT_RVC`` 子进程探针），随后 ``warm_rvc_worker`` 再次加载 RVC，重复重导入。

## 决策

- ``check_rvc_health(..., probe_import=True)`` 为默认，诊断/非 Worker 路径不变。
- ``probe_import=False``：仅文件/脚本/解释器环境预检，**不** spawn ``IMPORT_RVC``；报告 warnings 标明非完整探针。
- ``probe_import`` 须为严格 ``bool``（否则 ``ValueError``）；Worker warm 前 ``_rvc_ready=False``；warm 原样向外抛异常（不吞）；``warm_rvc_worker`` 返回 False 或 sticky ``_rvc_prepare_done`` 时仍 not ready。

## 未改

显式重试、外部 CUDA 探针、health 30s cap、Safety/封存 profile、Root 探针；不声称真实 prepare 固定提速。

## 验证

Root scoped gate：`test_rvc_health_probe_budget`、`test_rvc_startup_preflight`、`test_rvc_worker_lifecycle`、`test_cosyvoice_production_slice_c2b2a`、`test_app_voice_activation`、`test_voice_foreground_bound`
