# 2026-11-05 最终语音链路与资源里程碑复核

本轮已约定的代码工作、针对性离线验收及无设备真实模型30分钟观察通过。实现来自官方 Cursor Agent CLI（composer-2.5，最后会话91265b8e-2f72-40be-a25f-5fe396bb9362）；Codex负责设计、逐项差异评审、独立门禁及实际监督。没有在途CLI、模型或测试，未提交/推送。此结论不等于所有设备/任意负载永远无泄漏或卡顿。

## 本轮代码与评审结论

- 退出/回收：承接已验收的真实解释器与Native Job所有权、RVC IO租约、阻塞写测试修复和预取关闭；新增诊断协作停止，停止后允许实际App/引擎/TFM finally执行，仍保留共享10秒退出上限和不明所有权拒绝。Root11/12/8/24及真实cooperative_cancel_nov5g通过；本轮真实关闭再次全部确认。详见既有各项review。
- CPU checkpoint mmap：只对固定、可信、可映射的CPU阶段权重启用；不改变精度/模型。前后准备期private约减少1050MiB；本机六句prepare均14.328秒，不能宣称启动更快。FP16实测变慢，试验已撤回，未进入生产。
- 首自然分句：保留默认分句兼容，App首段启用受限逗号切分。此前同38字真实对照首音频10000/4422/9812ms，分段版本总耗时11172ms（对照约9828–10000ms），首音更早但总耗时略增。详见first-clause review，不能把该短句效果外推所有输入。
- 孤立标点：真实长测失败后独立证明plan_cosyvoice_segments('。')原样派发，而vendor normalizer会丢弃P/S片段，可能出现empty_inference。Cursor只在原字符/段数上限校验后过滤纯P/S与空白，保留有实义的片段原文；通用分句、显示、历史、Edge、worker真错误判定均不变。生产增量约26行，线性扫描，无新线程/模型/权限。原失败的一字符正文未记录，不能断言必然是标点。
- 测试二次评审：已纠正TemporaryDirectory先删除后关引擎的错误顺序；失败输出按原契约保留至worker retirement，不为使断言通过而提前删除。普通失败及同一KeyboardInterrupt对象仍向上传播，finally确认实际回收。最终相关122项通过。
- 诊断观察：仅soak包装speak_stream，最多4条错误、3层异常链、8帧相对路径元数据，记录Unicode类别计数而不存正文；观察器故障不覆盖原异常。Root普通/致命/隐私/边界验证通过。

本轮修改未发现未处理的阻断项。没有进行无关全仓重构、批量格式化、清理旧未知文件、修改音色权重或扩大权限。

## 实际30分钟结果

父报告：[codex_real_soak30_punctuation_nov5o.json](../tmp/codex_real_soak30_punctuation_nov5o.json)。子报告：[1729765c168d/report.json](../data/test/voice-naturalness/cosyvoice-production-soak-30/1729765c168d/report.json)。验收条件在运行前保存在[codex-soak30-final-plan-2026-11-05.md](codex-soak30-final-plan-2026-11-05.md)。

- 真实App.process_text_input_streaming → Ollama 12B → CosyVoice → Nahida RVC → capture-only，固定无隐私合成输入。未打开麦克风/扬声器，未读写正式data/memory。
- steady窗口1800.000秒，父总耗时1833.83秒。按既定调度，前28分钟每15秒启动一轮，最后2分钟不启动新轮，观察空闲与回收；不是连续合成满30分钟。
- 112/112轮成功，114段WAV通过实际音频头/帧数/采样率校验，最多留存6段，speech_errors=[]，cancelled=False、complete=True。
- 首音频就绪（不是声卡出声）：中位6094ms，p95 7188ms，范围4000–7985ms；本机仍不是即时语音回复。轮次wall中位6141ms、p95 7282ms、最大8360ms。诊断限制输出32 tokens，首实际请求沿生产timeout，后续诊断15秒，不能外推长回答/复杂工具/任意对话。
- 父/子退出0，无timeout/资源abort；child/server/Native Job均quiet，App/speech consumer/Timer/aggregate TTS/RVC/TFM/warmup/guard均完成；清理0.14秒。TFM owned/pending/inflight/io_leases/pending_cleanup全0，worker_alive=False，cleanup_errors=[]。
- 365个监督样本，最大间隔5.047秒、不可观测进程数未出现缺口；运行中没有外部GPU轮询进程。原始准备期峰值进程树RSS10448.66MiB；可用RAM最低38047.24MiB、commit余量最低16851.38MiB。

