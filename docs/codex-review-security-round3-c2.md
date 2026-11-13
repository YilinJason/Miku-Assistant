# Codex Review：安全硬化 Round 3C2

> 最终结论：**通过。** F1–F7 已由代码审查、确定性回归、Codex 独立反例与完整离线测试确认修复；可进入安全 Round 3 里程碑复审。

## Codex 最终复验（2026-09-30）

- 独立父目录交换探针：`open-error=MemoryStoreUnavailableError`、`outside-db-exists=False`、`outside-entries=[]`。
- 独立 COMMIT 故障探针：公开错误为 `MemoryStoreUnavailableError`，`transaction-still-open=False`、`rollback-calls=1`、`same-connection-count=0`。
- 全量：**588 tests / OK（skipped=4）**。
- `compileall`：exit 0；`git diff --check`：exit 0（仅 Git LF/CRLF 工作区提示）。
- 边界保持：未启动真实服务/设备，未读取、修改或删除 `data/memory`，未提交、未推送。

## Finding 1 — P0：写性 PRAGMA 在打开后路径复验之前执行

- 位置：`src/memory/sqlite_security.py` 的 `connect_memory_db()`。
- 当前顺序是 `sqlite3.connect → harden_connection → revalidate_db_location`。读写 `harden_connection()` 会执行 `PRAGMA journal_mode=DELETE`、`secure_delete=ON` 等可能读取/修改数据库或 sidecar 的操作。
- 独立确定性探针在 pre-connect hook 把目标换成外部 sentinel 的硬链接，并注入记录副作用的 fake connection。当前虽然最终抛 `MemoryValidationError`，但输出为 `pragma-before-revalidate True`、`outside-unchanged False`。这是与 C1 相同的“事后 fail closed，副作用已发生”。

要求：

1. 打开连接后、任何 PRAGMA/schema/用户 SQL 前立即复验父目录、数据库当前路径、单链接身份；只有通过后才能调用 `harden_connection()`。
2. 硬化完成后再复验一次；任一步失败关闭局部连接，外部 sentinel 必须逐字节不变。
3. 新增 fake execute 顺序测试和真实普通文件 sentinel 测试，证明危险替换在第一个 `execute()` 前被拒绝。

## Finding 2 — P0：递归创建缺失父目录会跟随 junction 祖先写到外部

- 位置：`ensure_parent_directory()`。
- 当前只检查最终 `db_path.parent`；当它尚不存在时，`parent.mkdir(parents=True)` 会跟随更高层已经存在的 junction。新建出来的最终父目录本身不是 reparse，因此后续绑定会通过。
- Windows 独立复现：`redirect/` 为指向临时 `outside/` 的 junction，目标为 `redirect/new/nested/memory.sqlite3`。当前输出 `ancestor-junction-error None`、`ancestor-junction-outside-created True`，外部数据库被真实创建。

要求：

1. 安全创建必须从最近的已存在祖先开始，逐段验证每个既有/新建组件均为同一预期链上的普通目录且非 symlink/junction/reparse；不得使用未经祖先验证的 `mkdir(parents=True)`。
2. 可选择更严格地要求父目录预先存在，但必须保持正式默认 `data/memory` 的可用迁移，并更新现有 mkdir 契约测试；无论方案如何，任何祖先 reparse 必须在创建目录或连接前拒绝。
3. 增加 junction/symlink 祖先 + 多层缺失父目录的确定性测试，断言外部零目录、零文件；不能只测直接父目录本身是 junction。

## Finding 3 — P1：只绑定父目录身份，没有绑定打开时的数据库文件身份

- 位置：`TrustedDbParent`、`OpenedMemoryDb`、`revalidate_db_location()`、`SqliteMemoryStore`。
- `revalidate_db_location()` 每次只确认“当前路径是安全普通单链接文件”，没有比较它是否仍是连接建立时的同一个 `(st_dev, st_ino)`。同一父目录内将数据库改名并放入另一个安全 `.sqlite3` 时会通过逻辑检查；当前 Windows 因 SQLite 文件锁使直接复现得到 `PermissionError`，但实现本身没有跨平台身份不变量，也不能依赖锁作为安全协议。

要求：

1. 在 connect 后首次安全复验时绑定数据库文件身份与规范路径；`OpenedMemoryDb` 和 store 持有它。
2. 后续事务、VACUUM、计划删除前后必须同时比较可信父目录身份和数据库文件身份；任何替换、消失、多链接/reparse 均失败并回滚。
3. 用可控 stat/identity hook 或支持 rename 的平台测试确定性覆盖同父目录普通文件替换，禁止只伪造父目录身份。

## Finding 4 — P1：计划删除的部分异常路径没有回滚

- 位置：`verify_and_delete_plan_entries_atomic()`。
- 多个显式校验分支手工 `ROLLBACK`，但事务开始后若末尾 `revalidate_db_location()` 抛 `MemoryValidationError`，外层 `except MemoryValidationError: raise` 不回滚。
- 独立故障注入让第一次位置复验成功、删除后第二次复验抛 `MemoryValidationError`，结果为 `transaction-still-open True`、`same-connection-count 0`。连接虽最终关闭时通常会隐式回滚，但函数的“任一失败立即零删除/无悬挂事务”契约不成立，复用连接可能误提交。

