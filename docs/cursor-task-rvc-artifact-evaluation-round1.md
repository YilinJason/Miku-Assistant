# Cursor 任务：RVC 电流/金属伪影参数隔离评估 Round 1

- **日期**：2026-10-16
- **执行者**：Cursor Agent（实现与离线测试）；真实 `--execute` 仅由 Codex 复验后运行
- **状态**：READY
- **前置依据**：根 `AGENTS.md`、`skills/architecture-contracts/SKILL.md`、`skills/engineering-quality/SKILL.md`、`skills/collaboration-review/SKILL.md`、`.cursor/rules/system-safety.mdc`
- **相关入口**：`diagnose_expressive_tts.py`、`src/voice/naturalness_helpers.py`、`src/voice/rvc/{infer,worker,converter,vc_params,postprocess}.py`

## 已确认事实

1. 用户人工试听确认 CosyVoice → Nahida RVC 比 CosyVoice base 更自然，但 RVC 有明显“电流音”。
2. 真实六状态报告 6/6；RVC 输入/输出、资源回收和安全所有权已经通过，不是文件损坏或并行显存问题。
3. 六段 RVC 的近静音 RMS 约 `-62~-68 dBFS`，8–20 kHz 能量很低；现象不像持续白噪/硬件 hum，更可能来自有声段的检索颗粒、相位/声码器伪影或 F0 毛刺。
4. 当前生产 YAML：`index_rate=0.55`、`filter_radius=1`、`protect=0.5`、`rms_mix_rate=0.68`、`f0_method=rmvpe`。`filter_radius=1` 是此前为保留抑扬而降低的值；index 偏高与较少 F0 平滑都可能放大金属感。
5. 不应先加入激进低通/降噪。它可能掩盖问题并损伤齿音、气声和 Nahida 音色；本轮先做参数归因。

## 目标

新增一个**固定输入、固定 profile、默认 dry-run** 的 RVC 伪影诊断入口，复用上一轮六个 CosyVoice base WAV，只运行 RVC，产出可人工试听的参数矩阵。不得改生产默认参数；先用真实 A/B 选 profile。

## 固定输入与输出

- 输入只允许：`data/test/voice-naturalness/cosyvoice-instruct/01..06_*_base.wav`，label 必须与 `scripts/cosyvoice_eval_suite.py::SAMPLE_CASES` 一致。
- 输入缺失、空、不可读、采样率/时长非法时，在启动 RVC 前 fail closed。
- 输出固定：`data/test/voice-naturalness/rvc-artifact-eval/`。
- 不覆盖或删除 CosyVoice base、上一轮 RVC、上一轮 `report.json`。
- 最终文件名必须同时包含固定 label 和 profile id；报告列出 profile 参数、耗时、bytes、采样率与时长。

## 固定 profile 矩阵

基线 `idx055_fr1` 引用上一轮现有 RVC 文件，仅用于报告/试听对照，不重跑。

真实新增 profile：

| profile id | index_rate | filter_radius | 目的 |
| --- | ---: | ---: | --- |
| `idx025_fr1` | 0.25 | 1 | 只降低检索混合，隔离颗粒来源 |
| `idx055_fr3` | 0.55 | 3 | 只增加 F0 平滑，隔离音高毛刺 |
| `idx025_fr3` | 0.25 | 3 | 平衡候选 |
| `idx000_fr3` | 0.00 | 3 | 无索引控制组，判断索引是否为主因 |

其它参数固定沿用有效配置：`pitch=0`、`protect=0.5`、`rms_mix_rate=0.68`、`f0_method=rmvpe`、现有轻量后处理。禁止 profile 注入任意路径/数值。

## 实现要求

1. 新入口建议 `diagnose_rvc_artifacts.py`。CLI 仅接受 `--execute`；无参数必须零 mkdir、零写文件、零模型/子进程/GPU/网络/设备，只打印固定计划。
2. 通过 `build_execute_safety_policy`、`SafetyPolicy`、共享 `TempFileManager` 和现有 RVC Worker 调用；不得直接运行 `scripts/rvc_worker.py` / `rvc_infer.py`，不得绕过 RVC 能力白名单。
3. 跨进程单实例锁；每次运行唯一 staging。每个固定 base 先复制到本次 staging，再 `adopt_existing`，不要把持久 base 登记为临时文件。
4. profile 严格串行：一个 profile 的六段完成并 `shutdown(stop_rvc=True)`、确认 Worker 退出后才能构建下一个 profile。禁止多 profile 并发占 GPU，禁止失败后无限重试或冷启动风暴。
5. 每个 RVC 临时输出复制到最终输出后仍由 TempFileManager 回收；最终评估文件不得长期登记。失败时清理本轮半成品，不删除先前完整 profile。
6. 最终 `complete=true` 报告必须在全部 profile 完成、staging 已清理、Worker 已停止后原子发布。失败报告为 `complete=false`，带固定阶段/label/profile id，不记录任意异常文本或完整用户路径。
7. dry-run 和日志只包含固定非敏感样本/参数；不启动主程序、Ollama、CosyVoice、麦克风、扬声器、浏览器；不自动播放；不访问 `data/memory`。
8. 不改 `config/config.yaml`、生产 `TTSEngine` 默认值或现有后处理。本轮只建立可复验的选择依据。

## 测试门禁

- dry-run 零副作用；拒绝任意额外参数。
- 固定输入集合和四 profile 参数完全匹配，bool/NaN/越界不能混入。
- 输入缺失/损坏时零 RVC；持久 base 永不登记或删除。
- RVC 收到已登记的 staging copy；最终文件不被 TempFileManager shutdown 删除。
- profile 严格串行；前一个 Worker stop 在后一个 init 前；中途失败不进入后续 profile。
- lock busy、adopt/copy/RVC/report/cleanup/shutdown 失败均非零、非 complete、锁释放、无 staging 泄漏。
- baseline 只读引用，不重跑、不删除。
- 专项、RVC/自然度相关回归、全量 unittest、`compileall`、`git diff --check`、dry-run。

## 交付边界

- Cursor 不运行真实 `--execute`，不读取/修改/删除 `data/memory`，不提交、不推送。
- 完成后更新 `docs/cursor-handoff-status.md` 顶部，列明修改、真实退出码和未运行项，然后停止等待 Codex Review。
