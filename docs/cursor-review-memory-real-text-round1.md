# 真实 Ollama 长期记忆文本验收入口：Codex Round 1 Review

## 结论

本轮暂不验收。Cursor 的 269 项离线测试、`compileall`、`git diff --check` 和默认 dry-run 均通过，JSON Schema、临时数据库、显式 `--execute` 与报告脱敏的主体方向正确；但 Codex 补充故障复现确认，当前实现仍可能在后台任务尚未开始、wrapper 本轮记忆没有写入、或本地收到 schema 外字段时错误地判定成功。

本轮只修复下列契约与对应测试/文档。不得运行 `diagnose_memory.py --execute`，不得启动真实 Ollama、麦克风、TTS、RVC、角色、主程序或系统操作；不得修改默认 `memory.enabled=false`；不得提交或推送。

## 必须修复

### 1. P1：`wait_until_idle()` 在 dequeue 与 `in_flight` 之间会提前返回

位置：`src/memory/ingestion.py:361`～`src/memory/ingestion.py:386`，`src/memory/ingestion.py:228`～`src/memory/ingestion.py:244`

worker 先执行 `queue.get()`，随后才在另一临界区设置 `_in_flight=True`。在这两个动作之间，队列已经为空且 `_in_flight` 仍为 False，`wait_until_idle()` 会返回 True，尽管 extractor 尚未开始。

Codex 用 gated `queue.get()` 确定性复现：提交一个成功事件，worker 已取走事件但被阻塞在返回前，此时 `wait_until_idle(0.1)` 返回 `True`。

修复要求：

- 空闲判断必须对“所有已经由 `submit()` 接受、但尚未完成处理或明确丢弃的任务”线性化计数；不能依赖 `qsize + in_flight` 拼接两个非原子状态。
- 推荐在成功入队时于现有锁内增加 accepted-pending 计数，并在每个任务的最终处理路径或显式 discard 路径准确减少；归零时通过现有 Condition 通知。
- `stop`、BaseException 后替代 worker、`drain=False` 丢弃、重复 close 与超时状态必须继续准确。
- 增加上述 gated dequeue 的确定性测试；禁止用 sleep 或耗时阈值作为主要断言。

### 2. P1：wrapper 可在本轮偏好未进入记忆时仍报告 pass

位置：`src/memory/diagnostic.py:574`～`src/memory/diagnostic.py:638`，`src/memory/diagnostic.py:722`～`src/memory/diagnostic.py:785`，`src/memory/diagnostic.py:530`～`src/memory/diagnostic.py:555`

当前四个直接 extractor 场景与 wrapper 共用一个 store；`memory_in_system` 只检查通用的“记忆参考数据”标记；`FakeDiagnosticLLM` 在查询最喜欢的饮品时，即使 system 中没有 `桂花乌龙`，也会直接返回该答案。

Codex 将 wrapper seed 对应的第五个提取响应改为空数组，保留前四个场景的正常响应。实际结果仍为：

```text
wrapper.status=pass
memory_in_system=True
reply_hit_fact=True
exit_code=0
```

这没有证明“第一轮提交偏好 → ingestion 写入 → 等待 idle → 第二轮 recall 注入”的本轮链路。

修复要求：

- wrapper 验收使用隔离的空 store，或在 wrapper 前以受控方式清空场景数据；不得让前四个场景污染判断。
- `memory_in_system` 必须检查本轮唯一合成事实（`桂花乌龙`）确实出现在传给底层 LLM 的 system 中，不能只检查通用标题。
- fake 前台 LLM 只有在 system 真正包含该事实时才返回命中答案；缺失时返回不命中的文本。
- 增加故障测试：wrapper seed extractor 协议成功但返回空数组时，wrapper 不得 pass，整个诊断不得退出 0。

### 3. P1：去重、recall 与 context 只记录指标，没有参与验收结果

位置：`src/memory/diagnostic.py:428`～`src/memory/diagnostic.py:485`

`_run_extractor_scenario()` 会写两遍候选并记录 `deduped`、`recall_hits`、`context_chars`，但 stage 的 status 在写库之前已经确定，后续这些值无论是否符合预期都不会改变 status。因而第二次精确重复仍被新建、稳定偏好完全无法召回或 context 为空时，诊断仍可能退出 0。

修复要求：

- 对非空候选明确验证第二次精确重复没有新建且去重数量与本次候选相符；存储契约异常计为 contract failure（退出 1）。
- 对稳定偏好/资料场景验证真实写入后的 lexical recall 与 context formatter 结果；模型文本质量导致未命中可计为 quality warning（退出 2），组件异常计为 contract failure。
- 报告继续只保存数量和预定义摘要，不保存完整 prompt、回复或记忆正文。
- 用可注入 fake 制造“第二次仍 created”“recall 空”“context 空”等边界，至少证明这些情况不能退出 0。

### 4. P2：本地解析仍接受 schema 禁止的额外字段

位置：`src/memory/ollama_extractor.py:285`～`src/memory/ollama_extractor.py:303`，`tests/test_memory_diagnostic.py:192`

Schema 设置了 `additionalProperties: false`，任务测试要求本地解析继续拒绝额外字段，但 `_parse_item()` 当前明确忽略未知键。Codex 输入：

```json
[{"text":"x","unexpected":"accepted"}]
```

实际返回一个合法 `ExtractedMemory`，没有抛 `MemoryExtractionResponseError`。服务端 schema 不是本地信任边界，fake、旧版服务或不遵循 schema 的响应仍须由客户端防御。

修复要求：

- `_parse_item()` 只接受 `text/category/importance/confidence` 四个键；出现其他键时抛具名 `MemoryExtractionResponseError`。
- 保留 ADR 已决定的缺省字段兼容规则；本项只收紧未知键，不要求四字段全部必填。
- 增加明确的额外字段失败测试，并同步 ADR 中“忽略额外键”的错误措辞。

## 必须保留

- 请求仍为 `/api/chat`、`stream=false`、两条无历史消息、每次独立 JSON Schema、`temperature=0`。
- `keep_alive` 继续接受非空字符串与有限数值（含 0），拒绝 bool、NaN、Infinity 和其他类型。
- dry-run 不创建 SQLite、不启动 ingestion、不发送 HTTP；显式报告路径之外不写文件。
- execute fake/真实路径只用合成文本与临时数据库；不得触碰生产 `memory.db_path`。
- Ctrl+C/SystemExit 与关闭不完整语义、报告脱敏和退出码契约继续成立。

## Round 2 验收命令

仅运行纯离线验证：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 diagnose_memory.py
```

不得运行 `diagnose_memory.py --execute`。完成后更新 `docs/cursor-handoff-status.md`，写明实际测试数量与仍未进行的真实 Ollama 验收，然后停止等待 Codex Round 2。

## Codex 本轮实际验证

```text
unittest discover → Ran 269 tests in 6.522s … OK
compileall -q src tests diagnose_memory.py → OK
git diff --check → OK（仅既有 LF/CRLF 提示）
diagnose_memory.py（无 --execute）→ exit 0，确认 dry-run 摘要
```

未运行真实 Ollama、`--execute`、麦克风、扬声器、TTS、RVC、角色、主程序或任何系统操作；未提交、未推送。
