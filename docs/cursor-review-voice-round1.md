# Codex 独立评审：语音流式轮流说话（Round 1）

## 结论

当前实现尚未通过里程碑验收。全量纯离线测试实际通过，但测试没有覆盖 `VoiceSession` 的关键循环边界；静态调用链显示，在播报超时或部分内容已开始播报后生成失败时，会话会立即再次调用 STT。此时底层 `tts.speak()` 可能仍在阻塞播放，和“只有本轮播报明确完成后才恢复监听”的目标冲突。

## 已独立验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
Ran 128 tests in 2.987s
OK
```

测试仅使用 fake；未启动真实麦克风、扬声器、RVC、Ollama、浏览器或系统操作。既有 `src/voice/stt.py` 与 `scripts/rvc_worker.py` 修改仍保留。

## 必须修复

### 1. 播报未静默时 `VoiceSession` 会恢复 STT

调用链：

- `VoiceSession.run()` 调用 `_handle_streaming_turn(text)`，不读取结果。
- `_handle_streaming_turn()` 返回 `None`。
- 无论结果是 `PLAYBACK_FAILED/TIMEOUT`、`CANCELLED/INVALIDATED`、`STOPPED` 或带有已开始播报内容的 `GENERATION_FAILED`，循环随后都会打印“继续监听”并再次调用 `process_voice_input()`。
- `TurnSpeechPlayer` 明确不能硬中断已经进入的 `tts.speak()`；`TIMEOUT` 或失效只说明等待结束/轮次失效，不能证明声音已停止。

因此超时场景可在 `SlowTTS.speak()` 仍被 Event 阻塞时进入第二次 STT。流先产出完整句子、该句进入 TTS、随后流失败/抛错时也有同样风险。

修复要求：

- 给 `VoiceSession` 一个明确的“可否安全恢复监听”结果。只有 `COMPLETED`，或能够证明本轮从未开始/无需播报的结果，才可进入下一次 STT。
- 对 `TIMEOUT`、`INVALIDATED`、`STOPPED` 以及“已有部分播报但生成失败/取消”的情况，退出语音会话或进入不会调用 STT 的安全暂停状态。不要声称底层音频已被硬中断。
- 不要通过读取 `_speech_player` 等私有字段判断；由助手返回的窄契约表达是否可恢复监听。
- 保持生成成功历史不因播放失败/超时而回滚。

### 2. 等待阶段外部取消没有清空所属待播队列

`TurnSpeechPlayer.wait_for_turn(..., cancel_event=...)` 在取消事件置位后直接返回 `INVALIDATED`，但不改变该轮终态；`MikuAssistant._await_stream_playback()` 对该状态也没有调用 `_release_own_speech(turn)`。结果是等待者已经返回取消，而本轮排队句子仍可能继续播放。

修复要求：

- 助手等待路径收到取消/`INVALIDATED` 时，按既有 token 所有权与 CAS 规则释放自己的播报轮，丢弃尚未开始的句子；不得无条件 invalidate 新轮。
- 已经进入底层 `speak()` 的音频仍可能继续，调用方必须据此避免恢复 STT。

## 必须补充的离线测试

1. **真实 `VoiceSession.run()` 循环顺序**：首句在模型完成前进入 TTS，末句 `speak()` 用 Event 阻塞；释放前断言第二次 `process_voice_input()` 没有发生，释放后才发生。现有 `test_next_stt_after_last_speak_with_early_sentence` 只直接调用助手，创建的 `VoiceSession` 与 fake STT 没有参与断言，不能证明 CLI 循环边界。
2. **超时不重开麦克风**：`speak()` 保持阻塞，等待超时后断言 `VoiceSession` 不会第二次调用 STT，并返回安全的会话结果。
3. **部分播报后生成失败不重开麦克风**：流先给出可切句内容并确认 `speak()` 已进入，再给 `FAILED` 或抛异常；在底层音频未释放前不得第二次调用 STT。
4. **等待阶段取消清队列**：用多个已接受句子和 Event 阻塞首句；置位外部取消后断言等待被唤醒、未开始句子不会播放、状态明确，且不影响后来新轮。
5. 保留现有完成、失败、shutdown、系统/搜索单次执行、历史提交与入口传参覆盖；全部线程在 `finally` 释放并 join。

## 文档修正

README、ADR、工程基线和交接状态中“播报结束后才继续听”的说法只能在上述错误路径修复并由 `VoiceSession.run()` 测试证明后保留。交接状态需记录本轮实际测试数量与未验证的真实设备限制。

完成后停止修改，更新 `docs/cursor-handoff-status.md`，等待 Codex Round 2 评审。不要提交或推送。
