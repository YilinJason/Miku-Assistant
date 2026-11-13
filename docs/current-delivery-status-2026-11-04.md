# 当前交付状态（2026-11-05）

## 防噪补充研究（当前效果仍需改进）

[最新研究](research-real-time-voice-input-strategies-2026-11-05.md)已补充公开产品策略与超过最低门限的合成噪声实验：现有VAD仍可能放行非人声，单改mode3不足。下方Phase A仅是当时的代码与有限离线验收，不能理解为真实房间误触发已解决。下一版建议先做输入降噪/概率型VAD/连续帧管理与更全面数据验收；本轮为研究交付，生产代码保持不变，未启动真实录音或模型。

## 安静环境误触发优化（代码与离线验收通过）

依据：[本轮最终审查](codex-review-voice-input-final-2026-11-05.md)。默认 Paraformer 麦克风路径增加持续有界能量门限和轻量本地 WebRTC VAD；拒绝的片段不进入 ASR/回复/记忆/工具。最终 182 项相关回归 + 7 项独立检查通过，固定语音与合成噪声回放、500 次 VAD 调用资源观察通过。真实用户麦克风效果待退出旧进程后复测；不是本人身份识别，也未完成双工回声 Phase B。无在途 Cursor 或测试，未提交/推送，正式记忆未访问。

## 本地 ASR 默认切换（Codex 最终验收通过）

正式依据：[本地ASR最终验收](codex-review-local-asr-final-2026-11-05.md)。官方Cursor编写，Codex独立评审和执行。默认 `paraformer`、`local_asr_worker:true`、`cloud_stt:false`；独立CPU环境/固定模型已安装校验。232项相关离线测试通过（1项既有跳过），18+8项独立探针与实际配置轻量检查通过；最终真实固定音频诊断退出0、worker/I/O/PID全部回收。冷首次约11.4秒，热识别p50约0.36秒；ASR冷启动可用物理/commit门槛4/6GiB。共享828个旧WAV精确元数据和Nahida参考hash不变，正式记忆库未访问；TTS/RVC/记忆设置未改。真实用户麦克风/声卡、目标低配硬件与新ASR全栈长测仍为独立验收。未提交推送，没有在途任务。下列“待评审”等历史描述不代表当前状态。

---

# 当前交付状态（2026-11-05 语音长测收口）

本轮剩余代码与无设备真实模型验收已收口。正式结论及限制见 [`codex-review-final-voice-soak-milestone-2026-11-05.md`](codex-review-final-voice-soak-milestone-2026-11-05.md)。实现由官方 Cursor CLI 完成，Codex 独立评审与执行门禁。

- 退出与实际进程树回收、blocked-write测试可靠性、预取/预热接入均已闭环；诊断协作取消实际验证通过。
- CPU checkpoint mmap准备期private约降低1GiB；首自然分句真实对照首音10000/4422/9812ms。FP16变慢，已撤回。
- 真实长测中发现纯标点规划缺口，已最小修复；真合成错误与致命异常仍传播，不放宽权限/超时/资源门槛。
- 最终30分钟观察窗口通过：112轮成功、114段实际音频校验；前28分钟按15秒轮次，末2分钟空闲。实际Ollama12B/CosyVoice/Nahida RVC/App链路，capture-only无麦克风/声卡。
- 最终122项相关回归、18项Root测试通过；320轮队列/256轮预取组合的Windows句柄达到128条状态上限后稳定，释放回基线。本窗口未发现持续泄漏证据。
- App/Timer/全部模型自有进程/TFM实际关闭；TFM全0；共享828个WAV精确元数据不变；141个运行文件hash不变，AST/diff-check通过。退出后GPU用量回1128MiB，可用RAM约47.3GiB。

首音频就绪中位约6.1秒、p95约7.2秒，仍不是即时回复。模型组合进程树RSS峰值约10.2GiB，本机结果不代表低配机器。保留准入与资源上限，不以测试通过保证永远无泄漏/卡顿。

生产默认仍Edge；memory.enabled/retention.enabled仍True，正式data/memory未访问；Nahida参考音频hash不变。未提交、未推送；没有在途CLI、模型、测试任务。主额度<=10%停止规则继续保留，未使用reset credits。

剩余验收：真实麦克风/STT/声卡、主观音质、自然对话与目标低配设备实测。历史状态完整归档tmp/status-before-final-soak-20261105；此前失败/取消报告均保留，不能当作PASS。
