# 存储硬化 Round 2 最终复验：仍需一次收口

## 2026-09-29 Codex 最终结论：Round 2 离线验收通过

F1-A/B/C/D 已按同一生命周期协议完成收口，上一轮四个独立反例全部转绿：shutdown-prewarm 后 Worker 不存活；双缺失时 STT 失败会回滚局部 TTS；等待者在 shutdown 后短界限返回；内部 `TypeError` 不会触发第二次 shutdown。已有 TTS、仅补 STT 失败时仍保留调用前已发布的 TTS。

独立验证：专项 39 项通过；全量 418 项通过且无 `ResourceWarning`；`compileall` 与 `git diff --check` 通过；离线 soak `ok=true`、`bounds_ok=true`、`reclaim_ok=true`、`weakrefs_alive_after_shutdown=0`。未运行真实 Ollama、RVC、麦克风、扬声器、浏览器或清理实扫，因此本结论是 **Round 2 离线工程验收通过**，不替代后续 30～60 分钟真实语音 soak 与体验验收。

本轮无剩余阻断项。未读/未删 `data/memory`，未提交、未推送，Round 3 未开始。

## 2026-09-29 二次复验：F2/F3/F4 已通过，F1 生命周期仍需最终收口

Cursor 本轮对有界 `SessionTimer`、真实 weakref soak、显式 TTS 重试和 closing 优先检查的修改已通过。专项测试 34 项与全量 413 项均通过，soak 返回 `bounds_ok=true`、`reclaim_ok=true`、`weakrefs_alive_after_shutdown=0`，全量测试不再出现未关闭 `scandir` 的 `ResourceWarning`。

Round 2 仍不能最终验收。以下四个反例都属于本文件 Finding 1 的语音加载/关闭生命周期，须作为**一个协议修复**一次完成；不要回退已经通过的 F2/F3/F4，也不要进入 Round 3。

### F1-A（P1）：shutdown 竞态会遗留刚预热的全局 RVC Worker

位置：`src/core/assistant.py:225-227,293-330`、`src/voice/tts/engine.py:476-495`

`_discard_unpublished_tts()` 无条件调用 `shutdown(stop_rvc=False)`。当这是进程中唯一的 TTS，且 shutdown 恰好发生在 `prepare_rvc()` 已启动 Worker、尚未发布 TTS 之间，没有“正式 TTS”需要保护；跳过 `stop_rvc_worker()` 反而遗留 Worker/显存。

Codex fake Worker 复现：

```text
shutdown_during_prewarm {
  'result_ok': False,
  'worker_alive': True,
  'flags': [False]
}
```

要求：single-flight 已消除同一 assistant 内的重复 TTS 后，关闭/异常路径必须按真实局部所有权回收本轮唯一 TTS；若它可能已预热 RVC，应停止该 Worker。不得用 blanket `stop_rvc=False` 把 shutdown 竞态变成显存/子进程泄漏。增加 fake Worker 状态测试，关闭完成后必须 `worker_alive=False`，局部 TTS exactly-once 关闭。

### F1-B（P1）：完整语音加载不再事务化，STT 失败后留下 TTS/RVC

位置：`src/core/assistant.py:341-465`

当 TTS/STT 都缺失时，`ensure_voice_stack()` 先通过 `ensure_tts_for_playback()` 发布 TTS，再构造 STT。STT 构造失败后返回失败，但新 TTS 仍绑定且未关闭；失败的语音切换因此可能长期保留 RVC Worker 和显存。

Codex 复现：

```text
stt_failure {
  'result_ok': False,
  'tts_still_bound': True,
  'tts_shutdown_calls': 0,
  'flags': []
}
```

要求：

1. 两者皆缺时，在同一 single-flight 事务中把 TTS、STT 保持为局部对象；STT 构造与协调预热全部成功且生命周期仍 open 后，才一次发布两者。
2. STT 构造、预热、发布检查或 closing 任一步失败，回收本轮局部 TTS（含其 RVC Worker），不得留下半套已发布语音栈。
3. 已有 TTS、仅缺 STT 的路径继续只补 STT，不得关闭调用前已发布的 TTS。
4. 增加“两者皆缺且 STT 失败”与“已有 TTS 且 STT 失败”两个不同所有权测试。

### F1-C（P1）：single-flight 等待者在 shutdown 后不能及时退出，超时循环还可无限等待

位置：`src/core/assistant.py:260-285,411-428,1307-1310`

