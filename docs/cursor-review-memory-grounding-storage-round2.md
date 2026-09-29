# Codex 统一评审 Round 2：Memory Grounding + Storage Hardening Round 1

## 结论

上轮六项中的 SQL 受保护前缀、全局 prune 预算、history 产品接线、第三人称本地过滤、临时删除可中断等待与 TTS 所有权已经按原触发条件修复。独立运行 312 项离线测试、`compileall` 和 `git diff --check` 均通过。

本轮仍发现 4 个未被现有测试覆盖的资源/并发缺陷，暂不通过最终验收。Cursor 只修复本单，完成后停止等待 Codex 最终验收；不得进入存储任务 Round 2/3。

## Finding 1（P1）：FileCleaner 仍会在 Python 3.12 中物化整个目录，并且重复周期会饿死后续条目

位置：`src/core/file_cleaner.py:220-284`

当前改成 `iterator = iter(rule.directory.iterdir())`，但本项目使用的 Python 3.12 中，`pathlib.Path.iterdir()` 内部实现是：

```python
for name in os.listdir(self):
    yield self._make_child_relpath(name)
```

因此首次 `next(iterator)` 仍先由 `os.listdir()` 读取完整目录。外层逐项计数并不能限制目录枚举造成的内存峰值，现有自定义 fake iterator 测试绕开了 `Path.iterdir()` 的真实实现。

此外每轮都从同一目录开头重新扫描。稳定顺序为 `[noise-1, noise-2, miku_old.wav]`、`max_scan_files=2` 时，连续执行两轮后目标文件仍存在：

```text
cleaner_starvation_target_exists True
```

修复要求：

1. 使用真正流式的目录枚举（例如受控 `os.scandir()` 上下文），每取一个 `DirEntry` 就消耗扫描预算；不得调用 `Path.iterdir()`、`os.listdir()` 或对全目录排序/转 list。
2. 停止、异常和达到预算时必须关闭枚举句柄。
3. 解决跨周期饥饿：在不无界跳过/物化的前提下保存安全的续扫状态或使用等价的有界分批机制，使预算后的条目最终能被检查；目录变化、删除和重启需失败开放。
4. 行为测试必须使用真实 Python 目录 API 或注入的 `scandir`，并让 `os.listdir` 调用直接失败；覆盖超过预算的大量噪音条目、连续多轮最终到达后续目标、句柄关闭。不得只用假的 `Path.iterdir()` 证明懒扫描。

## Finding 2（P1）：maintenance lease 仍允许前台和提取与维护重叠

位置：`src/memory/admission.py:71-105`

当前 `try_acquire_extract()` 不检查 `_maintenance`，`try_acquire_maintenance()` 不检查 `_extracting`。维护 lease 已持有时，提取仍能成功进入：

```text
extract_during_maintenance True
```

`begin_foreground()` 只等待一次 0.5 秒，超时或虚假唤醒后不再检查 `_maintenance`，于是同一时刻出现：

```text
foreground_during_maintenance foreground=1 maintenance=1 thread_alive=False
```

这与协议注释“直至 release 与前台互斥”以及 Round 1 的无前台/idle 维护条件冲突。

修复要求：

1. `try_acquire_extract()` 在 maintenance 活跃时必须失败；`try_acquire_maintenance()` 在 foreground、extracting 或 maintenance 任一非零时必须失败。
2. `begin_foreground()` 使用条件循环处理虚假唤醒，维护 lease 未释放前不得增加 foreground 计数；维护批量本身必须保持小且有界，以免正常情况下长期阻塞前台。
3. 前台等待期间设置明确的 foreground-pending 状态，使新的维护不得插队；释放维护后优先唤醒前台。
4. 用 Event/Barrier 做确定性测试：maintenance→extract、extract→maintenance、maintenance→foreground、虚假 notify、多前台等待、异常释放；禁止用 `sleep` 猜竞态。

## Finding 3（P1）：MemoryRuntime.start() 可创建多个维护 Timer，close 只能取消最后一个

位置：`src/memory/runtime.py:342-347,472-496`

`start()` 每次都调用 `_schedule_maintenance_timer()`，没有幂等保护；新 Timer 覆盖 `_maintenance_timer` 引用。连续 `start()` 两次后两个 Timer 都存活，close 只取消第二个，首个成为不可达的长寿命守护线程：

```text
duplicate_timers_before_close True True True
orphan_timer_after_close True False
```

修复要求：

1. `MemoryRuntime.start()` 线程安全且幂等，同一 runtime 任意时刻最多一个维护 Timer。
2. 调度、回调重排和 close/cancel 使用同一生命周期锁或 generation/token，旧回调不得覆盖新引用或在关闭后重排。
3. close 对拥有的 Timer 做有界 cancel + join（不得 join 当前回调线程），完成后不得留下该 runtime 的维护线程。
4. 使用可控 fake Timer 或确定性事件测试重复 start、start/close 竞态、回调/close 竞态和重复 close；验证创建数、活线程数与重排次数。

## Finding 4（P1）：ingestion.close 抛错后 runtime 被误标已关闭，资源无法重试回收

位置：`src/memory/runtime.py:498-549`

`close()` 在调用 `ingestion.close()` 前设置 `_closed=True`。若 ingestion 关闭抛出普通异常、`KeyboardInterrupt` 或 `SystemExit`，没有路径恢复该标志。第二次 close 会走顶部的“已关闭”返回，既不重试 ingestion，也不关闭仍由 runtime 拥有的 store。

确定性复现中 ingestion 首次抛错、第二次本应成功，实际结果：

```text
close_retry runtime_closed=True ingestion_calls=1 store_close_calls=0 second_drained=True
```

返回的 `drained=True` 还是凭空构造的成功状态，掩盖了资源泄漏。

修复要求：

1. 区分 closing 与 closed；只有 ingestion 已停止且自有 store 已成功关闭后才设置最终 closed。
2. 关闭开始时先阻止新维护并取消/收拢 Timer，但 ingestion/store 任一步失败都必须保留所有权和可重试状态。
3. 普通异常保持向上传播；KI/SystemExit 保持原样上抛；三类异常后第二次 close 均真实重试未完成步骤，不重复已成功步骤。
4. 不得在没有真实完成关闭时构造 `drained=True`；补 ingestion 首次失败、store 首次失败、KI/SystemExit、worker_alive 和成功重复 close 的行为测试。

## 已复验通过的内容

- retention SQL 已在候选阶段排除受保护记录，按 `importance → created_at → id` 稳定选择，并共享单次 batch 预算。
- 全受保护超限可表达 `over_capacity_unresolvable`。
- `ollama.history.max_stored_turns` 已严格校验并接入默认产品装配；注入 history 不被覆盖。
- 新提取中的无 owner 第一人称会被本地过滤；旧库第一人称仍以 `subject=user` 注入。
- TempFile Worker 等待可中断；TTS 只回收自己拥有的 manager。
- retention 保持显式 opt-in，禁用时已有启动日志说明；默认保护 `USER_CONFIRMED`。

## Codex 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 312 tests in 8.641s ... OK

compileall -q src tests diagnose_memory.py
→ OK

git diff --check
→ OK（仅既有 LF/CRLF 提示）
```

全部验证均为纯离线 fake、临时目录和线程契约测试；未启动真实 Ollama、主程序、麦克风、扬声器、TTS/RVC、浏览器或系统操作，未运行 `diagnose_memory.py --execute`，未清理用户文件。

## 修复后验证

修复以上四项后运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

更新 `docs/cursor-handoff-status.md` 顶部，写明四项实际修复、测试数与未验证限制，然后停止等待 Codex 最终验收。
