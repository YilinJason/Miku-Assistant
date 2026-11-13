# ADR：可选本地表达型TTS后端

- 日期：2026-10-17
- 状态：设计确认，分片实施，默认配置仍为Edge；用户已选原始01_warm_companion_rvc.wav，新生产链路试听尚待反馈
- 背景：CosyVoice→Nahida诊断6/6、有效伪影矩阵24/24通过，但尚无生产入口。用户再次要求继续完成后四项，先推进可独立验证的可选后端；不推断用户已选定A/B/C/D或电流音已经消除。

## 边界

1. `TTSEngine`保持对话/播报兼容，增加可选`cosyvoice`基础合成适配器；不把PyTorch/CosyVoice依赖装进主Python或RVC环境。
2. 独立持久worker只处理有限长度的清理后文本和固定style id，不开放WebUI、端口、麦克风/播放。固定本地资产、独立解释器和脚本经新`local_tts_worker`专用能力；裸策略/缺省/MIKU_DRY_RUN均拒绝，不依赖通用subprocess许可。
3. 父进程持有子进程、stdout reader与临时文件；输出只能写入TempFileManager预创建且身份未变的文件。拒绝任意命令/脚本/模型/路径、链接与替换物。
4. 一次最多1次合成、无内部无限排队，不自动重试故障。按轮次/代际取消并拒绝晚到结果；停止失败保留进程引用、禁止重启。退出/初始化失败有界回收，idle超时退出整个worker释放模型。
5. worker加载前检查可用物理内存与CUDA空闲显存；不足或无法观测则返回资源不足，不抢占其它应用。CUDA默认至少4096MiB可用，主机至少8192MiB可用，PyTorch显存上限不高于4GiB、CPU线程<=4。具体数值由已有3.1GB峰值基线开始，再由真实共存测试调整，不能宣称绝不卡顿。
6. 保留现有Ollama/RVC预热顺序；CosyVoice仅显式选用时懒加载，加载/合成与同TTS的RVC请求严格串行。跨应用GPU共存必须真实测量；显式资源准入阈值不能被下层静默降低。
7. 风格只从固定本地表选，模型正文不能写入任意instruct/命令。日志仅阶段/id/字数/耗时，不落正文/令牌。子环境allowlist+固定本地缓存、禁代理/凭据/PYTHONPATH；仅加载已有固定资产，不自动下载。
8. fallback仅使用已经配置且通过权限校验的后端；取消/资源不足不偷偷转云。生产默认音色与RVC参数待试听选择，不在该可选后端实施阶段切换。

## 切片

- A：资产共用、严格配置与专用能力（无设备/模型加载），离线测试与Codex复审。
- B：worker/client与生命周期，fake验证失败/取消/超时/单实例/资源准入，复审后才真实合成。
- C：装配/TTSEngine兼容接入、队列背压、取消/退出，复审与真实固定文本测试。
  - **C1（已通过）**：`TTSEngine(engine="cosyvoice")` 可选本地适配；`src/voice/tts/cosyvoice_backend.py` 负责固定 `warm_companion`、TMP_DIR owned WAV、`CosyVoiceWorkerClient` 生命周期。`cosyvoice_config` 仅接受 `None` 或 `voice.tts.cosyvoice` 内层 dict。播报为**顺序分句、一次一段**（最多 3200 投影字 / 32 段）；`local_cosyvoice_shutdown_status()` 供关闭协调。构造不预热 RVC、不加载模型；默认仍为 Edge。
  - **C2a（已通过）**：`assemble_default` / `build_tts_engine` 接线：`validate_assembly_voice_tts_config` 先于日志/目录/TFM；`engine=cosyvoice` 时传 `cosyvoice_config=voice.tts.cosyvoice`；共享策略仅在 `local_tts_worker: true` 时尝试 `TrustedLocalTtsProfile(PROJECT_ROOT)`，封存失败回退未封存。`MikuAssistant.shutdown` 在 `local_cosyvoice_shutdown_status().complete` 为 false 时不标记 TTS/共享 TFM 完成。实测参考：冷首句约 19s，热 23–43 字含 RVC 约 5.8–8.3s；不声称模型已预热或无等待。
  - **C2b1（已通过）**：窄 **显式 effectful** prepare API（未接 `assembly` / `resource_coord` / CLI，构造与默认路径仍不自动预热）：`CosyVoiceWorkerClient.prepare`、`CosyVoiceTtsBackend.prepare`、`TTSEngine.prepare_local_tts`。零 synth、零 TFM.create、零 Edge/RVC/播放；复用 B/C1 epoch、`_begin_request`、idle 退休与 backend 待删 owner 两阶段 gate。冷热区别：prepare 只加载/复用 worker，不合成首句音频；热首句仍依赖后续 `speak`/`save`。
  - **C2b2a（当前）**：独立同步协调器 `src/core/local_voice_warmup.prepare_local_voice`（**未**接 App loader / `assembly` / CLI / 预取）：仅 `engine_type=cosyvoice`；配置须 `serialize_gpu_warmup=True`、`warmup_order=ollama_then_rvc`、Ollama 启动预热开启；`require_rvc=True` 时还须 RVC 预热开启；不支持组合零副作用拒绝。顺序：本轮 Ollama **fresh** 空请求成功（原子`begin_fresh_warmup`获取/复用进行中generation，公开不可变快照必须对应同代且成功；代际替换立即失败，不用先start后snapshot或`model_touch_succeeded`冒充）→ `prepare_rvc`（可 `require_rvc=False` 跳过）→ `prepare_local_tts`。`OllamaClient.start_warmup(..., restart=False)` 默认幂等不变；`TTSEngine.prepare_rvc` 在 `worker_enabled` 时 `warm_rvc_worker` 失败须 `_rvc_ready=False`；显式首次推理预热开启时warm失败或ready不真实也必须False。C2b2b 再接 App owner/关闭。
