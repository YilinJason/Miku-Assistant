# Cursor 交接状态

## 2026-09-27 音频侧首句延迟

- **状态**：第一阶段 Ollama 优化真机确认有效；第二阶段音频优化离线通过，等待真机复验
- **最新实测**：Ollama 首增量 2.877 秒、服务端总计 3.909 秒；首个 MP3→WAV 1.303 秒、首个 RVC 推理 5.320 秒；插话后的 Worker 冷重启使一次 RVC 总等待达到 12.1 秒
- **修改**：启动时执行一秒不播放的 RVC 推理预热；librosa 重采样预热作为默认关闭的可选项；空闲 Worker 插话时保留，活跃工作立即有界终止；取消代际与互斥重建避免旧任务/新 Worker 竞争
- **结果**：191 项 unittest、compileall、`git diff --check` 通过
- **边界**：第二阶段未启动真实麦克风、扬声器、edge-tts 或 RVC；真实首句改善待用户复验；未提交、未推送

## 2026-09-26 首轮响应延迟

- **状态**：Codex 已按真实日志定位并完成离线优化，等待用户真机复验
- **实测根因**：冷态 Ollama 首增量约 46.2 秒；热态约 7.4 秒；首段 RVC 约 10.3 秒，且原实现每段重复约 1.8 秒 CUDA 探测
- **修改**：Ollama 后台空 chat 预热、30 分钟可配置保活、官方阶段指标日志；RVC Worker 缓存键不再重复设备探测
- **决策**：`docs/architecture/adr-voice-latency-prewarm.md`
- **结果**：183 项 unittest、compileall、`git diff --check` 通过
- **边界**：本轮未发起新的 Ollama 生成、麦克风、扬声器、edge-tts 或 RVC 推理；真实改善尚待复验；未提交、未推送

## 2026-09-26 持续监听与插话

- **状态**：Codex 离线实现与评审通过，等待用户真机调参
- **决策**：默认持续监听；转写后过滤助手回声、重复语气音和极短内容；有效插话取消生成、待播队列、播放器与 RVC Worker
- **兼容**：串行 `safe_to_resume_listen` 路径和旧 STT provider 保留
- **架构**：`docs/architecture/adr-duplex-listening.md`
- **真机步骤**：`docs/manual-voice-runtime-check.md`
- **结果**：174 项 unittest、compileall、`git diff --check` 通过
- **边界**：不是声学 AEC/语义 VAD；未启动真实麦克风、扬声器、Ollama、edge-tts、RVC；未提交、未推送

## 2026-09-26 语音运行时反馈修复

- **状态**：Codex 离线评审通过，等待用户真机联合复验
- **评审记录**：`docs/codex-review-voice-runtime.md`
- **真机步骤**：`docs/manual-voice-runtime-check.md`
- **结果**：168 项 unittest、compileall、`git diff --check` 通过
- **边界**：未启动真实麦克风、扬声器、Ollama、edge-tts、RVC；未提交、未推送

---

- **状态**：已通过 Codex 最终离线验收
- **任务入口**：`docs/cursor-next-stt-milestone.md` / `docs/codex-review-stt-final.md`
- **约束**：未提交、未推送；未启动 main.py / 真实麦克风 / Google / Whisper / RVC / Ollama / 浏览器 / 系统操作；保留全部既有未提交修改（含 STT、RVC Worker、语音流式契约）

## 独立评审结论

对照仓库代码与 `tests/test_stt_contract.py` 自行核验（不信任既有摘要）：

| 区域 | 结论 |
|------|------|
| `recognition.py` | 不变量成立；导入无设备副作用。原先仅靠 `__post_init__`，对绕过构造的破损对象缺少边界收口。 |
| `stt.py` | Provider 注入跳过默认初始化；WaitTimeout/UnknownValue/RequestError/OSError/未就绪/短音频/Whisper 空结果与异常映射正确；`listen()` 字符串兼容；`recognize()` 不打印；取消在开麦前与录音后检查；KI/SystemExit 上抛；校准/短音频/临时文件/回退保留。 |
| `assistant.py` | 旧 `listen` 兼容与预取消正确；`process_voice_input` 契约保持。原先对结构化结果只做 `isinstance`，破损 SUCCESS/带正文失败可能漏过。 |
| `voice_session.py` | SILENCE 静默重试、UNRECOGNIZED 节流、CANCELLED 退出、致命错误单次 STT → `stt_error`；不与 `unsafe_pause` 混用；旧「STT 模块未初始化」不进 LLM。流式播报既有行为保留。 |
| 测试 | 既有覆盖大体到位；缺破损对象收口、SystemExit、Whisper 临时文件删除与缺模型早退的行为断言。 |

## 实际修改

1. `RecognitionResult.coerce()`：非法类型 / 绕过 `__post_init__` 的破损对象 → `INTERNAL_ERROR`，正文清空。
2. `STTEngine.recognize`、`MikuAssistant.recognize_voice_input`、`VoiceSession._recognize_once` 经 `coerce` 收口。
3. Whisper 缺模型时在打开麦克风前返回 `NOT_READY`。
4. 补充测试：coerce 不变量、SystemExit 上抛、Whisper 临时文件清理与早退、`process_voice_input` 字符串契约、破损 SUCCESS 不得进 dispatch 且结果为 `stt_error`（非 `unsafe_pause`）。
5. ADR 补充 `coerce` 说明。

## 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 158 tests in 4.411s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
→ OK (exit 0)

git diff --check
→ OK（仅 Git LF/CRLF 提示，无 trailing whitespace / conflict marker）
```

全部 fake/离线；日志中的构造失败、网络错误、播报失败与识别失败来自预期故障用例。

## 明确未验证

- 真实麦克风、Google 识别、Whisper 模型与真实连续语音体验
- 阻塞 `Recognizer.listen()` 的即时硬取消
- VAD、唤醒词、后台录音、回声消除、barge-in
- 未更换或安装 STT 模型/依赖

## 需 Codex 复审

Codex 已完成上述五项复审。发现 `coerce()` 首次实现未完整复验所有字段：破损成功结果的 `error_detail` 会被忽略，缺字段实例会逃逸普通异常。现已改为通过数据类构造器完整重验 `status/text/error_detail`，并补充对应测试；Whisper 缺模型测试也改为设备构造一旦发生便失败。未发现其他阻塞缺陷。

Codex 最终实际验证：

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -v
→ Ran 158 tests in 4.435s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
→ OK (exit 0)

git diff --check
→ OK（仅 Git LF/CRLF 提示）
```

---

**里程碑已验收完成；未提交、未推送。**
