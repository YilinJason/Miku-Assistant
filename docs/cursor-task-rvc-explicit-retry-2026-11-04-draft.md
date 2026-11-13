# 当前等待切片：失败RVC显式重试（已放行Cursor）

Edge/RVC foreground已Root115回归+最后Root8/42独立PASS，本稿已放行。API名称：stdlib-only src/core/rvc_prepare_metadata.py:RvcPrepareState/get_rvc_prepare_state；暖协调器当前返回(ok,errors,warnings)三元组。按实际源码复核。保持默认模型/Nahida/声学/预算/Safety、全部dirty和正式memory禁读改删；无提交推送、不启动真模型/设备/清理。读取AGENTS/四skills。禁止git checkout/reset/restore/clean/覆盖旧文件回退；Root快照是原始审查基线。

## 问题和范围

prepare_rvc失败永久sticky是防自动重启风暴所需，但`tts`现成Edge对象会直接ok，不重试RVC。资源恢复后应有明确安全的显式重试，不要求用户总重启程序。仅allow_retry=True且真实引擎声明RVC准备已完成失败时触发；普通请求继续旧fallback+警告，ready健康或无RVC不得重复冷启动。Cosy绑定后失败情况同样不能假成功。

## 窄设计

- TTSEngine新增独立retry_rvc_preparation窄入口（或等价），使用既有_speak_lock try-acquire，无阻塞等待当前播报/保存；closing/inflight/非失败状态不重新分配。readonly状态取前切片冻结状态，不读其它对象任意私有flag或Mock特判。
- 必须先确认全局旧RVC为**同SafetyPolicy+同config key**、无活跃转换/预热/正在启动，再有界退休；使用现有全局lifecycle gate，校验和stop在同一所有权范围，禁止先查身份后gate外stop的TOCTOU。可新增窄retire_rvc_worker_for_retry helper并复用已通过_shutdown_existing_client真实完整quiet，不能仅poll/Popen退出。busy/其它policy或配置/未知状态都拒绝，不停止其它owner。阻止新活动用client已有工作互斥的非阻塞准入，不嵌套未知锁/重写NativeJob。
- 只有实际quiet后清失败cache并执行正常prepare；资源再不足仍metadata快速拒绝，非受控异常/KBI原样传播；失败再次sticky，不能自动循环或每次回复重启。旧owner未quiet保留引用和阻新分配，重试stop可以继续回收。
- App/Core allow_retry贯穿到同唯一foreground job及sync事务，完成失败RVC的Edge/绑定Cosy不能因“已有TTS对象”提前ok；最终ready_fn/result转换使用同显式重试条件，失败时result失败且普通fallback仍可用。Edge+RVC也保持短前台预算/pending；不新增线程框架或直接在UI同步stop/load。没有新状态API的legacy注入保持兼容，不靠catch TypeError再调用来探测签名。
- warmup协调器仍先确认Ollama不会并行抢GPU，再调用retry入口；候选新引擎不必重试；正常start/保存/转换不额外触发retry。不全量重建健康TTS/STT或重置共享TempFileManager。

### 同时堵住构造迟返后再启动

当前warm_rvc_worker在get_rvc_worker构造（含CUDA外部探针）返回后立即start；如果期间shutdown/interrupt发生、当时全局client尚未发布，第一次stop可能没有可停对象，构造迟返仍会start新模型。给正常prepare与retry统一传可选Event-like cancel（合并engine shutdown/interrupt，复用现有_MergedLocalTtsCancel），在get前/返回后、start后、warm前后检查。取消后只退休本次拿到的确切client/owner，不无条件stop新policy的全局client；stop未quiet保留引用，不能靠取消标志伪回收。不重写NativeJob或新增监控线程，旧无cancel参数调用行为兼容。新explicit retry清旧interrupt只在接受新请求、同epoch且shutdown未发生时进行，不能清掉更新的打断。

## 必要验证

确定性fake先低内存失败→释放资源显式`tts`→一次退休/一次准备→成功，普通请求从不重试。健康/无RVC显式tts仍复用；busy/不同policy/key/stop不完整/线程或IO未quiet均零新spawn、状态保留；之后quiet再重试可成功。重复并发显式请求合并已有job，仅一轮stop/load。KBI/普通异常保留原对象、失败sticky；shutdown/晚返回仍不发布。穿透实际TTSEngine新API与App接线，不只上层Mock。配置前台短预算，确认调用快速返回pending，不用sleep猜线程进度；所有fixture最终真实join。

另用Event卡住实际warm helper的get_worker构造返回：期间置shutdown/interrupt、第一次stop无client，再释放get返回，断言start与warm均0次、确切client stop/保留行为准确、fixture最终join。不能只在prepare入口看一次取消，不能凭daemon当清理。

沿用内存固定facts与fake capability runners，不改变原权限和关闭断言，不改Root探针。按相关新测试→App/CLI/Edge/Cosy/RVC合同分组现有Root scoped gate前台限时、WAV零差。完成STOP待Root；未实测低延迟不宣传真实时延。

测试唯一label，Root监督器拒绝已有artifact，不得解除保护/复用或覆盖Root报告；返回实际JSON路径。失败停止定点修正，不detach。此次只重试与迟返取消桥，预取/代码精简另板块，勿提前实施。
