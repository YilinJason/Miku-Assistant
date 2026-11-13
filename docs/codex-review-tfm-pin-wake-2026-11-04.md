# TFM pin/wake Root：最终 PASS

2026-11-04最终逐行diff复审与Root独立5/5、相关78/78、原所有权探针9/9、旧队列身份1/1均通过。报告labels：tfm_pin_wake_root5_final_20261104b、tfm_pin_wake_root_contract_20261104b、tfm_pin_wake_root_ownership_20261104b、tfm_pin_wake_root_stale_20261104b。所有WAV826精确集合零差，无超时。独占目录真实OS删除的单次解pin样本2.217ms（perf_counter，非模型语音实测）；最后pin才删、明确delay不提前、权限拒绝及stop-before-clear均验证。

改动范围仅即时remove遇pin记录0延迟，以及空队列非阻塞取/pump/Event等待与stop复核；原明确延迟、OS锁退避、身份claim和未知启动actualquiet不变。官方Cursor会话d237892d-8669-455b-b21c-2d8c07b98c6e已STOP。**HoL、LLM冷载和整体性能最终验收仍未完成**。下文保留失败与修复依据，不作为当前状态。

Root独立5项4PASS/1FAIL，报告tfm_pin_wake_root5_20261104a，WAV826零差，无超时。即时解pin/明确delay/权限/普通停机通过；失败：stop恰在clear wake前已置位，clear吞掉通知后仍调用Event.wait(.5)。虽然别的生产者set偶尔会掩盖，不能靠偶然wake满足短shutdown预算。任务已明确clear后/wait前复核stop，需要补。

## 统一返修，仅窄范围

1. 空队列clear之后/进入wait之前复核_stop。若已stop直接结束loop（finally照常terminal/actualquiet），不能再等半秒。保留get_nowait→clear→pump→第二次get→wait通知顺序，避免新任务丢wake；不改明确delay的_wait_interruptible，不引高频轮询。
2. 新test_tfm_pin_wake_2026_11_04.py _finish_tfm join后必须assert线程确实已退出（或delayed_worker_aliveFalse），不忽略join结果。两个tracked_remove测试应在patch内finally确认_finish_tfm完成再恢复patch；外层可再幂等兜底。其它fake无阻塞测试不用扩大框架。补真实stop先于clear的确定性竞态用例，capture wait-after-stop而非只睡觉期待时序，确认finally安全actualquiet。
3. 不删除或修改Rootprobe/监督器/报告；快照tmp/tfm-pin-wake-root-before-20261104确实存在且46953bytes，只作Root比较。文档勿声称不存在或已通过完整调度。只compile/diffcheck、handoff STOP，仍不运行测试。Root独立5与原TFM契约复验后验收。
