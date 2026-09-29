# 当前优先任务：修复 Ollama `/api/chat` 的 system 消息契约

## 真实故障证据

2026-09-27 的真实文本验收中，`tmp/memory-live-test.sqlite3` 已保存并召回正确事实：

- 测试代号：`银杏4729`
- 最喜欢的饮品：`桂花乌龙`
- 日志：`memory recall done entries=4 chars=488 elapsed_ms=0.6 error=-`

但模型回答成了 `Project Nightingale` 与 `抹茶拿铁，少糖多奶`。仓库提示、日志和测试数据中没有这组错误事实。SQLite、词法召回和记忆上下文格式化已经工作，失败发生在上下文交给 Ollama 的最后一段协议。

`src/llm/ollama_client.py` 当前在非流式与流式 `/api/chat` 请求中执行：

```python
payload["system"] = system
```

Ollama 的 Chat API 请求契约没有顶层 `system` 字段；system 提示必须放在 `messages` 中，角色为 `system`。顶层 `system` 属于 `/api/generate` 契约。当前实现因此可能被服务端静默忽略，现有 fake 只捕获了 Python 方法参数，没有验证真实 HTTP JSON，造成离线假通过。

官方依据：

- <https://docs.ollama.com/api/chat>
- <https://github.com/ollama/ollama/blob/main/api/types.go> 中的 `ChatRequest` 与 `Message`

## Round 1 必须完成

1. 修复 `OllamaClient.generate_result()` 与真正的 `generate_stream()` HTTP 路径：
   - 有非空 `system` 时，把 `{"role": "system", "content": system}` 放进 `messages`；
   - 顺序固定为 `system → 最近历史 → 当前 user`；
   - `/api/chat` payload 中不得再出现顶层 `system`；
   - `system is None` 或空字符串时保持现有消息序列，不插入空 system。
2. 提取一个共享的消息构造边界，供流式与非流式路径共用，避免两个协议实现再次漂移。不得修改或污染 `ConversationHistory` 内部记录；system 只属于当前请求，不提交到短期历史。
3. 保持现有兼容行为：
   - 成功才提交完整 user/assistant 轮；失败、空回复、中断与提前关闭不提交；
   - 历史窗口与完整轮裁剪不变；
   - `keep_alive`、超时、结构化状态、warmup 和流事件契约不变；
   - `MemoryAwareLLMClient` 继续只负责 recall、system 增强和成功轮提交，不在 user prompt 中复制记忆。
4. 不修改 Modelfile、模型、数据库内容、召回排序或提取规则。本轮先修已证实的 HTTP 契约，不添加基于关键词的硬编码答案、二次生成或输出后修补。
5. 更新 `docs/architecture/adr-memory-product-integration.md` 与 `docs/engineering-baseline.md`：明确 `/api/chat` 的动态 system 上下文必须作为首条 system message 传输，测试必须检查最终 HTTP payload，不能只检查包装器参数。

## 必须新增的离线回归

使用 mocked `requests.post` 检查最终 `json` 参数，而不是只检查传给 fake LLM 的 `system` 参数：

1. 非流式：system 是 `messages[0]`，历史保持原顺序，当前 user 是最后一条；payload 没有顶层 `system`。
2. 流式：与非流式完全相同的 system、历史和当前 user 顺序；payload 没有顶层 `system`。
3. 无 system：不产生 system 消息，原有 payload 与历史语义不变。
4. 历史隔离：构造请求后修改捕获到的 payload，不得反向修改 `ConversationHistory`。
5. 记忆端到端 fake：使用真实 `MemoryAwareLLMClient + OllamaClient`、临时 store/query 和 mocked HTTP；最终 HTTP JSON 的首条 system message 必须同时包含 `银杏4729`、`桂花乌龙` 与 `subject=user`，当前问句必须位于最后一条 user message。
6. 流式异常、提前 close、非流式失败等既有测试继续通过，不能产生双提交。

## 验证与停止点

仅用 Python 3.12、fake HTTP 和临时目录运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

完成后更新 `docs/cursor-handoff-status.md` 顶部，写明修改文件、测试数量与未验证项，然后停止等待 Codex Review。

不要启动真实 Ollama、主程序、麦克风、扬声器、TTS/RVC、浏览器或清理任务；不要清空或改写 `tmp/memory-live-test.sqlite3`；不要进入存储硬化 Round 2/3；不要提交或推送。
