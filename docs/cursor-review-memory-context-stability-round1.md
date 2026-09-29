# Cursor Review：关闭完整性与 context 稳定性 Round 1

## 评审结论

本轮核心方向正确，且 Codex 独立复跑 287 项离线测试、compileall 与 diff-check 均通过；但发现 1 个阻断项和 1 个契约清理项。修复前不能宣称最终验收通过。

本轮未运行真实 `diagnose_memory.py --execute`，未启动/停止 Ollama 或设备，未提交、未推送。

## Finding 1（阻断）：`deferred_incomplete_close` 没有清理执行者，测试与异常执行持续泄漏临时 SQLite 目录

### 证据

`src/memory/diagnostic.py` 在 incomplete close 时仅执行：

```python
report.temp_dir_cleanup = "deferred_incomplete_close"
```

它没有启动延后清理、没有在 worker 结束后重试 `runtime.close()`，也没有其它生命周期持有者。`tmp_path` 随函数返回丢失，因此该状态实际含义是“永久保留到外部人工清理”，不是“已安排延后清理”。

`tests/test_memory_diagnostic.py::InterruptCloseTests::test_incomplete_close_recorded` 每次运行都让 fake 永久报告 `worker_alive=True`，随后不回收本测试创建的目录。Codex 全量测试后检查 `%TEMP%`，已发现 **23 个** `miku-memory-diag-*` 目录，每个包含约 20 KiB 的 `diagnostic.sqlite`；最新目录时间与本轮测试吻合。继续跑测试会无界增加目录数。

这不会解释用户此前的 GPU 卡顿，但它本身是可复现的磁盘资源泄漏，也使交接中“延后清理”的表述失真。

### 修复要求

1. 仍然禁止在 worker 存活时关闭 store 或删除 SQLite 目录，禁止强杀线程。
2. 正常的真实 worker 在第一次 close 超时后若随后结束，必须存在实际的安全回收路径：等待 idle/worker 结束、重试 runtime close、确认 store 关闭后再删除本轮唯一的临时目录。实现必须有界，且不能让后台清理异常覆盖原始 `close:incomplete` 结果。
3. 若在明确的总超时后仍不能回收，报告状态必须如实写成 `retained_incomplete_close`（或同义值），不能称为 deferred；文档说明这是失败证据与剩余风险。
4. 单元测试不得污染 `%TEMP%`：测试应注入/捕获自己创建的目录并在 worker/fake 释放后清理；不得依赖全局扫描删除。
5. 新增 Event/Condition 驱动的确定性测试：
   - 第一次 close 时真实 worker 仍活跃，不得提前删目录；
   - 释放 worker 后重试关闭并最终删除该次测试创建的目录；
   - 永久 incomplete fake 的测试结束后也不得留下目录；
   - 原始报告仍为 contract failure，不能因后续清理成功被改成 pass。
6. 不得扫描或删除现有 23 个目录；它们位于工作区外，需由用户另行授权清理。本轮只防止新增泄漏。

允许选择小型的同步清理宽限期或明确管理的 cleanup reaper，但不要引入通用任务调度器。若使用线程，必须明确 daemon/进程退出语义并提供可等待的测试入口，避免新的后台线程泄漏。

## Finding 2（契约清理）：`DEFAULT_NUM_CTX = 2048` 已不再是默认值

`src/memory/ollama_extractor.py` 仍公开定义 `DEFAULT_NUM_CTX = 2048`，但构造器、runtime、配置与 diagnostic 的真实默认都已改为 `None/inherit`，仓库内也没有任何调用方使用该常量。名称会误导后续代码重新把 2048 当默认值，重新引入 runner context 抖动。

删除该未使用常量，或改成明确表示 legacy/显式示例值的非默认名称；同步注释与文档。不得改变当前 `None = inherit` 契约。

## 保留本轮已正确实现的行为

- wrapper seed 与 query 后各有一次有界 idle 屏障。
- final idle 超时为 `final_idle_timeout` 协议失败。
- schema v3 / tool 1.2.0 的 seed/final/aggregate idle 字段。
- 默认 `num_ctx=None`，payload 省略 `options.num_ctx`；显式整数仍严格校验并写入。
- `num_predict=512`、`temperature=0`、短 diagnostic keep_alive 与默认 `memory.enabled=false` 不变。

## 修复后验证

运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

还要在测试前后记录 `miku-memory-diag-*` 目录数量，证明全量测试没有新增临时目录。只做计数与列名，不删除既有目录。

更新 `docs/engineering-baseline.md` 与 `docs/cursor-handoff-status.md`，写明实际测试数和未运行真实服务。完成后停止并等待 Codex Round 2。

## Codex 实际验证

```text
Python 3.12 unittest discover → Ran 287 tests in 7.376s, OK
compileall -q src tests diagnose_memory.py → OK
git diff --check → OK（仅既有 LF/CRLF warning）
%TEMP%/miku-memory-diag-* → 23 个，最新目录由本轮全量测试产生
```

