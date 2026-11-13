# ADR: RVC 子进程 CPU / BLAS / Torch 线程预算

## 状态

Accepted（2026-11-04，Root独立6+仓库13/66与最终fixture60，WAV826零差；docs/codex-review-rvc-cpu-budget-2026-11-04.md）

## 背景

CosyVoice 子进程已有 4 线程 BLAS 上限；RVC 三路径（常驻 worker、一次性 infer、固定 probe）此前未设 Torch/BLAS 预算，CPU 回退或多模型并存时易与主进程及其他 worker 争抢 CPU。

## 决策

- 新增 ``src/voice/rvc/cpu_budget.py``（stdlib-only）：``compute_rvc_cpu_thread_budget`` 固定 ``max(1, min(4, (os.cpu_count() or 2)//2))``；``child_env_with_rvc_cpu_budget`` 从给定 mapping copy、**按 casefold 去掉四键全部大小写变体**后写入标准 ``OMP/MKL/OPENBLAS/NUMEXPR`` 键，不 ``setdefault``、不修改父 ``os.environ``（Windows 下 ``omp_num_threads`` 与 ``OMP_NUM_THREADS`` 不能并存绕过）。
- ``SafetyPolicy.spawn_rvc_worker``：sanitize 后、实际 spawn 前注入 child env（Windows owned 与非 Windows popen 均覆盖）。
- ``execute_owned_rvc_run``（Windows infer/probe）：内部 popen env 使用同一 helper；``run_rvc_infer`` / ``run_rvc_probe`` 非 Windows ``run`` 路径同样注入。
- ``apply_rvc_torch_thread_budget``：仅 ``torch.set_num_threads`` / ``set_num_interop_threads``；**不**写 ``os.environ``（BLAS 仅经 spawn child env）。
- ``scripts/rvc_worker.py`` / ``scripts/rvc_infer.py``：模型加载前 ``apply_rvc_torch_thread_budget()`` 一次；失败 ``SystemExit(1)``。
- 固定 ``rvc_probe`` ``-c`` 引导片段在 ``import torch`` / ``rvc_python`` 前调用同一 apply helper；不开放调用方自定义 probe 代码。
- **不**新增 config 热调参；**不**调用 ``safe_setenv`` 修改用户主进程环境；**不**改变 argv/Job/IO/超时/输出协议。

## 验证（fake / 设计边界）

- ``tests/test_rvc_cpu_budget.py``：预算有界、恶意 env 被 child copy 覆盖、父环境不变、fake ``torch`` 调用顺序、三路径 fake spawn 捕获 env、未知 ``env`` run 参数零 spawn。
- 回归门禁：**仅** ``tmp/codex_scoped_test_gate_2026_11_02.py --label rvc_cpu_cursor_20261104b --modules … --timeout 120``（禁止第二监督器；``tmp/cursor_rvc_cpu_budget_gate_2026_11_04.py`` 已标 UNUSED）。

**未证明**：真实 CPU 占用率、端到端延迟、与 Cosy/LLM 并存的实测争抢缓解。

## 未改

声音模型参数、RVC 声学默认、Root probe/监督器、legacy 播报/TFM/LLM 性能项、正式 memory。
