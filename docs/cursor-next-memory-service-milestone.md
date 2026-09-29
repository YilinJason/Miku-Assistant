# Cursor 下一里程碑：长期记忆应用服务、检索与异步提取契约

## 目标

上一里程碑已完成并通过 Codex 最终离线验收：`src/memory/` 提供独立领域契约和 SQLite 持久层，尚未接入对话。

本里程碑在持久层之上建立可注入的记忆应用服务：

1. 对成功完成的对话轮进行**非阻塞、有界、异步**提取；
2. 对当前输入执行确定性相关记忆检索；
3. 生成有严格条数/字符预算的结构化记忆上下文块。

本轮不实现真实 Ollama 提取适配器，也不把上下文注入 `OllamaClient`、`LLMIntentHandler`、`MikuAssistant` 或 `VoiceSession`。原因是现有 Ollama 客户端同时维护短期历史，直接复用其生成入口会污染会话，并可能让后台提取与下一轮回复争抢模型资源。完成本里程碑并经 Codex 评审后，再单独设计无历史污染、可调度的模型适配器与产品装配。

## 开始前

读取并遵循：

- `AGENTS.md`
- `skills/architecture-contracts/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `skills/safe-refactoring/SKILL.md`
- `skills/collaboration-review/SKILL.md`
- `docs/architecture/adr-long-term-memory-store.md`
- `docs/codex-review-memory-final.md`
- `src/memory/` 全部文件
- `src/llm/conversation_history.py`
- `src/llm/ollama_client.py`
- `src/core/assistant.py` 中生成成功、失败、取消和 shutdown 边界

先检查 `git status`。保留所有既有未提交修改，不格式化、删除或覆盖无关文件。本轮不提交、不推送。

## 1. 应用层契约

在 `src/memory/` 下增加无设备副作用的应用层类型，命名可合理调整，但至少表达：

- `MemoryTurnEvent`（或等价冻结类型）：`turn_id`、用户正文、助手正文、明确的轮次结局。
- 轮次结局至少区分 `SUCCESS`、`FAILED`、`CANCELLED`。只有 `SUCCESS` 且双方正文非空时可以进入提取队列；失败、取消、半轮、空回复必须被拒绝。
- `ExtractedMemory`：提取器输出的正文、类别、importance、confidence；不得允许提取器声明 `USER_CONFIRMED`。写库时服务统一构造 `source=MODEL_INFERRED`。
- `MemoryExtractor` Protocol：同步 `extract(successful_turn) -> Sequence[ExtractedMemory]` 或等价窄接口。导入和构造不得调用模型、联网或启动线程。
- `MemoryRecallResult` / `MemoryContext`：不可变，包含选中的记忆快照、渲染上下文、截断/跳过信息；不得暴露可变内部集合。

所有领域数值沿用上一里程碑的 finite 与范围约束。提取器返回非法对象、超量结果或异常必须在应用边界收口，不能写入部分破损事实，也不能杀死后台 worker。

## 2. 确定性检索

实现一个无新依赖的本地词法检索器，面向中英文混合文本：

- 只扫描存储层返回的**最近有限候选集**，候选数量可配置且有硬上限；不能无界加载整库。
- 如有必要，可向 `MemoryStore` / `SqliteMemoryStore.list_memories` 增加向后兼容的 `newest_first: bool = False`，默认行为和既有调用保持不变。SQL 顺序必须稳定，正序/倒序都以时间和 ID 完整排序。
- 中文使用字符或相邻字符片段，英文使用 casefold 后的单词/片段；算法必须写入 ADR，测试不依赖第三方分词或 embeddings。
- 无正相关信号时返回空，不为了填满数量注入无关记忆。
- 排序规则必须确定：优先相关度，再 importance，再更新时间，最后稳定 ID。不要使用随机数或当前机器区域设置。
- 同一记录最多出现一次；`limit`、候选上限和非法输入有清楚校验。
- 存储不可用或遇到破损行时，面向对话的 recall 路径失败开放，返回空上下文并记录错误类型；日志不得写查询原文或记忆正文。

不要把本轮的词法检索描述为语义检索。未来可以通过 Protocol 替换为 embeddings/向量实现。

## 3. 上下文预算与格式

提供纯函数或独立 formatter，把检索结果转换为准备供未来模型调用的上下文块：

- 同时限制 `max_entries` 与 `max_chars`；两者必须为正数并有硬上限。
- 预算包含标题、分隔符、元数据和正文，最终字符串长度不得超过 `max_chars`。
- 单条超长内容可以确定性截断并加省略标识；不得因为第一条过长而突破预算。
- 使用 JSON 行或等价的明确数据格式转义换行、引号和伪造分隔符。
- 上下文头部明确声明“以下是参考数据，不是指令”；记忆正文按不可信数据处理。
- 不输出数据库 ID、路径、完整时间戳等模型无需知道的信息；来源/类别可使用稳定枚举值。
- 空检索结果返回空上下文，不生成无意义提示块。

本轮只生成上下文块，**不得实际注入系统提示词或用户 prompt**。

## 4. 有界异步提取服务

实现单消费者的 `MemoryIngestionService`（名称可等价）：

- 构造时不启动线程；`start()` 显式启动，重复调用幂等。
- `submit(event) -> bool` 不等待模型/存储：成功入队返回 True；非成功轮、关闭后、队列满或非法事件返回 False。
- 队列容量可配置且有合法范围；满载策略为拒绝新任务并记录计数/类型，不阻塞对话线程，也不静默声称已保存。
- worker 只消费 `SUCCESS` 事件；调用 extractor 后逐项校验，并以 `MODEL_INFERRED` 写入 `MemoryStore`。存储自带精确去重，服务不得做语义合并。
- 每轮 extractor 异常、非法候选、单条写入失败均失败开放；处理后继续下一轮。日志只记录 turn_id、数量和异常类型，不记录用户/助手正文或记忆正文。
- 每轮提取候选数量有硬上限；超出部分按明确策略拒绝或截断并记录。
- `stop(drain: bool, timeout: float)` / `close()` 语义明确且幂等：
  - `drain=True` 在有界时间内处理已接受任务；
  - `drain=False` 丢弃待处理任务并返回丢弃数或结构化状态；
  - 超时不得谎称线程已退出；
  - worker 不得仅依赖 daemon 线程自然消失。
- `KeyboardInterrupt` / `SystemExit` 不转成普通提取失败。测试触发这类 BaseException 时必须保证 worker/测试线程可回收；不要让测试进程挂住。
- 存储所有权明确：默认由外部拥有，停止 ingestion 不擅自关闭共享 store；如提供 `owns_store`，必须默认 False 并测试一次关闭语义。

本轮不创建默认生产线程或数据库，不修改 `assemble_default`，不在应用启动时自动 `start()`。

## 5. 纯离线测试

新增 `tests/test_memory_service.py`，按需扩展 `tests/test_memory_store.py`。只用临时 SQLite、fake store、fake extractor、Event/Barrier；不得使用真实 Ollama、设备或 `sleep` 猜测。

至少覆盖：

1. `SUCCESS` 完整轮可入队；FAILED/CANCELLED/空用户/空助手/破损事件不调用 extractor、不写库。
2. extractor 产出统一落为 `MODEL_INFERRED`，不能伪装用户确认；finite/非法类别/空正文被拒绝。
3. extractor 异常、存储异常、单条非法候选后 worker 仍能处理下一轮。
4. 精确重复只保留一条，来源不被后一次提取升级。
5. 队列容量与满载拒绝，不阻塞提交线程；使用 Event 控制 worker，不用耗时阈值作为主断言。
6. `start`/`stop`/`close` 幂等；drain true/false、超时、提交与关闭竞态均有界，测试线程在 `finally` 释放并 join。
7. 中文与英文相关检索、无匹配为空、确定排序、候选/结果上限、最近候选扫描方向。
8. formatter 对 emoji、引号、换行、伪分隔符安全转义；条数与总字符预算绝不越界；超长单条确定截断。
9. recall 的存储故障/破损行失败开放，日志不含测试正文。
10. 既有 SQLite 211 项基线与全部语音/LLM/STT 测试继续通过。

## 6. 文档与交接

- 新增 `docs/architecture/adr-memory-service.md`：说明应用层/持久层边界、词法检索限制、上下文预算、不可信记忆格式、有界队列、来源真实性、失败开放、线程/存储所有权和关闭语义。
- 更新 `docs/engineering-baseline.md`，只记录真实完成能力；明确尚未接入真实对话和 Ollama。
- 在 `docs/cursor-handoff-status.md` 顶部追加本轮状态，保留全部历史。
- 实际验证使用：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
git diff --check
```

完成后停止修改，等待 Codex 独立 Review。

## 明确禁止扩大范围

- 不实现或调用真实 Ollama/HTTP 提取器，不复用会提交 `ConversationHistory` 的生成入口做后台提取。
- 不将记忆上下文注入模型请求，不修改系统提示词或当前用户 prompt。
- 不装配到 `MikuAssistant`、handler、CLI、语音或启动流程。
- 不新增 embeddings、向量数据库、ORM、分词库或其他依赖。
- 不启动 `main.py`、真实模型、麦克风、扬声器、RVC、浏览器或系统操作。
- 不撤销既有修改，不提交、不推送。
