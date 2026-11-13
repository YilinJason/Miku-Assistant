# 语音与 RVC 音色

## STT（默认本地 Paraformer）

- **默认引擎**：`voice.stt.engine: paraformer`；推理在 **`.venv-asr`** 持久 worker（CPU torch，与主程序/RVC/Cosy 环境分离）。
- **采集模式**：`voice.stt.input_audio.capture_mode` 默认 **`continuous`**（全双工会话显式开启的一段 owned 采集线程 + 段队列 2 / 12 s / 640 ms 静音收尾 / 3 s 陈旧丢弃）；**`utterance`** 保留 legacy SpeechRecognition/PyAudio 整句路径（≤15 s）。串行语音与诊断 **不** 后台采集。
- **权限**：`security.permissions.local_asr_worker: true`（默认）；`cloud_stt: false`（默认）。裸 `SafetyPolicy()` 仍拒绝 spawn。装配期在固定 layout 校验通过时封存 ASR profile；**安装资产后须重启助手**。
- **未就绪**：缺 venv/资产/封存 profile 时 `is_ready` 为 false；`recognize` 在打开麦克风前返回 `NOT_READY` 与准备提示，**无** Google 回退。
- **准备**：`py scripts/prepare_paraformer.py --check` / `--execute`（主程序 **3.12**）；`py diagnose_asr.py` / `--execute`（固定 WAV 诊断，见 `docs/codex-review-local-asr-real-accepted-2026-11-05.md`）。
- **准入**：冷启动 headroom **4096 MiB 物理 / 6144 MiB commit**（见 `adr-host-memory-admission-2026-11-04.md`）。
- **遗留 Google**：`engine: speech_recognition` + `cloud_stt: true` 才会上传音频到 Google。

## 安静环境误触发（Phase A 声学准入）

默认 **Paraformer** 在 ASR 前启用 `voice.stt.input_audio`（**默认 `backend: silero_onnx`** 概率 VAD + 有界能量；`backend: webrtc` 为显式兼容；见 Phase A ADR 与 `adr-silero-vad-phase1-adapter-2026-11-05.md`）。**Utterance** 路径：当 guard 启用且 `noise_suppression: webrtc`（默认）时，**先于 VAD** 对单句做 WebRTC NS+HP（`pywebrtc-audio==0.2.0`，16 kHz mono，NS level 1，**AEC/AGC 关**）；每句新建 `AudioProcessor`，句末释放。**Continuous** 路径：在 owned 采集线程上对帧做可选增量 NS + Silero 流式 VAD（见 `adr-input-audio-streaming-core-2026-11-12.md`）；`noise_suppression: none` 跳过。真实房间 NS/AEC 效果 **未** 单独验收；默认 **不** 将录音写入磁盘。**两条麦克风采集路径**（`capture_mode: utterance` 与 `continuous`）各在自身采集链路内 **应用一次** NS/VAD（utterance 句级、continuous 流式）；**不** 在 ASR 前二次套 guard。**已信任的** 直接 `AudioData`/诊断 PCM 包装（如 `diagnose_asr.py --execute` 固定 WAV）默认 **绕过** 麦克风 guard，以免重复处理。缺省 `STTEngine(engine=paraformer)` 加载与 YAML 一致的 `input_audio` 默认，注入 `RecognitionProvider` 时仍不强制 guard。

**Silero 模型（固定路径，不运行期下载）**：`vendor/silero-vad/silero_vad.onnx`（2 327 524 字节；SHA256 见 `src/voice/silero_vad.py`）。其它克隆需按仓库说明自行放置并校验；缺文件时 `is_ready`/guard 返回 fail-closed 提示，**不会**回退 Google 或 WebRTC。CPU 栈见 `requirements-input-audio.txt`（主 `requirements.txt` 已 `-r` 引入）。**主入口** `python main.py` 在导入 CLI 前调用 `apply_main_numeric_thread_budget()`（四键线程=1；见 `adr-main-numeric-thread-bootstrap-2026-11-05.md`）；嵌入方须在导入 numpy/onnxruntime 前显式调用同等 bootstrap。兼容 WebRTC 时主环境需 **`webrtcvad-wheels==2.0.14.post1`**。`enabled: false` 为显式关闭，**不会**自动回退 Google。可选 `voice.stt.microphone_device_index` 绑定输入设备；无效索引报错。阈值与 VAD 为 provisional 调参目标；**真实房间**全双工/回声仍待人工验收（见 `docs/manual-input-audio-v2-test-2026-11-12.md`）。

## 架构

```
speech projection → split_for_speech → edge-tts（句级 rate/pitch/volume）→ MP3/WAV → RVC → 播放
         ↓ 多句回复
    分句流水线：第1句合成完即播放，后续句并行准备；每句一次 edge 请求，韵律按句末标点派生
         ↓ 失败时
    pyttsx3（Windows 离线应急，无 RVC；`voice.tts.rate` 仅影响本路径）
```

主程序 **Python 3.12** 不安装 torch/fairseq；RVC 在独立虚拟环境 `.venv-rvc`（Python 3.11）中运行。

edge-tts 会将朗读文本发送到外部服务；句级韵律见 `voice.tts.edge_prosody` 与 `docs/architecture/adr-edge-sentence-prosody.md`。离线测试 ≠ 真实听感验收。

## 性能优化

