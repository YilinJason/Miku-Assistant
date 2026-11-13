# Phase 2a — 数值线程预算与概率VAD正式接入

Phase1已由Codex关闭验收，见 docs/codex-review-input-audio-v2-phase1-accepted-2026-11-05.md。用户授权继续总体优化；仍是Cursor写代码/正式测试，Codex评审/独立验证。读AGENTS和相关skills及安全规则。保留dirty工作，禁止commit/push/reset/clean、正式memory/模型设备测试/下载pip。不要改RVC音色/模型/环境。

本阶段只做正式接入；连续录音/NS、双工后续分段，禁止提前宣称完成。当前本机已安装候选依赖和固定模型，Root A/B在数值单线程环境中0/25非人声通过、9/9固定语音通过、500次内存稳定。未经限制NumPy导入的commit增加约500MB，所以不能直接切后端而漏掉启动预算。

## 1. 固定入口的资源预算（不扩大运行时助手权限）

- 新建stdlib-only `src/core/numeric_bootstrap.py`，一个无用户参数的固定函数 `apply_main_numeric_thread_budget()`，只将当前进程的 OPENBLAS_NUM_THREADS/OMP_NUM_THREADS/MKL_NUM_THREADS/NUMEXPR_NUM_THREADS 设为字符串1。所有env写入使用现有 `safe_setenv`；可在函数内部创建只供这个固定可信入口使用的SafetyPolicy(env_modify=True)，**不得返回/复用该policy或传给assistant/assembly/handlers**。这是用户明确批准的启动资源设置，运行时security.permissions.env_modify保持false；不接受任意key/value、不写系统/用户持久环境/注册表。通过ADR明确启动受信任固定动作与助手运行期权限的区别，不以此绕过其他受管控操作。
- main.py仅在调用其main()/作为脚本启动时执行预算，然后才import src.cli.app。import main不改环境、不加载numpy/ORT/模型。保持main()原有无参CLI接口；保留原路径处理和支持入口，不新增后台进程/窗口或shell启动器。
- 不偷偷在Silero库/文本模块import时修改环境。嵌入调用者若要同等资源表现，应显式在导入数值库前调用这个无参固定bootstrap，文档说明。RVC/ASR现有自有worker线程profile继续覆盖它们自己的env，不扩权限。测试启动顺序、精确4键、不会将ENV_MODIFY能力泄露给正常SafetyPolicy，以及import main没有环境写入/数值导入。

## 2. STT拥有概率VAD，默认真实麦克风使用

- InputAudioSettings增加严格backend (`silero_onnx`默认，`webrtc`显式兼容) 和 speech_probability_threshold 默认0.6；保持现有enabled/能量/时长/比例语义，不能盲目增加时长而损害短词。backend-only字段要严格type/range/unknown keys校验；既有WebRTC专用测试显式选择webrtc，不关闭整个guard掩盖失败。默认配置、Config默认、缺省旧配置均选silero_onnx；provider注入仍轻量且不自动开模型。
- STTEngine按需拥有至多一个SileroOnnxVAD实例（不使用全局永久模型缓存）；就绪检查给清晰资产/依赖提示，不开mic、不启动Paraformer/大模型。避免每次检查重复加载ORT或读取完整权重；轻量依赖/资产检查与真实第一次加载失败分开处理。
- 声学判定使用score_pcm，概率门限0.6；按valid_samples累计真实voiced/最长连续样本/比例，分别对应min_voiced_ms180、min_consecutive_ms90、ratio0.12，不计尾帧padding。拒绝在_paraformer_client创建/transcribe/LLM/memory/tool之前，失败fail-closed、稳定提示，不偷偷回退webrtc/云。
- 保留明确的直接AudioData诊断不执行mic VAD，但PCM硬边界不变；支持backend=webrtc且现有布尔frame oracle测试可用。CPU候选不用GPU、torch或新进程/线程。不要把概率说成声纹/ASR置信度。
- 关闭：STTEngine.shutdown/get_shutdown_status/is_shutdown_complete必须包含owned VAD。即使Paraformer client尚未创建，也必须关闭VAD。取消传入caller + shutdown的组合事件；busy/closing/close timeout不伪称完成，可重试、保留引用。关闭期间及之后不得发布迟到识别或新建模型，不能破坏既有Paraformer worker回收。
- 日志启动/后端就绪时一次记录backend、固定model版本/参数摘要、configured mic index；拒绝指标DEBUG/限频，不记录原始PCM/全文/用户私密路径。不得让预检错误误提示安装Google或另一个后端。

## 3. 测试/依赖/文档

- 新增 tests/test_numeric_bootstrap.py、tests/test_silero_stt_integration.py（全fake：真实Mic/InferenceSession阻断）。覆盖缺省旧配置/显式webrtc、拒绝不启动client、概率尾帧计数、正常短句、缺资产/依赖/图错误、取消/关闭pending/retry、没有Paraformer client时仍回收VAD、startup顺序与policy隔离。已有`test_input_audio`、STT/preflight/assembly模块合理更新，无真实设备/模型。
- requirements.txt接入固定CPU requirements-input-audio.txt（不引入torch），README/VOICE和ADR同步模型固定下载来源与校验/本机已准备状态，其他克隆缺资产应有清晰准备指引，不得运行期自动下载。别改Codex评审文档或写成已通过native集成。
- 自验用现有supervisor唯一入口，新label `input_v2_integration_cursor_nov5j`，模块：tests.test_numeric_bootstrap tests.test_silero_stt_integration tests.test_silero_vad_adapter tests.test_input_audio tests.test_stt_contract tests.test_local_asr_final_switch tests.test_paraformer_races_round2 tests.test_assembly_rollback，timeout90。修复失败可新label重跑，保留失败报告。

完成后STOP交Codex审查。不得跑原生probe、真实应用/设备/模型、清理任务或memory库。不得进入连续输入/NS或双工。