## 内存与句柄复查

以下逻辑轮次快照只覆盖App/Cosy/RVC子树，不包含兄弟Ollama服务；完整父样本包含所有受控进程。

| 指标 | 前10轮均值 | 后10轮均值 | 差额 |
| --- | ---: | ---: | ---: |
| RSS MiB | 4857.22 | 5265.48 | +408.26 |
| private MiB | 11043.78 | 11487.91 | +444.13 |
| 线程数 | 111.4 | 105.0 | -6.4 |
| 句柄数 | 2509.9 | 2704.7 | +194.8 |

private差额约4.02%，低于预设复查阈值max(256MiB,早窗口5%)=552.19MiB；后20轮在11225.58–11629.53MiB间周期回落，未呈持续单向增长。父逐进程前后100秒采样显示App private约1046.55→1047.38MiB，Ollama模型解释器约12973.49→12334.13MiB。不能由此保证更长窗口或其他输入永远无泄漏。

句柄差额超过预设32，因此没有直接放行：Root补做真实队列320轮以及真实TTS预取/队列+fake合成256轮，两者均实际Windows GetProcessHandleCount，gc只用于检查点区分不可达循环与活跃所有权，没有改生产GC策略。

- 队列：[speech_handles_320_nov5p.json](../tmp/speech_handles_320_nov5p.json)：状态32/64/128条时句柄368/400/464，192/256/320轮保持128条与464句柄；对象关闭并释放后327，等于基线327。
- 预取组合：[prefetch_handles_256_nov5q.json](../tmp/prefetch_handles_256_nov5q.json)：128/192/256轮都128条与466句柄；释放后316，低于预热基线329。全部TFM/session quiet。
- 这证明有界历史状态及预取所有权组合达到上限后稳定，能复现并解释一部分真实句柄增长；没有逐个还原真实窗口的每个句柄，不能把所有差额强行归为同一来源。结合实际进程退出与退出后内存恢复，本轮没有发现须继续修复的持续泄漏证据。

测试前可用RAM48384.67MiB、commit余量44104.53MiB；初次退出快照48442.11/44199.61MiB，GPU前后均1128MiB used /14934MiB free。最终复核快照48529.30/44273.51MiB，kernel paged/nonpaged1597.89/1622.85MiB。桌面后台未冻结，不能把全机小幅变化归属App。历史约60GB占用仍无确切归属；不改系统/驱动/注册表来掩盖它。

## 文件、源码与离线验收

- 真实运行前后141个src/config/诊断文件SHA256完全一致；对应140个Python文件及新增测试共141份AST通过。git diff --check返回0，只有LF/CRLF提示。
- 828个共享miku_*.wav的name/dev/ino/size/mtime_ns完全零差；旧两个未知文件保留，不依据时间推断所有权删除。
- 用户选定01_warm_companion_rvc.wav仍382444字节，SHA256 7c4aee8b69428e4bbe3bb773cfc47efae1618efb46b7b80ebaf1465739fe19ba。
- 最终针对性测试：punctuation_contract_final_nov5n 122项；prefetch_handle_bound18_nov5q 18项（含元数据透明性、标点、320/256轮资源边界）；所有gate共享WAV828零增删。前轮通过证据保留，不将它们声称为本轮重跑的全仓套件。

原始失败与取消完整保留：soak30_full_nov5h 504.44秒、第32轮stage_playback_failed；soak30_trace_nov5k 646.42秒/41轮主动取消修规划缺口；更早额度取消不是PASS。旧状态文件完整副本见tmp/status-before-final-soak-20261105。

## 使用范围与后续验收

本机约63GiB物理RAM/16GiB显存；本地三模型仍较重。保留低内存准入、CPU/并发上限、延迟加载、空闲释放与拒绝新工作机制，不保证8GiB等低配设备能运行本机模型组合。生产配置仍Edge默认、memory.enabled=True/retention.enabled=True；本测试对Cosy使用局部显式构造，未切换用户默认TTS。源码与权限配置没有因为诊断而放宽。

当前剩余为用户实际麦克风/STT/声卡、音色主观听感、打断与连续自然对话体验，以及目标低配硬件上的实测。它们不由capture-only或静态检查替代，不承诺绝对无卡顿/泄漏。本轮已约定代码与无设备链路闭环，不继续无依据改动或重复长测。