等待者以 `while _voice_loading: cv.wait(timeout=120)` 循环。shutdown 只设置 `_shutting_down`，没有通知 condition；若 active loader 卡在构造，等待者在关闭后仍睡眠。即使 120 秒超时，循环也没有总 deadline，会再次等待。

Codex 复现：

```text
waiter_after_shutdown {'waiter_still_alive_after_300ms': True}
```

要求：

1. shutdown 设置 closing 后主动唤醒所有 loader 等待者；注意锁顺序，不能在持有 lifecycle lock 时再等待 loader lock。
2. 等待使用单一有限 deadline；超时返回结构化失败，不能重置 120 秒后无限循环。
3. 测试保持第一个构造器阻塞，启动第二个 ensure，再调用 shutdown；第二个调用应在短界限内返回关闭失败，不依赖第一个构造器释放。

### F1-D（P1）：`_shutdown_engine` 会把内部 TypeError 误判为签名不兼容并重复关闭

位置：`src/core/assembly.py:98-113`

当前先调用 `shutdown(stop_rvc=...)`，捕获任意 `TypeError` 后再调用一次 `shutdown()`。若 shutdown 已执行部分清理、随后内部抛 `TypeError`，资源会被关闭两次，违反 exactly-once。

Codex 复现：

```text
typeerror_shutdown {'calls': 2}
```

要求：不得用运行时 `TypeError` 决定是否重试。调用前通过明确协议/签名能力判断，或为 TTS 提供独立的窄关闭入口；无论关闭抛何种普通异常，单次回收最多调用一次。增加内部 `TypeError` fake，断言 `calls==1`。

### 本次复验命令

完成上述 F1 协议修复后，除本文件原有命令外，再运行新增的四条确定性测试。交接需逐条报告：shutdown-prewarm Worker、双缺失 STT 失败事务、等待者关闭唤醒、内部 TypeError exactly-once。完成后停止等待 Codex；不得提交、推送或进入 Round 3。

## 结论

Cursor 声明的 7 项修复中，F3（串行预热等待）、F6（有限超时）和 F7（共享 Ollama 卸载语义）已通过离线复验；Windows RSS/子进程 fallback 也已在当前机器返回可观测值。

Round 2 仍不能验收。以下问题均由 Codex 使用 fake 依赖或离线脚本稳定复现，不涉及真实 Ollama、RVC、麦克风、扬声器、浏览器或私人记忆库。请一次性修复本文件全部项目，完成后停止等待复验；不要进入 Round 3。

## Finding 1（P1）：TTS 已加载后切换语音会重复构造 TTS，并停止现有全局 RVC Worker

位置：`src/core/assistant.py:299-410`、`src/voice/tts/engine.py:476-483`、`src/voice/rvc/infer.py:104-112`

`ensure_voice_stack()` 在 `self.tts_engine` 已存在、仅缺 STT 时仍创建第二个 TTS，对它执行预热，然后在发布阶段把它当作竞态 loser 调用 `shutdown()`。真实 `TTSEngine.shutdown()` 会调用全局 `stop_rvc_worker()`，因此这个“重复实例回收”会停止已经由正式 TTS 使用的 RVC Worker，导致进入语音模式时额外构造、额外预热和下一次播报冷重载；这正是卡顿与音色短暂失效风险。

Codex 确定性复现：

```text
{'tts_first_ok': True, 'voice_ok': True, 'tts_created': 2,
 'same_bound': True, 'shutdown_calls': [0, 1]}
```

修复要求：

1. `ensure_tts_for_playback()` 与 `ensure_voice_stack()` 必须共用 single-flight 加载协议，不能并发构造多个 TTS 再靠关闭 loser 收口。不要在持有生命周期状态锁时执行设备/模型构造；可使用独立 loader mutex/condition，shutdown 仍应能先标记 closing。
2. 已有 TTS、仅缺 STT 时只构造并发布 STT，不重复构造/预热/关闭 TTS；已有 STT、仅缺 TTS 时只补 TTS。
3. 任一局部构造失败或 shutdown 竞态只关闭本轮真正拥有的局部对象；不得关闭已发布对象或停止已发布 TTS 的 RVC Worker。
4. 增加顺序和并发测试：先 TTS 后 voice、先 voice、并发 TTS/voice、构造中 shutdown。断言 TTS 工厂仅调用一次、正式 TTS 在助手 shutdown 前未被关闭、STT 仅一次、无资源在关闭后发布。

## Finding 2（P1）：生产计时器按 turn 永久保存，新增了线性内存增长

