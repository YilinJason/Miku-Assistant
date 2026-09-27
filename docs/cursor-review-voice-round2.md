# Codex 独立评审：语音流式轮流说话（Round 2）

## 结论

Round 1 的超时、显式失败事件和等待取消修复方向正确，132 项纯离线测试实际通过；但异常路径与“无 TTS”路径仍使 `safe_to_resume_listen` 的语义不可靠，里程碑暂未通过。

## 已独立验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
Ran 132 tests in 3.863s
OK
```

未启动真实麦克风、扬声器、RVC、Ollama、浏览器或系统操作；STT 与 RVC Worker 的既有修改仍保留。

## 必须修复

### 1. 流迭代器抛异常后，`VoiceSession` 仍会重新打开麦克风

`process_text_input_streaming()` 的 `except BaseException` 会释放所属播报轮后原样抛出。`VoiceSession.run()` 的通用 `except Exception` 随后只打印错误并继续循环。若流已产出完整句子且 `tts.speak()` 正在阻塞，释放只能丢弃未开始的句子，不能证明当前音频已经停止；下一轮 STT 仍会立刻发生。

修复要求：

- 流式语音调用抛出普通异常时，在无法证明未开始播报的情况下不得继续监听。可以在 `VoiceSession` 流式分支将异常转成 `unsafe_pause`，或由助手转成带窄契约的结构化结果；不得吞掉 `KeyboardInterrupt`。
- 增加真实 `VoiceSession.run()` + Event 测试：流先产出可切句内容，确认 `speak()` 已进入，再由迭代器直接抛 `ConnectionError`/`RuntimeError`；底层音频释放前不得第二次调用 STT，会话应安全暂停。
- 仍需在 `finally` 释放并 join 测试线程。

### 2. 没有播放器时 `_try_enqueue` 仍把本轮标成“已开始播报”

`_enqueue_sentence()` 在 `player is None` 时返回 True；`_try_enqueue()` 只检查返回值与非空句子，于是即使没有 TTS 引擎也会把 `speech_started=True`。后续 `FAILED`/取消会返回 `safe_to_resume_listen=False`，错误地暂停完全没有音频的语音会话。

修复要求：

- “已开始/可能存在播报”只能在本轮确有受管理播放器和有效播报归属、且句子被接受时置位。不要因为无播放器路径的 no-op 成功而置位。
- 增加无 TTS fake 测试：流先产出完整句子再返回 `FAILED`（或取消），结果应证明可安全恢复监听；不得读取播放器私有字段。
- `COMPLETED` 事件的非成功分支当前硬编码 `safe_to_resume_listen=True`，也应按同一事实判断，避免非标准/异常完成事件在已经入队后误报安全。

## 保持不变

- 保留 Round 1 的真实 `VoiceSession.run()` 顺序、超时、显式 FAILED、取消清队列与新轮隔离测试。
- 播放失败/超时不回滚已提交历史；系统/搜索仍单次执行。
- 只运行 fake 离线测试，不提交、不推送。完成后更新 `docs/cursor-handoff-status.md` 并停止修改，等待 Codex Round 3。
