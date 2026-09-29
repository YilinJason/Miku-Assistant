# Codex Review：长期记忆持久层 Round 1

## 结论

当前模块边界、短期/长期历史隔离、参数化 SQL、去重展示正文、来源保留和高版本 schema 非破坏拒绝方向正确；现有 205 项测试通过。但以下问题已由 Codex 独立复现，尚不能验收。只修复本文件列出的范围，不接入对话、提取、检索或 UI。

## 必须修复

### 1. 浮点领域不变量未闭合

`src/memory/models.py::_validate_unit_interval` 只比较上下界，`float("nan")` 的两个比较都为 false，因此 `NewMemory(text="x", importance=float("nan"))` 当前会成功构造。`NaN` 不能进入领域对象或 SQLite。

修复要求：

- 用 `math.isfinite`（或等价）拒绝 `NaN`、正负无穷；仍拒绝 bool。
- `MemoryRecord` 验证 `updated_at >= created_at`。
- `MemoryRecord.dedupe_key` 必须与 `normalize_dedupe_key(text)` 一致，避免公开领域对象携带互相矛盾的数据；不要改写展示正文。
- 增加 `NewMemory` 和 `MemoryRecord` 的确定性测试。

Codex 复现：

```text
NewMemory True
```

其中 `True` 表示构造后的 `importance` 仍是 NaN。

### 2. 存储异常没有统一收口

`get()`、`add()`/`update()` 的预读取、`schema_version` 及 `_fetch_*` 可直接泄漏原生 `sqlite3.Error`；损坏时间/来源等行解码异常也可能以 `ValueError`/`MemoryValidationError` 逃出。构造时 `parent.mkdir(...)` 位于 try 外，失败不会映射为 `MemoryStoreUnavailableError`。`_open()` 在连接成功后的初始化异常路径也应确保关闭局部连接。

Codex 已用临时库复现：关闭底层连接后调用 `get("id")`，实际抛出：

```text
sqlite3.ProgrammingError: Cannot operate on a closed database.
```

修复要求：

- 所有公开存储操作把底层 `sqlite3.Error` 映射为 `MemoryStoreUnavailableError`，保留 cause，但日志和公开消息不包含完整记忆正文。
- 数据库行损坏时返回明确的存储/数据损坏错误，不用空结果伪装；不要吞 `KeyboardInterrupt` / `SystemExit`。
- 将目录创建纳入异常转换边界。
- `_open()` 任一步失败都关闭已创建的连接；高版本 schema 仍保持原有具名异常和非破坏行为。
- 增加关闭底层连接、目录创建失败、破损行读取的测试。

### 3. 文档声明的事务与实际 autocommit 不一致

`sqlite3.connect(..., isolation_level=None)` 使连接处于 autocommit；Codex 实测 `store._conn.isolation_level is None`。此时 `with conn:` 不能提供文档所声称的事务边界，schema 的 `executescript` 与 `user_version` 更新也不是一个可靠的原子迁移。

修复要求：

- 使用真实事务（恢复非 autocommit 或显式 `BEGIN`/`COMMIT`/`ROLLBACK`），写操作与 schema 初始化/版本更新的语义要和 ADR 一致。
- 避免依赖 `executescript` 的隐式提交；schema v1 初始化失败时不得把版本标成已完成。
- 保留单连接 + `RLock`，不新增线程、ORM 或依赖。
- 用真实 SQLite 临时库验证事务失败后原数据与 `user_version` 保持正确；不要只靠伪连接自定义 `__enter__` 来证明生产连接的事务行为。

### 4. 恢复交接文档历史

本轮把 `docs/cursor-handoff-status.md` 的既有语音、性能、STT 交接记录整体替换成了记忆里程碑摘要。任务要求“更新顶部”，不能删除既有历史。

修复要求：

- 在文件顶部保留本轮记忆状态。
- 恢复原有全部交接内容，禁止根据聊天回忆重写；以修改前实际文件/版本差异为依据合并。
- 工程基线和 handoff 更新实际重跑后的测试数量、耗时与限制。

## 补充回归

- 精确重复：先写 `MODEL_INFERRED`，再以同正文 `USER_CONFIRMED` 写入时不得静默升级已有来源。
- `MemoryDuplicateError` 当前公开导出但没有使用。删除这个未采用的 API，或明确唯一实际触发契约并加测试；不要保留误导调用方的死契约。
- 保留 `ConversationHistory`、Ollama、助手、语音和现有 191 项基线行为。

## 完成条件

使用 Python 3.12 运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
git diff --check
```

全部通过后更新 `docs/cursor-handoff-status.md` 顶部，说明 Round 1 修复和实际结果，然后停止修改，等待 Codex Round 2。不得提交、推送或运行真实设备/模型。
