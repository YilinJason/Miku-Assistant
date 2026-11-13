# Paraformer 本地 ASR 一期迁移 — 最终验收

2026-11-05。官方 Cursor Agent CLI 编写源代码、测试与 README，Codex 负责设计、逐轮代码评审、独立探针和真实诊断执行。本次整段本地识别迁移已完成；未提交、未推送。此前失败报告保留，不计入通过结果。

## 当前行为

- 默认 `paraformer`，SpeechRecognition/PyAudio 继续采集麦克风，16kHz mono signed16 PCM 通过有界协议交给独立 `.venv-asr` CPU worker。模型只加载一次，一次只处理一个请求；没有自动 Google 回退或运行期下载。
- 已安装 Python3.12.3、CPU torch/torchaudio2.6.0+cpu、FunASR1.4.22、numpy1.26.4、transformers4.49.0。主程序环境未安装 FunASR/torch；RVC/Cosy 环境保持独立。官方模型固定 revision/大小/hash 验证通过，准备报告为 `tmp/paraformer_asr_setup_5eb952939a2c.json`。
- 默认应用 `local_asr_worker:true`、`cloud_stt:false`；裸 SafetyPolicy 仍拒绝该能力，通用 subprocess/Shell/远端模型权限没有放开。专用能力限定解释器、脚本、资产和最小子环境，Windows NativeJob 覆盖真实解释器及子进程。策略是应用边界，并非 OS 沙箱。
- 缺权限/资产/profile 时，在开麦前返回 NOT_READY；文本模式无模型资产也可启动。CPU线程1–4、默认2；冷启动45s、推理15s、关闭2s有界预算。可用物理内存门槛4096MiB、可用系统commit门槛6144MiB，低于门槛拒绝启动，不降低其它模型门槛。
- 退出覆盖 STT/App/未发布候选；未完成的进程或I/O所有者保留，不能启动新worker覆盖引用。关闭/取消抑制过期成功结果，关闭中不再开麦。保留显式 Google/Whisper 与无 shutdown 的旧适配器兼容。

## 独立验证

| 检查 | 结果与依据（报告位于仓库 tmp） |
| --- | --- |
| 最终相关离线回归 | 188项，1项既有跳过；`codex_gate_asr_final_contracts_pass_nov5u_2026_11_02.json` |
| App语音装配/记忆接线 | 44项通过，假模型/临时数据库；`codex_gate_asr_app_memory_integration_nov5w_2026_11_02.json` |
| Codex独立所有权探针 | 18项通过，含死进程但旧reader/writer未结束时拒绝再启动；`codex_gate_asr_final_root_fixed_nov5t_2026_11_02.json` |
| Codex独立诊断判定探针 | 8项通过；`codex_gate_asr_resume_root_verdict_nov5q_2026_11_02.json` |
| 实际配置/工厂轻量检查 | 默认本地、已封存、云STT拒绝、不开设备/worker、不导入torch；`codex_gate_asr_actual_defaults_nov5v_2026_11_02.json` |
| 静态核对 | 148个Python运行文件AST通过；README19个本地链接有效、5个PowerShell代码块语法通过；git diff --check通过（仅LF/CRLF提示）。源hash：`asr_final_source_hashes_20261105.json` |

YAML 语义差异仅7项：STT引擎、4个Paraformer有界参数、local_asr_worker与cloud_stt。TTS/RVC/Ollama/记忆/retention设置未改。正式 `data/memory` 未读取或修改。

## 最终真实 CPU 诊断

`tmp/asr_gate_infer_final_nov5v.json`：20.125s，退出0、NativeJob全部安静、未触发内存/时限中止。子报告 `tmp/paraformer_asr_diagnostic_9971d31fa7374c9592b4b708d6f9585c.json` 的 complete、报告落盘、8个固定片段、重复识别、STT包装、两层shutdown、PID消失、参考文件与共享WAV元数据全部通过。记录的54884/57296进程已退出。

- 冷首次识别11400.24ms，包含导入/模型加载与首段推理；热p50=359.29ms、p95=775.08ms。前一次独立通过运行p50=340.58ms/p95=673.82ms。均不含录音停顿、LLM、TTS/RVC或播放时间。
- 外层10次采样峰值进程树RSS1988.45MiB/private3997.24MiB；前一轮观察到冷private4507.29MiB，因此保留6GiB commit准入。没有在低内存机器上制造真实内存压力。
- 两轮实际通过运行均确认真实进程树与I/O退出；短窗口未见残留，不能据此保证任意负载永不泄漏。
- 828个旧miku WAV精确大小/mtime全部不变。Nahida参考SHA256仍为 `7c4aee8b69428e4bbe3bb773cfc47efae1618efb46b7b80ebaf1465739fe19ba`。旧失败诊断已保留到仓库tmp，未删除未知文件。

## 交付边界

本阶段是整段中文本地识别，没有流式/VAD/热词/标点/说话人附加模型。Edge TTS默认仍联网；仅ASR转为本地。固定素材包含旧Instruct样本，不能从文件标签推断标准答案或声称得到用户口音准确率。真实用户麦克风/声卡、噪声环境和新ASR全栈长测仍需单独体验验收，未冒充已测。

README含主Python3.12的准备、只读检查、诊断、启动和显式Google回滚说明。当前机器已准备好，无需重装环境；重启助手后可进入voice模式测试。没有在途Cursor、模型或测试任务。继续遵守主额度剩余<=10%停止，不自动重置额度。
