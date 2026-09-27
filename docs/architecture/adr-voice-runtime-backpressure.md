# ADR：语音轮次背压与 RVC 运行时资源边界

- **状态**：已采纳，待真机复验
- **范围**：语音会话监听/播报互斥、默认流式响应、RVC Worker 超时与退出

## 实测问题

2026-09-26 真机日志证明非流式语音路径在启动后台 TTS 后立即进入下一次 STT。麦克风在 edge-tts、RVC 转换和播放期间反复打开，环境声可能被识别成新输入，并继续创建 LLM/TTS 工作。该路径使用不可追踪的 daemon TTS 线程，异常退出时也缺少统一的播放完成边界。

同一日志还显示：

- Ollama 非流式完整回复在一次样本中约等待 19 秒后才开始 TTS。
- RVC Worker 首段推理曾达到约 14 秒，六段流水线约 57 秒；Ollama 与 RVC 共用 GPU 是待复验的资源竞争因素。
- 旧 Worker 协议把阻塞式 `readline()` 包在 deadline 循环中，实际无法按参数超时。
- Worker 失败后每个句段都会冷启动一次性 RVC 子进程，长回复可能反复加载模型。
- Worker 的 stderr 使用 PIPE 但未持续排空，第三方库日志可能填满管道并阻塞子进程。

## 决策

1. 语音会话无论是否展示增量文本，都使用 `process_text_input_streaming(..., wait_for_playback=True)` 的受管理播报路径。只有取得 `safe_to_resume_listen=True` 才再次调用 STT。
2. 默认启用 `assistant.stream_reply`，尽早展示 Ollama 增量并在完整句子形成后开始 TTS；保留配置与运行时切换入口。
3. 记录 Ollama 首段与生成完成耗时，和现有 edge-tts/RVC/播放计时共同定位延迟，不记录用户正文。
4. RVC Worker stdout 改为后台阻塞读取 + 调用方有界等待；stderr 持续排空。推理超时后终止并回收 Worker 句柄。
5. Worker 失败时默认直接使用 edge-tts 音色，不再逐句冷启动 RVC 子进程。需要诊断旧回退时可显式设置 `fallback_on_worker_failure: true`；`worker_enabled: false` 仍表示主动使用一次性子进程。
6. TTS 关闭会设置停止状态、停止 pygame 播放并终止 RVC Worker；助手先关闭 TTS，再有界等待播报消费者退出。
7. 主程序启动 Worker 时传入自身 PID；Worker 监视父进程，主程序发生无法进入 `finally` 的硬退出时也会结束，避免继续占用 CPU/GPU。

## 取舍

- Worker 故障后的当前回复可能使用默认 edge-tts 音色，以换取可用性和资源上限。
- `playsound` 等第三方阻塞后端不能保证即时停止；当前默认 pygame 路径可以主动停止。
- 超时能终止失去响应的 Worker，但不能保证第三方 CUDA 库在硬崩溃后的所有显存状态；操作系统/驱动级故障仍需进程外观测。
- 默认流式缩短感知等待，不保证模型本身生成或 RVC 推理更快。

## 验证

离线 fake 覆盖非流式显示下的监听/播报互斥、真实有界管道读取、Worker 失败不启动冷回退、显式回退兼容、进程有界终止、父进程存活检测和 TTS 幂等关闭。真实麦克风、扬声器、Ollama 与 CUDA RVC 的联合延迟及资源占用仍需用户复验。
