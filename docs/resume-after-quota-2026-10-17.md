# 额度停止恢复记录（2026-10-17）

Codex账户primary used90%=剩10%，weekly剩86%。已按用户明确要求停止新开发、真实测试及Cursor。刷新时间UTC 2026-10-18T01:40:44+00:00（纽约10月17日21:40:44，约值以账户查询为准）；未确认刷新不得继续。

最后Cursor：0bf1f996-8603-4d3e-9c28-8b3e7a5848b9 / exec75291；到10%时发Ctrl+C停止，最终工具输出决定是否已退出。无活跃真实模型测试。上轮Cosy worker在root退出后已只读确认没有匹配模型测试进程；Ollama为共享服务，保留既有驻留，不停止。无提交/推送，所有修改留工作区，保留此前HEAD7dff31b。

当前阻塞：CPU staging尚未最终通过，不可跑真实模型/接C2b2b。初版d3604bbb报告236 fake通过，但root alias probe失败；返修当前闭包方案已让alias probe通过，**初始化异常仍会锁泄漏**（_make_patched_load在外层try前）。root独立tmp/codex_cpu_staging_init_probe.py实际False/断言失败，详见docs/codex-review-cosyvoice-cpu-checkpoint-staging.md最新段。当前代码是否被Cursor进一步改动需刷新后先检查，不凭旧摘要定论。EOF test_reader_eof_not_reused_as_ready仍有偶发失败报告，不可用重跑绿替代同步原因调查。

已完成：C2b2a独立协调器1007全量离线通过（106秒、644集合零差）；RVC warm helper失败仍True已修，Codex86项独立通过。三模型真实共存默认30秒及诊断60秒均失败。RVC cuda正确但准备75–83秒；CPU cap4对照无实质改善，未合入。两模型省RVC也60秒失败，采样定位torch CUDA storage.copy_。vendor CPU module加载CUDA checkpoint后再次toGPU有多余往返，Cursor正在窄CPU staging修复，无vendor/pin/精度/音色/超时改动。性能收益未测。

证据：
- 三模型默认：data/test/voice-naturalness/cosyvoice-production-coexist/8c91b8a59344/report.json，Ollama2.063/RVC76.812/Cosy32.063秒False，全部回收。
- 三模型60诊断：data/test/voice-naturalness/cosyvoice-production-coexist-extended/e50897971109/report.json，RVC83.016/Cosy62.047秒False，全部回收。
- 两模型60诊断：data/test/voice-naturalness/cosyvoice-production-coexist-two-models/6f88b5abc6af/report.json，False；当次shutdown reader_alive=True/TFM保留，不能称complete。退出后模型进程只读检查无匹配。
- py-spy0.4.2临时安装tmp/diagnostic-tools，不入生产依赖；tmp/codex_cosy_model_stack.json实际已核对后代worker438696，未采集locals，主线程torch.load→CUDAcopy→CosyVoiceModel.load66行。

刷新后的顺序：查询账户确认>10% → 核实git/CLI已停与最新源码 → Cursor完成scope初始化finally锁释放及EOF确定性同步（禁止真实库/设备/模型/提交推送）→ Codex逐行review+两个root probes+独立相关门禁 → 再两模型诊断及三模型固定短句实测。当前默认Edge/local_tts_worker=false保留。不得直接进入App接线。

后续未完：C2b2b App loader private owner/关闭依赖；C2c有界预取、长句时长预算、播放取消交接；D真实30–60分钟soak（隔离DB与人工设备验收）；跨模块最终review；版本整理与可恢复Git检查点（不push）。用户原始01_warm_companion_rvc.wav基准保留，新生产链路听感问卷未答，不假定验收。禁止读取/修改真实data/memory，生产代码由Cursor写，Codex设计/严格评审。历史644个小测试WAV不泛删。
