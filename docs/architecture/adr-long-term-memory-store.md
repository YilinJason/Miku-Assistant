# ADR：长期记忆领域契约与 SQLite 持久层

- **状态**：已采纳（仅持久层底座；未接入对话）
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步

## 问题

`ConversationHistory` 只管理进程内短期对话窗口，不能承担跨会话长期记忆。若直接把聊天记录写入模型客户端或提示词层，会混淆短期上下文与长期事实，也无法独立验证存储、schema 与来源真实性。

## 决策

1. 在 `src/memory/` 建立独立模块：无 I/O 领域对象 + `MemoryStore` Protocol + `SqliteMemoryStore` 适配器。
2. **短期历史与长期记忆分离**：不修改 `ConversationHistory` / `OllamaClient` / `VoiceSession` 行为；本轮不装配到产品路径。
3. **来源真实性**：`USER_CONFIRMED` 与 `MODEL_INFERRED` 原样持久化；更新不得隐式升级来源。
4. **去重**：仅对规范化键（去首尾空白、压缩连续空白、大小写折叠）做精确唯一约束；**展示正文原样保存（含首尾与内部空白）**，规范化只作用于去重键，不改写用户可见正文。不做语义合并。
5. **Schema**：`PRAGMA user_version`；当前版本为 `1`。遇到更高版本非破坏性拒绝（`MemorySchemaIncompatibleError`），不自动降级、清表或覆盖。
6. **故障契约**：无效输入 → `MemoryValidationError`；缺失 → `MemoryNotFoundError`；关闭/I/O → `MemoryStoreUnavailableError`；破损行 → `MemoryDataCorruptError`。不吞 `KeyboardInterrupt` / `SystemExit`，不用空列表伪装故障。
7. **实现约束**：仅用标准库 `sqlite3`；参数化 SQL；写操作与 schema 初始化使用真实事务（非 autocommit；`BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK`）；schema 失败不得提升 `user_version`；单连接 + `RLock` 串行；导入模块不打开库；测试库仅用 `TemporaryDirectory`。列表硬上限 500，另允许 `limit=501` 作为应用层 has-more 有界探测槽位。
8. **隐私**：日志默认不写完整记忆正文；不默认保存语音转写或整段聊天。

## 替代方案

- 把记忆塞进 `ConversationHistory`：混淆窗口与长期事实，拒绝。
- 本轮引入向量库/ORM/embeddings：扩大依赖与范围，拒绝。
- 高版本 schema 自动清库：有数据丢失风险，拒绝。

## 回退

删除 `src/memory/`、`tests/test_memory_store.py` 与本 ADR；不影响现有对话链路。

## 明确未做

自动提取服务与检索（见 `adr-memory-service.md`）、提示词注入、语义检索、UI、以及在启动流程创建用户数据库。

## 验证

纯离线 `unittest`（`tests/test_memory_store.py` + 全量 discover）；见 handoff。