- D：隔离库长测/资源遥测与人工语音验收，最终跨模块评审、版本整理。

## 限制

用户已选原始`01_warm_companion_rvc.wav`作为Nahida音质基准（见`docs/voice-preference-baseline.md`）；未选择A/B/C/D矩阵。新生产链路输出尚待人工试听反馈。新可选后端通过工程门槛不代表新输出音质或30–60分钟设备长测已通过。真实data/memory不作为测试库，禁止清理或读取。


## 共存验收状态（2026-10-17）

1007离线通过后真实共存仍失败：Ollama2.063秒、RVC76.812秒、Cosy32.063秒失败，零合成，全部回收。CPU cap4对照未改善（74.969→74.453秒），未合入该调参。RVC准备结果helper fail-open已由Cursor修正，Codex86项独立通过。正在一次已有extended-cold60诊断采集加载阶段；生产30秒默认不改，未接App owner、未进入预取/长测。最终结论以具名评审与交付清单顶部为准。

### CPU checkpoint staging（窄切片，待 Codex 复审）

- **根因（代码确认，非性能验收）**：pinned vendor `CosyVoiceModel.load` 以 CUDA `map_location` 调用 `torch.load` 载入 `llm.pt`/`flow.pt`/`hift.pt`，再 `load_state_dict` 到仍在 CPU 的 module，最后 `module.to(CUDA)`，形成多余 GPU→CPU→GPU 与临时显存；py-spy 栈见 `tmp/codex_cosy_model_stack.json`（只读依据，不入依赖）。
- **实现**：`src/voice/cosyvoice/checkpoint_loading.py` 在 `scripts/cosyvoice_runtime_worker._construct_automodel` 构造 AutoModel 的有限作用域内，仅对**本线程**、**model_dir** 下上述三文件、**`weights_only=True` 且原 `map_location` 为 CUDA** 的 `torch.load` 改为 `cpu`；`spk2info.pt`/未知路径/文件对象/CPU 加载/未显式 `weights_only=True` 不改。重入或并发第二作用域 `CpuCheckpointStagingError`；其它线程与其它加载原样委托。不改 vendor pin、资产、超时、默认引擎与权限。
- **未声称**：真实构造耗时或共存已通过；性能收益须 Codex 复审后实测。

### CUDA non-blocking module transfer（窄切片，待 Codex 复审）

- **假设（未实测）**：CPU staging 后 vendor `CosyVoiceModel.load` 在 Windows 上仍以阻塞式 `module.to(CUDA)` 逐 tensor 同步；PyTorch `non_blocking=True` + 单次 `torch.cuda.synchronize` 可能减少主机等待（见官方 pinmem/nonblock 教程），**收益未在本仓库测量**。
- **实现**：`src/voice/cosyvoice/model_loading.py` 在 `scripts/cosyvoice_runtime_worker._construct_automodel` 内与 CPU staging **嵌套**；仅替换 pinned **`CosyVoiceModel` 基类**（`type(self) is CosyVoiceModel`）的 `load`，且仅当 CUDA `self.device`、**恰好三个位置参数且无 kwargs** 且为 `model_dir` 下 `llm.pt`/`flow.pt`/`hift.pt` 时走优化路径：`torch.load(map_location="cpu", weights_only=True)`/`load_state_dict(strict=True)`/hift `generator.` 前缀剥离与 vendor 一致，三模块 `.to(device, non_blocking=True).eval()`，末次传输后单次完成 `cuda.synchronize`；拷贝/strict/eval/完成同步失败则构造失败。仅**完成同步尚未尝试**时的失败路径做至多一次清理 `synchronize`，不重复完成同步等待，清理同步抛错不掩盖原异常。其它线程/子类/CPU/关键字或混合参数/路径 → 捕获的原始 `load` 以原 `*args/**kwargs` 一次委托；作用域外保留的闭包别名永久委托其捕获的 original。无全局 `Module.to`/`Tensor.to` patch、不改 vendor。
- **测试**：`tests/test_cosyvoice_cuda_nonblocking_model_load.py`（fake torch）；门禁 `tmp/cursor_cpu_checkpoint_staging_validation_gate.py` 已纳入该模块。
- **未声称**：真实 AutoModel 构造耗时、默认 30s 共存或 py-spy 栈变化；须 Codex 受控实测后再结论。

**2026-10-18 撤回（`docs/cursor-task-withdraw-cuda-transfer-2026-10-18.md`）**：Codex 受控实测未证明该适配器相对「仅 CPU checkpoint staging」有保留价值；已删除 `model_loading.py` 与专用测试，runtime worker 恢复为 staging-only 构造。上文实现/测试描述保留为历史记录；**不得**据此声称 non-blocking 路径仍活跃或已验证加速。CPU checkpoint staging 小节仍有效。
