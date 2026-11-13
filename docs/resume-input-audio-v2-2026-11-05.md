# 输入链路v2恢复检查点 — 2026-11-12已恢复

**当前已完成本轮输入优化并通过独立验收**：读docs/codex-review-input-audio-v2-final-2026-11-12.md。最终327fake+3公开时序行为+6接线边界通过；真实CPU固定文件控制器/接线与100次生命周期回收通过。当前没有在途Cursor/测试，用户下一步自行做真实麦克风房间测试（docs/manual-input-audio-v2-test-2026-11-12.md），不是额度停止。声学AEC/实际播放参考/声纹/误打断恢复及真实长测未完成，不得认定全语音项目已验收。正式记忆未访问、Nahida保持、未提交推送。下方“正在执行/暂停/R1未修”等为历史状态；勿续跑旧任务。

最新在途（11月12日）：流式核心已独立验收（275fake +6独立边界 +34固定文件/10000连续帧native），见docs/codex-review-input-audio-v2-stream-core-accepted-2026-11-12.md。当前尚未验收/接入的input_capture控制器草稿由新的官方Cursor会话 **1da80a06-0b32-4716-88af-277879b790f2** 返修，任务docs/codex-review-input-capture-r1-2026-11-12.md。前作者027e...曾运行rawunittest，已中断并CIM确认其和测试均退出；不得续跑该旧任务。独立input_v2_capture_contract_nov12af三项均失败，必须先修复，尚未运行真实mic/ASR/播放。所有测试必须通过现有boundedsupervisor，禁止rawunittest和无限fake帧忙循环。下一步独立replay控制器后才接STT/VoiceSession，然后处理采集时间与输出参考。

当前：Phase2a及WebRTC NS切片已独立验收，见docs/codex-review-input-audio-v2-phase2a-accepted-2026-11-12.md、docs/codex-review-input-audio-v2-ns-accepted-2026-11-12.md。最新252fake+真实固定文件NS/Silero/STT(假ASR)通过，501nativeDSP回收。主Python312新增pywebrtc-audio0.2.0，其他模型环境未改。官方Cursor会话027e8438-6142-4d10-a86d-3340dafa3eec正在执行docs/cursor-task-input-audio-v2-streaming-core-2026-11-12.md（仅核心；未接麦克风）。先查真实在途状态与代码再继续；完成后Codex独立review/固定文件和资源验收，才接有界capture和后续双工参考。用户真实房间测试仍未做。以下额度停止和R1未修状态为11月5日历史。

状态：**用户规定主额度剩余<=10%停止；工具最新usedPercent=91（剩余约9%），2026-11-06 04:58 UTC停止。总体任务未完成。** 用户说额度恢复后从本文件继续，先重读额度；不要根据已经过期的resetsAt字段假定恢复。

已发送Ctrl+C中断本轮Cursor CLI会话（shell session97355，第一次工具返回resource_exhausted、再次查询该session不存在）。随后只做退出核实：Win32_Process中无 ExecutablePath 位于 D:\Miku_Assistant_LLM\tmp\cursor-cli-2026.10.22\ 的node.exe，确认该CLI未继续运行。未提交/推送/回退用户修改。

## 当前交付/未验收边界

1. Phase1候选适配器已独立通过：docs/codex-review-input-audio-v2-phase1-accepted-2026-11-05.md。101项相关fake通过；真实固定音频与合成噪声A/B WebRTC放行13/25，Silero0/25，9/9固定语音通过；不是房间/ASR错误率。
2. 单线程受控子进程下Silero冷239ms、1s音频热p50约4.08ms/p95约5.51ms，额外private约36MiB；500次调用内存稳定、session weakref消失。未限数值线程时NumPy首次导入会使commit增加约500MB，已经定位并验证固定四个环境变量=1可消除该额外提交开销。
3. Phase2a已由Cursor编写（默认Silero接入、main.py数值bootstrap、STT拥有VAD和关闭、配置/依赖/docs）。作者自验 `input_v2_integration_cursor_nov5k` 152项通过，但 **未由Codex最终验收**。
4. 最后一轮评审返修 docs/codex-review-input-audio-v2-phase2a-r1-2026-11-05.md 已发给Cursor，尚在执行时因额度停止。**工作区可能包含部分返修或自验结果，必须实际读取，不能按作者摘要/旧门禁认定最终通过。不要启动当前新版做真实设备测试。**
5. 生产配置默认已被Phase2a改为silero_onnx；没有回退覆盖现有改动。应先完成本轮代码审查与独立验证再让用户重启。没有替用户启动真实mic/播放/ASR/LLM/TTS/RVC，正式data/memory未访问。

