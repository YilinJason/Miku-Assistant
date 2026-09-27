# Codex 评审：语音响应、资源回收与串行监听（已被全双工里程碑更新）

- **状态**：本轮离线验收通过；产品默认监听策略已由 `adr-duplex-listening.md` 更新
- **范围**：用户反馈的响应慢、运行/异常退出卡顿、播报期间错误开麦
- **约束**：未提交、未推送；未启动真实麦克风、扬声器、Ollama、edge-tts 或 RVC

## 已确认原因

历史运行日志显示，旧的非流式语音路径在启动 daemon TTS 线程后立即返回并再次调用 STT。因此 edge-tts、RVC、播放和麦克风同时工作，环境声被反复送入识别。一次样本中，Ollama 等完整回复约 19 秒，RVC 首段约 14 秒、六段流水线约 57 秒。Worker 失败时旧策略还会按句冷启动一次性 RVC 子进程；Worker stdout 的所谓超时仍会卡在阻塞 `readline()`，stderr 也没有持续排空。

## 评审后的实现

1. 语音会话无论是否显示增量，都走可跟踪的播放完成契约。该轮实现按 `safe_to_resume_listen` 串行开麦；后续全双工模式保留此兼容路径，并在回复期间使用独立输入门控持续监听。
2. 默认启用真正的 Ollama 流式响应，记录首段与生成完成耗时，不记录正文。
3. RVC Worker 的 stdout 等待、推理和终止都有界；stderr 持续排空。
4. Worker 故障默认回退普通 edge-tts 音色，避免每个句段冷启动模型；显式关闭 Worker 时仍保留一次性子进程兼容路径。
5. TTS shutdown 会拒绝新任务、停止 pygame、终止 Worker；Worker 监视主进程 PID，硬退出后自行结束。

## 实际离线验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 168 tests in 4.692s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests scripts
→ OK (exit 0)

git diff --check
→ OK（仅 Git LF/CRLF 提示）
```

新增故障覆盖包括：非流式显示期间不提前开麦、非流式异常后不重开麦、真实阻塞管道超时、推理超时终止 Worker、stderr/进程有界回收、默认不冷回退、显式兼容回退、当前父进程检测及 TTS 幂等关闭。

## 尚需真机确认

- 流式首段文字和首段音频的实际延迟。
- Ollama 与 CUDA RVC 同时使用 GPU 时的显存和吞吐表现。
- 全双工更新后的真实播放期间应继续监听，同时不把助手回声或普通环境声创建为新轮。
- 正常退出及关闭终端后 Worker 的实际消失时间。

执行步骤见 `docs/manual-voice-runtime-check.md`。
