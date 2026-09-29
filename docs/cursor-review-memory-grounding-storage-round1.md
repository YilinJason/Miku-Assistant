# Codex 统一评审：Memory Grounding + Storage Hardening Round 1

## 结论

现有 301 项离线测试、`compileall` 与 `git diff --check` 均通过，但本轮暂不通过验收。独立审查发现 5 个可复现行为缺陷和 1 个并发契约缺口；其中目录扫描与后台线程问题会直接破坏“长期运行不卡顿、资源有界”的目标。

Cursor 仅修复本单，完成后停止并等待 Codex Round 2。不得扩到存储任务 Round 2/3，不得运行真实 Ollama、主程序、麦克风、扬声器、TTS/RVC、浏览器或系统操作；不得提交或推送。

## Finding 1（P1）：受保护记录可永久挡住可淘汰记录

位置：`src/memory/sqlite_store.py:209-326`

年龄淘汰先按 `LIMIT batch_limit` 取最旧记录，再在 Python 中跳过 `USER_CONFIRMED`。数量淘汰也只额外多取一个固定 `batch_limit`。当受保护记录占据查询前缀时，后面的 `MODEL_INFERRED` 永远不会进入候选；重复维护也不会前进。

独立最小复现：先写入 5 条较旧 `USER_CONFIRMED`，再写入 1 条 `MODEL_INFERRED`，以 `max_records=2`、`batch_limit=1` 连续 prune 两次，结果为：

```text
retention 0 0 6
```

两轮均删除 0 条，库仍有 6 条，容量上限无法收敛。当前排序还没有使用 `importance`，也没有满足任务中“相同时间/重要度的稳定顺序”；年龄阶段与数量阶段又可各删除一批，使单次总删除量可能达到 `2 * batch_limit`。

修复要求：

1. `preserve_user_confirmed=True` 时在 SQL 候选条件中排除 `USER_CONFIRMED`，不得先截断再过滤。
2. 明确并实现淘汰顺序：优先低重要度、再旧时间、最后 `id` 稳定打破平局；年龄条件仍必须成立。
3. `batch_limit` 是整次维护的全局删除/工作预算，年龄与数量阶段共享剩余额度；不得隐式翻倍。
4. 若总记录数超过上限仅因为受保护记录本身超过上限，返回结果必须能表达“仍超限但无可删记录”，且重复执行不得忙循环。
5. 增加：受保护前缀、全受保护、同时间不同重要度、同时间同重要度稳定 `id`、年龄+数量共享预算的行为测试。

## Finding 2（P1）：FileCleaner 在限额前已无界枚举整个目录

位置：`src/core/file_cleaner.py:191-229`

`entries = list(rule.directory.iterdir())` 会先枚举并保存整个目录，之后才检查 `scan_budget`。此外 `scanned` 只对“是文件且匹配 pattern”的条目计数，大量子目录或不匹配文件完全绕过扫描预算。

独立 fake 目录在 `max_scan_files=1` 时于第二次枚举抛错，清理器仍触发该错误，证明它在预算 1 之后继续取目录项：

```text
cleaner RuntimeError: enumerated past scan budget
```

修复要求：

1. 懒迭代 `iterdir()`，每取一个目录项立刻计入扫描预算，再判断类型和 pattern；禁止 `list(...)`、排序整个目录或其他全量物化。
2. 达到扫描或删除预算后立即停止迭代。
3. 增加可计数/越界即抛错的 fake iterator 行为测试，覆盖大量不匹配文件与子目录；不得只用源码字符串断言。
4. `from_config` 对布尔值和有限数值做严格校验，避免字符串 `"false"` 被 `bool(...)` 解释为 `True`，负数也不得静默夹成 1 分钟。

## Finding 3（P1）：延迟删除 Worker 无法在 shutdown 时及时退出，且 TTS 不回收其所有权

位置：

- `src/voice/audio/temp_files.py:149-198`
- `src/voice/tts/engine.py:449-456`

Worker 在首次延迟与重试退避中使用不可中断的 `time.sleep()`。队列项延迟 30 秒时调用 `shutdown(timeout=0.05)`，线程仍存活：

```text
delayed_worker_alive_after_shutdown True
```

同时 `TTSEngine.shutdown()` 没有调用其持有的 `TempFileManager.shutdown()`，正常退出也会遗留 Worker。同步 `remove()` 最坏还会在调用线程累计等待约 14 秒，可能把播放/请求路径卡住。

修复要求：

1. 所有 Worker 等待改成 `stop_event.wait(timeout)`，收到停止信号立即结束；重试退避同样可中断。
2. `TTSEngine.shutdown()` 必须 exactly-once、幂等地回收其拥有的 TempFileManager；外部注入时需明确所有权契约。
3. 对已锁定文件避免在前台路径长时间同步睡眠；首次或很少量快速尝试后放入有界队列，最终可留给定时清理。
4. 测试长 delay、重试退避中 shutdown、重复 shutdown、队列满、TTS shutdown 后不存在 `TempFileDelayedDelete` 活线程。

