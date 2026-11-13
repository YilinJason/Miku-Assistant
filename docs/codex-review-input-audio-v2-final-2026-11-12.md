# 输入链路 v2：本轮最终评审与验收

## 结论与范围

本轮完成了安静环境噪声误触发的分层改进，以及持续输入和迟到识别的时序修复。官方 Cursor Agent CLI 编写生产代码和正式测试；Codex 负责设计、逐阶段源码与调用方评审、独立复现和验收。未启动真实麦克风、播放或整套 ASR/LLM/TTS/RVC 模型；**真实房间效果及30～60分钟语音长测仍待用户自行执行**。

借鉴来源：PersonaPlex 的连续音频与输入前处理思路；Silero 的 CPU ONNX 人声概率模型；strands-labs/pywebrtc-audio 提供的 WebRTC NS。保留本地 Paraformer、现有 LLM、长期记忆及 Nahida 输出链路。没有安装 PersonaPlex 7B。

来源及取舍见 [PersonaPlex 调研](research-personaplex-2026-11-12.md)、[市场策略与原噪声缺口](research-real-time-voice-input-strategies-2026-11-05.md)。

## 已落地

1. 主入口在导入数值库前设置固定四键线程预算为1，避免先前约500MB的额外 NumPy 提交开销；助手运行期权限不扩大。
2. 默认 CPU Silero 概率准入，前置 WebRTC 降噪与高通，AGC/AEC关闭；失败不回退原始音频或云识别。拒绝片段不进入 ASR/LLM/工具/记忆。
3. 显式双工会话开启一个采集线程，连续维护 NS/VAD 状态，段队列容量2、单段12秒、静音结束640ms、超过3秒的待识别段丢弃；识别期间仍采集。串行/诊断/显式utterance保留兼容入口。
4. 模式退出、启动失败、并发关闭、设备溢出与原生关闭失败均有明确所有权；关闭不完整保留引用，禁止开替代线程，允许重试。未开始的线程、迟到启动及模式结束后的识别结果不能复活录音或触发回复。
5. 双工文本门控使用**采集时间区间**与**回复活动时间窗**重叠，修复 ASR 较慢时回声参考提前过期的问题；稍后用户重复同一句话仍允许作为新输入。

当前时间窗包含生成与播放，参考文本仍是生成任务文本的保守估计。**不是实际已播放文本/PCM参考，也不是声学AEC或声纹认证。** 没有实现误打断后的音频续播恢复。上述能力须另行设计、验证，不能把 PersonaPlex 演示或本轮固定文件结果当作保证。

## 独立证据

| 检查 | 实际结果 |
| --- | --- |
| 最终20模块相关 fake 回归 `input_v2_final_independent_nov12bi` | 327项通过，14.205秒；监督器未超时；WAV728→728 |
| 最终公开语音会话时序探针 `input_v2_echo_timing_independent_nov12bh` | 3项通过：迟到回声拒绝、窗后同句接受、窗前输入接受；无真实设备/回复模型 |
| 接线独立竞态 `input_v2_wiring_contract_nov12bd` | 6项通过：串行兼容、部分启动清理、退出失败、关闭后禁迟到启动、只读取消、并发开始幂等 |
| 正式STT+真实NS/Silero+假文件源/假ASR `input_v2_wiring_native_nov12ba` | 25个合成噪声均未到ASR；1个Nahida固定语音通过；同份处理后PCM/采集时钟验证；模式退出丢弃迟到SUCCESS；27个控制器weakref释放，最终VAD session释放 |
| 真实CPU采集控制器回放 `input_v2_capture_native_nov12an` | 25/25噪声拒绝，9/9固定语音对照通过；134个控制器回收；100次生命周期 private 60,899,328→60,899,328字节 |
| 流式等价与资源 `input_v2_stream_core_native_nov12ac` | 不同NS分块字节一致、VAD流/批概率误差<1e-6；10,000帧 private约增加1MiB，在8MiB门限内；session回收 |
| 短生命周期NS `input_v2_ns_lifecycle_nov12v` | 501个native DSP weakref释放；500次调用 private约增加128KiB |
| scoped compileall / git diff --check | 通过；Git仅有LF/CRLF提示 |

原生报告：`tmp/capture_wiring_native_20261112_7eda04156d8f.json`、`tmp/capture_native_20261112_3ef1c3bc8d5e.json`、`tmp/input_stream_native_20261112_ba985d9d7812.json`、`tmp/ns_reclaim_20261112_5a79bd8c7387.json`。完整失败门禁保留，未将作者摘要或早期通过数当作独立验收。

这些是固定合成非人声与既有RVC语音对照，**不是用户房间误识别率、本人声纹准确率或真实设备长测**。CPU/内存结果只证明本次新增输入链路；不能据此承诺整套大模型在所有低配设备都不发生等待。

## 依赖、数据与变更边界

主解释器 Python3.12；新增固定CPU ONNX栈及 `pywebrtc-audio==0.2.0`，主环境没有torch；ASR/RVC/Cosy独立环境与模型未改。NS wheel已核对官方PyPI SHA256，具体记录见NS验收。未访问正式 `data/memory`，未更改长期记忆策略；所有本轮探针均未写录音WAV，原728个临时WAV集合保持。

用户选定 `01_warm_companion_rvc.wav` SHA256仍为 `7c4aee8b69428e4bbe3bb773cfc47efae1618efb46b7b80ebaf1465739fe19ba`。未提交、推送、回退或清理用户修改。

作者会话：先027e8438-6142-4d10-a86d-3340dafa3eec，后1da80a06-0b32-4716-88af-277879b790f2。前会话未按规定监督的fake运行曾被Codex中断并核实退出；后续验收全部使用有界监督器。最终没有在途作者/测试任务（以最终进程核查为准）。

## 下一步

用户按 [隔离人工测试说明](manual-input-audio-v2-test-2026-11-12.md) 自行启动，先测耳机下安静、键盘、轻声短句、连续三问及反复开启/关闭。记录真实误触发和漏识别后再针对房间调参。外放、实际播放参考/AEC、声纹及误打断恢复保持未完成状态，不能宣称全语音体验已通过。
