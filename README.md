# Miku Assistant LLM

Windows 桌面语音助手：本地 **Ollama** 大模型 + **语音对话** + **edge-tts / RVC 音色** + **系统控制** + **浏览器搜索**。

面向中文用户，默认将 edge-tts 合成音经 **RVC** 转为 Nahida 角色音色，支持文本 / 语音 / 交互三种模式。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| **LLM 对话** | 通过 [Ollama](https://ollama.com/) 调用本地模型（默认 `Miku_Assistant-emotion:latest`） |
| **文字转语音 (TTS)** | edge-tts 在线合成 → MP3 转 WAV → 可选 RVC 音色转换 |
| **语音转文字 (STT)** | 麦克风输入，基于 SpeechRecognition + PyAudio |
| **语音模式** | 回复期间持续监听；助手回声、语气词和极短噪声转写会被过滤，有效插话可取消当前生成/播报 |
| **RVC 加速** | 常驻 **Worker** 只加载一次模型；有 NVIDIA GPU 时 **CUDA** 推理；Worker 故障默认回退普通音色以避免重复冷启动 |
| **系统控制** | 关机 / 重启 / 锁屏（弹窗二次确认） |
| **网页搜索** | Google / Bing / Baidu，支持 B 站搜索与打开首页 |
| **安全策略** | 集中式权限控制，注册表写入永久禁止 |
| **日志与清理** | 运行日志写入 `logs/`，临时/测试文件自动清理 |

---

## 系统要求

| 项目 | 说明 |
|------|------|
| **操作系统** | Windows 10 / 11（系统控制、RVC 安装脚本面向 Windows） |
| **Python 3.12** | 主程序 |
| **Python 3.11** | RVC 独立环境 `.venv-rvc`（使用 Nahida 音色时需要） |
| **Ollama** | 已安装并运行（`ollama serve`） |
| **麦克风** | 语音模式需要 |
| **网络** | edge-tts 与 LLM 需要；可选 pyttsx3 作为离线 TTS 应急 |
| **GPU（可选）** | NVIDIA + CUDA 12.x 可显著加速 RVC（如 RTX 40 系列） |

---

## 快速开始

### 1. 克隆仓库

```powershell
git clone https://github.com/<你的用户名>/Miku_Assistant_LLM.git
cd Miku_Assistant_LLM
```

### 2. 安装主程序依赖

```powershell
py -m pip install -r requirements.txt
```

可选离线 TTS 应急（edge-tts 不可用时自动回退）：

```powershell
py -m pip install -r requirements-optional.txt
```

### 3. 安装并配置 Ollama

```powershell
ollama serve
ollama pull Miku_Assistant-emotion:latest
```

在 `config/config.yaml` 中修改 `ollama.model` 为你本地已有的模型名。

### 4. 准备 RVC 音色模型（可选但推荐）

将 Nahida RVC 模型文件放到项目中（**不会随 Git 提交**，需自行准备）：

```text
src/voice/Nahida/
├── nahida-2h-500epoch.pth
└── added_IVF7975_Flat_nprobe_1_v2.index
```

首次安装 RVC 推理环境（只需一次）：

```powershell
winget install Python.Python.3.11
powershell -ExecutionPolicy Bypass -File scripts/setup_voice_env.ps1
```

有 **NVIDIA GPU** 时，安装完成后可升级为 CUDA 版 PyTorch（显著加速）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/upgrade_rvc_cuda.ps1
```

验证：

```powershell
py diagnose_rvc.py
py test_rvc_conversion.py
```

> 若不配置 RVC，助手仍可使用 edge-tts 默认音色（晓晓）朗读。

### 5. 启动助手

```powershell
py main.py
```

指定模式：

```powershell
py main.py --mode interactive   # 默认：文本 + 可切语音 + 可开关 TTS
py main.py --mode text          # 纯文本
py main.py --mode voice         # 纯语音
py main.py --auto-tts           # 启动时强制开启自动朗读
py main.py --stream             # 显式启用增量显示（当前配置默认开启）
```

---

## 使用说明

### 交互模式（默认）

启动后直接输入文字对话。常用命令：

| 输入 | 作用 |
|------|------|
| `help` | 显示帮助 |
| `开启语音模式` / `voice` | 进入持续语音监听 |
| `关闭语音模式` / `退出语音` | 退出语音模式 |
| `tts` | 切换是否自动朗读 LLM 回复 |
| `stream` | 切换增量显示与分句播报（首句完整后即可开始 TTS） |
| `quit` / `exit` / `退出` | 退出程序 |

### 语音模式

进入后对着麦克风说话即可。退出关键词：`退出`、`结束`、`quit`、`exit` 等。

默认启用 `assistant.duplex_listening`：识别后增量显示回复，首句可提前 TTS；回复期间麦克风继续监听。与助手已生成文本相似的回声、重复语气音和极短转写不会创建新轮；说“停”“等等”或提出新的完整问题会取消当前生成/播报并开始新轮。关闭该配置时回到串行兼容模式，等待播报完成后再监听。

当前实现是 **STT 转写后的回声与低信息门控**，不等同于声学回声消除（AEC）或语义 VAD。扬声器外放仍需真机调参，建议优先使用耳机。架构边界见 [`docs/architecture/adr-duplex-listening.md`](docs/architecture/adr-duplex-listening.md)。

预检项：麦克风可用、Ollama 连接正常、模型存在。

默认装配会在后台预载 Ollama 模型，并通过 `ollama.keep_alive` 保持一段时间，减少首轮或长时间空闲后的冷加载。冷启动仍可能较慢；日志中的 `[Ollama计时]` 会分别给出加载、提示词计算、生成和 token/s。`keep_alive` 会持续占用模型显存，可在 `config/config.yaml` 中缩短或设为 `0`。

启用 RVC 时还会在启动阶段执行一次不播放的短推理。插话只终止活跃 RVC 推理，空闲 Worker 会保留，避免下一轮重新加载模型。`voice.tts.rvc.prewarm_audio_processing` 可选预热重采样，因首次 JIT 时间受环境影响较大而默认关闭。

### 自然语言指令示例

**系统控制**（会弹出确认对话框）：

- 「关机」「关闭电脑」
- 「重启」「重新启动」
- 「锁定」「锁屏」

**搜索**：

- 「搜索 Python 教程」
- 「在 b 站搜索 原神」
- 「打开 b 站」

---

## 架构说明

```text
用户输入 (文本/语音)
       ↓
 IntentRouter ──→ 系统控制 / 搜索 / LLM 对话
       ↓
 LLM 回复 (Ollama)
       ↓
 文本清理（去除 Markdown 星号等）
       ↓
 edge-tts 生成 MP3
       ↓
 MP3→WAV（40000Hz，RVC 友好采样率）
       ↓
 RVC Worker（常驻；失败默认回退普通音色，避免逐句冷启动资源风暴）
       ↓
 后处理（高通 + 软限幅，减轻电流感）
       ↓
 分句流水线播放（长回复降低首句等待）
       ↓
 pyttsx3 应急（edge-tts 不可用时，无 RVC）
```

| 组件 | 环境 | 说明 |
|------|------|------|
| 主程序 | Python 3.12 | 不安装 torch / fairseq |
| RVC 推理 | `.venv-rvc`（Python 3.11） | `scripts/rvc_worker.py` / `scripts/rvc_infer.py` |
| TTS 主引擎 | 主环境 | edge-tts |
| TTS 应急 | 主环境 | pyttsx3（`requirements-optional.txt`） |

详细语音文档见 [`docs/VOICE.md`](docs/VOICE.md)。

---

## 配置

主配置文件：[`config/config.yaml`](config/config.yaml)

### 常用配置项

```yaml
ollama:
  model: "Miku_Assistant-emotion:latest"

voice:
  tts:
    engine: "edge-tts"
    pipeline_enabled: true
    rvc:
      model_path: "src/voice/Nahida/nahida-2h-500epoch.pth"
      device: "auto"              # auto | cpu | cuda:0
      worker_enabled: true
      worker_infer_timeout: 45
      fallback_on_worker_failure: false  # 防止逐句冷启动造成资源风暴
      subprocess_timeout: 90
      index_rate: 0.55            # 略低可减少颗粒感/电流感
      protect: 0.5                # 保护清辅音，减轻伪影
      rms_mix_rate: 0.75
      input_sample_rate: 40000
      postprocess_enabled: true   # 输出高通 + 软限幅

assistant:
  auto_tts: true
  stream_reply: true
  duplex_listening: true       # 回复期间继续监听并允许有效插话
  duplex_listen_timeout: 0.8
  duplex_phrase_time_limit: 6.0
  duplex_echo_grace_seconds: 1.0
  duplex_min_interrupt_chars: 2
  duplex_echo_match_ratio: 0.72
  logging:
    file_enabled: true
    file_name: "miku.log"
  file_cleanup:
    tmp_max_age_hours: 1.0
    logs_max_age_days: 1
    test_max_age_hours: 72
```

### RVC 音质调参（电流感 / 伪影）

若转换后仍有轻微电流感或颗粒感，可按顺序微调 `voice.tts.rvc`：

| 参数 | 方向 | 说明 |
|------|------|------|
| `index_rate` | **降低**（如 0.45~0.55） | 检索特征占比过高易出 artifacts |
| `protect` | **略升**（如 0.5~0.55） | 保护气声与清辅音 |
| `rms_mix_rate` | **略降**（如 0.6~0.75） | 混合更多源音量包络 |
| `postprocess_highpass_hz` | 80~120 | 去除低频 hum |
| `postprocess_enabled` | `false` | 对比试听，确认是否为后处理引起 |

修改后**重启助手**；Worker 会按新参数重新加载。

### 安全策略

`security.permissions` 控制子进程、浏览器、文件删除、系统电源等操作。  
**注册表写入在代码层永久禁止**，无法通过配置开启。

---

## 项目结构

```text
Miku_Assistant_LLM/
├── main.py                      # 程序入口
├── config/config.yaml           # 主配置
├── src/
│   ├── cli/                     # 文本/语音/交互会话
│   ├── core/                    # 助手核心、路由、配置、日志、清理
│   ├── llm/                     # Ollama 客户端
│   └── voice/
│       ├── tts/engine.py        # edge-tts + 分句流水线
│       └── rvc/                 # Worker、推理、后处理、参数
├── scripts/
│   ├── setup_voice_env.ps1      # RVC 环境一键安装
│   ├── upgrade_rvc_cuda.ps1     # 升级 .venv-rvc 为 CUDA PyTorch
│   ├── rvc_worker.py            # RVC 常驻 Worker（JSON 行协议）
│   └── rvc_infer.py             # RVC 子进程 fallback
├── docs/VOICE.md
├── logs/                        # miku.log（自动清理）
├── data/tmp/                    # TTS/RVC 临时音频
├── diagnose_rvc.py
└── test_rvc_conversion.py
```

---

## 运行时数据与日志

| 路径 | 内容 | 自动清理 |
|------|------|----------|
| `logs/miku.log` | 运行日志（含 `[TTS计时]`） | 超过 **1 天** |
| `data/tmp/` | `miku_*.mp3/wav` 临时音频 | 超过 **1 小时** |
| `data/test/` | RVC 测试输出 | 超过 **72 小时** |

日志同时输出到终端与文件；单文件上限 10MB，轮转保留 5 个备份。

---

## 故障排查

| 现象 | 处理 |
|------|------|
| Ollama 连接失败 | 运行 `ollama serve`，检查 `base_url` |
| 模型不存在 | `ollama list`，修改 `ollama.model` |
| 语音模式无法启动 | 检查麦克风、PyAudio |
| 仍是默认晓晓音色 | `py diagnose_rvc.py`，确认 RVC 环境与模型路径 |
| RVC 很慢 | 运行 `upgrade_rvc_cuda.ps1`，`device: auto` |
| 轻微电流感 | 降低 `index_rate`，提高 `protect`；见上文调参表 |
| 朗读含星号 | TTS 前已自动过滤 Markdown 符号 |
| 查看耗时 | 日志搜索 `[Ollama计时]`、`[TTS计时]`；先区分模型加载、生成、TTS、RVC 与播放 |

---

## 发布到 GitHub

仓库**不包含** RVC 模型（`.pth` / `.index`），克隆后需自行放入 `src/voice/Nahida/`。

### 首次推送（本地已有代码）

```powershell
cd D:\Miku_Assistant_LLM
git init
git add .
git status   # 确认无 .pth/.index/.venv-rvc 被加入
git commit -m "Initial commit: Miku Assistant LLM"
```

在 [GitHub 新建空仓库](https://github.com/new)（不要勾选 README），然后：

```powershell
git branch -M main
git remote add origin https://github.com/<你的用户名>/Miku_Assistant_LLM.git
git push -u origin main
```

若已安装 [GitHub CLI](https://cli.github.com/)：

```powershell
gh auth login
gh repo create Miku_Assistant_LLM --public --source=. --remote=origin --push
```

---

## 开发说明

### 依赖

- `requirements.txt` — 主程序（Python 3.12，含 librosa / scipy / soundfile）
- `requirements-optional.txt` — pyttsx3 离线应急
- RVC 依赖仅在 `.venv-rvc`，**不要**装入主环境

### 诊断命令

```powershell
py diagnose_rvc.py
py test_rvc_conversion.py

# CUDA 是否可用（.venv-rvc）
.venv-rvc\Scripts\python.exe -c "import torch; print(torch.cuda.is_available())"
```

### 环境变量

| 变量 | 作用 |
|------|------|
| `MIKU_DRY_RUN=1` | 禁用系统电源、子进程、Shell、浏览器（测试用） |

---

## 致谢与第三方组件

- [Ollama](https://ollama.com/) — 本地 LLM
- [edge-tts](https://github.com/rany2/edge-tts) — 微软神经网络 TTS
- [rvc-python](https://github.com/JarodMica/rvc-python) — RVC 推理
- [SpeechRecognition](https://github.com/Uberi/speech_recognition) — 语音识别
- [pygame](https://www.pygame.org/) — 音频播放

RVC 音色模型需自行准备并遵守相应使用条款。

---

## License

本项目源代码许可证请见仓库中的 LICENSE 文件（若尚未添加，请自行补充）。  
第三方库与 RVC 模型遵循各自协议。
