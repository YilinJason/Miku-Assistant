# Codex Review：长期记忆应用服务 Round 2

## 结论

Round 1 的三个 P1 并发缺陷已修复：满队列 `drain=True` 不再删除任务，`submit`/`stop` 边界已线性化，worker 遇到 `KeyboardInterrupt` / `SystemExit` 后可以由替代 worker 排空。finite 校验和 230 项基线也已更新。

暂不做最终验收。仍有一个 owned store 生命周期缺陷和一个错误的候选截断状态需要修复。本轮不得扩大到真实提取器、对话注入或产品装配。

## 必须修复

### 1. P1：`close()` 超时后会在 worker 仍存活时提前关闭 owned store

位置：`src/memory/ingestion.py:224`～`src/memory/ingestion.py:246`

触发条件：

1. `owns_store=True`；
2. worker 正在可控 fake extractor 内阻塞；
3. 调用 `close(drain=True, timeout=很短)`，导致 `stop()` 返回 `worker_alive=True, drained=False`；
4. `close()` 仍立即调用 store 的 `close()`；
5. 释放 extractor 后，worker 尝试写入已经关闭的 store，已接受任务写入失败。

Codex 确定性复现结果：

```text
first  = timed_out=True, worker_alive=True, drained=False, pending=1
second = worker_alive=False, drained=True, pending=0
saved=[]
add_after_close=True
close_calls=1
```

第二次 `close()` 显示队列已空，是因为第一次关闭 store 后 worker 的写入被关闭流程自身破坏，并非任务成功保存。

修复要求：

- 当 `stop()` 返回 `worker_alive=True` 时，不得关闭 worker 仍可能访问的 owned store；
- 保留关闭所有权，使后续显式 `close()` 或 worker 安全退出后的受控路径能且只能关闭 store 一次；
- 明确并记录超时后的语义：返回状态必须继续显示未完成，不能把“写入关闭存储后失败”当作成功排空；
- 普通 `drain=True` 关闭必须让已接受且 extractor 已成功返回的写入在 store 关闭前完成；
- `drain=False` 超时同样不能让仍在执行的 worker 访问已经关闭的 owned store；
- 不使用 daemon 或强杀线程，不吞 `KeyboardInterrupt` / `SystemExit`。

新增确定性测试：Event 阻塞 extractor，`owns_store=True`，第一次 `close(drain=True, timeout=0)` 必须报告 worker 存活且 store 尚未关闭；释放 Event 后通过后续 close/受控退出完成写入并只关闭 store 一次。用 fake 断言从未发生 `add_after_close`，所有线程在 `finally` 释放并 join。

### 2. P2：候选数量恰好等于上限时错误标记 `truncated_candidates=True`

位置：`src/memory/lexical_recall.py:190`

当前实现使用：

```python
truncated = len(candidates) >= candidate_limit
```

当数据库总共只有 2 条、`candidate_limit=2` 时，全部候选已经读取，却仍返回 `truncated_candidates=True`。Codex 复现结果：

```text
candidate_count=2
truncated_candidates=True
```

修复要求：

- 只有确认存储中存在未扫描候选时才设置 `truncated_candidates=True`；
- 推荐有界读取 `candidate_limit + 1`，只对前 `candidate_limit` 条打分，并以额外一条判断是否截断；
- 处理当前应用上限 500 与存储层读取上限 500 的边界，可向存储层增加一个受控探测槽位或采用等价的有界方案；不得无界扫描或新增 count 全库热点；
- `candidate_count` 继续表示实际参与打分的候选数量，不包含仅用于 has-more 探测的额外项。

新增测试：

1. 总数小于上限 → False；
2. 总数恰好等于上限 → False；
3. 总数大于上限 → True，且只扫描/报告上限数量；
4. 覆盖允许的最大 candidate limit 边界。

## Round 2 已验证通过的部分

- 满队列 + 阻塞首个 extractor：两个已接受任务均被处理；第一次短超时准确报告 `pending=2`，第二次 stop 后 `pending=0, drained=True`。
- worker 遇到 `KeyboardInterrupt`：替代 worker 处理后续已接受任务，最终 `pending=0`。
- `submit`/`stop` 新增确定性交错测试通过。
- `ScoredMemory.score` 和 `stop(timeout)` 已拒绝非有限值。
- 工程基线已更新为 230 项。

## 独立验证证据

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 230 tests in 5.923s … OK

compileall -q src tests → OK
git diff --check → OK
```

另运行了 owned store 超时关闭与候选截断两个纯 fake/临时 SQLite 确定性复现；未启动真实 Ollama、麦克风、扬声器、TTS、RVC、浏览器或系统操作。

## 修复后验证

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
git diff --check
```

完成后更新 ADR、工程基线和 `docs/cursor-handoff-status.md` 顶部，停止修改，等待 Codex 最终验收。不得提交、推送或扩大范围。
