# Codex Review：长期记忆产品接线 Round 1

## 结论

当前 245 项离线测试、`compileall` 和 `git diff --check` 均通过；默认关闭、无历史提取、system 注入和基本装配路径已经落地。

暂不做最终验收。Codex 额外确定性复现发现：记忆辅助逻辑仍可能把已经成功的前台回复变成异常，流式取消没有显式关闭底层迭代器，runtime 构造与关闭失败会丢失资源所有权，关闭超时后助手也没有可达的最终回收路径。请只修复以下契约，不扩大到真实 Ollama、模型调度或其他子系统。

## 必须修复

### 1. P1：turn_id 工厂异常会破坏已经成功的前台回复

位置：`src/memory/aware_client.py:162`～`src/memory/aware_client.py:163`

`_try_submit()` 在 `try` 之外调用 `_turn_id_factory()`。若注入工厂异常，`generate_result()` 不再返回底层已经成功的 `GenerateResult`；流式路径也会在 `COMPLETED` 到达时抛错，调用方收不到成功终结事件。

Codex 复现：

```text
底层 generate_result -> SUCCESS("ok")
turn_id_factory -> RuntimeError
MemoryAwareLLMClient.generate_result -> RuntimeError（错误）
```

修复要求：

- turn id 创建、事件构造和 `submit()` 全部属于失败开放的记忆辅助步骤；普通异常不得改变或阻止前台结果/终结事件；
- `KeyboardInterrupt` / `SystemExit` 继续上抛；
- 日志不能因为没有 turn id 而再次失败，也不得记录正文；
- 增加非流式成功与流式 `COMPLETED` 两个确定性测试，工厂异常时仍返回/产出原成功结果且没有半轮写入。

### 2. P1：外层流式迭代器提前关闭时没有显式关闭底层流

位置：`src/memory/aware_client.py:93` 起

`generate_stream()` 保存了底层 `stream`，但没有 `finally` 调用其 `close()`。Codex 使用带 `close_calls` 的迭代器复现：读取一个 delta 后关闭外层生成器，底层 `closed=False`。真实 `OllamaClient` 可能依赖 CPython 引用回收间接进入自身 `finally`，这不是可移植的资源生命周期契约，也不适用于其他兼容迭代器。

修复要求：

- 用 `finally` 显式关闭底层可关闭迭代器；正常完成、FAILED、迭代异常、`GeneratorExit`/提前 close 均只关闭一次；
- 普通 close 异常只记录类型，不能覆盖原终结事件或原异常；`KeyboardInterrupt` / `SystemExit` 不吞；
- 提前 close 仍不得提交记忆；增加 close-aware fake 的行为测试。

### 3. P1：`MemoryRuntime.create_from_settings()` 中途失败会泄漏已创建的 store

位置：`src/memory/runtime.py:278`～`src/memory/runtime.py:305`

store 在 extractor、ingestion 和 query 之前创建；后续任一步抛错时方法尚未返回 runtime，装配回滚表也尚未登记它，因此没有所有者关闭 store。

Codex 复现：

```text
store_factory -> 已创建可关闭 store
extractor_factory -> RuntimeError
store.closed -> False（错误）
```

修复要求：

- `create_from_settings()` 使用局部回滚：store 创建成功后，后续任一步失败都关闭 store；
- 清理失败只记录，原始构造异常必须原样抛出；KI/SystemExit 的清理与传播语义写清并测试；
- 覆盖 extractor 构造失败，按可触发性补 ingestion/query 构造失败；不得启动线程。

### 4. P1：runtime 关闭失败/超时后所有权被永久丢失

位置：`src/memory/runtime.py:232` 起；`src/core/assistant.py:955`、`src/core/assistant.py:980`～`src/core/assistant.py:1032`

有两个相关问题：

