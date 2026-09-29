# ADR：有界会话历史、记忆保留与低干扰文件清理

- **状态**：已采纳（Round 1；模式装配与最小权限见后续 Round）
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步

## 问题

长会话与长期记忆、临时音频若只增不减，会导致进程内存、SQLite 体积与清理线程无界增长。全库 `VACUUM` 或启动时同步扫盘会干扰前台对话。仓库根 `tmp/` 与系统临时目录不可被通用清理器误删。

## 决策

1. **`ConversationHistory`**：增加 `max_stored_turns`（默认 500）；每次 `commit_turn` 后按完整 user/assistant 轮裁剪，禁止留下半轮。请求窗口 `request_window` 与存储上限分离。
2. **记忆 retention**：`memory.retention.*` 严格配置；默认 `enabled=false`。启用后按 `max_records` / `max_age_days` 有界 prune；默认保留 `USER_CONFIRMED`；相同时间按 `created_at ASC, id ASC` 稳定淘汰。
3. **维护时机**：`MemoryRuntime.maybe_run_maintenance` 仅在 ingestion idle 且无前台 lease 时运行；禁止在用户请求、召回或每条写入热路径执行 `VACUUM`。`vacuum_offline()` 为显式离线入口。
4. **`FileCleaner`**：启动只调度后台 Timer，调用线程不同步无界扫描；每轮 `max_scan_files` / `max_delete_files`；同实例互斥一轮。规则目录仅限 `data/tmp`、`logs`、`data/test`，不包含仓库根 `tmp/`。
5. **延迟音频删除**：`TempFileManager` 使用单一有界队列 + 单一 daemon Worker；队列满时丢弃并留给定时清理器。
6. **兼容**：默认记忆仍关闭；未开 retention 时不启动维护定时器；历史默认有限但不改变请求窗口语义。
7. **Retention 启用策略**：`memory.retention.enabled` 默认 false；仅开启 `memory.enabled` **不会**自动 prune。需在配置中显式 `retention.enabled=true`；装配启动日志在 memory 开、retention 关时发出警告。默认仍保护 `USER_CONFIRMED`。

## 替代方案

- 启动时同步全量扫盘：阻塞装配，拒绝。
- 每条写入后 VACUUM：锁库干扰前台，拒绝。
- 一文件一线程延迟删除：线程无界，拒绝。

## 回退

恢复无上限的 `ConversationHistory` / 旧 `FileCleaner` / 旧 `TempFileManager`；关闭 `memory.retention`；删除本 ADR。

## 明确未做（Round 2/3）

按模式装配、RVC/GPU 协调、子进程能力收窄、远端 HTTP 策略、日志隐私强化。
