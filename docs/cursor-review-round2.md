# 第二轮独立评审

2026-09-26：独立执行全量离线测试 108 tests / 2.108s / OK。仍未验收，以下属于上一轮第 3 项尚未闭合的缺陷，请修复，不扩大范围。

1. **已离线复现旧轮故障污染新轮**：TurnSpeechPlayer._run 在旧轮 speak 阻塞期间 begin_turn，新轮 enqueue；随后旧 speak 抛错，会无条件设置全局 consumer_failed 并 drain 全队列。实际结果 new_turn_failed=True、new_spoken=False。异常处理必须在同一状态锁下判断失败 item.turn_id 是否仍是当前轮；过期故障不能设置新轮失败或清空新轮数据。增加 entered/release/new_spoken Event 的确定性测试。
2. **助手取消没有稳定的轮次身份**：_invalidate_speech_turn 将同一个 _stream_cancel set 后立即 clear；旧生成仍读取这个共享事件。无 TTS 旧流根本没有 turn_id 校验，新输入后旧流还能继续 on_delta/角色转发；有 TTS 旧流入队失败后的 _abort 又会 invalidate 当前新轮，造成反向取消。请每输入捕获不可复用的轮次 token/独立 Event，新输入置位旧 token；旧轮的失败、finally、abort 仅能清理自己拥有的队列，不得失效新轮。入队/显示/角色转发/请求下一段均检查所属轮次。用两个可控假流证明旧轮恢复后不能显示、转发或清空新轮。
3. **提前取消仍执行路由副作用**：process_text_input_streaming 在 cancel_event 已置位后仍调用 _route_streaming；已 shutdown 也没有入口短路。这会执行系统/搜索 handler，之后才跳过 TTS。入口应在任何路由/模型调用之前拒绝已取消或已关闭的调用。分别用计数 fake 验证系统、搜索、LLM 零调用；非流式关闭后也应拒绝新任务。禁止真实系统操作。

保持历史客户端唯一提交者、原有安全与兼容边界。补测试后运行全量离线测试，更新 ADR/基线/交接状态，停止修改等待评审。
