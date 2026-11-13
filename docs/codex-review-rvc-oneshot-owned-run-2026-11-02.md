# Codex严格评审：单次RVC owned run（16ecbb8c）

## 返修第二次评审：仍 CHANGES REQUIRED

Root 独立旧七项 probe 现全 true，0.469 秒，WAV 826→826；此前 825 与本次 826 的跨次变化来源尚未查明，不把它说成同一轮门禁新增。Cursor 报告 408/45 测试通过尚不等于 Root 独立全量验收。

新增纯 fake、无子进程诊断 `tmp/codex_rvc_oneshot_admission_probe_2026_11_02.py` 0.687 秒确认两项失败：creator_exception_acknowledged=false；atomic_admission_at_most_one_spawn=false，实际 spawn_count=2。该 probe 不得修改。

1. **P1 准入不是原子操作**：execute 分别在 `_ensure_previous_reclaimed()` 与创建/发布 record 两段加锁。合法交错是两请求均检查到空，再依次覆盖 record，各自调用 spawn。改成短锁内的 compare-and-reserve 循环：只在同一个状态锁内确认当前空/可替换并发布唯一 starting record；需要 retire 时释放锁等待，返回后重新验证，不能使用旧检查结果。禁止用额外线程或大锁包住 I/O。若已分配 owner，必须先登记在不可丢失记录再判断 generation；不能在登记前 raise 导致 owner 丢失。retire 与新准入也必须按同一协议互斥，不能只定义不用 `_retiring`。
2. **P1 creator 异常没有 ack**：只有成功的两个 capture.start 后 `rec.starting=False`；spawn 抛错、属性访问失败、第二线程 start 失败均永远 starting=True。except 接管 pending 后需在所有可能产生 effect 的操作真正结束时，在 finally/受控状态提交中 ack，再 bounded retire；原异常身份不变。无分配的普通启动失败必须可以回到 quiescent，不能永久拒绝新请求。真实未退出 owner/reader/lease 仍保留待重试。
3. 原九点尚未全部落实：`errors` 单独指定未隐含 text；errors codec handler 未预验证；retire timeout 未拒绝 nan/inf；`_StreamCapturer` 仅 OSError/ValueError 标记失败，其他线程异常仍可能返回成功；`io_lease_active` 查询异常可覆盖原异常/跳过关闭；bootstrap 测试仍只是源码比对，未新增所要求的行为矩阵。逐项核对并补真实行为测试，不声称九点全部完成。不要为了新测试修改 Root probe。

先修以上并独立状态推演，再旧七项+新增两项+原 status probe，各一次；通过后只跑一次相关扩展回归。新增 probe 的第二屏障有 2 秒期限用于暴露非法双启动；修复后单一被准入者可能收到诊断用 BrokenBarrierError，判定只看 spawn_count<=1 与 creator ack，不能据此改生产等待预算。更好的正式测试应让一个启动 gate 等待，由第二请求被拒绝后释放 gate，并确认所有线程退出。

保持其他范围限制，STOP 待 Root；尚未放行真实模型或下一切片。

状态：CHANGES REQUIRED；未允许真实模型/下一切片。Root读完整coordinator/SafetyPolicy/TFM/状态集成/测试，原生Job与持久reader未改，bytearray尾部+4096 chunk的内存界正确。但当前只跑30项不足，且原有已验证功能发生回归。

Root tmp/codex_rvc_oneshot_contract_probe_2026_11_02.py（不改，25秒树监督）7项全false：starting_retire_incomplete、starting_lease_retained、unknown_exit_lease_retained、partial_spawn_owner_adopted、success_waits_for_capture_eof、encoding_implies_text、status_constructor_compatible。0.375秒、无模型/子进程、825 WAV零差。

## 先修确认回归与同一所有权协议

