# 当前优先任务：阻断助手回答污染长期记忆

## 真实故障证据

真实测试已经证明当前 ingestion 会把助手自己的回答当成用户事实：

1. 22:43 的旧请求没有把记忆 system 正确传给 Ollama，模型编造了 `Project Nightingale` 与 `抹茶拿铁，少糖多奶`。
2. 该轮 `turn_id=832fb61b64e84153b18603eb1de49775` 随后被 ingestion 接受 2 条；SQLite 写入：
   - `42b6f68fa0044fbbad26f29abcb5ae44`：`用户的测试代号是 Project Nightingale。`
   - `a8f25792de3e49e39205bdc7c12f0e1b`：`用户最喜欢的饮品是抹茶拿铁，少糖多奶。`
3. 修复 Chat system 之后，下一次 recall 已包含上述污染记录，模型同时复述正确与错误事实；`turn_id=140579b8fe064f3b870cd58d579daa21` 又接受 3 条，其中新增：
   - `4a97c2e8ed194442ac99007bd4415237`：`用户最喜欢喝抹茶拿铁（少糖多奶）。`
4. 这些记录均为 `source=model_inferred`，不是用户确认事实。当前 live 测试库已经有 9 条；本任务不得直接删除或改写它。

代码原因：

- `OllamaMemoryExtractor.build_chat_payload()` 把 `user_text` 与 `assistant_text` 一起交给模型；
- `should_skip_extraction()` 使用二者拼接后的文本判断稳定信号；
- 本地过滤没有验证候选事实是否由本轮用户原文支持；
- ingestion 因此会把 assistant-only 幻觉以 `MODEL_INFERRED` 落库，并在后续 recall 中自我强化。

## Round 1 目标

长期记忆只能来源于本轮用户明确表达的内容。助手回答可以触发成功轮提交，但不得成为事实证据。用户只是询问“你记得/你知道我的资料吗”时，不得把回答内容重新提取为新记忆。

## 必须完成

1. **用户原文是唯一事实来源**：
   - 修改 extractor 输入契约，候选事实只能依据 `user_text`；
   - 不得把 `assistant_text` 作为可提取事实正文发送给 extractor。若因兼容原因仍携带，必须放在明确的非证据字段中，且本地验证不能读取它；优先完全不发送；
   - 更新 `EXTRACTOR_SYSTEM_PROMPT`，明确问题中的未知答案不能由模型或助手回答补全。
2. **准入只看用户侧信号**：
   - `should_skip_extraction()` 不得因 `assistant_text` 含“喜欢、名字、住在”等稳定词而调用提取；
   - 纯记忆查询（例如“你知道我的测试代号和最喜欢的饮品吗？”）必须跳过或最终产生 0 条；
   - 用户同一句中确实包含自我陈述时仍可提取，例如“我最喜欢桂花乌龙，你呢？”。不要把所有问号句一刀切。
3. **增加可验证的用户证据契约**：
   - extractor 结构化输出增加仅供验证的用户原文证据字段，例如 `evidence_quote`；
   - 该证据必须是 `user_text` 的非空原样子串，不能来自 `assistant_text`；
   - 候选事实的核心值必须被该证据支持。缺失证据、证据不在用户原文、仅引用问题标签却补造答案、或候选与证据冲突时，本地拒绝；
   - 证据字段不写入 SQLite，不进入 recall/system，不记录到普通日志；持久化仍只保存规范化事实，并保持 `MODEL_INFERRED`。
4. **失败开放但不污染**：
   - 无法证明用户来源时返回 0 条，不得猜测；
   - 不因单个无效候选丢弃同轮其他有证据的合法候选；
   - 解析、队列、关闭、admission lease 与性能遥测契约保持不变。
5. 更新 `docs/architecture/adr-memory-service.md`、`docs/architecture/adr-memory-product-integration.md` 与 `docs/engineering-baseline.md`，明确“用户原文唯一事实来源”和本地证据验证边界。

## 必须新增的离线回归

1. 用户问“你知道我的测试代号和最喜欢的饮品吗？”，assistant 回答 `Project Nightingale / 抹茶拿铁`：不得调用 extractor，或 extractor 结果必须全部拒绝；store 新增 0 条。
2. 即使 fake extractor 为 assistant-only 内容返回 `confidence=1.0`，仍不得写库。
3. 用户明确说“我的测试代号是银杏4729，我最喜欢桂花乌龙”：两条带合法用户证据的候选正常写入。
4. 混合句“我最喜欢桂花乌龙，你喜欢什么？”仍保留用户已经声明的偏好，不因问号整体跳过。
5. `evidence_quote` 不在 `user_text`、只在 `assistant_text`、为空、类型错误、被截断伪造或只引用问题标签后补造答案：逐条拒绝。
6. 同轮一个合法、一个无证据候选：仅写合法项。
7. prompt injection、算术、寒暄、第一人称 owner、JSON Schema 未知字段、条数上限与关闭/线程测试继续通过。
8. 增加真实 service 路径的 fake 集成：`MemoryTurnEvent → OllamaMemoryExtractor(fake transport) → MemoryIngestionService → 临时 SqliteMemoryStore`，证明 assistant-only 幻觉无法落库。

## 当前污染库处理边界

本轮只修代码并生成只读审计结果，**不得修改** `tmp/memory-live-test.sqlite3`。不得自动按文本关键词删除，也不得清空数据库。

可以新增一个小型离线管理入口，但必须满足：

- 默认只读 dry-run；
- 删除必须显式 `--execute` 并按完整 memory ID 指定；
- 执行前打印将删除的 ID、正文和来源，ID 不存在或正文已变化时失败关闭；
- 不提供“删除全部匹配词”或默认清库；
- 本轮不得实际对 live 测试库执行删除。

完成 Codex Review 后，再由用户显式运行定向清理。预期需要删除的污染 ID 是：

```text
42b6f68fa0044fbbad26f29abcb5ae44
a8f25792de3e49e39205bdc7c12f0e1b
4a97c2e8ed194442ac99007bd4415237
```

正确记录及其同义重复暂不自动合并。

## 验证与停止点

仅用 Python 3.12、fake transport 和临时 SQLite：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

完成后更新 `docs/cursor-handoff-status.md` 顶部，记录修改文件、测试数量、live 库只读行数与未验证项，然后停止等待 Codex Review。

不要启动真实 Ollama、主程序、麦克风、扬声器、TTS/RVC、浏览器、清理任务或系统操作；不要进入存储硬化 Round 2/3；不要提交或推送。
