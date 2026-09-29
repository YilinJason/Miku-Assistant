# Codex Review Round 1：长期记忆资源治理、诊断遥测与误提取收口

## 结论

本轮离线测试与静态检查通过，但发现 2 个阻断行为缺陷和 2 个契约/文档缺陷，暂不通过最终验收。Cursor 仅修复本单，完成后停止并等待 Codex Round 2；不得运行真实 `diagnose_memory.py --execute`。

## Finding 1（P1）：成功前台轮次在自身 foreground lease 内提交，可能被 admission gate 丢弃

位置：

- `src/memory/aware_client.py:87-102`
- `src/memory/aware_client.py:107-147`
- `src/memory/ingestion.py:447-457`

触发：`MemoryAwareLLMClient.generate_result()` 在 `gate.end_foreground()` 之前调用 `_try_submit()`；流式路径同样在收到 `COMPLETED` 时、foreground lease 仍持有期间提交。ingestion worker 取到该事件后调用 `try_acquire_extract()`，会看到 `foreground_count > 0` 并把已经接受的事件永久跳过。

后果：启用长期记忆后，正常成功对话可能因线程调度而随机不入库；流式路径更容易发生，因为完成事件 yield 给调用方之前已经提交，而 foreground lease 直到生成器继续/关闭后才释放。该 gate 因此可能消除它本来要保护的记忆功能。

Codex 定向证据：fake ingestion 在 `_try_submit()` 内观察到 `foreground_count=1`；返回后才归零。

修复要求：

1. 对一次前台调用使用明确、exactly-once 的 lease 生命周期。
2. 成功结果必须先结束本轮 foreground lease，再非阻塞提交本轮记忆；不得因修复造成双提交或漏关底层流。
3. 若系统中仍有其他并发前台调用，gate 可继续拒绝/跳过后台提取；本单只要求当前调用不能被自己的 lease 阻断。
4. 补确定性测试：非流式与流式的 `ingestion.submit()` 均观察到本轮 lease 已释放；真实 `MemoryIngestionService` + 同一 gate 的成功轮不会因自身 foreground 标记被丢弃。

## Finding 2（P1）：流式建立阶段异常会永久泄漏 foreground 计数

位置：`src/memory/aware_client.py:107-135`

触发：`gate.begin_foreground()`、`_augment_system()` 和 `self._inner.generate_stream()` 位于保护用 `try` 之前。若 recall 抛出 `KeyboardInterrupt` / `SystemExit`，或底层 `generate_stream()` 在返回迭代器前同步抛错，`_close_once()` 不会执行。

后果：`foreground_count` 永久大于 0，之后所有后台提取都会被跳过，直到进程重启。

Codex 定向复现：令 `query.build_context()` 抛出 `KeyboardInterrupt`，推进外层生成器一次后 `foreground_count=1`。

修复要求：

- 从 lease 获取成功开始，所有后续出口都必须 exactly-once 释放，包括 recall 的 KI/SystemExit、底层流同步构造异常、迭代异常、正常完成、调用方提前 close、底层 close 异常。
- 保留 KI/SystemExit 上抛、普通异常语义和底层流只关闭一次。
- 对上述每个出口补行为测试，并断言 `foreground_count == 0`。

## Finding 3（P2）：遥测持续时间接受浮点值，违反已写明的整数契约

位置：`src/memory/ollama_extractor.py:147-177`

任务要求与 Ollama 响应契约中的 duration 为非负整数纳秒。当前 `_ms()` 接受任意有限非负 `float`；例如 `load_duration=1.5` 会进入报告为 `0.0000015 ms`，而不是 unavailable。

修复要求：duration 与 token count 一样，只接受 `int` 且拒绝 `bool`、float、负数；畸形值映射为 `None`，不得影响内容解析。补 `1.5`、NaN、Inf、负数、bool 和合法 0/整数测试。

## Finding 4（P2）：ADR 决策段落重复，已经无法作为单一决策依据

位置：`docs/architecture/adr-memory-product-integration.md`

`## 决策` 内第 1～9 项被重复插入多次，`替代方案` 标题和部分条目也重复。当前文档无法清楚区分最终决策与历史拼接内容。

修复要求：将 ADR 重写为一份且仅一份有序 1～9 决策，保留本轮最终语义与首次真实 exit 2 的事实；去除重复段落，不删除仍有效的回退、未验证和安全边界。增加轻量文档检查或在交接中说明人工复核结果，不必为 Markdown 引入新依赖。

## Codex 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 281 tests in 6.655s … OK

compileall -q src tests diagnose_memory.py
→ OK

git diff --check
→ OK（仅既有 LF/CRLF 提示）
```

额外定向复现：

```text
流式 recall 抛 KeyboardInterrupt 后：foreground_after_recall_KI=1
非流式成功提交时：foreground_during_submit=1；方法返回后=0
遥测 load_duration=1.5：load_duration_ms=0.0000015（应为 None）
```

所有验证均为纯离线；未启动 Ollama、主程序或设备，未运行真实 `--execute`。

## 修复后验证

完成上述四项后运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

更新 `docs/cursor-handoff-status.md` 顶部，列出实际测试数与四项修复。禁止扩大范围、提交、推送或真实执行。
