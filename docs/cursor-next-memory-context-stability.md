# Cursor 下一轮：真实 v2 验收关闭完整性与同模型 context 稳定性

## 结论与目标

用户已运行真实命令：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 diagnose_memory.py --execute --report tmp\memory-text-report-v2.json
```

真实报告 `tmp/memory-text-report-v2.json` 不能通过：四个 extractor 场景与 wrapper 内容链路均通过，但关闭结果为 `drained=false / worker_alive=true / pending=1 / incomplete=true`，并在临时目录清理时出现 `PermissionError`。

Ollama 日志还证明同一个 `Miku_Assistant-emotion:latest` runner 在 wrapper 前台默认 context（实际 `8192`）与记忆提取器 `num_ctx=2048` 之间切换。每次前台/后台交替都可能重新启动 runner。这会制造额外的模型加载、显存重建与机器卡顿，违背本轮资源治理目标。

本轮只修复这两个已实证的问题：

1. wrapper 最后一轮成功后，必须等待该轮已接受的记忆任务完成，再进入关闭。
2. 当记忆提取器与前台使用同一个模型时，默认不得用不同的 `num_ctx` 强迫 Ollama 来回重建 runner。

完成离线实现后停止，等待 Codex Review。Cursor 不运行真实 `--execute`。

## 开始前

读取并遵循：

- `AGENTS.md`
- `skills/architecture-contracts/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `skills/safe-refactoring/SKILL.md`
- `skills/collaboration-review/SKILL.md`
- `docs/codex-review-memory-performance-quality-final.md`
- `docs/cursor-next-memory-performance-quality.md`
- `docs/architecture/adr-memory-product-integration.md`
- `docs/memory-real-text-acceptance.md`
- `src/memory/diagnostic.py`
- `src/memory/runtime.py`
- `src/memory/ingestion.py`
- `src/memory/ollama_extractor.py`
- `src/llm/ollama_client.py`

先检查 `git status`，保留全部未提交修改。不提交、不推送，不启动或停止 Ollama、主程序、模型或任何设备。

## 真实证据

### 1. 最后一轮后台任务未等待

`_run_wrapper_roundtrip()` 只在 seed 后调用一次 `runtime.wait_until_idle()`。查询轮 `client.generate_result(WRAPPER_QUERY_USER, ...)` 成功后会提交新的记忆事件，但函数立即返回。随后 `run_execute()` 在 `finally` 中只给 `runtime.close(drain=True)` 5 秒；真实提取尚未完成，因此报告出现：

```text
close: drained=False timed_out=True worker_alive=True pending=1 incomplete=True
diagnostic tempdir cleanup failed err=PermissionError
```

不要仅把 `close_timeout` 调大来隐藏这个时序缺陷。

### 2. 同一模型 context 抖动

真实日志显示：

- extractor 请求以 `num_ctx=2048` 启动 runner；首次冷加载约 `28.6s`，整轮约 `44.9s`；
- wrapper 前台 `OllamaClient` 没有传 `num_ctx`，同一模型按实际默认 `8192` 重新启动 runner；
- wrapper 产生的后台提取再次请求 `2048`，runner 又被重建；
- 下一轮前台与最终后台提取继续在 `8192/2048` 间切换。

退出后复核没有 `llama-server` 或 Python 残留，GPU 已回到约 `1.7 GiB` 系统基线。因此这是运行期模型重载与资源抖动，不是已证实的退出后内存泄漏。

## 1. 补齐 wrapper 最终 idle 屏障

- seed 成功后保留现有 idle 等待。
- query 成功或 warning 路径提交记忆事件后，再调用一次有界 `runtime.wait_until_idle(idle_timeout)`。
- 只有最终 idle 成功后，wrapper 才可报告 pass/warning 并进入 close。
- 最终 idle 超时必须形成明确失败，例如 `protocol_error=final_idle_timeout`；不得把它记为内容质量 warning。
- 保留现有 `idle_wait_ok` 作为聚合兼容字段，并新增或等价表达 seed/final 两阶段结果，避免报告把“seed 等完”误写成“全部等完”。如果改变公开 JSON 字段类型或含义，递增 report schema/tool version 并同步文档与测试。
- 使用 Event/Condition fake 精确覆盖 query 返回后后台任务仍在执行的边界；禁止用脆弱的 sleep 猜测。
- 正常 final idle 完成后，`close` 必须是 `drained=true / worker_alive=false / pending=0 / incomplete=false`。