位置：`src/core/perf_timing.py:19-69`、`src/core/assistant.py:468-816`

`SessionTimer._stages` 使用 `turn_id::stage` 唯一键保存每轮结果，没有容量或淘汰。生产非流式和流式入口现已每轮写入，所以进程运行越久，该字典越大。

Codex 确定性复现：

```text
{'timer_stage_count_after_10k': 10000, 'turn_seq': 10000}
```

修复要求：

1. 日志仍保留 session/turn/stage/elapsed/cold 信息，但内存内记录必须有硬上限；可以只保留最近固定数量，或生产只写日志、测试通过显式 collector 捕获。
2. 上限必须在构造时严格校验并有保守默认；不记录 prompt、回复或记忆正文。
3. 同时覆盖非流、流式成功、失败、取消；运行远超容量后断言记录数不超过上限且冷热语义正确。

## Finding 3（P1）：soak 报告明知对象仍存活却返回成功，弱引用验收无效

位置：`diagnose_resource_soak.py:37-83`、`tests/test_storage_round2_review_fixes.py:290-314`

脚本在检查弱引用时仍保留 `assistant` 强引用，循环变量 `a` 也保留最后一个助手；没有 `gc.collect()`，也不把存活数纳入 `ok`/退出码。当前真实输出同时出现：

```text
"ok": true,
"weakrefs_alive_after_shutdown": 2
```

测试中的 `self.assertTrue(callable(ref))` 只证明 `weakref.ref` 可调用，完全没有证明对象被回收。全量测试还出现 `ResourceWarning: unclosed scandir iterator`，来源是长目录 cleaner 测试保留活动 sweep 后未在 `finally` 调用 `stop_auto_cleanup()`。

修复要求：

1. 关闭后先采集所需终点状态，再删除所有强引用（包括循环变量），执行受控 `gc.collect()`，断言每个 weakref 为 `None`。
2. 任一对象仍存活时报告 `ok=false` 并返回非零退出码；报告应分别呈现 bounds 与 reclaim 结果，不能让 `ok=true` 与失败证据并存。
3. 单元测试必须真实断言回收，不得只断言 weakref 可调用。
4. cleaner 长目录测试使用 `try/finally` 关闭活动 sweep；全量测试不应留下未关闭 `scandir` 的 `ResourceWarning`。
5. 保持 workload 全部为 fake；不访问设备、GPU、网络或真实清理目录。

## Finding 4（P2）：TTS 失败后的手动重试和关闭状态契约仍不成立

位置：`src/core/assistant.py:222-297`、`src/cli/text_session.py:116-131,161-183`

首次加载异常会永久设置 `_tts_load_failed=True`。CLI 却提示“输入 `tts` 可重试”，之后的显式操作仍立即返回“本会话不再重试”。另外，`ensure_tts_for_playback()` 和 `ensure_voice_stack()` 都在检查 closing/closed 之前先返回已有引擎成功，因此 assistant shutdown 后仍报告加载成功。

Codex 确定性复现：

```text
first_ok=False
second_after_manual_retry_ok=False
second_errors=['TTS 先前加载失败，本会话不再重试']

ensure_after_shutdown_ok=True
engine_still_bound=True
```

修复要求：

1. 区分自动播报的“本轮失败后不重复刷日志”与用户显式 `tts` 命令重试。显式重试应能重新进入 single-flight 加载；若产品决定本会话禁止重试，则 CLI 不得声称可重试，但当前交互需求优先采用显式可重试。
2. 所有 ensure 入口先检查 closing/closed；关闭开始后即使旧对象仍绑定也必须返回结构化失败。
3. 增加“第一次工厂失败、第二次显式重试成功”和“shutdown 后 ensure 失败且不构造”的产品路径测试。

## 复验要求

只修复上述 Round 2 收口项；保留 `memory.enabled=true`、`retention.enabled=true`、现有 `.gitignore` 隐私规则和私人数据库。不得读取或删除 `data/memory`，不得进入 Round 3，不得启动真实服务、设备、浏览器或清理实扫，不得提交或推送。

完成后运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py diagnose_memory_ids.py diagnose_resource_soak.py
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 diagnose_resource_soak.py --turns 1000 --fails 100 --json
git diff --check
```

交接必须附：四项对应改动、确定性回归测试、soak JSON（`ok=true` 时 `weakrefs_alive_after_shutdown` 必须为 0）、全量测试数，以及未运行的真实集成边界。完成后停下等待 Codex 最终复验。
