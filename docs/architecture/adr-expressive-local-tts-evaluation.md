# ADR：本地表达型 TTS 评估边界

- **状态**：Round 1 工程复验通过；等待人工 A/B 听感决策
- **日期**：2026-10-04
- **任务**：`docs/cursor-task-expressive-local-tts-evaluation-round1.md`
- **评审**：`docs/codex-review-expressive-local-tts-round1.md`
- **触发**：Edge 句级韵律与 RVC 保真调优通过，但人工试听仍有明显机器人感

## 已确认现状（Codex 真实验证）

1. Edge `Communicate` 仅提供整段 `rate/pitch/volume`；标点 profile 和 RVC 能保留音高跨度，却不能生成情绪、呼吸、重音和自然停顿。
2. 官方 CosyVoice 位于被 Git 忽略的 `vendor/CosyVoice`（提交 `074ca6dc…`，Apache-2.0）；Matcha-TTS 子模块 `dd9105b3…`。
3. 独立解释器 `vendor/CosyVoice/.venv`（Python 3.11，Torch `2.3.1+cu121`，已识别 RTX 4070 Ti SUPER）；禁止污染主 Python 3.12 与 `.venv-rvc`。
4. 模型 `pretrained_models/CosyVoice-300M-Instruct`（HF `706bee19…`）；固定文件清单合计 **2295746007** bytes（不含 `asset/` / `.cache/`）。
5. WeText 本地资源 `pretrained_models/wetext`（ModelScope `030bb1fe…`）；推理不得依赖网络，worker 内将 `wetext.wetext.snapshot_download` 定向到该目录。
6. 冷加载约 9.4 s；FP32 CUDA allocated/reserved 约 1666/1768 MB；进程退出后回收。Windows `onnxruntime` 为 CPU 版时 CUDA provider 警告可接受，不为此改主/RVC 环境。

## 决策（Round 1 + F1–F9）

隔离的 **CosyVoice-300M-Instruct → Nahida RVC** 人工 A/B，不替换生产默认 TTS：

1. 入口 `diagnose_expressive_tts.py`（项目 Python）+ 窄 worker `scripts/cosyvoice_eval_worker.py`（仅 CosyVoice Python）。
2. 默认 dry-run：零 mkdir/子进程/模型/RVC/设备/网络；仅 `--execute` 跑真实链路。
3. 固定资产与六组 `(label, text, instruct)`；argparse 不接受任意文本/路径/解释器；subprocess 参数列表、`shell=False`、有限超时。
4. **严格串行**：worker 一次加载、写完全部基础 WAV + 原子 JSON 后退出；父进程确认退出码与契约后才初始化 RVC；跨进程单实例锁 + 每次唯一 staging。
5. **F1**：原子 WAV 对 `*.wav.tmp` 显式 `format="WAV"` / `subtype="PCM_16"`，写后 `sf.info` 校验再 `os.replace`。
6. **F2**：worker 环境为 Windows/CUDA allowlist，不继承父进程密钥/代理/`PYTHONPATH`。
7. **F3**：worker `stderr=DEVNULL`（契约只依赖退出码与原子 JSON）。
8. **F5/F6**：加载前实测 git HEAD / HF revision / 清单字节；报告区分 expected/actual；父级核对数值有限性与 WAV bytes/可读性。
9. **F7**：`complete=true` 与 `return 0` 仅在本次 staging 已确认清理之后发布；清理失败覆盖 `complete=false` / `failure_reason=staging_cleanup` 并返回非零。
10. **F8**：staging 清理白名单精确包含 `worker_report.json.tmp`；不扩成任意 `*.json.tmp`。
11. **F9**：将 staging base 精确登记给 `TempFileManager` 后直接作为 RVC 输入；RVC 成功后才复制最终 A/B，不放宽 I/O 所有权门禁，最终文件不作为临时资产回收。
12. 复用 `naturalness_helpers` purge / TrustedRvc；core/extended A/B 兼容。

## 明确不做

- 不把 `vendor/` 纳入 Git 跟踪，不复制 2.3 GB 模型进仓库。
- 不改 `config/config.yaml` 默认引擎；不启动主程序/麦克风/扬声器/WebUI。
- 不由 Cursor 执行真实 `--execute`；真实设备复验由 Codex 执行。

## 后果

- 离线门禁覆盖 F1–F9。Codex 真实执行为 6/6，显存与子进程均完成回收；六组 A/B 已生成。
- 工程通过不能替代主观听感。人工确认明显改善前，不切换生产默认 TTS；若 RVC 明显压缩表现力，应保留 CosyVoice base 或继续调节 RVC，而不是仅看音高数值。
