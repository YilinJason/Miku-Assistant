# ADR: 本地语音前台有界 pending（Cosy 单 Job 加载线程）

## 状态

Accepted（2026-11-03，评审返修 Job 协议）

## 背景

Cosy 冷启动可达数十秒；CLI 不能无限同步阻塞。App 同步所有权（lane + owner 槽）已验证，本切片在其上增加**有界 pending**，不复制第二套 prepare/retire 事务。

Cosy `resource-admission` 超时仍为**未解决性能项**：不得伪 ready、不得放宽默认 30s 模型预算。

## 决策

### 增量接口

| 接口 | 语义 |
|------|------|
| `ensure_tts_for_playback` / `ensure_voice_stack` | 保留同步 single-flight（Root 旧 probe 不变） |
| `request_tts_for_playback` / `request_voice_stack` | 配置/绑定 Cosy 时走 Job 有界路径；受追踪 loader **自创建** `daemon=True`（进程退出兜底，非完成语义）仅调 ensure |
| `request_*` 返回 `ok=True` | **必须**经本次 `ready_fn` 只读确认，不得只认 worker/上一任务 `stored.ok` |

### `_ForegroundLoadJob`（creator / worker / reap）

| 字段 | 职责 |
|------|------|
| `generation` | 与 `done`/result 不跨代复用 |
| `thread` + `start_attempted` / `start_ack` / `start_unknown` / `worker_entered` | 构造返回即挂 `thread`；`start` 尝试后 BaseException → **unknown**（禁 `is_alive=False` 当 quiet）；`worker_entered` 独立於 `terminal` |
| `kind` | `tts` / `voice_stack`；运行中可升为 `voice_stack`，worker 循环补 STT |
| `done` / `result` / `terminal` | worker **finally** 发布；不在 worker 线程 re-raise |
| creator **BaseException** | 含 KBI/SystemExit：finalize（未启动→terminal+done；已启动→保留 thread）；**调用者**重抛 |
| `join_foreground_loader_bounded` | 仅 job 静止时 True；unknown 且线程未 quiet 时 **False**（即使 `is_alive=False`） |
| active 引用 | **仅** reap 时清除（`thread.is_alive()` 假 + join）；worker 不摘 ref |

Sticky：终态失败缓存 `_fg_sticky_failure` + `_tts_load_failed`；Cosy request **禁止**绕过有界机同步 `ensure` 长等；`allow_retry` 清 sticky 后才新 Job。

### `VoiceStackLoadResult.pending`

- `pending=True`：非 ready、非失败  
- CLI/旧适配器：`getattr(load, "pending", False) is True`

### Readiness

- 只读 getter 异常或非 bool → fail-closed（不 `bool(truthy)`）  
- idle 失效清 `_local_voice_prepare_ok`

### Shutdown

- `notify_foreground_shutdown` + interrupt + `join_foreground_loader_bounded`  
- TFM：lane ∨ owner ∨ **未完成 Job**；daemon 兜底**不**视为回收完成，status 仍 incomplete

### CLI

- `standalone` + preflight pending → 文本 REPL（可再 voice），不开麦  
- 注入无 autoload：`request_*` 同步 guard 立即失败

## 后果

- 测试：`tests/test_voice_foreground_bound.py`  
- 监督：`tmp/codex_voice_foreground_bound_supervisor_2026_11_03.py`（60s，一次）

## 非目标

- 预取、默认 30s/声学变更、Cosy admission 解决
