# Codex 最终评审：STT 结构化结果与 Provider 边界

## 结论

里程碑通过最终离线验收。七项需求均已实现，旧字符串入口保留，结构化结果不会把失败或取消当作用户输入；`VoiceSession` 对可重试与不可重试状态采用不同策略，未发现剩余阻塞缺陷。

## 评审中发现并修复

1. 新结构化会话最初没有延续旧 STT 的“没听清”3 秒提示节流；已迁移到 `VoiceSession` 并用确定性时钟测试。
2. Assistant 的取消事件最初只由 `STTEngine` 处理；已在 Assistant 边界调用前后补充检查，旧 `listen()` 依赖也不会在预先取消时被调用。
3. 默认适配器补充了录音返回后取消测试，证明不会继续进入转写。
4. 兼容旧版 Assistant 时，字符串 `"STT 模块未初始化"` 最初会被误判为成功语音；已映射为 `NOT_READY`，不会进入 handler/LLM/TTS。
5. Cursor 复审增加 `RecognitionResult.coerce()` 后，首次实现仍会忽略破损 `SUCCESS` 携带的 `error_detail`，且缺字段实例会抛出 `AttributeError`。最终改为通过数据类构造器完整重验三个字段，普通破损输入统一收口为 `INTERNAL_ERROR`，`KeyboardInterrupt/SystemExit` 仍不被捕获。
6. Whisper 缺模型测试已加强为：若代码尝试构造麦克风便立即失败，明确证明 `NOT_READY` 在设备访问前返回。

## 独立验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
Ran 158 tests in 4.435s
OK
```

`python -m compileall -q src tests` 通过；`git diff --check` 通过（仅已有 LF/CRLF 提示）。全部测试为 fake/离线。

## 限制

未验证真实麦克风、Google 识别、Whisper 模型或真实连续语音体验。`cancel_event` 不能硬中断已经阻塞在 `Recognizer.listen()` 中的录音；VAD、唤醒词、回声消除和 barge-in 不在本里程碑范围。

未提交、未推送；没有更换或安装 STT 模型/依赖。
