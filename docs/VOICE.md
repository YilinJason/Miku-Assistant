# 语音与 RVC 音色

## 架构

```
edge-tts 生成语音 → RVC Worker（常驻，失败默认回退普通音色）→ Nahida 模型 → 播放
         ↓ 多句回复
    分句流水线：第1句合成完即播放，后续句并行准备
         ↓ 失败时
    pyttsx3（Windows 离线应急，无 RVC）
```

主程序 **Python 3.12** 不安装 torch/fairseq；RVC 在独立虚拟环境 `.venv-rvc`（Python 3.11）中运行。

## 性能优化

- **耗时日志**：日志中以 `[TTS计时]` 标记 edge-tts / RVC / 播放各阶段毫秒数
- **RVC Worker**：助手启动时预加载模型，并默认用一秒不播放的测试信号完成首次推理预热；Worker 异常时默认回退普通 edge-tts 音色，避免重复加载模型拖垮系统。仅诊断需要时显式开启 `fallback_on_worker_failure`
- **首段重采样**：可选在内存中预热 librosa；默认关闭，因为不同环境的首次 JIT 时间波动较大
- **资源回收**：Worker 通信与推理都有界等待，持续排空错误输出；主进程异常消失时，Worker 的父进程监护会主动退出
- **设备**：`voice.tts.rvc.device: auto` 会在检测到 CUDA 时使用 `cuda:0`
- **分句流水线**：`pipeline_enabled: true` 时长回复按句播放，降低首句等待

## 持续监听与插话

`assistant.duplex_listening: true` 时，回复任务在受控后台运行，语音会话主线程继续短窗口监听。识别出的文字会在进入路由/LLM 前经过输入门控：

- 与当前助手输出高度相似的内容按回声丢弃；
- “嗯、啊、呃、哈哈”等重复语气音以及极短内容在输出期间丢弃；
- “停、等等、别说了”等控制词和有内容的新问题可以打断；
- 有效插话会取消旧生成、清空该轮未播放句子并停止当前播放器；RVC 正在推理时终止进程，空闲时保留已加载模型供下一轮复用。

这是转写级防误触发，不是声学 AEC 或基于音频模型的 VAD。复杂扬声器回声仍可能被 STT 转成看似有效的新句子；真实使用优先选择耳机。后续声学层可通过新的连续 PCM provider 接入，现有 `RecognitionResult`、`VoiceInputGate` 和 `VoiceSession` 契约无需重写。

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
py test_rvc_conversion.py
```

## 配置

见 `config/config.yaml` → `voice.tts.rvc`：

- `model_path` — Nahida `.pth` 模型
- `index_path` — 特征索引 `.index`（可选）
- `device` — `auto`（有 CUDA 用 cuda:0）、`cpu` 或 `cuda:0`
- `worker_enabled` — 是否使用常驻 RVC Worker
- `worker_infer_timeout` — 单段 Worker 推理等待上限，超时会回收 Worker
- `worker_warmup_inference` / `worker_warmup_timeout` — 是否在启动时完成首次 RVC 推理及其上限
- `prewarm_audio_processing` — 是否预热首段重采样路径
- `fallback_on_worker_failure` — Worker 失败后是否冷启动一次性子进程；默认 `false`
- `subprocess_timeout` — 显式使用一次性子进程时的等待上限

## 常见问题

| 问题 | 处理 |
|------|------|
| RVC 未就绪 | 运行 `setup_voice_env.ps1` |
| 仍是默认晓晓音色 | 运行 `diagnose_rvc.py` 查看错误 |
| CPU 转换慢 | 每句约 10–20 秒，属正常 |
