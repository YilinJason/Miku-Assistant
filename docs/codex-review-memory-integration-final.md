# Codex 最终评审：长期记忆产品接线

## 结论

长期记忆产品接线与无历史 Ollama 提取适配器通过最终离线验收。默认关闭兼容、记忆上下文注入、完整成功轮异步提交、配置装配、失败回滚和关闭重试均已形成可离线验证的产品路径。

本结论不代表真实 Ollama 提取质量、同机 GPU 并发性能或端到端陪伴体验已经验收。

## 已验证

- `memory.enabled=false` 时不创建数据库、不启动 ingestion、不包装 LLM；损坏的休眠子配置不影响原有启动路径。
- 启用时在设备工厂前严格校验配置；整数与 timeout 拒绝 bool、错误类型和非有限值。
- `OllamaMemoryExtractor` 直接使用无历史 `/api/chat` 请求；构造不联网，轮次正文以 JSON 数据传入。
- 提取响应、字段、HTTP 和 JSON 错误被结构化收口；不污染 `ConversationHistory`，不把推断事实标为用户确认。
- `MemoryAwareLLMClient` 只把有界记忆上下文追加到 system；查询失败保持原始请求并允许前台继续。
- 非流式和流式仅在完整成功回复后提交一次；FAILED、缺终结、迭代异常和提前关闭不提交半轮。
- turn_id、事件构造和 ingestion 提交的普通异常不会破坏已经成功的前台回复。
- 外层流关闭会显式关闭底层迭代器一次。
- 装饰器显式保留生成、历史、预热、连接检查，以及 CLI/语音预检使用的 `model`、`check_model_available()` 和 `list_models()`。
- runtime 构造中途失败会关闭已创建 store，并保留原始异常。
- store 关闭失败和 worker 超时均保留所有权；后续 close/shutdown 只重试未完成的 memory 关闭，不重复回收 TTS 等资源。
- 系统与搜索意图不进入 LLM 成功轮记忆提交路径。

## 独立故障复现

Round 1 与 Round 2 的附加 fake 复现全部通过，包括：

```text
严格整数配置：bool / float / 数字字符串均拒绝
禁用 + 损坏休眠配置：正常返回 enabled=False
runtime 构造失败：store_closed=True
turn_id 工厂失败：仍返回原 SUCCESS
流式提前关闭：inner close_calls=1
store 首次关闭失败：保留所有权，第二次关闭成功
助手 memory 超时：第二次只重试 memory，TTS close_calls=1
包装客户端：model / check_model_available / list_models 正常代理
runtime.close(timeout=True)：MemoryValidationError，未触发关闭副作用
```

## 独立验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 257 tests in 6.102s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
→ OK

git diff --check
→ OK
```

全部为纯离线测试；未启动真实 Ollama、主程序、麦克风、扬声器、TTS、RVC、浏览器或系统操作。未提交、未推送。

## 未验证边界

- 真实 Ollama 记忆提取的准确率、JSON 遵循率、延迟与失败表现。
- 后台提取与下一轮前台生成竞争同一 Ollama/GPU 时的响应影响；当前没有优先级或抢占调度。
- 长期记忆实际启用后的端到端文本与语音体验、隐私管理及记忆纠错流程。
- 语义或向量检索；当前只有确定性有界词法检索。

下一阶段应先进行受控的真实文本链路验收，确认提取质量、提示词安全和性能开销，再决定是否默认启用或引入模型请求调度。
