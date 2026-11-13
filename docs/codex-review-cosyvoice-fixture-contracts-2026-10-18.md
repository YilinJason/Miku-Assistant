# 2026-10-18 Codex：staging实现通过探针；离线门禁因fake行为不准确仍阻塞

2e2836a0修复的初始化锁边界及闭包已复审，root init/alias两个probe实际exit0。239门禁实际失败2项、824集合零差，不可重跑求绿。当前只修测试fixture契约；不改生产client/timeout/预算/状态。

## 根因已由独立诊断确认

1. `tmp/codex_eof_reproduce_gate.py`（45秒监督）第9次复现EOF第二synthFalse，记录`tmp/codex_eof_reproduce.trace.txt`：_reap_worker_for_reuse返回False、stop_failed=True、proc仍alive，reader/writer均已死；单例运行0.086秒。`_ProtocolWorkerProcess.wait`只是立即poll再抛TimeoutExpired，完全没有等待线程退出，不能模拟Popen.wait(timeout)。生产terminate→wait1s→kill→wait1s正确，但fake立即跳过两次等待，线程未调度完就被断言为仍alive。**只对Idle/Eof场景增加真实有界thread join的wait覆盖**（超时确实抛，只有线程确实结束才返回）；不把_stuck fake改为成功，不改生产回收逻辑。并把test_reader...的_teardown_entered.wait移到_stdin_consumer_stopped断言之前。
2. `test_progress_does_not_extend_startup_deadline`不是简单放宽0.9秒阈值：_StartupProgressWorkerProcess仍使用_LineQueueStream，其read空0.5秒就错误返回EOF，而场景故意ready延迟1.25秒。client正确对EOF提前失败，所以0.859秒。该专用StartupProgress fake改为Condition+明确EOF的stream，_run finally可靠close；空数据不EOF，保持原0.9≤elapsed<2.5与14事件断言，不靠延长睡眠/放宽阈值。不能更改全体_LineQueueStream导致无关测试大面积行为变更。

两项都要在测试中保留真实线程回收/finally，不能强清registry、吞失败或删断言。

## 验证

读AGENTS/skills及本review。保留全部dirty。root probes禁止改。主解释器固定C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe（不要含糊py -3）。先两个故障专项与rootEOF 20次诊断（是修复后的有限压力验证，不是全套重跑求绿）；再tmp/cursor_cpu_checkpoint_staging_validation_gate.py一次120秒/UTF8/精确文件集合824前后不变。若失败保存trace停止，先专项日志避免海量协议stdout。compile/diff，更新交接后停止Codex review。不真实模型/设备/data/memory/vendor/生产改动/提交推送。
