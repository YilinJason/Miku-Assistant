# ADR：持久日志隐私与 logs/ 文件边界（Round 3C1）

- **状态**：已采纳；Codex 最终离线复验通过（F1–F13）
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步
- **任务**：`docs/cursor-task-security-round3-c1.md`；评审 `docs/codex-review-security-round3-c1.md`

## 问题

1. 日志文件名 / 根目录可越界；硬链接与运行时 junction 替换可写到 `logs/` 外。
2. 用户正文、路径、异常原文进入持久日志。
3. 轮转容量可被 `backupCount=0` / 极小 MB→`maxBytes=0` 静默关掉；路径 `open("w")` 可在 fail closed 前截断外部硬链接目标。
4. 业务 `ValueError` 与安全边界失败混用；运行时关闭/更名遗留句柄；已有文件打开后未比对当前路径与 fd。

## 决策

1. **路径边界**：安全文件名；`LOGS_DIR` 非 symlink/junction；拒绝多硬链接；handler 绑定可信根身份并在每次打开/轮转前后复验。Windows 打开使用 `FILE_SHARE_DELETE`，路径在已持有句柄时仍可被替换，由身份复验 fail closed（不依赖独占锁掩盖竞态）。
2. **专用异常（F8/F13）**：`LogBoundaryError(ValueError)` 仅用于路径/根/reparse/硬链接/路径↔fd 身份失败；`emit` 只对该异常 fail-closed 并上抛。普通格式化/写入/截断 `OSError` 关闭不确定流后保留为普通异常，走 `handleError()`，不从 `logger.info` 冒泡。
3. **有界容量（F9/F12）**：`max_size_mb_to_bytes` 上取整且至少 1 字节；`backup_count=0` 不创建 `.1`，且**不得**按路径二次 `open(..., "w")`；仅对已通过 `_open()` 全套校验的 fd 做 `seek(0)`/`truncate(0)`，截断前后复验 fd、当前路径与可信根；路径被换入外部对象时只影响原安全句柄并随后 fail closed。
4. **应用自有 handler（F10）**：`_miku_app_owned` 标记；`file_enabled=False` 移除并关闭全部自有文件 handler；文件名变化先创建新再关旧；创建失败保留旧 handler；同路径参数更新持 `handler.lock`。
5. **打开后身份（F11）**：无论打开前是否存在，打开后重新校验当前路径，并比对打开前身份（如有）、当前路径身份与 fd；不一致关闭并抛 `LogBoundaryError`。
6. **默认无正文日志** + `exception_log_label`；`include_tracebacks` 默认 false。
7. **明文本地日志 ≠ 加密**。

## 非目标

- Round 3C2 / 自然陪伴 / 加密日志

## 后果

- junction 替换与打开/截断竞态均 fail closed；外部 sentinel 不被路径截断破坏。
- 业务坏消息格式化不中断主程序；关闭/更名不遗留打开句柄；容量配置不会再静默变成无限追加。
