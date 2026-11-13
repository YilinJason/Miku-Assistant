# ADR: 隔离 Ollama launcher 与诊断监督器 env 策略（tmp）

## 状态

Accepted（2026-11-04，fake + Root 待复验）

## 问题

三模型 `--isolated-ollama` 在模型启动前失败：`codex_isolated_ollama_launcher` 内 `safe_subprocess_run(env=...)` 被 `sanitize_run_kwargs` 拒绝（禁止覆盖 `env`）。监督器在 child 未 spawn 时 `_safe_poll(None)` 返回 0，summary 可能误报 `exit_code=0`。

## 决策

1. **父监督器**（`codex_real_voice_supervision._start_isolated_server`）继续经 owned spawn 的 `popen_kwargs["env"]` 注入四项 Ollama 变量；launcher 子进程继承。
2. **Launcher**（`tmp/codex_isolated_ollama_launcher_2026_11_03.py`）：import-safe；先 `verify_isolated_ollama_env()`（127.0.0.1:11445 / 1 / 1 / 1m）；**有效 env 后** `worker_entry_join_owned_job()`（继承 Job token），再 `safe_subprocess_run([fixed ollama.exe, "serve"])` **不传 `env`**。不放宽 SafetyPolicy、不用裸 subprocess。
3. **监督器终态**：`run_error` 记录原异常类名；`child_spawned=False` 时 summary/CLI `exit_code` 非 0；`cleanup_complete` 仅表示回收，不代表运行成功。KBI/SystemExit 仍原样传播。

## 验证

- `tmp/codex_isolated_ollama_launcher_fake_2026_11_04.py`（真实 Policy + fake `_run_fn`）
- Root：`tmp/codex_launcher_root_boundary_2026_11_04.py`（join→serve 顺序）
- `tmp/codex_real_voice_diagnostic_supervision_fake_2026_11_03.py` 增补 server/未 spawn 路径
- 既有 Root6 + fake16；**未跑**真实 Ollama/模型

## 未改

`src/core/safety.py`、生产 Core、SDK 导入与关闭协议。
