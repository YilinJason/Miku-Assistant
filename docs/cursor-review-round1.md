# Codex 独立评审：第一轮

状态：需要修复，尚未验收。2026-09-26 独立运行 Python312 `-X utf8 -m unittest discover -s tests -q`：97 tests / 0.349s / OK。通过现有测试不覆盖以下缺陷。请只修复本里程碑范围，保留 STT 修改，不提交推送，不启动真实设备。

1. **背压静默丢句**：assistant.py 两处忽略 enqueue 的 False；队列满仅等待 0.5 秒就丢句，但仍返回成功。正常慢 TTS 会稳定触发。改为可取消、可检测消费者故障的背压等待，或明确终止本轮并报告失败，不能静默继续成功。用 Event 阻塞第一句、填满队列后证明无丢句且退出能解除等待。
2. **关闭后仍播放待播项**：speech_queue.py shutdown 仅在队列满时清空；未满则 STOP 排在剩余句子后；_run 在 stop 已置位时仍执行普通项。改为停止后丢弃全部待播项，当前正在 speak 可有界等待。消费者 speak 抛错后也不能继续播放已排队的同轮项。增加确定性测试。
3. **取消及新轮次失效不完整**：assistant 的 finally 仅 close 流，on_delta 异常/KeyboardInterrupt 不废弃播报队列；shutdown 不停止生成循环，之后仍可创建 player/角色转发。begin_turn 仅在 LLM+TTS 路径调用，新轮次关闭 TTS 或命中系统/搜索时旧队列仍有效。提供统一轮次失效/取消状态，在生成交付和入队前检查；异常/取消/退出丢弃未播项并显式关闭流。旧轮消费者失败不得污染新轮。不要宣称支持底层硬打断，也不要求立即中断阻塞的网络读取。
4. **分句契约遗漏**：sentence_splitter 未处理英文句号；无标点大 chunk 仅切一次 max_buffer，剩余缓冲仍可远超上限，finish 一次刷出巨大尾句。处理英文句末并对单大 chunk 连续分割，测试中英文、跨 chunk、超长输入全部片段及残留上界，不丢字、不重复。
5. **测试缺乏同步证据**：当前 test_first_delta_and_sentence_before_completed 只在函数返回后检查 TTS，不能证明首句在完成前到达；stale-turn 测试未阻塞消费者却断言首句没播放，存在竞态；背压断言接受多种结果且轮询 Event.wait 等同 sleep。改为 entered/release/done Event 明确控制时序，所有 worker 在 finally 释放并 join。补 CLI 路径的搜索单次执行、取消、消费者失败及 shutdown 待播清理场景。
6. **回退重复遍历处理器**：_dispatch_pre_llm 先遍历非 LLM，缺少 generate_stream 再 _dispatch_text，导致系统/搜索 handler 重复执行。修复路由选择使每个 handler 每输入只执行一次，维持原优先级与 SafetyPolicy；测试未命中前置 handler 的调用次数以及兼容 LLM fallback。

修复后更新 ADR、基线及 cursor-handoff-status.md，记录实际测试和限制。完成后停止编辑等待下一轮独立评审。
