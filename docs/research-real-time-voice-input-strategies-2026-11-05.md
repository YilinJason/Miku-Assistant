# 实时语音产品输入策略对比与 Miku 优化建议

调研日期：2026-11-05。范围：官方产品/API 文档、开源项目源码，以及当前工作区的只读审计和本地合成信号实验。

结论：当前仍需优化。优先补齐 **输入降噪、概率型人声准入、连续帧状态管理及真实噪声验收**。目前没有证据要求先更换 Paraformer。语义断句、回声消除、背景说话人隔离分别解决不同问题，不应当作同一个开关。

本次没有调用 Cursor 修改生产代码，没有下载或启用新模型、没有上传用户录音、没有启动麦克风/播放/ASR。仅新增研究文档和 tmp 中的有界实验探针；现有生产源码与上轮最终 hash 清单比较，改动数为 0。

## 1. 新实验揭示的实际缺口

上轮静音、低幅白噪声、低幅嗡声等负样本确实通过，但这些样本整体偏简单，很多低于能量下限 300，不能代表会触发录音的真实噪声。这是上轮效果验收的覆盖缺口，代码/契约检查通过不能等价为用户环境防噪通过。

本次用现有监督器执行 `tmp/codex_voice_input_research_2026_11_05.py`：25 个确定为非人声的合成片段（白噪声、60Hz 嗡声、440Hz 单音、180ms 噪声突发、3次30ms噪声点击），覆盖 100/600/2000/6000/12000 五档幅度；另有 3 个固定语音、原幅度和 0.1 倍幅度的 6 个有限正对照。

| 当前程序的独立声学门控 | 非人声总数 / 被放行 | 峰值帧 RMS 超过最低门限300的子集 / 被放行 | 固定语音通过 |
| --- | --- | --- | --- |
| 当前 mode 2，其余默认参数 | 25 / **13** | 19 / **11** | 6 / 6 |
| 仅改 mode 3，其余相同 | 25 / **11** | 19 / **10** | 6 / 6 |

这些是 **VAD 放行数，不是 ASR 误转写率，也不是用户房间错误率**。没有启动 Paraformer、未测实时采集门限随环境的变化；超过下限300不保证超过某时刻的实际门限。实验只证实：即使通过现有声学准入，输入仍可能不是人声，单纯换 mode 3 不足以闭环。