## 恢复顺序

- 检查额度和git status，保留全部dirty；读AGENTS、任务/相关skills。继续官方Cursor作者，Codex设计/评审/独立执行；不可让Codex直接替代作者改生产代码。
- 检查Phase2a R1实际修改及报告 `tmp/codex_gate_input_v2_integration_r1_cursor_nov5l_2026_11_02.*` 是否存在/完整。最后CLIsession id027e8438-6142-4d10-a86d-3340dafa3eec，路径tmp/cursor-cli-2026.10.22/dist-package/cursor-agent.ps1，composer-2.5。
- R1重点：VAD构造/发布与shutdown竞态；close必须is True、异常保持pending/引用、aggregate状态；analyze通用cancel/disabled在backend dispatch之前；bootstrap测试恢复环境且真实验证CLI首次import时序；就绪日志版本/hash。
- 若尚未修完，按该评审续发Cursor。完成后逐行复查所有权/调用方，独立跑现有scoped gate（具体模块见Phase2a任务，扩展相关voice/assembly/lazy回归）。fake测试必须阻断真实Mic/InferenceSession，线程用barrier并join，不开设备/模型。
- 原生复测用现有 `tmp/codex_scoped_test_gate_2026_11_02.py --probe codex_silero_native_2026_11_05.py`，新唯一label，timeout60；该探针已改成显式调用 **真实bootstrap**，比较WebRTC时显式backend=webrtc。不要再加--numeric-threads-one掩盖真实入口没生效。候选CPU模型小且本探针有host余量门槛/进程private门槛；无mic/播放/大模型。
- 仍需独立验证**正式STT拥有实例+概率门控**的固定音频路径、真实main首次import前资源预算、关闭/回收及配置范围，不仅是standalone候选适配器。必要时由Codex写tmp reviewer probe，沿现有gate白名单监督，无新常驻runner。
- Phase2a闭环后才进入Phase2b连续有界输入流/本地NS，随后Phase3双工采集/实际播放时序与误打断恢复。声学AEC/语义断句/本人声纹各有独立约束，不能用计时器宣称AEC，也不默认采集声纹/长期录音。真实房间复测由用户自己启动进行。

## 固定证据/环境

- 主Python C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe 3.12.3；RVC/ASR/Cosy独立环境未改。
- 主环境新增onnxruntime1.24.1（CPU）、flatbuffers25.12.19、protobuf7.36.2、sympy1.14.0、mpmath1.3.0；numpy2.3.5/packaging25保持。无torch/GPU安装。
- 模型vendor/silero-vad/silero_vad.onnx，2327524字节，commit066409f3d9db45a30d21dfcbf9147826ffdd7052，SHA256=1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3；来源tmp/silero_asset_manifest_20261105.json与LICENSE。
- 原生通过报告tmp/silero_native_20261105_e177fce324c1.json；门禁input_v2_numeric_threads_probe_nov5i，WAV728→728；失败元数据/内存准入报告保留。
- 任务前快照tmp/input-audio-v2-before-20261105/（main.py此前没有快照，保留现状；不要用整仓git diff当本轮全部改动）。初始整仓有大量旧未提交修改。
- 本轮任务docs/cursor-task-input-audio-v2-2026-11-05.md、docs/cursor-task-input-audio-v2-phase2a-2026-11-05.md。研究依据docs/research-real-time-voice-input-strategies-2026-11-05.md。

停止原因仅为用户额度规则，不代表开发完毕或最终效果达标。额度恢复后继续上述未完成闭环。
