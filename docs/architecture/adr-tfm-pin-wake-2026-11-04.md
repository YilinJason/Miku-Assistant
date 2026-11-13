# ADR：TFM 解 pin 后的无必要等待（窄切片）

## 状态

Accepted（Root独立5/5、相关78/78、原所有权9/9与旧身份1/1；docs/codex-review-tfm-pin-wake-2026-11-04.md；不表示HoL已解决）

只读快照（比较用，不可回滚）：`tmp/tfm-pin-wake-root-before-20261104/`（46953 bytes，Root 比较用）。

## 背景

`remove()` 在 IO pin 期间把待删意图记入 `_pending_cleanup` 时沿用了 OS 文件锁失败路径的 3s delay，末 pin 释放后 worker 仍 `_wait_interruptible(3.0)`，前台 admission 长时间 pending。空队列 worker 先 `Queue.get(timeout=0.5)` 才处理 `_worker_wakeup`，已有 Event 时仍可能多等最多约 0.5s。

## 决策

1. **即时 `remove` 遇 IO pin**：`_note_pending_cleanup_locked(..., delay=0.0, ...)`。OS 锁失败入队的 `eff_delay=3.0`、`remove_delayed(delay=...)`、锁内重试退避 `_wait_interruptible(2.0 * (attempt + 1))`、身份 claim、重试上限与 exhausted fence **不变**。
2. **空队列 worker**：`get_nowait` → `clear` wake → 锁内 `_pump_pending_into_queue_locked` → 再 `get_nowait`；仍空则 **复核 `_stop`**，已 stop 则退出循环；否则 `wait(wake, 0.5)` 后 pump。不新增线程/Timer/忙等；shutdown 仍 `set` stop+wake。

## 未解决（本 ADR 不声称修复）

- 长 delay 或 OS 锁重试项仍可能 **队头阻塞（HoL）** 其它删除项；需后续独立 deadline 调度任务。
- `_wait_interruptible` 语义未改；不得用任意 wake 缩短明确 delay 或 OS 锁重试等待。

## 验证（Cursor，未跑 gate）

- 生产：`src/voice/audio/temp_files.py`（上述两处）
- 测试：`tests/test_tfm_pin_wake_2026_11_04.py`（独占 `TemporaryDirectory`；finally 先 release lease → `shutdown`/`join`）
- 局部：`py -3.12 -m compileall src/voice/audio/temp_files.py tests/test_tfm_pin_wake_2026_11_04.py`
- `git diff --check` 上述文件

Root 建议：对齐 `tmp/codex_tfm_pin_wake_root_2026_11_04.py` 探针 + `tests/test_tfm_bounds.py` 相关组 scoped supervisor。

## 未做

修改/运行 Root probe 或监督器；覆盖报告；提交推送；正式 memory；真实模型设备。
