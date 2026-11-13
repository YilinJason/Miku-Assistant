# CosyVoice 一段有界预取（待放行，当前不得执行）

前台 request 已 Root 分层 PASS，见 codex-review-voice-foreground-2026-11-03.md 顶部。先完成真实共存诊断结论及相关修复闭环，再由 Root 明确放行本文件。当前 dirty 工作区全部保留，不提交推送；不读改删 data/memory，不改 Nahida/style/默认引擎、模型预算或 SafetyPolicy。读取 AGENTS、architecture-contracts、engineering-quality、collaboration-review；生产及测试由 Cursor 实施，Root 独立验收。

## 已确认现状

- CosyVoiceTtsBackend.speak_sequential 每段合成→RVC→播放串行；TTSEngine._speak_cosyvoice 不使用现有 pipeline_enabled。
- Edge 另有无界 Queue 的旧流水线，作为后续性能切片处理；本次不同时改 Edge。旧 Cosy speak_sequential 保留串行兼容入口。
- 每段 base WAV + RVC WAV 最多两个 owned 文件；失败/清理延迟仍保留所有权，不能为了下一段放行丢登记。

## 设计和可观察上限

在现有 pipeline_enabled=True 时允许 Cosy 播放当前段同时合成/转换下一段；False 保留串行。最多一个 producer，以及一个 future 段，**正在准备或已排队合计一段**。当前播放段+future最多两段，正常路径最多4个 owned WAV。队列maxsize=1本身不足：必须在 future 准备前拿 credit，直到消费者真正取走后才释放，严禁 queued1+preparing1。合成/转换仍各自串行，不并发多重 GPU 模型推理；播放仅消费已准备文件。

窄 helper 可抽离纯调度，但不引入框架/ThreadPoolExecutor 隐藏无界队列，不为复用强改已通过前台控制器。新接口保持旧顺序 API，发动机/适配器实际调用应有测试。低配可显式关闭 pipeline，本次不暗改 resource.profile 语义。

## 生命周期和失败

- owner 在 start 之前登记，creator ack/worker entry/terminal/实际退出分开；参考已审前台协议。所有 lock 只短状态转换，不包 start/join/模型/播放/文件删除。
- 未确认启动、producer alive、在途 prepare/convert、未知迟返输出任何一个都阻新流水线及 TFM/shutdown complete；保留 owner。返回前只做统一短 join 预算（建议<=2s），超时 truthful incomplete，不能10分钟等待，不能每轮另开线程累积。daemon仅进程退出兜底。shutdown 先 cancel/interrupt，再有限 join，重试能继续回收；无额外清理线程。
- 正常每段播放后立即安全回收；取消/播放异常/合成失败关闭交接，已排队和迟返输出有且只有一个 cleanup owner。不得强删仍 pin 文件、丢 owned 或吞 producer 异常伪成功。异常传播按原 Cosy 行为，晚到结果绝不播到下一轮。
- producer终态用独立 Event/result，不能满队列再 put sentinel 卡死；等待可被取消打断，无忙轮询。只有实际 quiet 才摘 thread；线程未确认退出不能 count=0/synthesis_idle=True。
- 首段失败中止整轮；后续段失败按顺序中止，不跳到更后段。后台 BaseException 发布失败/终态，调用者原异常保留；fixtures 释放 gate、捕获全部异常并真实 join。
- 所有者清理失败时拒绝继续分配，不能靠增大 retained cap 掩盖泄漏。现有 MAX_RETAINED_OUTPUTS 常量未实际引用，不把它当已实施上限。

## 必须验证

确定性 Event fake：第一段播放阻塞时第二段已开始准备；第二段 ready 时第三段尚未开始；消费第二段后才允许第三段。检查准备/排队/播放峰值及 owned<=4，完整32段后回到0。串行选项完全不预取。保留 RVC 顺序、style、文本边界/不丢字。

覆盖 Thread构造失败、start异常/OS bootstrap未知、shutdown重叠/晚返回、producer合成异常/KeyboardInterrupt、play异常、取消时queue满、无法取消的准备、有pin或删除失败。每次保留实际线程/文件状态，可重试关闭，下一轮不能重新分配。用有界 fake 多轮取消/失败循环证明线程数、owned数、retained数不增长。

先 targeted fake gate 120秒一次，失败 STOP；不得全量 discover/设备/main/真实模型/自动下载。更新 ADR/handoff 给 Root；Root源码和独立探针通过后，才考虑一次短真实固定文本对照。真实长测不在本轮。未测真实提速不得承诺连续对话无等待。
