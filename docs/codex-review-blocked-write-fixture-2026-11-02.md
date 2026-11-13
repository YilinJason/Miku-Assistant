# Codex review：blocked-write fixture（4d8cd17f）

## 最终结论：PASS（2026-11-02）

Root 已复查专用 EOF/finally/wait、真实 worker_exit_gate、四处线程体捕获、join 后异常合并、最终关闭状态、BaseExceptionGroup 和完整 Windows 路径相等断言。生产未改，原控制/输出/超时覆盖保留。

独立 tmp/codex_blocked_teardown_probe.py 三项全 true；tmp/codex_blocked_write_schedule_probe.py 在 request_ended_before_status=true 时通过。独立八模块 tmp/codex_scoped_test_gate_2026_11_02.py --label local_tts_fixture_final：294 tests OK(skip1)，43.293s（监督器44.875s），WAV825→825，added/removed 空，无超时。

再次执行实际 Windows 无模型 tmp/codex_owned_job_real_gate.py：主动停止4进程47ms、自然退出157ms、父退出5进程16ms，三项全 true；总1.578s，模型0。该阶段本地 TTS Job 最终闭环，可以执行持久 RVC owned-worker 切片。它不是声音自然度、三模型性能或长期无泄漏证明。

下面保留已修复问题与独立失败依据，不代表当前仍未通过。

## Root 独立 294 门禁：blocked-write 通过，另有 Windows 路径断言错误

root 两个 probe 全通过（合法 request-ended-before-status 顺序下 test_passed=true）。独立 tmp/codex_scoped_test_gate_2026_11_02.py：294 tests/51.166s，1 failure，skip1；WAV825→825 精确集合无变化。唯一失败为 slice_a.LocalTtsWorkerCapabilityTests.test_explicit_permission_reaches_fake_popen:213，argv[0] 的 Windows 规范路径含 OpenAI.Codex，而临时 root 字符串为 openai.codex。实际 Windows 同路径，字符串区分大小写导致假失败。

仅补 tests/test_cosyvoice_production_slice_a.py 这一合法启动测试的路径断言：用 Path 的平台路径语义/resolve 比较确实相同的完整路径，保持解释器/脚本/argv顺序与固定参数要求，不能只比basename/后缀。若同一用例的其它固定路径有同类断言可一并等价规范化。生产不改，blocked-write已收口的实现保留。先该用例一次，再同样八模块 gate 一次、记录结果STOP。此为明确定位的既有测试可移植性问题，不是生产能力身份检查错误，不改 production identities_match 规则。

## 返修复审：三项 probe 通过；最后一个调度依赖有确定性复现

Root 重跑 tmp/codex_blocked_teardown_probe.py 三项全 true，异常收集/最终状态/控制流聚合已通过。**但 shutdown 前的三个 assert 不是 shutdown 后资源仍活的屏障。** Root 新增只读调度探针 tmp/codex_blocked_write_schedule_probe.py：仅将主线程构建 shutdown status 的时刻排在正在结束的 request 之后（用 Event，没有生产修改），request_ended_before_status=true，原测试 0.272 秒就以 True is not false 失败。这是合法调度，不是生产超时。

请按以下固定设计做最后一项，不再把前置断言称为确定性屏障：

- _BlockedWriteWorkerProcess 增加可选 worker_exit_gate（仅此专用 fake）；_run 的 finally 在关 EOF 前标记 exit_waiting，并对该 gate 有限等待。默认 None 不改变其他用例，不能在生产加 gate。
- 仅 test_shutdown_during_blocked_write 通过 factory 提供这个 gate；teardown 增加可选 gate 并在任何 shutdown/join 前 set 释放，确保断言失败也回收。
- 本用例显式保证关闭时 worker 退出仍被 gate 阻塞，断言 complete=False、proc.poll() is None、exit_waiting 已达；此时 synth 是否已经返回 False 可以取决于调度，不能固定 result==[]。join 并确认最终 result=[False]/无线程错误，再释放 worker gate、继续有界关闭，断言 complete=True。
- 这样原 incomplete 覆盖被加强而非删除，并且上述“request 先结束”探针也必须通过。保留原 .5s 关闭预算，不修改 root 调度探针。

只针对这一 test/fake/teardown 最小修改；先 root 两个 probe + 相关 targeted 一次，通过再八模块 gate 一次，意外失败停止。生产仍不改，STOP 待 Codex。不要重复提交仅前置 assert 的方案。

状态：CHANGES REQUIRED。已逐项阅读最终 diff、base fake poll/wait、shutdown 状态构建及调用方。专用 EOF/finally、有界 wait、两个线程 join 的方向正确，生产未改。Cursor 报告 291 项通过不是独立验收结果。

Root 纯 fake 探针 tmp/codex_blocked_teardown_probe.py（不改它）确认以下三项全 false：

1. teardown.run 开始即复制 thread_errors，随后 join 期间才写入的后台异常遗漏。必须在所有目标线程有界 join 后取快照/聚合；所有 synth/stop 线程体都捕获并传递异常，不能只处理 controlled stop 的那一条。避免同一错误重复聚合。
2. continue_shutdown 的 complete 仍被忽略。所有 gates 释放/线程 join/proc.stop 后，使用一次有界 continue_shutdown 作最终复核；若仍 incomplete，报告错误而不是成功结束测试。允许中间 incomplete，最终仍未确认则不能放行。所有回收步骤都尽力执行，不能第一个错误导致后续线程不回收。不要强清 registry。
3. 捕获 BaseException 却用 ExceptionGroup 聚合，会在 KeyboardInterrupt + ValueError 时 TypeError，覆盖原控制流异常。使用 BaseExceptionGroup（自动适配普通异常组）或等价保留原异常的方式；添加故障注入确保两个异常可观察。

补充：controlled synth 线程当前无异常收集，应接入与 stop 一致的共享收集；其它三个 blocked-write 同样要捕获线程体异常，不能以后台 traceback 代替测试失败。检查初次 shutdown 的 assertFalse(complete) 是否确由明确仍存活的屏障保证；如果纯调度决定，增加专用受控屏障来证明 incomplete+保留owner+释放后complete，不能删覆盖或放宽生产预算。

仅修专用 fixture/相邻测试，生产与 root probe 不变。为上述可靠性分支增加少量有意义的 fake tests，然后 targeted 一次、八模块 gate 一次，任一意外失败停止交接；禁止重复求绿。实际 WAV 起始825，使用精确前后集合不依赖旧824常数。无模型/设备/data/memory/提交推送。更新交接，STOP 待 Codex。
