# Cursor 任务：本地表达型 TTS 评估 Round 1

> 先读取根 `AGENTS.md`、`.cursor/rules/system-safety.mdc`（若存在）以及 `skills/safe-refactoring/SKILL.md`、`skills/architecture-contracts/SKILL.md`、`skills/engineering-quality/SKILL.md`、`skills/collaboration-review/SKILL.md`。保留工作区全部既有未提交修改。只完成本 Round，完成后停止等待 Codex Review。不得提交/推送，不得读取、修改或删除 `data/memory`，不得启动主程序、麦克风、扬声器或任何 WebUI/API，不得执行真实关机/重启/锁屏/注册表写入。

## 目标与当前事实

用户人工试听确认 Edge→Nahida RVC 的音高跨度正常，但整体仍有明显机器人感。本 Round 只建立隔离的 **CosyVoice-300M-Instruct → Nahida RVC** 固定样本诊断回路，用于后续人工 A/B；不得切换生产默认 TTS。

已由 Codex 真实验证：

- 官方 CosyVoice 仓库位于被 Git 忽略的 `vendor/CosyVoice`，提交 `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc`，Apache-2.0；Matcha-TTS 子模块提交 `dd9105b34bf2be2230f4aa1e4769fb586a3c824e`。
- 独立解释器：`vendor/CosyVoice/.venv/Scripts/python.exe`（Python 3.11）；`pip check` 无冲突；Torch `2.3.1+cu121` 已实际识别 RTX 4070 Ti SUPER。
- 模型目录：`vendor/CosyVoice/pretrained_models/CosyVoice-300M-Instruct`；Hugging Face 提交 `706bee1915e9fd1f1214929e2a0509c874cff433`；模型文件总计 `2295746007` bytes；内置说话人含 `中文女`。
- WeText 本地资源：`vendor/CosyVoice/pretrained_models/wetext`，官方 ModelScope Git 提交 `030bb1febce0bd0168549a03e181cd9c1d70c799`。旧 `modelscope==1.20.0` 的匿名 API 当前误报令牌缺失；推理不得依赖网络，应将 `wetext.wetext.snapshot_download` 在 worker 内定向到这个已固定本地目录。
- 模型冷加载约 9.4 秒；FP32 加载时 CUDA allocated 约 1666 MB、reserved 约 1768 MB；进程退出后显存回收。
- Windows 安装的是 CPU `onnxruntime==1.18.0`。CosyVoice 构造时请求 CUDA provider 会警告并回退 CPU；内置 SFT/instruct 路径不需要从参考音频提取 token，当前允许该警告，禁止为此污染或替换主/RVC 环境。

## 设计边界

1. 新增仓库内诊断入口 `diagnose_expressive_tts.py` 与一个窄 worker（建议 `scripts/cosyvoice_eval_worker.py`）。主诊断使用项目 Python；worker 仅由上面的 CosyVoice Python 执行。
2. 默认无参数必须是纯 dry-run：零目录/文件创建、零子进程、零模型/RVC/设备/网络；只打印固定计划、版本约束、输出目录和样本标签。
3. 仅显式 `--execute` 才运行真实本地模型与 RVC。不得支持任意文本、任意 instruct、任意模型目录、任意解释器、任意输出目录或 shell 字符串。所有 subprocess 必须使用参数列表、`shell=False`、固定解析后的路径和有限超时。
4. CosyVoice 与 RVC 严格串行：worker 一次加载 CosyVoice，生成本套全部基础 WAV，写原子 JSON 报告并完全退出；父进程确认正常退出及每个 WAV 非空后，才允许初始化/预热 RVC。不得同时驻留两个 GPU 模型。
5. worker 不绑定端口、不启动 WebUI/API、不播放、不访问麦克风，不导入主程序配置。设置 `PYTHONUTF8=1`、`HF_HOME`、`MODELSCOPE_CACHE`、`MPLCONFIGDIR` 到 `vendor` 内固定目录；通过本地 WeText monkeypatch 禁止文本前端触网。
6. 复用 `test_rvc_conversion.py` 中现有 `build_execute_safety_policy`、`_purge_sample_artifacts`、`_require_nonempty_audio` 以及 `TTSEngine.apply_rvc_model` 的受信 RVC、临时文件所有权、fail-closed 清理与 shutdown 契约。可安全抽取共享 helper，但默认 core/extended 行为、路径、测试必须完全兼容；不得复制第二套宽松 RVC 执行路径。
7. `finally` 必须覆盖 worker 超时/取消/非零退出、基础 WAV 缺失、RVC 中途失败和报告写入失败。超时先 `terminate`，限时等待，再 `kill`；总清理有界。前序完整诊断产物可保留，但当前失败样本不得留下可被误认成功的旧/半成品。
8. 日志和报告只包含固定样本元数据、阶段耗时、文件大小、Torch CUDA peak allocated/reserved、退出码和错误类型；不得写环境变量值、绝对用户路径、异常文本中的敏感内容或模型张量。