## 2. 不在存活 worker 上清理临时 SQLite

- `run_execute()` 只有在 runtime 已确认 worker 停止且 store 已关闭后才清理临时目录。
- 若有界关闭仍 incomplete，不得尝试删除仍被 worker/SQLite 占用的目录并打印二次 `PermissionError`。
- 失败报告必须保留 `close:incomplete`，并以脱敏布尔/枚举说明临时目录清理被延后或未完成；不得把本机绝对临时路径、记忆正文或 prompt 写进报告。
- 不允许吞掉关闭失败后把总结果改成通过，也不允许强杀线程或关闭 worker 正在使用的 store。
- 为 incomplete close fake 增加确定性测试：不清理活动目录、不抛二次清理异常、退出结果仍非零。

## 3. 将 extractor `num_ctx` 默认改为“继承”，避免同模型 runner 重载

实现一个明确、可配置、向后可解释的契约：

- `memory.extractor.num_ctx: null` 表示不向 `/api/chat` 的 `options` 写入 `num_ctx`，让模型/服务使用自身 context。
- 仓库默认配置改为 `null`。这使默认的同模型 fallback 与前台请求保持 context 行为一致，避免本轮日志中 `8192 ↔ 2048` 的 runner 重建。
- 显式正整数仍受现有有限整数与硬上限校验，并继续写入请求。用户明确配置 `2048` 或 CLI 传 `--num-ctx 2048` 时保留其意图。
- `bool`、字符串、0、负数、NaN/Inf、越界值仍须在任何 HTTP/store/设备创建前拒绝；只有真正的 null/None 代表继承。
- `OllamaMemoryExtractor` 每次请求仍创建新的 options；`temperature=0` 和 `num_predict` 不丢失；继承模式下只是不出现 `num_ctx` 键。
- `MemorySettings`、runtime、diagnostic endpoint、CLI、报告、console 与 ADR 使用同一语义。报告应把该值写为 `null` 或清晰的 `inherit` 展示值，不能再声称默认是 2048。
- 若公开 report endpoint 从整数变为可空，递增 schema；保留读取旧 v2 报告所需的文档说明即可，不必做历史数据迁移。
- 不修改主聊天 context，不自动切换/下载模型，不引入第二模型。

`num_predict=512` 可保留，因为它限制生成长度，不是本次 runner context 重建的证据来源。

## 4. 离线验收要求

至少新增/更新这些测试：

1. extractor 默认继承时 payload 没有 `num_ctx`；显式整数时存在且值正确；非法值仍早失败。
2. 配置 null、缺省、显式整数和 CLI 覆盖从 config 一直贯穿到 endpoint/runtime/extractor。
3. dry-run、console 与 JSON report 对继承语义一致；schema/tool version 与公开字段一致。
4. wrapper seed 与 query 两次 idle 屏障；query 后的后台工作未完成时不得报告 pass。
5. final idle 成功后 close 完整，pending 为 0。
6. final idle/close 超时仍失败，并且不在存活 worker 上清理临时 SQLite。
7. 现有 HTTP/JSON/schema/取消/关闭、长期记忆与全仓离线测试继续通过。
8. `memory.enabled=false` 仍不创建 runtime/store/transport。

运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

更新：

- `docs/architecture/adr-memory-product-integration.md`
- `docs/memory-real-text-acceptance.md`
- `docs/engineering-baseline.md`
- `docs/cursor-handoff-status.md`

交接必须写明：真实 v2 失败是本轮依据；Cursor 没有运行真实服务；实际离线测试数量；默认 `num_ctx` 已改为 inherit；仍需用户在 Codex 最终评审后复跑真实验收。

## 禁止扩大范围

- 不运行 `diagnose_memory.py --execute`，不启动/停止 Ollama，不更改 Ollama 全局环境、注册表或模型文件。
- 不默认启用长期记忆，不写正式数据库，不读取真实对话。
- 不运行麦克风、扬声器、TTS/RVC、浏览器、角色或系统操作。
- 不把“当前没有退出后残留”扩写为“已证明绝无内存泄漏”；长时间 soak 仍未执行。
- 不提交、不推送，不撤销现有未提交修改。
