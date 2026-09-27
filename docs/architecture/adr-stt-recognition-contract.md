# ADR：STT 结构化识别结果与 Provider 边界

- **状态**：已采纳
- **范围**：STT 结果语义、兼容入口、VoiceSession 连续监听策略

## 问题

旧调用链 `STTEngine.listen()` → `MikuAssistant.process_voice_input()` → `VoiceSession.run()` 只传递字符串。静音超时、无法识别、在线服务错误、麦克风错误、未初始化和内部异常通常都变成 `""`，会话只能统一重试，无法避免永久错误导致的空串忙循环。

## 决策

1. `src.voice.recognition` 定义无设备副作用的 `RecognitionStatus`、不可变 `RecognitionResult` 与 `RecognitionProvider` Protocol。
2. `RecognitionStatus` / 不可变 `RecognitionResult`：只有 `SUCCESS` 能携带去除首尾空白后的非空用户文本；其余状态不得携带可被送入路由/LLM 的正文。`RecognitionResult.coerce()` 对非法类型与绕过构造不变量的破损对象统一收口为 `INTERNAL_ERROR`。
3. 状态明确区分：`SUCCESS`、`SILENCE_TIMEOUT`、`UNRECOGNIZED`、`SERVICE_ERROR`、`DEVICE_ERROR`、`NOT_READY`、`CANCELLED`、`INTERNAL_ERROR`。
4. `STTEngine.recognize()` 是结构化入口。构造时可注入 provider；注入路径不初始化默认 speech_recognition/Whisper 实现。未注入时保留当前实现、配置、Whisper 临时文件清理及 Google 回退行为。
5. `STTEngine.listen()` 继续返回字符串：成功为文本，所有非成功状态为空串；连续模式的旧提示由兼容层保留。结构化入口不打印 UI 文案。
6. `MikuAssistant.recognize_voice_input()` 对上提供结构化结果，并兼容只有 `listen()` 的旧依赖；旧空串只能映射为 `UNRECOGNIZED`，不猜测原因。`process_voice_input()` 保持原字符串接口与“STT 模块未初始化”提示。
7. `VoiceSession` 使用结构化入口：静音超时静默重试；无法识别经 3 秒节流提示后重试；取消正常退出；服务、设备、未就绪和内部错误返回 `stt_error` 并停止监听；这些状态不得进入模式判断、handler、LLM 或 TTS。
8. `unsafe_pause` 继续只表达“播报可能未静默”；STT 故障使用独立 `stt_error`，避免混淆音频播放与识别服务状态。

## 取消边界

Provider 接受可选 `cancel_event`。默认适配器会在打开麦克风前和录音完成后检查；当前 `speech_recognition.Recognizer.listen()` 是阻塞调用，事件不能硬中断已经进行的底层录音。本决策不声称支持即时录音取消、VAD 或 barge-in。

`KeyboardInterrupt` 与 `SystemExit` 不转换为普通识别错误，继续由上层退出路径处理。

## 兼容与迁移

- 生产装配继续以原参数构造 `STTEngine`，默认模型和依赖不变。
- 旧调用方可继续使用 `listen()` / `process_voice_input()`。
- 新会话和测试优先使用 `recognize()` / `recognize_voice_input()`。
- 仅字符串旧 provider 的错误细节已经丢失，兼容适配器不会伪造更具体的状态。

## 替代方案

- 继续使用空字符串并读取日志判断：调用方无法可靠决策，拒绝。
- 让 VoiceSession 捕获 speech_recognition 的具体异常：会把设备适配器细节泄漏到业务层，拒绝。
- 本轮直接更换本地模型或实现 VAD：会把依赖选型、设备性能和新行为混入契约迁移，留待后续里程碑。

## 验证与限制

纯离线测试使用 fake provider、假的 speech_recognition 模块和确定性结果序列，覆盖状态映射、兼容入口、异常传播及连续监听策略。未启动真实麦克风、Google 识别、Whisper 模型、Ollama、TTS/RVC 或系统操作；真实设备体验仍需单独验收。
