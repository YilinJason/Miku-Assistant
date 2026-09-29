# Codex Review：记忆 ingestion 出处契约 Round 1

## 结论

Round 1 暂不通过。用户原文已成为 extractor payload 的唯一输入，原始 `Project Nightingale / 抹茶拿铁` 的 assistant-only 传播路径已被阻断；但本地证据校验仍可接受语义相反的候选，纯记忆问句识别存在常见漏网形式，管理工具也未达到其声明的只读与防陈旧删除契约。

现有 355 项测试、专项 12 项、`compileall` 与 `git diff --check` 均通过，说明基线稳定，但没有覆盖以下反例。不得据此清理 live 数据库或开始真实复验。

## Finding 1（P1）：证据只校验值，允许否定与谓词翻转

`src/memory/provenance.py::fact_supported_by_evidence()` 只检查候选核心值是否出现在 `evidence_quote`，没有校验肯定/否定与谓词方向。Codex 直接复现以下全部返回 `True`：

```text
用户原文「我不喜欢抹茶拿铁」 → 候选「用户喜欢抹茶拿铁」
用户原文「我讨厌抹茶拿铁」   → 候选「用户喜欢抹茶拿铁」
用户原文「我不住在纽约」       → 候选「用户住在纽约」
用户原文「我不认识 Alice」      → 候选「用户认识 Alice」
```

这仍会把与用户原话相反的事实写入长期记忆。修复必须失败关闭：候选的极性和事实关系必须由用户证据支持；不确定时拒绝。至少覆盖喜欢/不喜欢/讨厌、居住/不居住、认识/不认识、身份肯定/否定。合法负向事实可以保存为负向事实，不能被正向改写。

可以采用严格的本地谓词族与极性匹配，或保存不改变含义的用户原话表示；不能继续仅靠“值出现过”判断，也不能把语义真实性再次交给 extractor 自证。

## Finding 2（P1）：常见纯查询被识别成自我陈述，并可写入占位垃圾

`looks_like_pure_memory_query()` 先因 `has_user_self_assertion()` 返回 False；`_SELF_ASSERTION_RE` 会把疑问句中的 `我叫`、`我住在`、`我最喜欢`、`我的测试代号是` 当成陈述。Codex 复现以下问句均为 `query=False / skip=False / candidate=True`：

```text
我的测试代号是什么？ → 用户的测试代号是测试代号
我最喜欢什么饮品？   → 用户最喜欢什么饮品
我叫什么名字？       → 用户叫什么名字
你还记得我住在哪里吗？ → 用户住在哪里
```

修复不能只追加一组整句正则。应在用户原文或 `evidence_quote` 的子句层区分陈述与询问：含 `什么/谁/哪里/哪儿/多少/哪个/是否/吗/?/？` 等未知槽位的证据不得成为事实依据；混合句仍只允许提取明确陈述子句，例如“我最喜欢桂花乌龙，你喜欢什么？”中的前半句。

同时拒绝“核心值只是字段标签或疑问占位符”的候选，例如 `测试代号`、`什么饮品`、`哪里`、`名字`。纯查询应在本地准入阶段跳过；即使漏过，候选证据边界也必须再次拒绝。

## Finding 3（P1）：ID 管理工具的 dry-run 不是真只读，执行也不能防陈旧或部分删除

`diagnose_memory_ids.py` 的 dry-run 使用普通 `SqliteMemoryStore`。该构造会创建父目录、以可写方式连接、执行 `PRAGMA journal_mode=DELETE`、`BEGIN IMMEDIATE`、`CREATE TABLE/INDEX IF NOT EXISTS` 与 commit。因此“默认只读”声明不成立。

执行模式在同一次 invocation 内重新读取快照，所以无法证明用户执行时仍是之前 dry-run 确认的正文；若 dry-run 后记录内容发生变化，新的执行会加载新正文并照常删除。多个 ID 又通过逐条独立事务删除，后续 ID 失败时前面的删除不会回滚；重复 ID 也会导致先删一条后失败。

两种可接受收口方式：

1. 移除本轮新增的管理工具，待代码验收后由 Codex 提供一次性、精确且可评审的清理操作；或
2. 完整修复：dry-run 使用 SQLite URI `mode=ro` 且不得调用 schema 初始化；生成包含 `id + text/source/updated_at`（或其确定性摘要）的确认计划；`--execute` 必须携带并验证该计划；所有 ID 先完整验证，再在单一事务内条件删除，任一缺失/变化/重复均零删除失败。补临时数据库测试证明只读、陈旧拒绝和全有或全无。

在此 Finding 修复前不得对 `tmp/memory-live-test.sqlite3` 使用 `--execute`。

## Finding 4（P2）：单个结构错误仍会丢掉同批合法候选

`_parse_response()` 对每项直接调用 `_parse_item()`；任一候选 category 类型错误、未知字段或领域校验错误都会抛出 `MemoryExtractionResponseError`，整批已解析的合法候选随之丢失。Codex 复现“第一条合法、第二条 category=123”时整次 extract 抛错。

Round 1 要求“单个无效候选不得丢弃同轮其他合法候选”。应区分响应 envelope/JSON 根结构错误与单项错误：前者继续整次失败，后者按 index 安全记录错误类型并跳过，保留合法兄弟项。若需要兼容既有“全无效时抛错”契约，可在没有任何合法项且存在结构错误时返回原错误；混合批次必须保留合法项。

另外将 `evidence_quote` 至少加入 JSON Schema `required`，减少真实结构化输出遗漏；本地验证仍必须保留，不能把 Schema 当成安全边界。

## Round 2 必须补充的测试

1. 上述四组极性/谓词翻转全部拒绝；相符的正向与负向事实分别通过。
2. 上述四种纯查询全部本地跳过，且伪造候选也无法通过证据验证。
3. 混合陈述+问句只保留陈述子句事实。
4. 标签/疑问占位值：`测试代号`、`什么饮品`、`哪里`、`名字` 全部拒绝。
5. 一个合法候选 + 一个字段非法候选：合法项仍进入临时 store，非法项不进入。
6. `evidence_quote` 在 Schema 中为 required；缺失时本地仍失败关闭。
7. 若保留 `diagnose_memory_ids.py`：dry-run 对只读/写保护数据库成功且不创建/修改文件；陈旧计划、重复 ID、执行期竞态或任一不存在时零删除；多 ID 成功时单事务全部删除。

## Codex 独立验证

```text
unittest discover -s tests -q
→ Ran 355 tests in 10.881s ... OK

unittest -q tests.test_memory_provenance
→ Ran 12 tests in 0.146s ... OK

compileall -q src tests diagnose_memory.py diagnose_memory_ids.py
→ OK

git diff --check
→ OK（仅既有 LF/CRLF 提示）
```

live 库记录数仍为 9；Codex 未执行删除。未启动真实 Ollama、主程序或设备；未提交、未推送。

## Cursor Round 2 停止点

只修上述四项并更新 ADR、工程基线与 handoff。使用 fake transport、临时 SQLite 与纯函数反例测试；不得修改 live 测试库，不得运行真实 Ollama/设备，不得进入存储 Round 2/3，不得提交或推送。完成后停止等待 Codex 最终复验。
