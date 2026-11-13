# ADR：TurnSpeechPlayer 惰性 OwnedThreadTask 与有界轮次状态

## 状态

Accepted（2026-11-04），Root独立9及最终相关123均通过，见具名Root review。未接TTS跨句预取，不代表真实音频性能验收。

## 问题

- 构造时 `Thread.start` 使取消/回收在登记前不可靠。
- `queue.put` 与 `accepted++` 非原子，存在幽灵计数与过期项。
- 消费者仅捕获 `Exception`，`BaseException` 可静默杀死线程。
- `_turns` 无界增长；`shutdown` 超时未硬界。

## 决策

1. **单消费者 owner**：构造只登记 `OwnedThreadTask`；首次非空合法 `enqueue` 在状态锁外 `start()`；`_consumer_start_claimed` 保证并发 enqueue 不重复启动。
2. **原子提交**：`collections.deque` + 同一 `Condition` 锁内 `_enqueue_may_commit_unlocked`（stop/failure/turn/terminal/production_finished/双 cancel）；满则 `cond.wait`。锁外取消快路径用于尽早拒绝已取消调用，**提交前锁内再次校验**，快路径不能充当提交准入证明。
3. **失败语义**：`Exception` 在 speak 内处理，可 `begin_turn` 恢复且同一线程继续；`BaseException` 出 `_run` 由 owner 保留 `.error`，标记 `_consumer_fatal`，未终态 waiter → `FAILED`，`begin_turn` 不清 fatal、拒新工作。可恢复 speak 失败仅记录 `err=<exception_log_label>`，禁止 `exc_info`/traceback。
4. **轮次表**：最多保留 128 个 `_TurnPlaybackState`；仅裁剪已终态最老项；无 dict 项的旧 `turn_id` → `INVALIDATED`。
5. **shutdown**：预算校验（有限、非负、≤10s）先于破坏性变更；`cancel` owner 后锁外 `join`；返回值 = owner quiet（强引用不丢）。

## 替代方案

- 继续 daemon `Thread`：无法复用已验收 OwnedThreadTask 矩阵。
- `Queue(maxlen=…)` 静默丢句：违背背压契约。

## 验证

- `py -3.12 -m compileall -q src/core/speech_queue.py tests/test_speech_queue_lifecycle.py`
- `git diff --check`（上述文件）
- Gate：`speech_queue_lifecycle_cursor_20261104c` → **123 OK**，~7.6s，WAV826 零差；本地 Root9 probe 复测 **9/9 OK**

## 未做

TTS 预取、三模型、Core 默认配置改动、Root probe/监督器。