1. **SafetyPolicy误删本地TTS方法**：_require_sealed_local_tts_python 的def头不见了，原body变成 rvc_runs_quiescent return之后不可达代码。spawn_local_tts_worker 仍调用它，实际必AttributeError。按 tmp/review-baselines-2026-11-02-oneshot/src/core/safety.py 恢复这一方法边界，不覆盖别人的修改。必须用原slice_a正向启动行为测试复验，不能只compile。
2. **creator/retire/准入必须完整**：_retire_record 对 starting=True/owned=None直接释放lease并返回True；execute把starting=False放在capture线程start之前；retire可清记录而execute晚启动线程。record.starting在最后一个effectful start返回/异常ack之前保持True；retire可请求取消并终止已知owner，但在creator/capturer未确认结束前必须False、保留同一记录/lease。busy新run不能覆盖旧记录。不能在coordinator状态锁内调用长retire等待；lazy coordinator创建必须在per-policy短锁下原子一次，避免两个首次调用产生两个controller。generation只增不减。
3. **部分spawn异常必须接管core owner**：except BaseException要先take_owned_worker_from_exception，即使本地owned变量None也接管到record；Popen成功返回后也先登记，过期也不能先丢掉它。starting最终ack后才能bounded cleanup。原异常类型/身份保持；查询/close/释放lease的清理异常一律unknown并保留owner，绝不覆盖原异常。quiescent/retire遇Native查询错误返回False，不抛出一条清理错误让App失去后续收尾。
4. **禁止finally无条件unpin**：execute.finally当前总_release_io_lease，超时且Job仍活也解pin；与任务完全相反。只有creator ack + raw/Job退出 +所有capture线程join成功后才释放；release失败保留token/mgr并重试，不能吞异常后清token。拆分execution_quiet与含lease的fully_quiet避免循环判断。pending记录任何条件未知都保留，下一次零新分配。正常成功也必须等capture真实EOF/join，不是只等finished50ms就返回partial CompletedProcess0；capture读取失败不能默默成功。
5. **保持Windows环境与run语义**：新popen_kwargs没env，core helper会只传两个Job变量，丢SystemRoot/PATH等原RVC环境。默认先复制现有父环境，再由helper覆盖私有Job字段；此切片不声称额外隐私硬化。encoding/errors指定时应像subprocess.run一样隐含text，不能只看text；validate codecs/kwargs/finite timeout在effect之前。截断元信息CaptureMeta要真正返回/可观察或删掉未用死代码，输出最多64KiB且不记录原文。
6. **dataclass向后兼容**：TtsShutdownStatus新字段被插入中间且必填，破坏原5参数构造。移至末尾并默认True；真实Engine必须显式传值，complete包含它。验证允许旧5字段Adapter无新字段（原契约），但若声明了新字段就必须是真bool且True。TempFileShutdownStatus新active_io_leases字段同样末尾默认0，不破坏旧调用。Root之前status故障probe应继续通过。
7. **TFM pin原子性**：当前assert_owned_regular_file各自结束后才进入_lock登记pin，另一线程可在间隙删除路径。必须在同一RLock保护下完成全部身份验证+登记；remove/delayed/shutdown已共用_can_delete_locked的pin检查，保留。active lease存在时shutdown不得完成；pin/release失败不删实际文件或强清所有权。持久convert取消未知路径这轮未接入须继续如实注明。
8. **App/Engine集成的边界**：Engine在stop_rvc=False也调用policy.retire，会终止另一个实例拥有的共享run，必须尊重现有共享所有权语义。App直接在if条件调用policy retirement，异常会跳过cleaner/character等独立步骤；必须通过既有_run_step捕获/记录失败并保留TFM，其他独立收尾仍执行。旧注入policy无声明API按旧路径；真实policy严格False/未知不能完成。沿用静态接口检测，不恢复Mock特判。
9. **行为覆盖与预算**：现有bootstrap测试只是源码assertIn/索引，不符合任务。对实际infer入口与固定probe代码用runpy/import hook验证missing/open/assign failure在src.voice/numpy/torch/model之前，成功可进入受控fake重依赖；实际main已有early_join调用，保留不误移。增加真实多阶段控制：Popen迟返/两个首次run/捕获迟退/IO错误、close/query/lease release失败、timeout仍活与pin、busy、invalid参数零effect、check/text/bytes/encoding、stop_rvc=False与App独立关闭。

不要再重写已证明的NativeJob或持久reader，不改声学/引擎/模型/生产默认预算，不启动main/模型/设备/正式data/memory、不提交推送。执行前自己按状态转移检查代码，再root7项与targeted各一次（用已有安全监督器--probe固定脚本，25秒）；通过才**一个**扩展gate，包含先前408相关模块+新run/lease/存储/状态，不仅30项，正常Windows上下文、<=180秒、精确WAV集合。任何意外失败停止交接，不无限后台重跑/加任意1秒特判。

更新ADR/handoff STOP；不要覆盖Root current-status。Core大板块仍未闭环，预热/预取尚未允许实现。
