# C2b1 Codex review：通过，可进入C2b2a

## 真实prepare验收

独立`tmp/codex_c1_real_gate.py --prepare-local`实际exit0/44.31秒；RVC→本地prepare（17.953秒，synth/convert调用0）→两句同文实际Cosy+Nahida成功。首句4.172秒、次句3.609秒（含RVC，不含播放）；加载等待被提前到准备阶段，不是总启动时间消失。最终所有worker/reader/writer退出、TFM owned/pending/inflight=0。

报告`data/test/voice-naturalness/cosyvoice-production-prewarm/59eaafcc50b8/report.json`。无设备/真实记忆使用、未切默认。下一步C2b2a先独立三模型串行协调器，再C2b2b接App的loader/关闭owner，不一次改完。

## 当前有效结论

Codex独立审查prepare三层接口/epoch/cancel/非阻塞准入、最终端点健康检查、在途关闭与TFM两阶段gate；ready probe四项False/exit0。返修9b8d1b4f的EOF专用fixture用Condition+明确EOF、fake退出finally关闭stdout，旧proc仍活时触发EOF再重建，未放宽生产断言；C1取消线程异常已捕获并主线程断言。

独立`tmp/codex_c2b1_validation.py`实际exit0：245 tests OK，57.08秒；120秒真实树监督器，文件集合644→644、added/removed均空。前一次失败被修正后的本次结果替代，未以重跑绕过失败。compile已通过。当前无活跃Cursor。

允许真实显式prepare→两句热合成捕获（无设备）；未接资源协调/未开启默认、C2b2仍未开始。下面为已收口历史评审过程。

## d151a3c5后独立复验：ready probe通过，相关门禁发现旧EOF fixture时序失败

Codex实际ready probe四项False/exit0、compile通过。独立`tmp/codex_c2b1_validation.py`**exit1**：245项中旧`B.CosyVoiceIdleRetirementTests.test_reader_eof_not_reused_as_ready`在第二次synthesize断言失败；集合644→644。单独复跑该用例通过（1.239秒）。C2b1生产只新增prepare，原synthesize逻辑未变；现有fake stdout的read在空队列等0.5s后返回空串，把暂时无数据当真实EOF，且该用例通过reader.close来模拟EOF，和真实pipe生命周期不一致。

请只收口测试语义，不盲目改生产/放宽timeout/重试次数：该EOF用例使用显式EOF sentinel/Event的fake stdout（没有数据应继续等，直到明确EOF/close）；fake进程真的退出时在finally关闭stdout，避免cleanup读线程悬挂；用例在旧proc仍teardown/alive时显式关闭stdout触发EOF，确认reader退出再发下一次，仍必须旧proc完整回收后才能spawn新proc且第二次成功。可仅该用例专用小fake，避免为此重写全部B fixtures。

同时C1的`test_queued_save_cancelled_by_interrupt_does_not_dispatch`预期RuntimeError从测试线程未捕获，日志出现unhandled Thread异常却不让unittest失败；把期望取消异常捕获入结果并在主线程断言，finally释放gate/join，保留零派发断言。不改业务取消语义。

测试顺序：先EOF专用case和取消case，用确定性事件验证并重复小专项几次确认无泄漏（不是反复245碰运气），再245相关一次，真120秒树监督器且文件集合零增；任何新失败如实返回。然后停止Codex复审，真实prewarm仍未运行。

423a0495已交付239项报告。API边界与scope正确：仅显式prepare，无自动调用，TTSEngine非阻塞speak锁、epoch合并cancel、backend在途计数与两阶段gate已看到。以下需一次收口，禁止真实模型及C2b2。

1. **ready成功条件不完整**。client.prepare在_ensure后只看proc/reader/writer非None与_ready，不看poll/is_alive/fatal；已退出proc、读者EOF/协议fatal、已停止writer都会假报True。改最终返回前核对所有端点仍健康、同epoch、非closing/取消；复用_worker_reuse_ok等已有helper并检查writer_alive，不放松idle guard。`tmp/codex_c2b1_ready_probe.py`是无I/O的独立故障注入，必须四项False/exit0（不要修改probe）。添加对应真实client fake行为测试。
2. **新测试强制清registry**。C2b1 tearDown直接`client_mod._ACTIVE_CLIENT=None`，违背任务要求并掩盖owner泄漏。记录本测试client refs，先按正常shutdown/stop fake进程/reader/writer顺序回收，必要时再正常shutdown重试，最后assert完整/registry自然None；禁止强制置空与吞异常假通过。新调用_register_tfm_shutdown必须带显式client（避免其legacy无client分支强清registry），不为本切片重构旧B fixtures。
3. **门禁会误报通过**。永久test文件里新增gate仅subprocess.run timeout、不是精确进程树回收，且文件新增仍return子测试0，只比较added不比较removed。优先将开发监督器移到tmp/Cursor具名脚本，测试模块保留标准unittest入口；监督器Popen.communicate有120秒总预算、超时精确PID树终止、UTF8捕获、前后完整文件集合变化返回非0。或修正现有gate达到等价保证。不要声称239 OK就已满足门禁契约。
4. **事件验证缺口**。外部cancel仅预先set，不是加载期间取消；未见prepare期间interrupt/close、timeout/资源错误与Popen延迟返回的实际客户端场景。复用B fake屏障，不实际加载；验证late ready不成功、owner保留/退出后retry可完成。零synth断言不能读取已被worker消费的stdin队列来证明（当前_stdin_lines会与worker抢消费）；在fake stdin.write入口或不可变history记录所有写入，确保prepare生命周期内无synth行、TFM.create计数0。

保持原互斥/idle退休/门面兼容测试，不弱化断言。仅C2b1窄接口/测试/监督器/文档。相关门禁一次通过且集合零增、compile/diff，交接后停止Codex复审。
