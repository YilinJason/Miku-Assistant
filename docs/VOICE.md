# 语音与 RVC 音色

## 架构

```
edge-tts 生成语音 → RVC Worker（常驻，失败回退子进程）→ Nahida 模型 → 播放
         ↓ 多句回复
    分句流水线：第1句合成完即播放，后续句并行准备
         ↓ 失败时
    pyttsx3（Windows 离线应急，无 RVC）
```

主程序 **Python 3.12** 不安装 torch/fairseq；RVC 在独立虚拟环境 `.venv-rvc`（Python 3.11）中运行。

## 性能优化

- **耗时日志**：日志中以 `[TTS计时]` 标记 edge-tts / RVC / 播放各阶段毫秒数
- **RVC Worker**：助手启动时预加载模型，避免每句冷启动；Worker 异常时自动回退 `rvc_infer.py` 子进程
- **设备**：`voice.tts.rvc.device: auto` 会在检测到 CUDA 时使用 `cuda:0`
- **分句流水线**：`pipeline_enabled: true` 时长回复按句播放，降低首句等待

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

## 常见问题

| 问题 | 处理 |
|------|------|
| RVC 未就绪 | 运行 `setup_voice_env.ps1` |
| 仍是默认晓晓音色 | 运行 `diagnose_rvc.py` 查看错误 |
| CPU 转换慢 | 每句约 10–20 秒，属正常 |
