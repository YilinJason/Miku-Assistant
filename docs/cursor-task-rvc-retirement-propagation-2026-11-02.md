# RVC 回收结果向引擎与 App 传播（待持久worker最终放行后下发）

仅在持久 worker owner 切片通过 Codex 严格复审后再执行；与单次 infer/probe 回收协调统一，不能提前报告整体 RVC 回收已完成。

已确认调用链缺口：TTSEngine.shutdown 调用 stop_rvc_worker() 却忽略 bool；自有 TempFileManager 仅看 cosy_shutdown_complete。MikuAssistant shutdown 当前只验证 local_cosyvoice_shutdown_status。若 RVC 进程/读线程/在途写入仍未回收，底层正确返回 False 仍可能被上层当作成功。

建议最小契约：TTSEngine 增加明确、冻结的聚合 shutdown 状态读取 API，包含已确认的 localTTS、RVC（持久及单次）、引擎在途调用完成；缺失/非法/异常不可假成功。shutdown 本身保留既有 None 返回兼容，但不标完成、不关闭其自有 TFM，直至其所有写入者退出。stop_rvc=False 的“另一个实例仍拥有共享RVC”现有边界保持，不停止非本实例负责的资源。

App 优先读取新的聚合状态；真实 TTSEngine 未完成时保留引用、_tts_shutdown_done=False、共享 TFM 不关闭、_non_memory_shutdown_done 不置True，允许重试。对没有新 API 的旧注入 fake/Edge 兼容按既有约定处理，不放宽 Cosy fail-closed 状态。memory/cleaner/character 等独立关闭继续进行，不能让一个组件失败阻塞所有清理。

所有生命周期状态在短锁内更新，IO/join 在锁外。保持前台文本可用，不新开每次失败一个回收线程，不扩大生产等待预算。需要 fake/Event 覆盖：RVC stop False→True、reader仍活/启动在途/写入仍活、None/异常/缺状态、共享与自有TFM关闭顺序、重复shutdown和部分初始化失败、Edge/注入兼容。主解释器Py312，离线有界门禁及精确WAV集合检查；不运行main/设备/模型/正式data/memory、不提交推送。

## 本轮具体边界

仅改 TTSEngine 的关闭状态、MikuAssistant 的关闭验证、相关 fake tests 与必要文档；不接入 Cosy 预热/不做预取，不重写已通过的 RVC reader/Job。默认引擎/声学/模型/超时不变。

建议使用小的冻结 TtsShutdownStatus 与 get_shutdown_status()（可选等价清晰命名）：complete、local_tts_complete、persistent_rvc_complete、synthesis_idle、temp_files_complete。名字明确当前覆盖持久RVC，单次infer/probe尚未实现的回收能力不得假称已覆盖。状态 getter 供 App 检查，不导入重依赖或启动任何资源。

shutdown每次先把本轮状态设未完成，再执行旧关闭流程并采集stop_rvc_worker严格bool结果；未确认时保留引用，不抛掉之前失败。引擎 speak_lock 仍被合成调用持有时，也不能标完或关闭其依赖；不要等待获取该长IO锁。stop_rvc=False 的已存在共享RVC拥有者路径保留，不能误杀别人仍使用的worker。自有TFM只在本实例负责的全部写入者确认结束后关闭；共享TFM由App最后关闭。

App在tts.shutdown后优先验证新状态（complete is True；None/异常/非法值=未完成）。对没有新API的旧注入组件继续既有兼容路径和Cosy严格状态检查；注意MagicMock会动态合成任意属性，不能误识别为真正实现了新API。失败时不清tts、不置tts_done/non_memory_done、不关闭共享TFM，其余独立步骤仍执行；再次shutdown可恢复。已进入closing后禁止新的ensure发布/播报。

本切片同时在关闭共享TFM之前检查现有_voice_loading：构造/懒加载尚未ack时不关闭它。未发布candidate持久所有权的完整协议已在 C2b2b draft 保存，当前不要偷偷接上新预热；若发现此 guard 不足以保留候选，请明确停下交接，不能把尚未闭环说已成功。

必测：RVC停止False→True、自有/共享TFM、合成锁忙、聚合status缺失/None/异常/非bool、Edge/旧注入兼容、memory/cleaner/character独立关闭、重复shutdown、loader在途不关共享TFM。用真实调用次数和对象身份/Events，不源码字符串检查，不真模型/设备/data/memory。

读 AGENTS 与 architecture-contracts/collaboration-review/engineering-quality。主Py312；先有意义targeted一次，再相关gate一次（safe_subprocess监督、120s、WAV实时精确集合不变）。异步Edge fake测试需正常Windows上下文，Codex已定位AppContainer socketpair挂起，不通过改生产asyncio掩盖它。意外失败停止，不能重复求绿。更新handoff STOP待Codex，不进入下一切片，不提交推送。