## Finding 4（P1）：ConversationHistory 上限没有接入产品配置

位置：

- `src/llm/conversation_history.py`
- `src/llm/ollama_client.py:56`
- `src/core/assembly.py:132-139`
- `config/config.yaml`

类构造器虽有 `max_stored_turns=500`，但 `OllamaClient` 仍无参数地创建 `ConversationHistory()`；`Config`、YAML 和装配层没有对应键。因此“可配置的存储上限”仅存在于单元类，用户无法配置运行中的主程序。

修复要求：

1. 增加严格配置项（建议 `ollama.history.max_stored_turns`），在任何设备/服务副作用之前校验：拒绝 bool、字符串、float、非正数和超硬上限值。
2. 从装配层传给 `OllamaClient`，再传给默认 `ConversationHistory`；保留显式注入 `history` 的兼容入口，注入时不得覆盖调用方对象。
3. 增加产品装配行为测试，证明配置值真正到达默认历史；保留 10,000 轮、完整轮裁剪、顺序与实例隔离测试。

## Finding 5（P1）：第三人称提取只依赖提示词，模型违约时仍写入第一人称

位置：`src/memory/ollama_extractor.py:22-34,396-459`

提示词要求第三人称，但解析器只做类型/范围校验。fake 返回 `{"text":"我最喜欢喝茶", ...}` 时，该项被正常接受：

```text
first_person_accepted 我最喜欢喝茶
```

这不满足 Grounding 任务“新输出为第三人称、禁止不带 owner 的第一人称输出”的离线契约。模型提示不能代替本地边界校验。

修复要求：

1. 在本地解析/过滤边界拒绝无明确 owner 的第一人称候选，失败开放为跳过该候选或整批明确失败；不要凭空改写含义。
2. 旧库第一人称记录继续按 `subject=user` 注入，不做迁移、不改写现有数据库。
3. 测试 fake 模型无视提示返回“我喜欢/我的代号”等内容时不会写入；合法“用户……”仍通过，未知字段/注入防护保持。

## Finding 6（P1）：维护的“无前台 lease”检查不是原子契约

位置：`src/memory/runtime.py:367-427`、`src/memory/admission.py`

`maybe_run_maintenance()` 读取 `foreground_count` 后再调用 prune。新的前台 lease 可在检查与 prune 之间开始，维护会与用户请求并行。自定义 gate 若没有 `foreground_count` 属性则完全跳过检查。现有测试只覆盖“检查时已经繁忙”，没有覆盖竞态。

修复要求：

1. 把维护准入纳入显式 gate 协议，使用原子的 maintenance lease；只有 ingestion idle 且成功获得维护 lease 才可 prune。
2. 维护 lease 存续期间不得与 foreground lease 重叠，并在正常、异常、KI/SystemExit 所有出口 exactly-once 释放。
3. 前台优先：前台已活跃时维护立即跳过；不得为后台维护长时间阻塞用户请求。
4. 增加 barrier/event 控制的确定性竞态测试，覆盖检查后前台开始、prune 异常、重复维护与自定义 gate；禁止依赖 `sleep` 猜时序。
5. 关闭路径同步取消并收拢维护定时器，避免回调在 close 期间重新调度一个长寿命 Timer。

## 配置与产品语义

当前 `config/config.yaml` 与 `MemorySettings` 的 `retention.enabled` 默认均为 `false`。这意味着即使用户以后开启 `memory.enabled`，定期数据库维护仍不会自动生效。修复时需要把最终策略写清：在主配置中给出用户可见、保守且实际启用的 retention 设置，或提供显式启用步骤并在启动日志中说明维护未启用。不能把“已实现但默认永不运行”表述为已经保障定期清理。默认仍须保护 `USER_CONFIRMED`。

## Codex 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 301 tests in 8.416s ... OK

compileall -q src tests diagnose_memory.py
→ OK

git diff --check
→ OK（仅既有 LF/CRLF 提示）
```

定向行为复现：

```text
retention 0 0 6
cleaner RuntimeError: enumerated past scan budget
delayed_worker_alive_after_shutdown True
first_person_accepted 我最喜欢喝茶
```

全部为纯离线 fake/临时目录验证；未启动 Ollama、主程序或任何真实设备，未运行 `diagnose_memory.py --execute`，未清理用户文件。

## 修复后验证

修复以上六项后运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

更新 `docs/cursor-handoff-status.md` 顶部，逐项列出修复、实际测试数量与未验证限制，停止修改等待 Codex Round 2。