要求：

1. `BEGIN IMMEDIATE` 成功后使用统一事务 guard；任意 `BaseException`（含 `MemoryValidationError`、`MemoryStoreUnavailableError`、SQLite 错误、`KeyboardInterrupt/SystemExit`）都先尽力 rollback，再按原契约重抛/映射。不得依赖调用者 close 隐式回滚。
2. 成功只允许单次 COMMIT；失败返回前 `conn.in_transaction=False`，新连接和同连接都仍看到原记录数。
3. 增加 post-delete 身份失败、KI/SystemExit 和 rollback 自身失败测试；不吞中断。

## Finding 5 — P1：运行时路径拒绝在 CRUD 之间映射不一致

- `_transaction()` 直接调用 `revalidate_db_location()`；数据库路径变成 symlink/硬链接等场景可抛 `MemoryValidationError`。
- `prune_for_retention()` / `vacuum_offline()` 显式映射该异常，但 `add/update/delete/clear` 只捕获 `sqlite3.Error`，因此同一种运行时存储失效会因方法不同暴露 `MemoryValidationError` 或 `MemoryStoreUnavailableError`。

要求：

1. 在集中边界把运行时路径/身份失效统一映射为 `MemoryStoreUnavailableError`；构造参数自身非法仍可保持 `MemoryValidationError`。
2. add/update/delete/clear/prune/VACUUM 使用相同公开错误契约；日志只写阶段与类型，不含路径、正文和原始异常。
3. 表驱动测试覆盖全部写操作，且每个拒绝都发生在对应 INSERT/UPDATE/DELETE/VACUUM 前。

## Round 2 复验门槛

- F1–F5 全部修复；计划 v2、PRAGMA、只读、旧 C1 安全边界不回归。
- 3C2 专项、memory/storage、Round 3A/B/C1、全量 unittest、`compileall`、`git diff --check` 全部真实通过。
- 继续只用临时目录/fake；不启动真实服务/设备，不读取/修改/删除 `data/memory`，不进入自然陪伴，不提交、不推送。完成后停止等待 Codex。

## Round 2 新增 Finding 6 — P0：父目录换成 junction 后 connect 在外部创建空数据库

- 位置：`src/memory/sqlite_security.py::connect_memory_db()` 与 `tests/test_security_round3c2.py::test_parent_swap_before_connect_fails_sentinel_intact`。
- 现有测试只断言外部既有 sentinel 内容未变，没有断言外部目录零新增文件。独立 Windows 探针在 pre-connect hook 将可信父目录改名并用 junction 指向外部目录；最终虽抛 `MemoryStoreUnavailableError`，但外部实际出现 `race.sqlite3`，大小为 0。
- 这仍是复验前的文件系统写副作用，违反本轮“打开竞态在首次 SQL 前失败、外部零文件/零字节变化”的契约。

要求：

1. 对新库先在仍受信任的父目录中建立并绑定普通单链接目标，再以 SQLite URI `mode=rw&cache=private` 打开；不得用会在交换后的路径自动建库的默认 `rwc` 行为。已有库也统一使用不创建目标的 `mode=rw`。
2. 安全预建必须使用 exclusive create，失败不覆盖，不跟随现有链接；随后复验父目录、目标类型/链接数和数据库身份。不得在 hook 之后或 SQLite connect 阶段创建缺失目标。
3. 扩展父目录交换测试：断言外部 sentinel 不变且 `outside/race.sqlite3`、journal/wal/shm 均不存在；可信旧父目录里的预建空目标允许保留，因为这是调用方请求的新库目标。另测外部预先存在同名 sentinel 时内容逐字节不变。
4. ADR 如实保留标准库/同用户 TOCTOU 剩余风险，不宣称彻底消除任意时刻的目录句柄竞态。

## Round 2 新增 Finding 7 — P1：普通 store 的 COMMIT 失败不回滚，连接继续持有事务

- 位置：`src/memory/sqlite_store.py::_transaction()`。
- 当前 `conn.commit()` 位于 `try/except` 的 `else` 中；COMMIT 抛异常不会进入 rollback 分支。
- 独立注入 `sqlite3.OperationalError` 后，公开错误虽映射为 `MemoryStoreUnavailableError`，但输出为 `transaction-still-open=True`、`rollback-calls=0`、同连接可见新增记录。连接复用时存在误提交与长期写锁风险。

要求：

1. BEGIN 成功后把 yield、post-revalidate 和 COMMIT 全部置于同一事务 guard；其中任意 `BaseException` 均先尽力 rollback，再上抛。成功 COMMIT 后再清除 begun 状态。
2. 不吞 `KeyboardInterrupt/SystemExit`；rollback 自身失败不得伪装原错误。公开 SQLite 错误映射保持既有契约。
3. 增加真实连接 + commit-failing proxy 回归测试：返回前 `in_transaction=False`、rollback 恰好调用、同连接与新连接均为零新增。另覆盖 COMMIT 的 KI/SystemExit。

## Round 3 复验门槛

- F1–F7 全部成立；专项与完整离线测试通过；父目录交换探针外部目录零新增；commit 失败探针无悬挂事务。
- 不进入安全里程碑或自然陪伴，等待 Codex 终审。
