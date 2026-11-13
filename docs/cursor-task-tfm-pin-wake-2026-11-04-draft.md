# TFM 解 pin 后的无必要等待（2026-11-04 已放行）

## 窄问题

当前remove(path)遇IO pin时按普通OS文件锁失败记录3秒delay；即使真实worker退出并release_io_lease，也重新完整等3秒，前台admission一直pending。空队列worker又先queue.get(timeout=.5)后才看wake，明明有Event仍多半秒等待。本切片只删这两处无必要等待，不重写整个删除调度。

## 方案与限制

- 只有即时remove遇pin分支记delay=0（最后pin释放后才执行）。明确remove_delayed(delay=...)保留请求的完整延迟语义；OS锁失败现有退避3秒/2秒倍数、重试上限/耗尽、身份claim均不变。重复已有pending/queued/inflight的行为保留，不为新速度清旧失败fence。不得为了绕过pin直接delete。
- 唯一worker空队列改为get_nowait→clear wake→锁内pump→若队列非空立即下一圈，仍空则wait(wake,.5)。清wake必须在pump/判空前，生产者在判空后set仍会唤醒，不能吞通知。shutdown已有set stop+wake；clear后及wait前必须复核stop（尤其stop在clear之前已置位时，不能清掉shutdown通知又等整半秒）。不要加新线程/Timer/更高频轮询/常驻忙等，删掉现有重复空等待。
- _wait_interruptible(delay)本轮不改：它代表明确delay和OS lock retry，不能任意wake导致提前删除。**长delay或OS锁重试项仍可能队头阻塞其它项，这个局限必须保留为后续独立deadline调度任务，不能本切片声称已全解决。**
- 不改owned/Pending容量、IOlease、NativeJob/模型/声音权限、epoch/未知线程ownership/锁外OS删除与actualquiet协议。仅temp_files.py+必要tests/docs；读AGENTS四skills、安全规则、保留dirty；禁正式memory、真实模型设备/目录扫清、提交推送。

## 验证

独占TemporaryDirectory+真实TFM+fake受控IO lease（通过现有受信fixture），pin期间不删，最后pin释放后应<1秒回收且pending/owned归零；多个pin只放最后一个才清，重复release幂等；重复remove不造多个worker，空worker不busyloop，shutdown立即醒并实际quiet；explicit delayed仍不早删；PermissionError/旧身份仍保持失败责任。所有共享data/tmp禁止新增，finally先释放全部gates/pins→actualworkerjoin+managercomplete/owned0再删目录；异常主线程断言。

本轮先源码/tests编写+局部compile/diffcheck后STOP，**不运行任何tests/gate**，由Root独立探针和已验收TFM契约回归。禁修改运行Rootprobe/监督器/覆盖报告。ADR Proposed。快照仅比较不能回滚。Cosy未Root最终PASS前此任务不得开始。

---

## Handoff（Cursor 2026-11-04 返修，STOP）

| 项 | 内容 |
| --- | --- |
| 改动 | `temp_files.py`：空队列分支 `clear`/pump/二次 `get` 后、`wait(0.5)` 前 `if self._stop.is_set(): break` |
| 测试 | `_finish_tfm` join 后 assert 线程 dead；`test_stop_before_clear_does_not_enter_idle_wait`；tracked_remove 用 patch 内/外双层 `_finish_tfm` |
| Root 反馈 | `docs/codex-review-tfm-pin-wake-2026-11-04.md`（5 项 4PASS/1FAIL → 本返修） |
| Cursor 已验证 | `compileall` + `git diff --check`（未跑 pytest/gate） |
| 快照 | `tmp/tfm-pin-wake-root-before-20261104/` 46953 bytes |
| 仍开放 | HoL / 完整 deadline 调度 |
