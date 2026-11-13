# ADR：SQLite 记忆库路径、连接硬化与删除计划隐私边界（Round 3C2）

- **状态**：已接受（Codex 最终复验通过）
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步
- **任务**：`docs/cursor-task-security-round3-c2.md`

## 问题

1. 记忆库路径可被 symlink/junction/reparse/硬链接重定向到库外文件。
2. 运行时父目录或库文件身份变化后仍可能继续写 SQL。
3. 扩展加载 / ATTACH / 临时落盘扩大文件访问面；已删正文可能残留在 journal。
4. 删除确认计划 v1 含完整记忆正文与绝对数据库路径，且可在未改 `--db` 时由计划替 CLI 选择写库目标。

## 决策

1. **集中边界**：`src/memory/sqlite_security.py` 由 `SqliteMemoryStore` 与 `id_audit` 共用；拒绝危险文件名/后缀、目录、reparse、多硬链接；从最近已存在祖先**逐段**创建父目录并拒绝任意 junction/symlink 祖先；新库在 hook/connect 前于可信父目录 **exclusive 预建** 并绑定文件身份；读写统一 URI ``mode=rw|ro&cache=private``（**不用 rwc**，避免父目录换成 junction 后在外部自动建空库）；connect 前后复验父目录与库文件身份。
2. **连接硬化**：读写与只读均经 helper；校验 `foreign_keys`、`trusted_schema=OFF`、`temp_store=MEMORY`、读写 `journal_mode=DELETE`、`secure_delete=ON`（未生效则构造失败）；`enable_load_extension(False)`；`SQLITE_LIMIT_ATTACHED=0`（API 可用时）；busy timeout 有界 ≤5s；只读 URI `mode=ro&cache=private` + `query_only=ON`。
3. **计划 v2**：无 `text`/`dedupe_key`/数据库路径；条目仅 ID、`updated_at`、带 `plan_nonce` 的行指纹 + `db_fingerprint`；`--execute` 始终用 CLI `--db`，指纹不一致则零删除；事务内重读核对指纹后再删；`BEGIN` 后任意 `BaseException` 统一 rollback（不吞 KI/SystemExit）；计划文件 `O_EXCL` 写入；v1 默认拒绝。
4. **Store 事务**：`_transaction` 将 yield、事后复验与 **COMMIT** 置于同一 guard；COMMIT 失败亦先 rollback，避免悬挂事务。
5. **错误映射**：运行时路径/身份失效统一为 `MemoryStoreUnavailableError`；构造参数非法仍为 `MemoryValidationError`。
6. **明文与 TOCTOU 剩余风险**：标准库 sqlite3 无静态加密；同 Windows 用户/管理员/恶意软件/镜像仍可读明文。标准库 VFS 与同用户目录句柄竞态无法在任意时刻彻底消除；本轮通过预建 + `mode=rw` + 复验把可复现的外部空库副作用关掉，**不宣称**已消除全部 TOCTOU。
7. **sidecar / VFS 限制**：新 sidecar 由 SQLite VFS 创建；本轮校验已存在 sidecar。
8. **生命周期**：导入与 `memory.enabled=false` 零库副作用；单连接 + RLock、schema/retention/离线 VACUUM 契约保持；每次事务常数次 stat/PRAGMA，无新线程/子进程。

## 非目标

- SQLCipher / icacls / 注册表 / 扩大子进程
- 自然陪伴体验
- 读取、迁移或 VACUUM 真实 `data/memory`

## 后果

- 路径与身份竞态在写 SQL 前 fail closed；计划不再复制正文或改库目标。
- 旧 v1 计划需重新 dry-run 生成 v2。
- 离线 unittest 通过 ≠ 真实生产库或同用户攻击面已消除。
