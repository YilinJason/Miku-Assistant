# Cursor 任务：安全硬化 Round 3C2——SQLite 记忆库与诊断计划隐私边界

> 状态：Round 1 已由 Cursor 实现，等待 Codex Review。  
> 基线：同一未提交工作区中的 Round 3A、3B、3C1；`docs/codex-review-security-round3-c1.md` 已由 Codex 最终复验通过。  
> 范围：只做 3C2。完成后停止等待 Codex Review；不得进入自然陪伴体验。

## 先读取

- `AGENTS.md`
- `skills/architecture-contracts/SKILL.md`
- `skills/safe-refactoring/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `skills/collaboration-review/SKILL.md`
- `.cursor/rules/system-safety.mdc`（若存在）
- `src/memory/{sqlite_store,id_audit,paths,runtime,errors}.py`
- `diagnose_memory_ids.py`
- `tests/{test_memory_store,test_id_audit,test_storage_bounds}.py`
- `docs/architecture/{adr-long-term-memory-store,adr-storage-retention-cleanup}.md`

## 威胁边界

当前记忆正文保存在普通明文 SQLite 中。标准库 `sqlite3` 不提供数据库静态加密；本轮不得宣称可抵御同一 Windows 用户、管理员、恶意软件、磁盘镜像或备份读取。也不得安装 SQLCipher、调用 `icacls`、写注册表或扩大子进程权限。

本轮要阻止的是应用自身的可避免风险：数据库路径被 symlink/junction/reparse/硬链接重定向；运行时父目录替换后继续写副作用；SQLite 临时落盘与已删除正文残留；扩展加载/ATTACH 扩大文件访问面；诊断确认计划把完整记忆正文和绝对数据库路径复制到 JSON，或由计划文件替 CLI 选择写库目标。

## 目标与不变量

### 1. 集中的 SQLite 文件边界

建立窄的 `src/memory/sqlite_security.py`（名称可调整），由正式 `SqliteMemoryStore` 和 `id_audit` 共同使用，不复制两套规则：

- 数据库目标必须是普通文件或尚不存在的普通文件目标；拒绝目录、symlink、junction/reparse point、FIFO/设备和 `st_nlink != 1` 的现有文件。
- 数据库名只允许安全的单文件名，后缀限定为 `.sqlite3`、`.sqlite` 或 `.db`；拒绝 Windows 设备名、控制字符、尾随点/空格和路径分量伪装。
- 创建/打开前绑定数据库父目录的规范路径与 `(st_dev, st_ino)` 身份；父目录必须是已存在普通目录且非 reparse。打开后、每次写事务/VACUUM 前后重新验证父目录和当前数据库路径身份。身份变化必须在 SQL 写副作用前 fail closed。
- 已存在的 `-journal`、`-wal`、`-shm` sidecar 至少要经过普通文件、非 reparse、单链接和同父目录校验；不得删除或覆盖不安全 sidecar。新 sidecar 的完整竞态防护受标准库 VFS 限制，须在 ADR 如实说明。
- 使用临时目录行为测试证明拒绝发生在 `sqlite3.connect` / `execute` fake 调用前；不得触碰 `data/memory`。

### 2. 统一且可验证的连接硬化

正式读写连接与只读审计连接都通过集中 helper 配置，并检查实际生效值：

- `PRAGMA foreign_keys=ON`
- `PRAGMA trusted_schema=OFF`
- `PRAGMA temp_store=MEMORY`
- 读写库保持 `journal_mode=DELETE`；不得改成 WAL，也不得在请求热路径 VACUUM
- `PRAGMA secure_delete=ON`（若本机 SQLite 返回不支持/未生效，构造失败而非静默宣称）
- 明确 `enable_load_extension(False)`；若 API 不存在记录为平台限制，不得启用
- 将 `SQLITE_LIMIT_ATTACHED` 设为 0（API 可用时）并用行为测试证明 `ATTACH DATABASE` 被拒绝；正式代码本身不得使用 ATTACH
- 只读审计继续使用 URI `mode=ro`，增加 `cache=private` 与 `PRAGMA query_only=ON`；缺失库不创建，任何写 SQL 失败
- 连接 busy/lock 等待必须显式有界并严格校验；默认不超过现有 5 秒，不引入无界重试或后台线程

普通 I/O/SQLite 故障继续映射为既有 `MemoryStoreUnavailableError`；`KeyboardInterrupt/SystemExit` 上抛；日志只记录异常类型/阶段，不记录记忆正文、SQL 参数、绝对路径或 SQLite 原始异常正文。用户主动运行 CLI 时可显示其显式选择的路径与审计正文。

### 3. 不含正文且不能重定向数据库的删除确认计划 v2

当前 v1 JSON 含 `db` 绝对路径和每条 `text`，而执行模式在用户未改 `--db` 时会接受计划中的路径。改为版本 2：

- 新生成计划不得包含 `text`、`dedupe_key`、绝对/相对数据库路径或其他记忆正文；每条只保留完整 ID、必要的非正文并发版本字段和带随机 plan nonce 的行指纹。低熵正文不得以无盐裸哈希写入计划。
- 计划只绑定调用方显式/默认选定数据库的路径指纹，绝不能提供或替换执行目标。`diagnose_memory_ids.py --execute` 始终使用 CLI 的 `--db`（或代码内固定默认），再验证其与计划指纹一致；篡改/错库/陈旧/重复任一失败都必须是单事务零删除。
- 事务内重新读取并验证所有行指纹后再删除，避免验证与删除之间的竞态；不得为了 v2 把正文写到日志或错误消息。
- 新写计划使用不跟随已有 symlink/reparse 且不静默覆盖已有文件的方式；父目录必须已存在。计划写失败不得影响数据库。
- v1 含正文计划默认拒绝并给出明确迁移提示；本仓库未提交阶段不需要继续生成 v1。控制台 dry-run 是用户显式审计界面，可继续显示所选记录正文。

### 4. 生命周期、性能与兼容

- 模块导入和 `memory.enabled=false` 保持零数据库/目录副作用。
- `SqliteMemoryStore` 保持单连接 + `RLock`、现有事务、schema、retention、关闭与离线 VACUUM 契约。
- 不读取、迁移、VACUUM、删除或 chmod 真实 `data/memory`；所有迁移/旧库兼容只在临时目录副本测试。
- 不因安全校验做递归扫描；每次事务只允许常数次 `stat`/PRAGMA，不增加线程、Timer 或子进程。

## 必须新增的离线行为测试

1. 路径表：安全新库/旧库可用；危险后缀、目录、symlink、junction/reparse、硬链接、设备名拒绝；拒绝发生在 fake connect 前。
2. 打开竞态：父目录或目标在验证后替换时，打开/首次 SQL 前失败；外部 sentinel 零字节变化。平台无法创建某类链接时仅跳过该专属测试。
3. 运行时替换：已打开 store 的父目录/数据库路径身份变化后，add/update/delete/clear/prune/VACUUM 均在写 SQL 前拒绝；原连接可安全关闭，无句柄/线程泄漏。
4. PRAGMA/能力：正式连接返回 `foreign_keys=1`、`trusted_schema=0`、`temp_store=2`、`journal_mode=delete`、`secure_delete=1`；扩展加载保持关闭；ATTACH 被拒绝；busy timeout 有界。
5. 只读：缺失库零创建；`query_only=1`；CREATE/UPDATE/ATTACH 均失败；无 journal/wal/shm 新文件。
6. 计划 v2：序列化 blob 不含两个不同 sentinel 正文和数据库路径；不同 nonce 指纹不同；错库、篡改、重复、陈旧、v1 均零删除；正确计划一次事务精确删除；计划不能改变 CLI 选择的数据库；已有计划文件/symlink 不被覆盖。
7. 故障：connect/PRAGMA/校验/事务/close 的 SQLite 与 OSError 映射一致且持久日志无路径、正文、原始异常；KI/SystemExit 不吞。
8. 全部既有 memory、storage、Round 3A/B/C1 测试不回归。

## 验证边界

只运行 Python 3.12 的 fake/临时目录/离线测试：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py diagnose_memory_ids.py diagnose_resource_soak.py
git diff --check
```

禁止启动 `main.py`、真实 Ollama/HTTP、RVC、麦克风、扬声器、浏览器或清理实扫；禁止读取/删除/修改 `data/memory`；禁止安装依赖；禁止提交或推送。

## 交付

- 新增 SQLite 隐私边界 ADR，明确明文剩余风险、sidecar/VFS 竞态限制、计划 v2 和兼容变化。
- 更新工程基线与交接顶部；区分静态/离线通过与真实库未测试。
- 返回修改文件、测试数量、跳过项、未运行项和剩余风险。
- Round 1 完成后停止，等待 Codex Review；不得进入自然陪伴体验。
