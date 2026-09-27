# ADR：语音会话轮流说话与播报完成契约

- **状态**：已采纳为串行兼容契约；生产默认监听策略由 `adr-duplex-listening.md` 更新
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步

## 问题

文本侧已有增量生成与分句播报。若语音会话在生成结束后立即打开麦克风，而 TTS 队列仍在播放，助手声音可能被识别为下一轮用户输入。

## 决策

1. **顺序轮流说话**：用户说完并识别 → 模型可增量生成且首句可提前入队 TTS → **仅当本轮播报确认完成（或本轮无需/从未开始播报）**才恢复 STT。不实现 barge-in、回声消除、唤醒词或底层音频硬打断。
2. **生成完成 ≠ 播报完成**：`StreamEvent.COMPLETED` / 分发成功只表示生成阶段；`TurnSpeechPlayer.wait_for_turn(turn_id)` 表示播报阶段。
3. **等待状态**：`COMPLETED` / `FAILED` / `INVALIDATED` / `STOPPED` / `TIMEOUT`。不得用 `queue.empty()` 推断完成（句子可能已出队仍在 `speak()`）。
4. **助手契约**：`process_text_input_streaming(..., wait_for_playback=True)` 在有效成功后等待所属播报轮；默认 `False` 保持文本/交互非阻塞。系统/搜索在等待路径下也走受管理队列。播放失败/超时不撤销已提交历史。
5. **可恢复监听窄契约**：`DispatchOutcome.safe_to_resume_listen`。仅 `playback_status=COMPLETED`，或可证明本轮从未入队/无需播报时为 True。`TIMEOUT` / `INVALIDATED` / `STOPPED` / 已有部分播报后的生成失败、取消、流迭代器普通异常、以及无 `COMPLETED`/`FAILED` 的正常耗尽均为 False；`VoiceSession` 据此退出循环且不再调用 STT，不读 `_speech_player` 私有字段，也不声称硬打断音频。无 TTS / 无播放器归属时不得因 no-op 入队误标「已开始播报」。
6. **等待阶段取消**：外部 `cancel_event` 使 `wait_for_turn` 返回 `INVALIDATED` 后，助手按 token 所有权 CAS `_release_own_speech`，丢弃未开始句子；已进入 `speak()` 的音频可能仍继续。
7. **VoiceSession**：可选 `stream_reply`（配置 / `--stream` / 交互 `stream`）；开启时增量打印并 `wait_for_playback=True`。关闭时保持原非流式 `process_text_input`。不可安全恢复时返回 `unsafe_pause`；流式分支逃逸的普通异常亦 `unsafe_pause`（不吞 `KeyboardInterrupt`）。

## 替代方案

- 仅 sleep 固定时长：不可靠，拒绝。
- 语音会话读 `_speech_player` 私有字段：破坏封装，拒绝。

## 限制

- 不声称硬打断当前 `speak` / 底层播放器。
- 真机麦克风、扬声器、RVC、Ollama 未在本里程碑验证。
- 阻塞网络读取中的取消仍无法立即打断（既有限制）。

## 验证

离线 `unittest`（含 `tests/test_voice_session.py` 真实 `VoiceSession.run` 边界）；见 handoff 最近结果。
