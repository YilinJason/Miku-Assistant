# Codex 独立评审：语音流式轮流说话（Round 3）

## 结论

Round 2 两项修复正确，134 项纯离线测试尚待本轮最终重跑；但流“无终结事件直接耗尽”仍会绕过窄契约，里程碑暂未通过。

## 必须修复

### 流在部分播报后直接 `StopIteration` 会误报可恢复监听

`process_text_input_streaming()` 的 `outcome` 初始为 `GENERATION_FAILED`，但 `safe_to_resume_listen` 使用 dataclass 默认 True。当前 `next(stream_iter)` 遇到 `StopIteration` 只执行 `break`：

- 若此前已收到可切句 `TEXT_DELTA`，句子可能已经入队并进入阻塞的 `tts.speak()`，`speech_started=True`。
- 循环结束后，`wait_for_playback` 的 `GENERATION_FAILED` 分支因为 `speech_started=True` 不调用 `_release_own_speech()`。
- 返回结果仍为 `safe_to_resume_listen=True`，`VoiceSession.run()` 因此立即调用下一次 STT；尚未开始的队列项也没有被丢弃。

这和显式 `FAILED`、迭代器抛异常的语义不一致，也违反“只有明确完成或证明未播报才恢复监听”。

修复要求：

- 将未收到 `COMPLETED`/`FAILED` 的正常耗尽视为结构化生成失败。丢弃 splitter 残留，按既有 token 所有权与 CAS 规则释放自己的播报轮。
- `safe_to_resume_listen` 必须按 `not speech_started` 计算；如果已有可能播报的句子则返回不安全，不能进入下一轮 STT。
- 不得把无终结事件当成模型生成成功，不得提交历史或转发角色。
- 保持 `KeyboardInterrupt` 上抛、普通迭代异常、无 TTS、显式 FAILED 和播放超时的既有行为。

## 必须补充的离线测试

1. 真实 `VoiceSession.run()`：流先产出完整句子，确认 `speak()` 已进入并由 Event 阻塞，然后生成器直接结束（没有 terminal event）。断言不会第二次调用 STT、返回 `unsafe_pause`，且未开始句子被丢弃。
2. 无 TTS 对照：同样无 terminal event，结果为 `GENERATION_FAILED`，但因可证明从未播报而 `safe_to_resume_listen=True`。
3. 用 Event 验证取消后没有第二句播报；将现有取消测试中的 `time.sleep(0.25)` 改为第二次 `speak` Event 的有界等待断言，避免 sleep 猜测。
4. 保留此前 134 项覆盖并重跑 Python312 全量 fake 离线测试。

## 约束

保留 STT、RVC Worker 与全部既有未提交修改；不启动真实麦克风、扬声器、RVC、Ollama、浏览器或系统操作；不提交、不推送。完成后更新 `docs/cursor-handoff-status.md`，停止修改等待 Codex Round 4。
