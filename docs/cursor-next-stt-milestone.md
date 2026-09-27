# Cursor 下一里程碑：STT 结构化识别结果与可注入 Provider

## 目标

当前 `STTEngine.listen()` → `MikuAssistant.process_voice_input()` → `VoiceSession.run()` 只传字符串；静音超时、无法识别、服务异常、设备异常和内部异常大多都收敛为 `""`。这使连续语音会话无法决定应重试、提示还是停止监听。

本里程碑只建立结构化 STT 契约、兼容入口和 VoiceSession 策略。不更换 STT 模型，不实现 VAD、后台持续录音、随时插话或底层录音硬取消。

开始前读取 `AGENTS.md`、以下技能与相关实现：

- `skills/safe-refactoring/SKILL.md`
- `skills/architecture-contracts/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `skills/collaboration-review/SKILL.md`
- `.cursor/rules/system-safety.mdc`
- `src/voice/stt.py`
- `src/core/assistant.py`
- `src/cli/voice_session.py`
- `src/cli/voice_preflight.py`
- `tests/test_assistant_orchestration.py`
- `tests/test_voice_session.py`

保留所有既有未提交修改，尤其是 `src/voice/stt.py` 的噪声校准、超时、短音频过滤、Whisper 临时文件和提示节流行为，以及已验收的流式播报/轮次契约。

## 1. 结构化结果契约

在 `src/voice/` 下增加独立、无设备副作用的契约模块（建议 `recognition.py`），定义：

- `RecognitionStatus`：至少明确区分 `SUCCESS`、`SILENCE_TIMEOUT`、`UNRECOGNIZED`、`SERVICE_ERROR`、`DEVICE_ERROR`、`NOT_READY`、`CANCELLED`、`INTERNAL_ERROR`（名称可等价，但语义不能合并回空字符串）。
- 不可变 `RecognitionResult`：包含 `status`、`text`、可选 `error_detail`；提供窄工厂方法或等价构造。
- `SUCCESS` 必须有非空文本；失败/取消不得携带可被当作用户输入的正文。非法 provider 结果不得进入 LLM。
- 错误详情用于诊断/简短 UI 提示，不记录原始音频，不引入隐私持久化。

定义运行时可注入的 `RecognitionProvider` Protocol（或等价窄接口）。至少包含结构化识别调用；如 readiness/麦克风预检由 provider 所有，则一并明确。模块导入不得初始化模型、麦克风或线程。

## 2. STTEngine 兼容迁移

- `STTEngine` 增加结构化识别入口（建议 `recognize(...) -> RecognitionResult`），并允许构造时注入 fake/provider；注入路径不得导入或初始化真实 `speech_recognition`、Whisper、麦克风或模型。
- 未注入 provider 时，当前 speech_recognition/Whisper 实现仍是默认适配器；保留当前 engine 配置与回退行为，不更换模型或依赖。
- 现有 `listen(timeout=None, phrase_time_limit=None, continuous=False) -> str` 必须保留，委托结构化入口：成功返回文本，所有非成功结果返回 `""`。
- `listen()` 的现有连续模式用户提示需保持兼容；新的结构化入口本身不直接 `print`，UI 决策交给 `VoiceSession`，避免双重提示。
- 默认适配器映射：录音等待超时 → `SILENCE_TIMEOUT`；无法识别/过短音频 → `UNRECOGNIZED`；Google `RequestError` → `SERVICE_ERROR`；麦克风 `OSError` → `DEVICE_ERROR`；未初始化 → `NOT_READY`；普通未分类异常 → `INTERNAL_ERROR`。
- 可选 `cancel_event` 在打开麦克风前及录音完成后检查并返回 `CANCELLED`。明确当前阻塞的底层 `recognizer.listen()` 不能被该事件硬中断，不得写成已支持即时取消。
- Whisper 的空文本归为 `UNRECOGNIZED`，模型缺失归为 `NOT_READY`，转写异常归为 `INTERNAL_ERROR`；临时文件清理必须保持。

优先渐进改造，避免为了形式大范围搬动 `src/voice/stt.py`，也不得覆盖用户已有 STT 修改。

## 3. Assistant 双入口

- 为助手增加结构化语音输入入口（建议 `recognize_voice_input(...) -> RecognitionResult`），只负责调用 STT、验证 provider 返回值和把逃逸普通异常收口为 `INTERNAL_ERROR`。
- 保留 `process_voice_input(continuous=False) -> str` 的公开兼容行为：正常文本不变；失败仍为空字符串；STT 未初始化时保留现有兼容提示 `"STT 模块未初始化"`。
- 对仅实现旧 `listen()` 的注入替身/第三方对象提供窄兼容适配：非空映射成功，空串只能映射为 `UNRECOGNIZED`；不要猜测不存在的错误原因。
- `KeyboardInterrupt` / `SystemExit` 不得被转成普通识别失败。

## 4. VoiceSession 策略

`VoiceSession` 改用助手的结构化入口，不直接读取 STT 私有字段：

- `SUCCESS`：沿用当前停止词、模式切换、LLM/TTS/流式播报流程。
- `SILENCE_TIMEOUT`：静默重试，不调用 LLM/TTS，不打印错误。
- `UNRECOGNIZED`：打印一次简短“没听清”提示后重试；不得由 engine 和 session 重复打印。
- `CANCELLED`：正常退出本次语音会话，不调用 LLM/TTS。
- `SERVICE_ERROR`、`DEVICE_ERROR`、`NOT_READY`、`INTERNAL_ERROR`：打印与状态匹配的简短提示并安全暂停，不再次调用 STT，避免空串忙循环。为此增加语义准确的会话结果（如 `"stt_error"`）；不要复用专指“播报可能未静默”的 `unsafe_pause`。
- 非成功结果不得增加“有效识别轮数”，不得触发停止词判断、模式切换或任何模型/系统 handler。
- `KeyboardInterrupt` 仍按既有路径退出并取消所属播报；不得吞掉。

保持独立 voice、嵌套 voice、流式开关和 `run_voice_session` 现有入口兼容。外层对新的 STT 暂停结果只需结束当前语音会话，不自动重启。

## 5. 纯离线测试

新增 `tests/test_stt_contract.py`，并按需扩展已有测试。全部使用 fake、patch 或注入 provider，不得打开真实麦克风、下载/加载模型或联网。

必须覆盖：

1. `RecognitionResult` 不变量：成功必须有非空文本，失败/取消不能伪装成用户输入。
2. provider 注入路径不初始化默认引擎，参数透传正确；结构化结果保留。
3. `listen()` 兼容层：成功返回文本；每种非成功状态返回空串；原有连续模式提示不重复。
4. 默认适配器分类：WaitTimeout、UnknownValue、RequestError、OSError、过短音频、Whisper 空文本/模型缺失/异常、普通异常和预先取消。可通过假的 `speech_recognition` 模块/对象验证，禁止设备。
5. Assistant 结构化入口、旧 `listen` 替身兼容、非法 provider 返回、普通异常与 `KeyboardInterrupt` 传播。
6. 真实 `VoiceSession.run()` 连续边界：
   - timeout → 继续到下一次识别，期间不调用 LLM；
   - unrecognized → 一次提示并继续；
   - success → 原流程一次；
   - cancelled → 退出且不调用 LLM/TTS；
   - service/device/not-ready/internal error → 各只调用一次 STT，返回 STT 暂停结果，不调用 LLM/TTS，也不进入忙循环。
7. 保留并通过此前 136 项回归，既有流式播报、取消、历史和安全测试不得削弱。

测试必须使用 Event 或确定性序列，不用 `sleep` 猜测；线程须在 `finally` 释放并 join。

## 6. 文档与交接

- 新增 `docs/architecture/adr-stt-recognition-contract.md`，记录状态语义、provider 所有权、兼容入口、VoiceSession 策略、取消限制和回退方式。
- 更新 `docs/engineering-baseline.md`：将 P5 从“多种失败为空串”更新为结构化路径已解决、字符串兼容入口仍有信息损失；增加新里程碑节和实际测试命令/数量。
- 更新 `docs/cursor-handoff-status.md`，列出修改文件、实际 Python 3.12 离线结果、未验证的真实设备/服务与需 Codex 评审的边界。

## 明确禁止扩大范围

- 不更换或安装 STT 模型/库，不修改默认模型。
- 不实现 VAD、唤醒词、后台持续录音、回声消除、barge-in 或底层即时录音取消。
- 不启动 `main.py`、真实麦克风、扬声器、RVC、Ollama、浏览器或系统操作。
- 不提交、不推送；不撤销任何既有未提交修改。

完成后停止修改，等待 Codex 独立评审。
