# ADR：内存会话历史组件拆分

- **状态**：已采纳（第五小步，保持行为的迁移）
- **日期**：与 `docs/engineering-baseline.md` 本轮更新同步

## 问题

`OllamaClient` 同时负责 HTTP 协议、连通性探测与多轮历史的存储/窗口选择，难以单独验证上下文选择与提交规则。长期记忆与持久化尚未启动，但会话历史管理需要可离线测试的独立边界。

## 决策

1. 新增 `src/llm/conversation_history.py` 的 `ConversationHistory`：进程内内存消息列表 + 请求窗口选择。
2. `OllamaClient` 持有一个历史实例（可注入）；`generate(...)` 签名与错误字符串返回语义不变。
3. **请求窗口**：默认最近至多 **10 条历史消息**（message 条数）+ 当前用户输入；由 `build_messages` 实现。
4. **存储容量**：本轮**不截断**，保持只增不减、仅 `clear`/`clear_history` 清空（有界存储另案）。
5. **提交规则**：仅在成功得到**非空**回复后 `commit_turn`（流式聚合分支与非流式分支共用）；失败/解析异常/空回复不写半轮。
6. **快照兼容**：原公开属性 `conversation_history` 改为返回**可修改但不影响内部记录的副本**（不是不可变对象）。仓库检索**未发现**调用方依赖可变内部列表；策略为返回副本，而非宣称「已无外部依赖」。清空仍用 `clear_history()`。
7. **隔离 vs 共享**：默认各自新建的历史实例相互隔离。显式注入同一个 `ConversationHistory` 时共享存储，任一方 `clear_history()` 会影响其他共享者。
8. 不引入数据库、向量检索、长期记忆、流式增量对外契约，不改 Ollama 请求协议字段。

## 替代方案

- 把历史留在 `OllamaClient` 仅抽函数：仍难单独测实例隔离与注入。
- 本轮一并做有界环形缓冲：改变存储行为，超出「保持兼容」范围，拒绝。
- 公开属性继续暴露可变内部列表：与「避免暴露可修改内部数据」冲突，拒绝。

## 取舍

- **利**：上下文选择与提交规则可离线验证；客户端职责变窄。
- **弊**：存储仍无界（P3 未解）；`conversation_history` 不再是内部列表的可变引用（仓库内未发现此类依赖，但仍需在文档标明兼容策略）。

## 迁移

- `LLMIntentHandler` / `generate(text)` 调用方式不变。
- 测试与可选注入：`OllamaClient(..., history=ConversationHistory())`。
- 回退：将历史逻辑内联回 `OllamaClient`，删除 `conversation_history.py` 与对应测试。

## 离线验证

```text
py -3 -m unittest discover -s tests -v
→ Ran 53 tests in 0.028s … OK
```

`tests/test_conversation_history.py` 覆盖请求窗口、提交规则、清空隔离、副本修改不影响内部，流式聚合 / 非流式分支，以及解析失败 / 流中非法 JSON / 流中连接异常在已有成功对话后的历史不变。见 engineering-baseline 最近一次全量结果。

本轮完成的是**会话历史管理拆分**，**尚未**实现持久化或长期记忆。流式分支仅验证「HTTP 行聚合后一次返回」下的历史提交规则，**不是**端到端流式。
