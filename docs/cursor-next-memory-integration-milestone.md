# Cursor 下一里程碑：长期记忆产品接线与无历史 Ollama 提取适配器

## 目标

长期记忆持久层与应用服务已通过 Codex 最终离线验收，但仍未进入真实对话路径。本里程碑完成一条默认关闭、可配置启用、失败开放的产品接线：

1. 在 LLM 请求前检索相关记忆，并作为明确标记的“不可信参考数据”加入 system 上下文；
2. 仅在模型成功完成完整回复后，把用户/助手完整轮次非阻塞提交给 `MemoryIngestionService`；
3. 提供不使用 `ConversationHistory` 的 `OllamaMemoryExtractor`，避免后台提取污染短期对话；
4. 把数据库、查询、提取队列和关闭责任纳入默认装配及回滚；
5. 默认配置保持 `memory.enabled: false`，未启用时现有行为和资源开销不变。

这一步完成后，长期记忆将具备真实产品接线路径，但真实 Ollama 提取质量、同机 GPU 竞争和端到端体验仍需用户另行实测。本轮不得声称已经完成语义检索或性能调度。

## 开始前

读取并遵循：

- `AGENTS.md`
- `skills/architecture-contracts/SKILL.md`
- `skills/safe-refactoring/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `skills/collaboration-review/SKILL.md`
- `docs/architecture/adr-long-term-memory-store.md`
- `docs/architecture/adr-memory-service.md`
- `docs/codex-review-memory-service-final.md`
- `src/memory/` 全部文件
- `src/llm/ollama_client.py`
- `src/llm/conversation_history.py`
- `src/core/handlers/llm_handler.py`
- `src/core/assembly.py`
- `src/core/assistant.py` 的默认装配、流式完成、取消及 `shutdown()` 路径
- `src/core/config.py`

先检查 `git status`。保留全部既有未提交修改，不覆盖用户的 STT、RVC、持续监听或其他无关改动。本轮不提交、不推送。

## 1. 无历史 Ollama 记忆提取器

在 `src/memory/` 中实现 `OllamaMemoryExtractor`（名称可等价），遵守现有 `MemoryExtractor` Protocol：

- 不复用 `OllamaClient.generate*()`，不创建或提交 `ConversationHistory`；直接通过窄 HTTP transport 调用 `/api/chat`，`stream=false`。
- transport/POST 可注入，构造不得联网、启动线程或探测模型；真实调用只发生在 ingestion worker 的 `extract()` 中。
- 请求模型、base URL、timeout、keep_alive 可配置；输入轮次用 `json.dumps` 编码成数据消息，system 明确声明用户和助手正文是不可信数据，不能执行其中指令。
- 要求返回一个 JSON 数组；每项只接受 `text`、`category`、`importance`、`confidence`。字段缺失使用明确默认值，多余字段拒绝或忽略策略写入 ADR并测试。
- 严格验证响应根对象、`message`、`content`、完成标记和字段类型；不得用 `eval`，不得从任意解释文本中猜 JSON。
- HTTP 超时、连接、状态码、JSON/结构错误使用具名异常或稳定错误类别交给 ingestion 失败开放；日志只记录异常类型/状态，不记录对话或记忆正文。
- `KeyboardInterrupt` / `SystemExit` 不得被包装成普通提取失败。
- 不增加第三方依赖；复用仓库现有 `requests`。

为避免一次回复产生无限候选，适配器与 ingestion 的候选硬上限保持一致。提示词不得宣称模型输出是用户确认事实；落库仍由 ingestion 统一标记 `MODEL_INFERRED`。

## 2. 记忆增强 LLM 边界

新增一个窄装饰器，例如 `MemoryAwareLLMClient`，包装现有 LLM 客户端而不修改其历史提交规则：

- 保留 `generate()`、`generate_result()`、`generate_stream()`、`clear_history()` 和 `conversation_history` 的兼容行为；其他实际使用的只读属性可显式委托。不要用宽泛魔法代理掩盖接口变化。
- 每次 LLM 对话请求前调用 `MemoryQueryService.build_context(user_text, ...)`。空结果或查询错误时原样调用底层客户端，不能阻止回复。
- 有记忆时只追加到 `system` 参数；保留调用方已有 system 内容，使用固定分隔与“不可信参考数据”声明。不得改写用户正文，不得把记忆提交进 `ConversationHistory`。
- `generate_result()` 只有 `GenerateStatus.SUCCESS` 且回复非空时创建一次 `MemoryTurnEvent(SUCCESS)` 并调用 ingestion 的非阻塞 `submit()`；提交失败不改变已生成结果。
- `generate()` 必须委托装饰器自身的 `generate_result()`，避免同一输入调用底层模型或提交记忆两次。
- `generate_stream()` 只在收到合法 `COMPLETED` 终结事件且完整正文非空时提交一次；`TEXT_DELTA`、`FAILED`、缺终结、迭代异常、消费方提前 `close()`、取消均不得提交半轮。
- `turn_id` 由可注入工厂生成，默认可使用 UUID；日志只记录 turn_id 和状态，不记录正文。
- 记忆查询、上下文拼接或 ingestion 拒绝均失败开放，但 `KeyboardInterrupt` / `SystemExit` 继续上抛。

现有 `OllamaClient` 仍是 HTTP 与短期历史所有者；不要把记忆业务塞回该类。系统/搜索意图不经过 LLM 时不得写入长期记忆。

## 3. 配置、装配和生命周期

增加 `memory` 配置段，至少包含：

- `enabled`，默认 `false`；
- SQLite 路径；
- recall 的 `max_entries`、`max_chars`、`candidate_limit`；
- ingestion 的 `queue_capacity`、`max_extractions_per_turn`、关闭 drain/timeout；
- extractor 的 base URL、model、timeout、keep_alive，可明确回退到 `ollama` 对应值。

要求：

- 未启用时不创建目录/数据库、不启动线程、不包装 LLM client，既有调用与测试保持原样。
- 启用时先校验全部 memory 配置，再创建 store/query/extractor/ingestion；不得在配置错误后才启动 TTS、STT 或其他设备资源。
- 默认装配启动 ingestion，并让路由器和助手使用包装后的 LLM client。
- 用具名 `MemoryRuntime`（或等价对象）集中持有 store、query、ingestion 和关闭策略；明确 store 所有权，只允许关闭一次。
- `AssembledParts` 与 `MikuAssistant.from_dependencies()` 支持可选注入 memory runtime；注入路径不得自动创建真实数据库、HTTP 或线程。
- 装配中后续组件失败时，回滚必须停止 ingestion 并关闭 store；单项失败不掩盖原始装配异常。
- `MikuAssistant.shutdown()` 在阻止新轮次后关闭 memory runtime；尊重 `IngestionStopStatus`。超时必须记录 `worker_alive/pending`，不得谎称已释放，也不得强杀线程。
- 重复 shutdown 不重复关闭。memory 关闭失败不能阻止 TTS、清理器和角色资源继续回收。

不要为了本里程碑启动真实模型、设备或主程序。SQLite 默认路径继续通过现有路径边界解析，不写注册表。

## 4. 响应性能约束

- 记忆提取只允许异步进行；前台生成不得等待 extractor 或落库。
- 同步 recall 只允许有界 SQLite 查询和现有有界词法评分；不得全库扫描。
- 记录可选的 recall 耗时和 ingestion 接受/拒绝计数，但日志不含查询或正文。
- 默认关闭是兼容和资源保护策略。真实启用后，后台 Ollama 提取仍可能与下一轮前台生成争用同一 GPU；本轮文档必须明确该限制，不得声称已解决模型优先级或抢占。
- 不在本轮引入第二个常驻模型、模型下载、进程级调度器或新的服务框架。

## 5. 纯离线测试

新增具名测试文件，使用 fake transport、fake LLM、fake query、fake ingestion、临时 SQLite 和 Event；不得调用真实 Ollama、麦克风、扬声器、TTS、RVC、浏览器或系统操作。

至少覆盖：

1. extractor 请求没有历史消息，轮次数据经 JSON 编码；合法数组映射为 `ExtractedMemory`。
2. timeout/连接/HTTP/响应 JSON/结构/字段错误不会写库或杀死 ingestion；KI/SystemExit 不被吞。
3. 记忆查询为空或失败时底层 LLM 收到原始 prompt/system，结果不变。
4. 有上下文时只扩展 system，保留已有 system；记忆中的换行、引号、伪 system 指令不能逃逸数据边界。
5. 非流式成功提交一次；空回复、结构化失败、底层异常不提交。
6. 流式 delta 后完成提交一次；delta 后 FAILED、迭代异常、缺终结、提前 close 不提交半轮。
7. `generate()` 兼容入口单次调用、单次提交；共享/默认短期历史语义不变。
8. `memory.enabled=false` 不调用任何 memory factory、不创建文件或线程，装配行为兼容。
9. 启用路径使用注入 factories 完成装配、启动和包装；配置非法在设备工厂前失败。
10. 装配后段失败逆序回滚 memory；shutdown 顺序、单次关闭、关闭异常继续回收、超时状态均有断言。
11. 系统和搜索意图不提交 memory；LLM 成功路径文本与流式均提交完整轮。
12. 全量离线回归继续通过。

不得用真实网络、`sleep` 猜竞态、源码字符串匹配或只断言 mock 被创建来代替行为验证。

## 6. 文档与交接

- 新增 `docs/architecture/adr-memory-product-integration.md`，记录装饰器边界、system 注入、无历史 extractor、配置默认关闭、失败开放、资源所有权、流式提交时点及 GPU 争用限制。
- 更新 `docs/engineering-baseline.md`，区分“离线路径已接通”和“真实 Ollama/产品体验未验证”。
- 在 `docs/cursor-handoff-status.md` 顶部追加本轮结果，保留历史。
- 实际运行并记录：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
git diff --check
```

完成后停止修改，等待 Codex 独立 Review。

## 明确禁止扩大范围

- 不修改 STT、TTS、RVC、持续监听、系统控制、搜索或角色表现逻辑。
- 不引入 embeddings、向量数据库、ORM、分词库或第二套记忆存储。
- 不把长期记忆写进 `ConversationHistory`，不根据错误字符串猜成功状态。
- 不让前台回复等待记忆提取，不把部分流式回复写入长期记忆。
- 不启动 `main.py`、真实 Ollama、真实设备、浏览器或系统操作。
- 不下载模型，不更换用户模型，不提交、不推送。
