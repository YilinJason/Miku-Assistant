# ADR：Ollama 独立增量输出接口

- **状态**：已采纳（第八小步；**未**接入 Handler / CLI / TTS）
- **日期**：与 `docs/engineering-baseline.md` 本轮更新同步
- **关联**：`adr-generate-result.md`、`adr-structured-dispatch.md`

## 问题

既有 `generate(..., stream=True)` / `generate_result(..., stream=True)` 在客户端内**聚合**整段后再返回，无法在完整响应到达前向调用方交付增量，也不区分「已有部分文本」与「成功完成」。实时对话需要独立的增量契约，同时不能破坏旧聚合路径。

## 官方流式协议（本轮所用字段与来源）

来源：

- [Ollama Chat API](https://docs.ollama.com/api/chat)（`ChatStreamEvent`）
- [Ollama Streaming](https://docs.ollama.com/api/streaming)（NDJSON 通则）

本客户端使用 **`POST /api/chat`** + **`stream: true`**，响应为 **newline-delimited JSON**（`application/x-ndjson`）。

| 字段 | 含义 | 本轮用法 |
|------|------|----------|
| `message` | 对象；含本段助手消息 | 必须为 dict；否则失败终结 |
| `message.content` | 字符串；**部分**助手文本 | 非空则产出 `TEXT_DELTA` 并累加 |
| `message.role` | 角色 | 不作为完成判定 |
| `done` | 布尔；`true` 表示流的**最终**事件 | **唯一成功完成标记** |
| `done_reason` | 结束原因（如 `stop`） | 记录在协议说明中；**不**单独作为提交条件 |
| `model` / `created_at` / 耗时字段 | 元数据 | 本轮不消费 |
| `error` | 部分实现以对象行返回错误 | 若存在非空则 `FAILED` |

说明：`/api/generate` 使用顶层 `response` 字段；**本轮不使用** generate 端点。

## 决策

1. 新增 `src/llm/stream_events.py`：`StreamEventKind`（`TEXT_DELTA` | `COMPLETED` | `FAILED`）与冻结的 `StreamEvent`。
2. 新增 `OllamaClient.generate_stream(prompt, system=None) -> Iterator[StreamEvent]`：
   - `requests.post(..., stream=True)`，按行读取并**立即** yield 增量（不先收集完整响应）。
   - **交付本行增量前**校验：`done` 若存在必须为 bool；非法类型 → FAILED/RESPONSE_ERROR（不交付该行增量）。仅 `done is True` 完成，`False` 继续；缺完成标记至 EOF 则失败。
   - 正常消费至结束：**恰好一个**终结事件（`COMPLETED` 或 `FAILED`），之后不再产出。
   - 仅在收到 `done is True` 后进入 `COMPLETED`；全文非空时 **`commit_turn` 一次**。
   - 缺 `done`、解析/结构错误、连接中断、服务 `error` 行 → `FAILED`，**不**改历史。
   - 消费方 `close()` 迭代器或异常路径：`finally` 中 `response.close()`；未完成则不提交。
3. **保留** `generate` / `generate_result` 行为；旧聚合路径的 `done` 判定与是否 `stream=True` **不顺带修改**。
4. 增量接口本身无自动重试、无后台线程、无异步框架；产品链可选消费见 `adr-stream-playback.md`。
5. **不宣称**语音硬打断或端到端实时对话仅靠本接口即完成。

## 兼容范围与限制

| 项 | 说明 |
|----|------|
| 兼容 | 旧 `generate` / `generate_result`（含 `stream=True` 聚合）调用方式与提交规则不变 |
| 新接口 | 仅 `generate_stream`；产品链尚未消费 |
| 限制 | 无取消传播到上游以外的「半截播放」处理；无 turn_id；thinking/tool_calls 本轮不解析 |
| 空完成 | `done: true` 且无文本 → `COMPLETED` + `EMPTY`，不提交历史 |

## 替代方案

- 改造 `generate_result(stream=True)` 为增量：破坏现有聚合语义，拒绝。
- 先读完全流再假装增量：违反「真正逐段交付」，拒绝。

## 离线验证

```text
py -3 -m unittest discover -s tests -v
→ Ran 84 tests in 0.034s … OK
```

见 `tests/test_generate_stream.py`；原有测试继续通过。
