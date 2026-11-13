# TFM deadline调度下一切片（2026-11-04 已放行实施）

Root已逐调用方审查，具体决策以docs/architecture/adr-tfm-deadline-2026-11-04.md为准。仅temp_files.py及必要tests/docs，不改已验收threadstart/claim/IO/安全代码。保留全部dirty。Cursor只写源与tests、局部compile/diffcheck，然后STOP；**不运行任何测试/gate**，由Root先审fixture并独立测试。禁止访问正式data/memory、运行真实模型音频设备/清理目录、修改Rootprobe/监督器/覆盖报告、git回退提交推送。测试只独占TemporaryDirectory，finally lease/gates→actualjoin/TFMcomplete→恢复patch→删目录；不能直接revoke真实文件丢owner。

私有pending载荷迁移只改必要repo tests，根probe仅Root可改。旧测试通过_wait_interruptible(delay)卡某项的同步位置已变，要迁移到有效行为边界（真实lock外delete阻塞/ready提交前等），保留原身份/关闭/唯一worker断言，不能挪到空闲wait后让整个worker人为被阻而伪装新调度。请报告各旧测试改动理由。可删除纯tuple内部镜像断言，已有即时解pin真实延迟行为证明不可降低。

前置pin/wake已Root最终PASS。当前不能把HoL写成已解决；先完成本文件的设计审查，再由Root放行官方Cursor。保持全部dirty/AGENTS与安全约束，禁正式memory/模型设备/提交推送。

候选优先评估：pending登记保存接受时的绝对单调due，单worker仅把已到期且未pin/未耗尽/身份匹配的条目pump入既有有界ready队列；未来项留在有界pending，不占满ready队列。队列空时用唯一wake等最近due或stop，新较早意图能唤醒重算。OS锁一次失败后在pending记下一个due及剩余尝试数，让出worker，不就地sleep阻塞其它条目。这样不用另建无界heap，也能保留既有ready容量和锁外身份claim。但它会改变pending tuple语义、pump/shutdown及部分private探针，须逐调用方核实后才实施，不把本建议当已批准设计。

## Cursor 实施交付（2026-11-04，STOP 点）

- **生产**：`src/voice/audio/temp_files.py` — `_PendingDeleteRecord(due_at/remaining_attempts/attempts_made)`、仅到期 pump 入 ready、worker 单次 `os.remove`、锁失败 `due_at=now+2*attempts_made` 并让出线程；`_wait_interruptible` 仅作空闲边界；`remove_delayed` 拒绝 bool/NaN/inf/负 delay 与非 int 负 `max_retries`。
- **测试**：`tests/test_tfm_deadline_2026_11_04.py`（新）；`tests/test_tfm_bounds.py` 中 `_wait_interruptible` 钩子用例迁移至 future pending / 锁外 delete 门闩；`tests/test_tfm_pin_wake_2026_11_04.py` 断言改为 `due_at` 即时性。
- **比较快照**：`tmp/tfm-deadline-root-before-20261104/temp_files.py`（只读，未回滚）。
- **局部验证（Cursor 已执行）**：`py -3.12 -m compileall` 上述源与测试；`git diff --check` 限定文件。
- **未执行**：任何 pytest/gate/Root probe；提交/推送；正式 memory/模型设备。

## Root初审返修（2026-11-04 STOP）

- **#1 worker 启动**：`_should_start_worker_for_pending_locked`；`_enqueue_delayed_locked` / `_offer_delayed` 以合法 pending 为准，future 亦 `_ensure_worker`。
- **#2 消费者**：worker 内 `break`→`continue`；pin/claim/can_delete 失败 `_defer_ready_item_locked`。
- **#3 dequeue 权威**：`_assess_dequeued_ready_item_locked` 复核 record/due/exhausted；exhausted 不自动重试。
- **#4 唤醒**：`_release_delete_claim_locked` wake；有 pending 时 idle 最长 0.5s 巡检。
- **#5 tests**：`test_tfm_deadline_2026_11_04.py` finally 在 patch 内 `_finish_tfm`；新增 future 到期/pin 竞态/exhausted fence/缺失文件/ max_retries=0；StaleQueue 去掉 `mgr.remove(path)` 互等风险。

## Root复审b返修（2026-11-04 STOP）

- **唯一生产**：`remove_delayed` non-pin 分支 `_enqueue_delayed_locked(..., refresh_exhausted=key in _retry_exhausted)`；`budget=max(1,int(max_retries))` 含公开 `max_retries=0` 重试。
- **tests**：删除私有 `_retry_exhausted` 镜像用例；新增真实锁失败耗尽→公开 `remove_delayed` 重试；Pin/Missing 用 `queue.get_nowait` 门闩；去掉 `_pending_cleanup` 观测。
- **冻结**：复审 b 其它 6 项生产行为不变。未跑 test/gate/Root probe。

## Root fixture 最后窄修（2026-11-04 STOP，生产冻结）

- LockBackoff / ExplicitExhausted：`allow_cleanup` 事件，finally 先放行精确 owned 删除再 `_finish_tfm`。
- ExplicitExhausted：锁失败计数达 2 后 `_wait_inflight_quiet`（公开 `inflight_count==0`）再公开重试。
- MissingFile：门闩内 `is_owned`+`is_path_deletable` 后 `raw_remove(ghost)` 模拟外部消失，不 patch `exists`。
- 仅 tests/docs；`compileall`+`diff --check`；未跑测试。

## Root 功能通过 + helper 收口（2026-11-04 STOP）

- 恢复独立 `_wait_inflight_quiet`（仅等 inflight==0）与 `_finish_tfm`（shutdown/complete/worker join）。
- 删除 `temp_files.py` 未用私有残留：`_PendingCleanup`、`_enqueue_pending_item_locked`、`_offer_delayed`、`_wait_interruptible`。
- 生产行为冻结；Root 重跑 42 组与 deadline7。

## Root 里程碑 idle 窄修（2026-11-04 STOP）

- `_worker_idle_wait_timeout_locked`：逾期 blocked 仅 skip，扫描全部 eligible 取最近 due；无 eligible 且有 pending 才 0.5 巡检。
- 测试：`IdleWaitEligibleDueTests`（pinned 逾期 + peer delay 0.08，删除须在 0.35s 内）。
- 未跑 test/gate；compile/diffcheck only。

必须先回答：pending/ready/inflight各计数及容量；重复remove与明确remove_delayed先后怎么处理；samepath换identity后旧due如何撤销；pin释放如何沿用原明确deadline；失败耗尽如何只在显式重试刷新；worker unknown-start和实际退出保持；shutdown何时不再取新项、未处理owned如何保留。拒绝布尔/NaN/inf/负delay或重试预算的规则需与原公开兼容分开说明，不能借优化偷偷改变参数契约。

验收必须行为证明：长future项不阻新due0，锁失败项不阻其它到期删除；重复意图不重复线程/排队且存储硬有界；pin未释放不删/明确delay不早删；旧身份不撤新意图；满队列和陈旧条目不饥饿新到期任务；无busyloop/无新Timer；stop先于clear不丢；总关闭deadline、不可中断OS调用边界如实、actualquiet后才释放所有者。保留本轮Root pin/wake与旧stale/claims/bounds矩阵，再加独立时序探针；私有载荷若改变，Root只适配探针的构造方式，不降低原断言。

这一阶段交付应先是简短ADR设计与具体差异范围，Root核对后再源码；随后Cursor STOP→Root fixture安全review→顺序scoped tests→独立回归→资源里程碑，完整闭环前不接LLM冷载保护。低配CPU/模型实际延迟不从这些文件删除测试中推断。
