# ADR — 主进程数值线程启动预算

## 状态

Accepted（Phase 2a，2026-11-05）

## 背景

Silero CPU ONNX 与 NumPy/ORT 栈在主进程内加载时，若未限制 BLAS/OpenMP 线程，Windows 上可能出现数百 MB 额外内存与不可预测的 CPU 争用。Phase 1 适配器假定 CPU 单线程 ORT session；主程序必须在导入数值库前设置进程环境。

## 决策

- 提供 **无参数** 固定函数 `apply_main_numeric_thread_budget()`（`src/core/numeric_bootstrap.py`），仅写入四个已知键为 `"1"`：`OPENBLAS_NUM_THREADS`、`OMP_NUM_THREADS`、`MKL_NUM_THREADS`、`NUMEXPR_NUM_THREADS`。
- 写入经 `safe_setenv`；函数内部构造 **一次性** `SafetyPolicy(permissions.env_modify=True)`，**不返回、不注入** assembly/assistant/handlers。
- `config.yaml` 中 `security.permissions.env_modify` **保持 false**；用户运行时助手仍不能任意改环境。启动预算属于用户批准的固定可信入口，与运行期权限模型分离。
- `main.py` 仅在 `main()` / `__main__` 路径、在 `import src.cli.app` **之前**调用；`import main` 无副作用。
- 库模块（含 Silero 适配器）不得在 import 时改环境。嵌入方若需同等行为，须在导入 numpy/ORT 前显式调用该函数（见 `docs/VOICE.md`）。
- RVC/Paraformer worker 继续使用各自 worker profile 覆盖 env，不扩大主进程权限。

## 后果

- 未从 `main.py` 启动且未显式 bootstrap 的嵌入场景，数值线程行为可能与正式 CLI 不一致。
- 测试通过 fake 断言四键与 policy 隔离，不依赖真实 ORT。
