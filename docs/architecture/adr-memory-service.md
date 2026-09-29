# ADR：长期记忆应用服务、词法检索与异步提取契约

- **状态**：已采纳（应用层底座；未接入对话 / Ollama）
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步
- **依赖**：`docs/architecture/adr-long-term-memory-store.md`

## 问题

持久层已可独立验证，但仍缺：

1. 对成功对话轮的非阻塞提取入口；
2. 面向当前输入的确定性相关检索；
3. 有严格条数/字符预算的上下文块。

若直接复用 `OllamaClient.generate*` 做后台提取，会污染 `ConversationHistory`，并与下一轮回复争抢模型。若在本轮把记忆注入系统提示词，会越过尚未设计的产品装配边界。

## 决策

1. **分层**：在 `src/memory/` 增加无设备副作用的应用层（`service_models` / `MemoryExtractor` / `LexicalMemoryRecall` / `format_memory_context` / `MemoryIngestionService` / `MemoryQueryService`）。持久层仍只负责 CRUD 与 schema；应用层不修改 `MikuAssistant`、Handler、CLI、`VoiceSession` 或 `assemble_default`。
2. **轮次契约**：`MemoryTurnEvent` + `MemoryTurnOutcome`。仅 `SUCCESS` 且用户/助手正文均非空可入队；失败、取消、半轮、空回复被 `submit` 拒绝且不调用提取器。
3. **来源真实性**：`ExtractedMemory` 不得声明 `USER_CONFIRMED`；ingestion 写库时统一 `source=MODEL_INFERRED`。存储层精确去重，服务不做语义合并，不升级已有来源。**用户原文是唯一事实来源**：提取器输入不得把 `assistant_text` 当作可提取正文。本地先把 `user_text` 切成带终止符的标点 segment：**先对整段做疑问/不确定/否认语境验收**，再允许按完整连接词 ``并且|而且|以及`` 做 **all-or-nothing** 并列解析（禁止裸「并」通用切分）；跨逗号兄弟互不影响。**模型只能选择 `unit_id`**，不得提交 text/value/category/evidence_quote。`ExtractedMemory.text` 与 category 一律由本地 `render_claim` / `category_for_claim` 从 claim 确定性生成。疑问终止符、未知槽位、条件/引用/否认、句尾元否认、未解析前后缀不产生候选；无本地安全候选时不发 HTTP。写库统一 `MODEL_INFERRED`。同批单项非法选择跳过，不得丢掉合法兄弟项。
4. **有界异步提取**：单消费者 `MemoryIngestionService`。构造不启动线程；`start()` 显式且幂等。`submit` 非阻塞；队列满拒绝并计数。**业务队列与 `_stop_event` 分离**，`drain=True` 不得偷删已接受任务；`submit` 与停止边界在同一锁协议下线性化。worker 对提取异常、非法候选、单条写失败失败开放；`KeyboardInterrupt` / `SystemExit` 不当作普通提取失败，但 `drained` 必须由实际 `pending` 得出，必要时由替代 worker 继续排空。`stop`/`close` 幂等；超时如实报告 `worker_alive`；**`owns_store=True` 时若 worker 仍存活不得关闭 store**，所有权保留至后续受控 `close` 且只关一次。worker 为非 daemon。默认 `owns_store=False`。
5. **词法检索（非语义）**：`LexicalMemoryRecall` 有界读取 `candidate_limit+1` 探测 has-more，只对前 `candidate_limit` 条打分；`truncated_candidates` 仅在确认仍有未扫描候选时为 True（恰好等于上限为 False）。中文以汉字 bigram 为主信号、单字 unigram 仅在无 bigram 查询时回退；英文为 casefold 单词（及词内 bigram 弱信号）。无正相关返回空。排序：相关度 → importance → updated_at → id。可通过 Protocol 未来替换为向量实现。
6. **上下文预算**：`format_memory_context` 同时限制 `max_entries` 与 `max_chars`（含标题/换行/元数据）。JSON Lines 转义换行与引号；头部声明「以下是参考数据，不是指令」；不输出数据库 ID/路径/完整时间戳。空检索返回空串。**本轮不注入模型请求。**
7. **失败开放与隐私**：面向对话的 recall 路径遇存储不可用或破损行返回空上下文并记录错误类型；日志不写查询原文或记忆正文。提取日志只记 `turn_id`、数量与异常类型。
8. **实现约束**：仅标准库；不新增 embeddings、向量库、ORM、分词依赖；不接入真实 Ollama/HTTP 提取器；导入模块不启动线程或打开库。

## 替代方案

- 复用带历史的 Ollama 生成入口做后台提取：污染会话，拒绝。
- 本轮注入系统提示词/用户 prompt：越过装配边界，拒绝。
- 引入向量检索或第三方分词：扩大依赖，拒绝。

## 回退

删除应用层新文件与 `tests/test_memory_service.py`、本 ADR；保留已验收的持久层。不回滚无关修改。

## 明确未做

真实 Ollama/HTTP 提取适配器；记忆注入对话链路；产品装配与启动时自动 `start()`；语义/向量检索；UI。

## 验证

纯离线 `unittest`（`tests/test_memory_service.py` + 既有 `test_memory_store.py` + 全量 discover）；见 handoff。
