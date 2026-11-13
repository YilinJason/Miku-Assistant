# 恢复：仅收口 blocked-write 测试可靠性

读取 AGENTS.md 与 collaboration-review / engineering-quality 技能，以及 docs/cursor-task-blocked-write-fixture-2026-10-18.md。本任务是旧切片的完成，不是扩大测试等待或修改生产代码。

已确认当前未完成点：
1. tests/support/cosyvoice_blocked_write.py 的 teardown 吞 shutdown/stop 异常，合成线程 join 后未检查退出；未使用的 wrapper 可以删掉。
2. 专用 _BlockedWriteWorkerProcess 需要 _run finally 显式 EOF、Popen.wait(timeout) 语义的真实有界线程等待，不依赖 base fake 的立即 TimeoutExpired。只改专用 fake，不重写全局 fake。
3. controlled rendezvous 测试的 stop 线程异常须收集回主线程，finally 释放 ready/write gate、回收 synth/stop 两线程并检查完成，确保 TemporaryDirectory 退出前生命周期结束。
4. 保留原生产关闭预算和原功能要求。同步条件必须可控，不能依赖调度决定 shutdown 第一次究竟 complete。需要断言关闭未完成时，使用明确的独立释放屏障表示仍存活资源；资源已退出时允许完成。不得用删断言、强清 registry、无限重试或 sleep 求绿。

先阅读并返回最终 diff 对应的严格线程所有权。仅改 tests/support/cosyvoice_blocked_write.py、B 中专用 fake/四个 blocked-write 测试及必要的邻近专用测试。生产 src/scripts/config 不变，所有 tmp/codex_* 探针不改。主解释器为 C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe；禁止主程序/模型/麦克风/扬声器/data/memory、提交推送。

准备与停止分阶段；5 秒仅测试 rendezvous，生产 timeout 不变。teardown 即使某一步出错也尝试其余回收，然后把异常与未退出状态报告给主测试，不能静默通过。线程体异常也应报告；不以 Python 后台 traceback 代替测试失败。

运行新 targeted 用例一次，再八模块 gate 一次（120 秒测试树总上限、精确 miku_*.wav 名称集合前后一致，不能只比数量）。旧 tmp/cursor_owned_job_validation_gate.py 使用裸 subprocess；若使用须先改为经 src.core.safety 的显式开发诊断 SafetyPolicy + safe_subprocess_popen/run，taskkill 只能是超时后自有测试 root PID 的 /T /F 清理，禁止任意 PID/注册表/电源。不能借生产具名能力执行任意脚本。不要运行旧 raw gate，也不要将上述诊断许可带入生产配置。若门禁失败停止并交接原因，不重复运行至绿。

完成后更新 docs/cursor-handoff-status.md 顶部，说明范围/实际结果/剩余边界，停止等待 Codex 复审，不开始 RVC 或预热/预取。