更具体的例子：总长90ms的三次点击片段，被当前 VAD 统计为330–360ms的 voiced 帧，达到180ms总人声、90ms连续人声和0.12比例要求。这与 WebRTC 的延续/平滑机制相符：boolean输出并非独立且高置信的人声标签。源码中存在 over_hang 和噪声估计状态，见 [WebRTC VAD 源码](https://raw.githubusercontent.com/daanzu/py-webrtcvad-wheels/release/cbits/webrtc/common_audio/vad/vad_core.c)。不能把“累计 True 帧时长”直接理解成真实人声时长。

证据：`tmp/codex_gate_voice_input_market_research_nov5p_2026_11_02.json`（正常完成，0.5秒，WAV728→728，无增删）；明细 `tmp/voice_input_research_20261105_60e70917e621.json`。负样本是模拟信号，不称作真实风扇/键盘录音；正对照是既有RVC音频，不代表本人轻声/口音表现。

日志显示最近一次 Paraformer 初始化在23:09，晚于当前 STT 源码22:59的修改时间，故不能直接将报告归因于“没有重启”。但日志缺少运行版本、实际配置摘要与每次声学放行依据，仍无法从日志定位用户某次异常的原始信号。

## 2. 市面可核实的策略

消费端 ChatGPT、Gemini 等产品没有完整公开全部内部输入链路。以下明确区分商业 API、Agent 平台、框架及本地助手；不把 API 文档当作消费端内部实现，也不宣称它们没有误触发。

| 方案类型 | 官方公开的机制 | 对 Miku 的启示 |
| --- | --- | --- |
| **OpenAI Realtime API** | 输入降噪先于 VAD/模型；按近讲/远讲麦克风配置。VAD 的起止检测与自动触发回复可以分开控制。语义 VAD 主要判断是否说完。 | 把前处理、人声准入、断句和回复触发分层。参考 [输入降噪文档](https://developers.openai.com/api/reference/resources/realtime/client-events)、[VAD文档](https://developers.openai.com/api/docs/guides/realtime-vad)。 |
| **Gemini Live API** | Proactive Audio 支持按交互相关性选择回应或保持安静，涉及对外部闲聊/背景声的响应控制。 | “检测到声音”和“这句话需要助手回应”应有不同的决策层；不把它当本人身份认证。参考 [Google官方说明](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/configure-gemini-capabilities)。 |
| **ElevenLabs Agents平台** | 对话等待、插话、背景语音过滤分别配置；另有检测第二说话人后提示换安静环境的功能。后者为Enterprise能力且不检测所有环境噪声。 | 分清非人声噪声、其他人说话、真实插话。参考 [Conversation flow](https://elevenlabs.io/docs/eleven-agents/customization/conversation-flow)。 |
| **LiveKit实时框架/服务** | 有Silero VAD、独立插话时长/词数门槛、误打断后恢复；降噪与竞争说话人隔离是不同前处理选项。部分增强能力涉及云服务/许可。 | 不把普通发言门槛与打断播报的门槛混为一谈；一次疑似噪声不应永久取消回复。参考 [轮次调优](https://docs.livekit.io/agents/logic/turns/tuning/)、[Silero插件](https://docs.livekit.io/agents/logic/turns/vad/)。 |
| **Pipecat开源框架** | 带时间戳的连续音频缓冲、Silero VAD与Smart Turn断句分层。 | 建立连续帧与采集时间契约；语义轮次模型是后续自然度优化，不是当前噪声问题的首要修复。参考 [官方Smart Turn说明](https://github.com/pipecat-ai/docs/blob/main/api-reference/server/utilities/turn-detection/smart-turn-overview.mdx)。 |
| **Home Assistant Assist本地助手** | 人声活动检测后进行唤醒词和后续指令处理，部分设备在本机先做唤醒。 | 可提供显式激活/闲置后退出活跃会话的可选模式，减少常驻误触发；陪伴模式不必每句要求唤醒。参考 [官方唤醒架构](https://www.home-assistant.io/voice_control/about_wake_word/)。 |

以上是借鉴设计，不要求把本地音频发送给这些商业服务。本次网页调研没有调用它们的语音API。

## 3. 当前代码与上述策略的差距

当前默认链路：

`系统默认/指定麦克风 → 有界音量起句 → SpeechRecognition整段采集（固定1.5秒停顿） → WebRTC整段VAD准入 → Paraformer整段转写 → 文字门控 → 回复/记忆`

- `src/voice/stt.py` 的真实Paraformer麦克风入口确实传 `apply_acoustic_guard=True`；已查到的默认绕过入口为受信任的 `diagnose_asr.py` 直接AudioData诊断，不是CLI会话入口。Google/Whisper旧后端不自动获得这层VAD；日志当前使用Paraformer。
- 项目没有显式接入输入NS（噪声抑制）/声学AEC。Windows、驱动或麦克风固件的既有处理本次未核实，不能把PyAudio输入一概说成完全未经处理。
- 当前VAD只输出整段接受/拒绝与True帧统计，不给稳定的人声概率或有效语音区间；被接受后，整段原PCM仍送ASR，噪声没有被去除。
- 每段重建WebRTC实例避免跨片段状态污染，但同时重置噪声估计。连续流方案可在同一会话中保留适当状态；是否改善本环境尚待A/B，不能无条件跨设备/跨会话共用模型状态。
- 固定1.5秒静音收尾主要影响等待与断句；把它改短并不能解决噪声是否为人声。
- Paraformer worker目前只提取text，接口没有已验证的no-speech probability/置信度契约。不能凭空增加一个“ASR置信度0.8”配置，或把有文字当成高可信。
- 双工仍按转写文字相似度防回声；已知录音/ASR返回晚于回声窗口的漏洞独立存在。真正AEC需要播放参考与时间对齐。

## 4. 推荐的本地优化路线

### 第一阶段：补证据并选型，先不换ASR

1. 启动时记一次实际引擎、输入设备/配置来源、门控后端、参数和源码版本。每轮用turn/capture id关联音量、拒绝原因、人声跨度、播放重叠和耗时；日志有界、限频，默认不写PCM、全文或声纹。
2. 在同一组固定数据上A/B：现有WebRTC、**Silero VAD CPU ONNX**、可选现有FunASR体系的FSMN-VAD。必须包括超过触发门限的噪声、多种SNR、真实短中文/轻声、夹杂噪声的讲话、音乐/背景讲话和播报尾音。
3. Silero是优先候选，理由是公开CPU/ONNX路线及实时框架采用，不是已证明在这台机器最好。其项目方[质量评测](https://github.com/snakers4/silero-vad/wiki/Quality-Metrics)显示噪声数据上与WebRTC存在差异，但属项目方数据，不能替代本机独立评测。[项目说明](https://github.com/snakers4/silero-vad)给出ONNX部署路线。
4. 当前主Python没有torch/onnxruntime，ASR环境也没有onnxruntime。本次没有安装它们。官方Silero Python utils入口导入torch；实施时须检查实际依赖路径，采用只需CPU ONNX的窄适配器或受控隔离，避免为轻量门控把另一套完整PyTorch模型栈装入主进程。模型与运行时需固定版本/校验，禁止运行时hub下载。

### 第二阶段：接入连续输入与前处理

- 一个语音会话持有一个明确关闭的输入流；连续短帧处理，预录缓冲、待识别段和队列均有硬上限。切模式/取消/退出释放流与模型状态；满载时明确丢弃过期片段，不能堆积历史录音。
- 对本次“安静时噪声”先评估本地NS，再做人声判断。可评估 [RNNoise](https://github.com/xiph/rnnoise) 或经过许可/构建验证的WebRTC音频前处理。它们不是pip加一行就有的AEC；强降噪可能损伤轻声、辅音，必须联测ASR。
- 概率型VAD采用起止分离门槛、连续证据和平滑状态；只向ASR提交有效语音及必要前后缓冲。短词“嗯/停/等一下”单独验收，不直接照搬其他平台插话500ms等默认值当普通起句门槛。
- 默认CPU、单实例、限制推理线程。测新增RSS/private/CPU/冷启动与热时延，通过资源准入后再启用。不能把模型文件几MB说成运行只占几MB，更不能在两个环境重复加载整套ASR来判断噪声。

### 第三阶段：双工与自然轮次

- 按实际采集与实际播放时间处理回声，而非按转写返回时刻清空参考；扬声器模式评估AEC，耳机模式不凭空增加处理负载。
- 分开“疑似插话”“确认新轮次”“非打断式短应答”。具备音频暂停/恢复契约后再引入误打断恢复，保持最多一轮生成与有界待处理输入。
- 语义断句作为可选后续能力，目标是减少固定等待和抢话，不作为非人声过滤的替代。
- 需要只接受本人时，另行设计近讲/按键/激活窗口及可选主动声纹注册。语音隔离或声纹都不是系统敏感操作的授权凭证。

## 5. 下一次验收标准

先验固定样本的误接受与误拒绝，再验真实房间按时间统计的误触发次数；另记中文字符错误率、短句漏识别和端到端P50/P95。两个核心目标同时看：**噪声不触发，真实轻声能识别**。较严阈值只让程序“谁都听不到”不算通过。

运行期间观察长时间CPU/RSS/private趋势与退出后流/线程/进程回收。拒绝的音频不得进入LLM、工具或记忆；通过音频准入的文本也不等于已认证本人。真实音频如需留样，须显式选择并存本地限量目录，不能为调试默认常驻录音。

本轮交付是研究结论与复现依据；下一轮可据此给Cursor分阶段任务，仍按“Cursor编写 → Codex评审 → 独立验证 → 再进入下一阶段”的流程。
