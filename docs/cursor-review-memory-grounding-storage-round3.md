# Codex 最终验收 Round 3：Memory Grounding + Storage Hardening Round 1

## 结论

Round 2 的主体修复有效：真实 `os.scandir` 已替代 `Path.iterdir`，maintenance 与 active foreground/extract 已互斥，Timer generation 与异常后顺序 close 已建立。独立运行 325 项离线测试、`compileall`、`git diff --check` 均通过。

最终定向验收仍发现 3 个未覆盖的边界，暂不放行。Cursor 只修复本单，禁止进入存储任务 Round 2/3；完成后停止等待 Codex 最终复验。

## Finding 1（P1）：整数续扫游标把前缀跳过移出预算，单轮实际工作仍会无界增长

位置：`src/core/file_cleaner.py:240-334`

当前下轮会从头打开 `scandir`，然后用：

```python
if position < resume_skip:
    position += 1
    continue
```

跳过旧前缀。跳过过程不增加 `scanned`，因此不受 `max_scan_files` 约束。把游标设为 1000、扫描预算设为 1 时，一轮实际调用 `next()` 1001 次：

```text
resume_cursor_next_calls_for_budget_1 1001
```

目录越大、游标越后，单轮 CPU/IO 越高，重新出现本任务要消除的卡顿。整数 offset 还会在目录增删/重排时跳过错误条目；以 `rule.name` 作为状态键也会让同名规则互相覆盖。

修复要求：

1. 每次底层 `next(scandir_iterator)` 都必须计入硬扫描预算，不能存在“不计预算的跳过”。
2. 同时保证后续条目最终可达。建议使用 FileCleaner 自己拥有的、按规则身份隔离的活动 sweep：跨短批次保存同一个真实 `scandir` iterator；每个回调只消费至多预算数量，然后用短延迟继续下一批，而不是等完整清理间隔或从头跳前缀。
3. 活动 iterator 必须在 exhausted、stop、规则禁用/移除、目录消失、异常与对象关闭时 exactly-once `close()`；不得永久持有句柄。若采用其他方案，也必须同时证明单轮底层枚举有硬上限、跨轮无饥饿、目录变化失败开放。
4. 状态按稳定规则身份或目录规范路径隔离，不能只用可能重复的显示名称。
5. 增加大游标测试：预算 1 时任意一轮底层 `next()` 不得超过 1；连续批次最终到达第 N 个目标；中途 stop/异常/目录变化关闭句柄；两个同名规则互不污染。

## Finding 2（P1）：foreground pending 没有阻止新的 extract 抢先

位置：`src/memory/admission.py:79-104`

`try_acquire_maintenance()` 已检查 `_foreground_pending`，但 `try_acquire_extract()` 只检查 active foreground 和 maintenance。前台已经登记 pending、等待维护释放时，新 extraction 可以在前台线程重新取得 condition 之前抢先开始。

确定性状态复现：

```text
extract_while_foreground_pending True
```

这与“释放维护后优先前台”及交接所称 `foreground(+pending)` 互斥不一致。

修复要求：

1. `try_acquire_extract()` 在 `_foreground_pending > 0` 时也必须失败，确保等待中的用户请求优先。
2. 维持现有语义：已经开始的 extraction 不被硬抢占；新的 foreground 可以进入并与无法抢占的既有 HTTP 共存。
3. 使用 Event/Barrier 做真实等待线程测试：维护持有 → foreground pending → 释放维护的同时尝试 extract；extract 必须失败，foreground 先进入。不要直接写私有计数作为唯一测试。
4. 覆盖 pending 因 KI/SystemExit/异常退出后计数归零，之后 extraction 可再次获得。

## Finding 3（P1）：并发 close 未被串行化，同一 ingestion 会被关闭两次

位置：`src/memory/runtime.py:548-624`

两个线程可同时在顶部看到 `_closed=False`。虽然各自短暂持有 `_lifecycle_lock` 设置 `_closing=True`，但没有在发现已有 close 进行中时等待或退出；两者随后都会在锁外调用 `ingestion.close()`。

用 barrier 让两个调用同时进入 ingestion，结果为：

```text
concurrent_close ingestion_calls=2 store_calls=1 results=2 errors=[]
```

真实 ingestion 或底层资源未承诺并发 close 安全，这会制造竞态、重复 stop 和状态覆盖。

修复要求：

1. 为整个 close 事务增加独立串行化机制；第二个 close 调用不得与第一个并行触碰 ingestion/store。
2. 首个 close 成功时，等待者返回同一个真实最终状态；首个 close 失败时，只允许一个等待者接手重试未完成步骤，并继续保留现有 exactly-once 所有权。
3. 不要在持有会被 Timer 回调需要的生命周期锁时执行可能阻塞的 ingestion/store close。
4. 用 Event/Barrier 测试两个及多个并发 close：ingestion 最大并发必须为 1；成功只调用一次；首次普通异常、KI/SystemExit、worker_alive、store 首次失败时仍可由后续调用安全重试；store 最终只成功关闭一次。

## 已复验通过的内容

- `os.scandir` 本身为流式枚举，达到当前批次预算会关闭本次句柄。
- maintenance 与 active foreground/extract 的准入条件已补齐，虚假唤醒不再直接穿透 maintenance。
- 重复 `start()` 不再创建多个 Timer；旧 generation 回调在 close 后不会重排。
- 顺序 close 的 ingestion/store 失败能够保留状态并重试，不再伪造成功。
- Round 1 的 retention、history、grounding 和 TempFile 修复仍通过全量回归。

## Codex 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 325 tests in 8.842s ... OK

compileall -q src tests diagnose_memory.py
→ OK

git diff --check
→ OK（仅既有 LF/CRLF 提示）
```

额外确定性复现：

```text
resume_cursor_next_calls_for_budget_1 1001
extract_while_foreground_pending True
concurrent_close ingestion_calls=2 store_calls=1 results=2 errors=[]
```

全部为纯离线 fake/线程测试；未启动 Ollama、主程序、真实设备或 `diagnose_memory.py --execute`，未删除用户文件。

## 修复后验证

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

更新 `docs/cursor-handoff-status.md` 顶部，列出三项修复、实际测试数和未验证限制，随后停止等待 Codex 最终复验。