- **耗时日志**：日志中以 `[TTS计时]` 标记 edge-tts / RVC / 播放各阶段毫秒数
- **RVC Worker**：助手启动时预加载模型，并默认用一秒不播放的测试信号完成首次推理预热；Worker 异常时默认回退普通 edge-tts 音色，避免重复加载模型拖垮系统。仅诊断需要时显式开启 `fallback_on_worker_failure`
- **首段重采样**：可选在内存中预热 librosa；默认关闭，因为不同环境的首次 JIT 时间波动较大
- **资源回收**：Worker 通信与推理都有界等待，持续排空错误输出；主进程异常消失时，Worker 的父进程监护会主动退出
- **设备**：`voice.tts.rvc.device: auto` 会在检测到 CUDA 时使用 `cuda:0`
- **分句流水线**：`pipeline_enabled: true` 时长回复按句播放，降低首句等待
- **RVC 保真（仓库 YAML）**：`rms_mix_rate: 0.68` 减轻响度过度平滑；`filter_radius: 1` 仅在 `f0_method=harvest` 时影响 F0 中值滤波（默认 rmvpe 不使用）；`protect: 0.5` 为关闭，更小值更保护清辅音/气声；代码兼容默认仍为 3 / 0.75 / 0.5；听感需人工 A/B

## 持续监听与插话

`assistant.duplex_listening: true` 时，回复任务在受控后台运行，语音会话主线程通过连续采集控制器消费已切分的音频片段并驱动识别（**不**声称独占物理麦克风设备）。识别出的文字会在进入路由/LLM 前经过输入门控：

- 与当前助手输出高度相似的内容按回声丢弃；
- “嗯、啊、呃、哈哈”等重复语气音以及极短内容在输出期间丢弃；
- “停、等等、别说了”等控制词和有内容的新问题可以打断；
- 有效插话会取消旧生成、清空该轮未播放句子并停止当前播放器；RVC 正在推理时终止进程，空闲时保留已加载模型供下一轮复用。

这是**转写级**插话/回声过滤：门控用 **采集时间区间** 与 **回复活动窗口**（任务 `started_at` 至 `finished_at + duplex_echo_grace`）判断是否视为“输出期”，避免 ASR 解码延迟导致墙钟 grace 失效；参考文本是已生成回复的**保守估计**，**不是**实际播放 PCM 或声学 AEC。与 **NS + Silero 声学准入**（`voice.stt.input_audio`）仍是两条线；**无**扬声器声学消除或声纹辨别。复杂扬声器回声仍可能变成有效转写；建议先用耳机。声学 AEC / 播放参考与误打断恢复为后续独立范围。

相关配置位于 `config/config.yaml` 的 `assistant.duplex_*`。设计和限制见 `docs/architecture/adr-duplex-listening.md`。

应急备用：安装 `pip install -r requirements-optional.txt` 后，当 edge-tts 不可用时会自动回退 pyttsx3。

## 首次安装 RVC 环境

```powershell
winget install Python.Python.3.11   # 若尚未安装
powershell -ExecutionPolicy Bypass -File scripts/setup_voice_env.ps1
```

## 诊断与测试

```powershell
py diagnose_rvc.py
py test_rvc_conversion.py              # dry-run：只打印计划与有效 prosody
py test_rvc_conversion.py --execute    # 真实 edge+RVC，输出 data/test/voice-naturalness/
```

`--execute` 为人工 A/B，不自动播放；不得当作离线 unittest 门禁。

## 配置

见 `config/config.yaml` → `voice.tts`：

- `engine` — 默认 `edge-tts`；可选 `cosyvoice` 本地表达型后端（须 `security.permissions.local_tts_worker: true` 且固定资产可封存）。装配已校验内层 `voice.tts.cosyvoice`，**模型仍在首次语音时懒加载**；不声称已预热。Codex 实测参考：冷首句约 19s，热句 23–43 字含 RVC 约 5.8–8.3s；有界预取与长句预算见后续 C2b。
- `edge_prosody` — 句级 rate/pitch/volume（问/感叹/省略 delta；非法值在构造 TTS 前失败）
- `rvc.model_path` — Nahida `.pth` 模型
- `rvc.index_path` — 特征索引 `.index`（可选）
- `rvc.device` — `auto`（有 CUDA 用 cuda:0）、`cpu` 或 `cuda:0`
- `rvc.worker_enabled` — 是否使用常驻 RVC Worker
- `rvc.worker_infer_timeout` — 单段 Worker 推理等待上限，超时会回收 Worker
- `rvc.worker_warmup_inference` / `worker_warmup_timeout` — 是否在启动时完成首次 RVC 推理及其上限
- `rvc.prewarm_audio_processing` — 是否预热首段重采样路径
- `rvc.fallback_on_worker_failure` — Worker 失败后是否冷启动一次性子进程；默认 `false`
- `rvc.subprocess_timeout` — 显式使用一次性子进程时的等待上限
- `rvc.protect` — 0.5 关闭清辅音保护；诊断可试 0.33 等更小值
- `rvc.filter_radius` / `rms_mix_rate` — 仓库 1 / 0.68；`filter_radius` 仅 harvest 有效，rmvpe 下可忽略

## 常见问题

| 问题 | 处理 |
|------|------|
| RVC 未就绪 | 运行 `setup_voice_env.ps1` |
| 仍是默认晓晓音色 | 运行 `diagnose_rvc.py` 查看错误 |
| CPU 转换慢 | 每句约 10–20 秒，属正常 |
| 韵律过猛/过平 | 调整 `edge_prosody`；若使用 harvest 可试 `filter_radius` 2/3 |
