# Codex Review：长期记忆应用服务 Round 1

## 结论

暂不验收。现有 225 项离线测试通过，但 ingestion 的停止协议在三个可确定复现的边界上会丢失已经由 `submit()` 返回 `True` 的任务；其中两种还会把未处理队列报告为 `drained=True`。本轮只修复这些契约和对应文档，不接入真实 Ollama、对话装配或提示词。

## 必须修复

### 1. P1：`drain=True` 在队列已满时会静默删除已接受任务

位置：`src/memory/ingestion.py:173`、`src/memory/ingestion.py:237`

触发条件：

1. worker 正在处理第一个任务并被可控 fake extractor 阻塞；
2. 有一个已接受任务填满剩余队列槽位；
3. 调用 `stop(drain=True, ...)`。

`_enqueue_sentinel()` 为给哨兵腾位置调用 `get_nowait()`，直接移除队首业务任务，却不增加 `discarded`。Codex 的确定性复现结果为：两个 `submit()` 都返回 `True`，最终只提取并保存第一个任务，两个 stop 状态均报告 `discarded=0`。

修复要求：

- `drain=True` 不得为了控制消息删除任何业务任务；
- 已接受任务必须在超时前被处理，或在超时时准确报告尚未排空；
- 控制信号不得与业务队列容量争抢后再偷删业务项。可使用独立 stop event、保留控制通道或其他可证明有界的协议；不要用无界阻塞掩盖问题；
- `drain=False` 才允许丢弃待处理任务，并必须准确返回丢弃数量。

新增确定性测试：容量 1、Event 阻塞首个 extractor、第二个任务填满队列，调用 `stop(drain=True)` 后释放 Event；断言两个已接受 turn 都被处理，且没有依赖 `sleep`。

### 2. P1：`submit()` 与 `stop()` 竞态可在哨兵之后接受任务

位置：`src/memory/ingestion.py:112`、`src/memory/ingestion.py:158`

`submit()` 对 `_closed` / `_closing` / `_started` 的检查和 `put_nowait()` 不在同一临界区。提交线程可先通过状态检查，停止线程随后设置 `_closing` 并放入哨兵，提交线程最后把业务事件放到哨兵之后并返回 `True`。worker 已因哨兵退出，任务永久留在队列。

Codex 通过 gated `put_nowait` 确定性复现：`submit_result=[True]`，`stop(...).drained=True`，extractor/store 均未收到该任务，队列仍残留业务项与后续哨兵。

修复要求：

- 将“服务可接受检查 + 业务入队”与停止边界做成线性化协议；
- 一旦 `submit()` 返回 `True`，任务必须位于停止边界之前，并由 drain 语义处理；停止边界之后只能返回 `False`；
- 不要只增加第二次无锁检查，因为仍可能产生不可证明的竞态；
- 并发计数和状态也应在相同锁策略下保持一致。

新增确定性测试：使用可控队列/fake hook 或 Barrier 将提交暂停在状态检查之后，交错执行 stop，再释放提交；断言不可能出现“返回 True 但任务位于退出信号之后”。测试线程必须在 `finally` 释放并 join。

### 3. P1：worker 因 `KeyboardInterrupt` / `SystemExit` 退出后，`stop(drain=True)` 会谎报已排空

位置：`src/memory/ingestion.py:255`、`src/memory/ingestion.py:274`、`src/memory/ingestion.py:176`

当前 worker 遇到 `KeyboardInterrupt` / `SystemExit` 后直接返回。如果其后已有已接受任务，`stop(drain=True)` 只是向无人消费的队列追加哨兵，发现旧线程已经死亡后返回 `worker_alive=False, drained=True`。Codex 的确定性复现中，第二个已接受任务未调用 extractor，队列仍有 2 项，但状态为 `drained=True`。

修复要求：

- 保留“不把 `KeyboardInterrupt` / `SystemExit` 当普通提取失败”的语义；
- worker 终止状态必须可观察，`drained` 必须由实际队列/在途状态得出，不能只由 `not timed_out` 推导；
- 明确选择并测试恢复策略：可以由显式重启/停止路径建立替代 worker 完成 drain，或准确返回未排空状态及待处理数量；不得静默丢失后仍声称成功；
- 修复 stop 前未 start、异常退出后再次 start/stop、重复 stop 的残留哨兵问题。

新增确定性测试：首个 extractor 在 Event 释放后抛 `KeyboardInterrupt` 和 `SystemExit`（subTest），其后预先排队一个成功任务；验证 worker 可回收，状态不谎报，并按所选契约处理或明确报告后续任务。

### 4. P2：新增数值契约未完整拒绝非有限值

位置：`src/memory/service_models.py:56`、`src/memory/ingestion.py:147`

- `ScoredMemory.score` 只检查 `score <= 0`，因此 `NaN` 和正无穷可进入不可变结果并破坏确定排序；
- `stop(timeout=...)` 只检查 `< 0`，未拒绝 `NaN` / `Inf`，随后可能在 `Thread.join()` 抛出底层异常。

任务要求新增领域数值沿用 finite 约束。请在应用边界显式使用 `math.isfinite`，并补 `NaN`、正负 `Inf`、bool 的单元测试。内部词法分数仍可保持正数、不要求归一化到 0..1。

### 5. P2：工程基线仍记录旧的 211 项结果

位置：`docs/engineering-baseline.md:310`、`docs/engineering-baseline.md:401`

本轮实际全量结果为 225 项，但工程基线的“最近离线结果”和底部验证快照仍写 211 项，仅交接文件写了 225。修复后必须重跑全量测试并用新的真实数量和耗时更新所有当前基线位置；历史章节中的旧结果继续保留。

## 独立验证证据

Codex 实际运行：

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 225 tests in 5.490s … OK

compileall -q src tests → OK
git diff --check → OK
```

另外运行了三个只使用 fake / Event 的临时复现脚本，分别确认上述 1～3 项；未启动真实 Ollama、麦克风、扬声器、TTS、RVC、浏览器或系统操作。

## 回归验证

修复后运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
git diff --check
```

完成后更新 `docs/cursor-handoff-status.md` 顶部为 Round 1 修复结果，停止修改，等待 Codex Round 2。不得提交、推送或扩大到真实提取器、对话注入、产品装配、设备或模型测试。