## 固定评估套件

使用内置 `中文女`，顺序、label、文本和 instruct 全部固定。建议放在一个可被父入口与测试共享、但 worker 不依赖主程序复杂导入的不可变常量结构中：

```python
(
    ("warm_companion", "早上好，今天也一起慢慢来。", "用温柔、亲近、自然的语气说这句话，语速稍慢，并有轻微的情绪起伏。"),
    ("caring_question", "你听起来有点累，需要先休息一下吗？", "用关心、体贴、轻柔的语气询问，结尾自然上扬。"),
    ("pleasant_surprise", "你真的做到了？！太棒了！", "用真诚惊喜、开心而不尖锐的语气表达。"),
    ("encouragement", "没关系，我们再试一次就好。", "用鼓励、可靠、有陪伴感的语气表达，保持自然停顿。"),
    ("soothing", "不用着急……我会一直陪着你。", "用安慰、平静、柔和的语气表达，语速较慢。"),
    ("serious_calm", "这一步需要谨慎处理，我们先确认清楚。", "用认真、冷静、清晰的语气表达，不要夸张。"),
)
```

## 输出与报告契约

- 固定输出目录：`data/test/voice-naturalness/cosyvoice-instruct/`。
- 每组最终生成 `NN_label_base.wav` 与 `NN_label_rvc.wav`；worker 可使用本次运行私有 staging 目录，成功后再由父进程受控移动/复制到固定目录。
- worker JSON 至少包含 `schema_version=1`、固定 upstream commits/model SHA、speaker、每条 label/合成秒数/音频秒数/bytes，以及进程内 cold load 秒数、总生成秒数、CUDA peak allocated/reserved MB。
- 父级最终 JSON 还应包含 RVC 每条耗时/bytes、worker exit code、总耗时、`success_count`、`expected_count`、`complete`。仅当 6 对非空文件和字段都完整时 `complete=true` 并返回 0。
- 报告采用临时文件 + `os.replace` 原子发布。失败返回非零，不发布 `complete=true`。
- dry-run 输出明确说明：本地 CosyVoice 推理阶段不应联网；RVC 与 CosyVoice 严格串行；不会自动播放。

## 代码质量与离线测试

新增聚焦测试（建议 `tests/test_expressive_tts_diagnostic.py`），至少覆盖：

1. 默认 dry-run 零 `mkdir`、零 `Popen/run`、零 RVC/TTSEngine/网络调用，且六组内容与计划路径准确。
2. argparse 不接受任意 suite/text/style/path；`--execute` 仅能使用固定资产。
3. worker 命令为固定绝对路径列表且 `shell=False`；环境缓存只指向 `vendor` 内；路径解析失败在启动前 fail closed。
4. fake worker 成功后才构造 RVC；事件顺序断言 `worker_exit < rvc_init < first_rvc`。
5. worker 超时、非零、缺文件、零字节、报告 schema/SHA/label 不符时不启动 RVC，返回非零并有界 terminate/kill。
6. fake 全成功生成精确 6 对文件与完整报告，shutdown 精确一次。
7. RVC 中途失败不会把旧目标或半成品算成功，仍 shutdown；失败报告不能标 `complete=true`。
8. worker 单元测试通过 fake CosyVoice/torch/soundfile 验证本地 WeText 定向、单次模型加载、固定 speaker/instruct、原子报告、峰值指标和异常退出；测试不得真实加载 vendor 模型。
9. 现有 `test_rvc_conversion.py` 的 core/extended 全部回归。

不要把环境或 2.3 GB 模型复制进 Git，不要把 `vendor/` 改为已跟踪。不要新增常驻线程/服务，不要改生产 `config/config.yaml` 的默认引擎。

## 验证与交接

运行新增专项、现有语音自然度/RVC 相关回归、全量 unittest、`compileall`、`git diff --check`，以及新入口 dry-run。不得由 Cursor 执行真实 `--execute`；真实 CosyVoice/RVC/显卡验证留给 Codex Review 后执行。

更新 `docs/architecture/adr-expressive-local-tts-evaluation.md`：把外部下载状态更新为已固定并补充版本/许可证/缓存与串行资源边界；更新 `docs/cursor-handoff-status.md` 顶部，列出修改文件、测试和未执行真实链路。完成后停止等待 Codex Round 1 Review。
