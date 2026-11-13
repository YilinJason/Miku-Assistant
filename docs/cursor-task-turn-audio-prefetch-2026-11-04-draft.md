# 默认流式路径的一段音频预取（已放行）

替代旧 `cursor-task-cosyvoice-bounded-lookahead-2026-11-03.md` 的生产路线：仅在单次speak内部预取覆盖不到默认stream逐句调用。前置OwnedThread、TurnSpeechPlayer及音频事务回滚均已Root最终通过，具名review顶部为准。本任务仍由Cursor实现、Root独立评审。遵守AGENTS与四repo skills，巨大dirty保留，禁git回退/正式data/memory/真实模型设备/下载清理/提交推送。不改默认12B模型、Edge引擎、Nahida/style、权限或TFM/NativeJob底层。

## 入口与兼容

- 新增TTSEngine.speak_stream(chunks, *, cancel_event=None, on_chunk_done=None)，同步消费原句Iterable[str]；整个turn只占一次_speak_lock。实际声明 `supports_turn_stream = True`（必须严格is True，避免Mock动态属性/任意truthy误判能力），旧fake或非支持引擎继续逐句speak。
- TurnSpeechPlayer的一次consumer调用覆盖同一turn：先取首句，之后同turn迭代器在Condition锁内只取自己的item；空队列但production未结束时cond.wait，结束后返回。换轮/取消后立刻终止旧迭代器，绝不能偷走新turn队列。每轮一个永久取消Event，invalidate/stop/失败置位，传给speak_stream；不复用clear。
- on_chunk_done()仅在一个原句的全部细分段播放+清理后由消费侧按原顺序调用一次；sanitize后空句也算已处理，但不得在producer侧提前计数。Core accepted仍按原句，stream未返回前speaking=True；最后返回不再额外finished++。异常/过期处理保留现有普通与fatal边界。
- speak原公开入口与pyttsx3/云权限/正常RVC基础音频回退保持；save接口不改。pipeline_enabled=False仍明确串行；pyttsx3串行不用后台producer。新stream失败必须传播而非被旧speak吞错误报完成；不能因部分音频已播放再把完整turn重复fallback一遍。

## 唯一producer与上限

可新增窄voice/tts调度类，复用已通过OwnedThreadTask。Engine只强持有一个活跃/待回收session owner，构造惰性，登记后start；不建池/监控/每句线程，不维护retired历史列表。现有Edge无界Queue与600秒join必须由同一有界路线替换，不能仅新增Cosy而保留默认危险路线。

当前播放段之外，future **正在准备或已经排队合计最多1段**。producer在开始下一准备前拿credit；当前播放/清理结束、future转成新的当前段时才放行下一个credit。Queue(maxsize=1)单独不足，不能queued1+preparing1。只有一个实际合成/转换任务，GPU推理不并行；仅与当前音频播放重叠。

正常路径当前+future最多Cosy4个WAV；Edge最多6个路径（4WAV+2MP3），不能把Edge也写成4总文件。每段播放后即时请求清理，不等整轮末。分段不丢字，保留Cosy现有3200字符/32段/每段120上限，明确这些按原句计划还是全turn应用，不偷偷扩大限制。text队列仍受现有maxsize/128硬界，不再收集整轮音频或正文。

## 取消、错误、迟返与清理

producer terminal/error和队列分离，禁止满队列put sentinel。取消唤醒credit与文本迭代等待，不忙循环；不自动重试模型/异常。所有factory/start/模型/播放/文件删除/join均在短状态锁外。

返回/关闭统一短join预算<=2秒，不能等待600秒；target terminal不等于实际Thread退出。启动unknown/creator未返回/producer仍alive/待清理owned任何一项都保留唯一owner并阻后续新session创建，不能一轮一个漏线程。后续请求只允许有界复核实际quiet与原路径清理，真实完成后才丢旧owner。不因新请求清除旧owner取消或更晚的Engine interrupt epoch。

播放异常、producer Exception/KBI/Exit、构造/start失败原对象必须可观察；先取消/关闭交接，已经排队与迟返输出恰有一个cleanup owner。原主异常不被二次清理异常替换。串行模式也走已通过事务回滚。未知still-writing输出由既有RVC/Cosy IO pin/retire fence保护，不能直接unlink。

准备适配器可能产生未返回的失败输出；前置事务回滚使它们由TFM pending或Cosy retained/retire负责。未解决的pending/retained必须阻进一步分配，不能只数成功返回descriptor。可对共享TFM pending采用保守busy门槛（文档说明可能暂时等待STT清理），不得清扫不属于本session的文件或忽略pin。cleanup失败保留current/future的固定有界引用；不要靠提高cap续跑。

Engine.shutdown首先发布shutdown/interrupt、stop播放器与cancel producer，然后有界join，再推进原backend/RVC/TFM流程；producer不quiet必须synthesis_idle=False、aggregate incomplete，不能TFM complete伪造整机退出。保存既有结构化status与可重试shutdown；无额外服务卸载/进程权限。

## 必须证明的生产接线与确定性矩阵

1. 真正调用TurnSpeechPlayer默认stream入口（同一turn两个独立enqueue），首段播放阻塞时第二句已经prepare；第二句ready时第三句未prepare；释放首段且完成清理后才允许第三段。不是仅直接调用helper的测试。
2. 普通speak多段与stream句间均走同一界限；Edge/Cosy串行选项零预取；legacy fake只调用speak；sanitize空句计数与production_finished不早完成、不丢字不重播；旧turn晚返不偷/播新turn。
3. 多段峰值、32段后文件回零；取消/播放失败/prepare普通与KBI/Exit/构造/start unknown/terminal外层仍alive/queue满/credit等待/迟启均truthful并拒绝开新producer。无法取消准备时短返回但owner保留，实际放行退出后才可重试。
4. 删除失败/pin/隐藏失败输出不继续分配、不强删、无原异常丢失。有限多轮取消/失败后实际thread/owned/retained不增长；root实际join/错误主线程确认、所有gate结果检查/finally释放。
5. App/Engine shutdown与新请求重叠，不丢aggregate incomplete，不清新epoch；保留既有RVC/Cosy/CLI/generate_stream/voice_session回归。

验证仅主Python312+现有scoped gate唯一label前台顺序<=120s。禁止raw unittest/discover/detach/并发、Rootprobe/监督器修改。先新模块，再相关回归；指定compile/diffcheck；简短ADR/交接后STOP。Root源码+独立接线/上限/取消探针过关才关闭该板块；fake提速不代表真实流畅，30–60分钟真实长测仍排除。