1. store `close()` 抛普通异常时，runtime 只记录日志，随后仍设置 `_owns_store=False`、`_closed=True`。第二次 close 返回成功快照，不再重试实际未完成的关闭。
2. ingestion 关闭超时返回 `worker_alive=True` 时，助手只打日志，不把它计入关闭失败，随后设置 `_shutdown_flow_done=True`。第二次 `shutdown()` 直接跳过，worker 后续退出后也没有路径关闭 store。

Codex 复现：

```text
store close raises -> close_calls=1, runtime_closed=True, owns_store=False
assistant runtime close -> worker_alive=True, pending=1
assistant.shutdown() twice -> runtime.close_calls=1, shutdown_flow_done=True
```

修复要求：

- store close 失败不得清除所有权或标记 runtime 完成关闭；错误应向助手关闭步骤传播或通过结构化状态表达，使助手继续回收 TTS 等资源但不记录无条件成功；
- worker 存活时继续保留 store 所有权；必须提供一个可达、可测试的最终回收路径。可以让后续 `shutdown()` **只重试尚未完成的 memory close**，或采用等价的受控完成机制；已经成功关闭的资源不得重复关闭；
- 普通成功路径与重复 close/shutdown 仍幂等；不使用强杀或 daemon 掩盖；
- 增加 Event 控制的超时→worker 退出→重试关闭测试，断言 store 最终只关一次；增加 store 首次 close 失败→保留所有权→后续成功测试；
- 不让关闭状态异常阻止 TTS、清理器和角色继续回收。

### 5. P2：配置解析先强转，导致非法类型被静默接受；关闭功能仍会读取损坏的休眠配置

位置：`src/memory/runtime.py:78`～`src/memory/runtime.py:130`；`src/memory/ollama_extractor.py:51`

当前先执行 `int(...)` 再调用 `_require_positive_int()`，会把 `True`、`1.5`、`"8"` 分别接受为整数 1、1、8。并且 `memory.enabled=false` 时仍解析所有 recall/ingestion/extractor 字段；休眠字段为 `"not-an-int"` 会在任何设备工厂前让原本禁用记忆的应用启动失败。transport 的直接构造也未拒绝 NaN timeout。

Codex 复现：

```text
max_entries=True  -> accepted as 1
max_entries=1.5   -> accepted as 1
max_entries="8"   -> accepted as 8
enabled=false + max_entries="not-an-int" -> ValueError / startup failed
```

修复要求：

- 不在类型校验前调用 `int()`；整数配置只接受真正的 `int` 且拒绝 bool、float、数字字符串；错误统一为具名 `MemoryValidationError` 并指出配置项；
- `memory.enabled=false` 只验证开关本身，不应因不会使用的 memory 子配置而破坏原有启动路径；返回快照可使用安全默认值，但不得创建路径、数据库或线程；
- 所有 timeout 入口拒绝 NaN/Infinity/bool；
- 增加上述四个配置场景和直接 transport finite 校验测试，证明非法启用配置在设备工厂前失败、禁用路径保持零副作用兼容。

## 已验证通过的部分

- extractor 的请求只有 system + JSON 编码轮次，没有 `ConversationHistory`。
- 基本响应结构、HTTP/JSON 故障分类与 ingestion 失败开放路径已有覆盖。
- 有界 recall 只扩展 system，空/失败 recall 保留原请求。
- 普通非流式成功和流式完成提交一次，FAILED/普通异常路径不提交。
- `memory.enabled=false` 在配置合法时不调用 runtime factory。
- 基本启用装配、后段失败回滚、系统意图不写记忆路径已覆盖。

## 独立验证证据

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 245 tests in 6.024s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
→ OK

git diff --check
→ OK
```

另运行了上述纯 fake 确定性复现；未启动真实 Ollama、主程序、麦克风、扬声器、TTS、RVC、浏览器或系统操作。

## 修复后验证

完成所有新增故障测试后重跑全量离线测试、`compileall` 和 `git diff --check`，更新 ADR、工程基线与 `docs/cursor-handoff-status.md` 顶部，然后停止修改等待 Codex Round 2。不得提交、推送或扩大范围。
