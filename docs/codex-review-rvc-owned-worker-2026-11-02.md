# Codex 严格评审：RVC owned worker（109fa042）

## 最终结论：持久 RVC worker 切片 PASS（2026-11-02）

Codex独立复查最终helper alias/SafetyPolicy/bootstrap、持续reader、统一写入准入、spawn/安装在途状态、stderr预登记、停止与失败后回收。原5项、startup/EOF3项、install-stop4项故障探针全true；各自20秒树监督且WAV825零差。

真实 .venv-rvc/Py311 无模型Job树3/3：主动停止4进程47ms、自然退出156ms、父退出5进程31ms，总1.484秒。独立常规Windows（沙箱外）12模块370 tests OK(skip1)，80.770s（监督器82.187s），120秒上限，无超时，WAV825→825，added/removed空。两个Windows路径断言及显式fake Job DI已复审，生产固定身份校验未放宽。

沙箱下联合gate超时已由15秒faulthandler栈定位在F16旧Edge测试的ProactorEventLoop socketpair.accept，不作为RVC实现通过/失败证据；本次正常Windows上下文独立结果才是最终联合门禁。自有超时诊断已回收，未加载模型或设备，未访问正式data/memory。

本结论仅覆盖持久worker核心回收；上层TTSEngine/App目前忽略停止失败的传播缺口、单次infer/probe进程树归属仍待处理。整体“退出与回收”板块尚未完成，不允许据本结论宣称全语音链路无残留或进入真实共存性能验收。后续先按具名传播任务闭合上层契约。

下方为已修复历史与独立失败证据。

## 独立验证环境诊断：核心12项+真实Native3项通过，仅补路径断言

最终安装修复已逐行复审。Root三组探针在自有树监督下5+3+4全true，WAV825零差。真实 .venv-rvc/Py311 无模型Job树测试3/3：停止4进程47ms、自然退出156ms、父退出5进程31ms，总1.484秒，模型0。

Root 12模块gate在Codex AppContainer沙箱120秒超时，不是放行结果。已定位两类原因：
1. tests.test_rvc_owned_worker.RvcOwnedSpawnTests.test_spawn_applies_job_env_and_preserves_parent_environ 的 argv list第0项大小写；tests.test_security_round3a.RvcCapabilityArgvTests.test_legal_worker_infer_probe_reach_fake_runners 同样比较 canonical OpenAI.Codex 与原始 openai.codex 路径。
2. 独立故障栈 tmp/codex_gate_rvc_security_trace_2026_11_02.stderr.txt 确认 F16SpeakCleanupTests.test_play_failure_still_clears_temps 卡在 asyncio ProactorEventLoop→socket.socketpair→accept 的沙箱本地socket初始化；没有RVC代码在阻塞栈。35秒监督器已回收测试进程，无WAV变化，CIM未发现旧联合测试残留。

**给Cursor的本次范围仅测试**：上述两个正向测试，把 argv[0] 用平台完整路径语义比较（Path.resolve），argv[1:] 仍完整精确比较，worker/infer/probe都如此；不只比basename/后缀。第一个正向cap测试若仍用policy.bind_process_runners而会真正创建Native Job，应改显式bind_fake_rvc_runners（只此有实际fake spawn的正向用例，拒绝零spawn用例不必改）。生产、预算、reader、identity规则不动。

只跑这两个targeted用例一次，交接STOP。**不要为沙箱socket限制改生产asyncio或伪造协程结果**，不需要你重复全门禁；Root将在常规Windows执行上下文（沙箱外，无模型/设备）独立运行同样120秒12模块gate。这样区分环境限制与实现，不以加大timeout掩盖失败。

## 第三版复审：原5项+启动3项 Root 独立通过；只剩安装在途的完成状态

Root 经 tmp/codex_scoped_test_gate_2026_11_02.py --probe 的20秒自有树监督，原5项全部true（3.453s）、启动/EOF3项全部true（2.547s），WAV825零差。持续reader与启动预算设计现已正确，**不要再重写它们**。

最后的同一所有权边界由 tmp/codex_rvc_install_stop_probe_2026_11_02.py 确认（不改probe）：在 stdout 已登记、stderr 安装仍由Event阻塞的启动阶段，stop返回True、丢_proc、last_stop_exited=True，但 starter还未退出。三项false；no_late_ready=true。该probe1.406秒，无模型。源码 _try_release_owner 从未检查 _start_inflight，因此可在构造还会继续安装reader时过早清引用。

只做以下最小修正，不扩大状态协议：

1. _try_release_owner 若 start_inflight 仍True，不关闭管道、不清owner、不标完成（先杀已知自有Job仍允许）。stop 在已终止proc但starter未ack时快速返回False。此状态阻止新分配，不能只有“无proc且starting”才认未完成。
2. _start_locked 的 finally 先在短锁内清 start_inflight；若启动失败/已取消且已返回proc仍需回收，在锁外有界重试当前owner回收，使错误/取消能够正常收尾。任何清理异常不得替换原异常、丢引用或假成功。正常已ready不新增stop。pending异常owner无法退出时继续保留，原KeyboardInterrupt身份不变。
3. _start_stderr_drain 必须在 Thread.start 之前将 thread 引用登记到self._stderr_thread（和stdout一样），当前是 start后返回再登记，有KeyboardInterrupt窗口。失败/过期proc不能启动新的reader；不要保留调用方再次覆盖引用的多余赋值。Thread.start/join/读IO都在短锁外。

上述修复后，同一配置/预算/reader实现保持不变。Root三组脚本（原5、新3、安装4项）在有界监督器下各一次，再targeted/联合370门禁各一次；意外失败停止交接。不要再无限后台命令，全部finally回收。生产声学/默认/Native核心/data/memory不变，不启动下一切片。交接停止待Root。

## 第二版复审：CHANGES REQUIRED（完整边界探针三项全 false）

初始五项 probe 的实现方向保留，不再新增机制。Root 在已交付源码确认，并用 tmp/codex_rvc_startup_boundaries_probe_2026_11_02.py（不得修改）实测三项全 false：

- startup_owner_registered_and_stoppable：Popen 已返回、reader 已启动、尚未 ready 时，client._proc 仍未接管；stop 无法终止实际已存在的 worker。
- valid_ready_after_one_second_accepted：显式在仍属于原3秒测试启动预算内的1.1秒后送合法 ready，start 失败。源码原因是新加 bootstrap_timeout=1.0，_attach_stdout_reader 内先等待并可能抛错；这已改变实际原60秒加载语义，**真实冷加载必然失败**。
- queued_message_preserved_at_eof：reader 在 EOF 时先等5秒，随后 _force_put(None) 丢掉队列内未取的有效 ready。

另外，CLI 有三条旧的 root fake probe 进程未结束；已由 Codex 中断自己的 exec45613（exit1），随后只读 CIM 确认所有匹配旧probe均消失。**不要再无限后台重跑探针。** 用有总时限的自有进程树监督器；任何意外失败先停止交接，不用新增同步等待/覆盖队列让fake求绿。

### 按下面固定且更简单的实现完成原协议，不再以1秒/5秒特判修复

1. **持续 reader 的标准语义**：删除 _force_put、_delivered/wait_first_delivery 与同步首消息 bootstrap 特判。一个线程 read/parse；有效消息进入容量1队列，使用每次<=50ms、可由stop打断的 put 背压，绝不替换已排队有效消息。自然EOF只设置独立 Event，不强塞None。poll_message 用<=50ms的 get 循环、原请求deadline；先消费已排队有效消息，队列空且EOF/stop再返回None。close 设置stop，等待者会在<=50ms醒来。读异常设EOF并结束，不 sleep+无限重试IO错误。producer在满队列等待时必须能被stop打断。删除没用的常量和分支。
2. **先登记再启动/等待**：Popen成功返回后，立刻在短 _spawn_lock 中接管 _proc，不论generation是否已过期（过期者接管后回收、False）。给 reader/thread 创建引用后先保存进同一 owner 状态，再 Thread.start；不要先启动并阻塞读完首消息，才登记。随后只在锁外用原 _WORKER_START_TIMEOUT 读取ready。任何BaseException均保留已有proc/reader并有界清理，不丢owner；不要改root探针或生产预算。删除会倒退 _owner_generation 的未使用 _register_owner；generation只能增加。
3. **写入状态必须和准入原子**：_stdin_write 当前先在_spawn_lock校验，放锁后再拿_io_state_lock，并只复核identity；stop可在中间开始回收，后续write又进入已在close的stdin。把 _stdin_writes_inflight 的检查/递增/递减统一由同一个短_spawn_lock保护，在同一临界区检查proc/epoch/generation/stop/retiring并递增，IO在锁外。回收先在该锁内建立retiring准入；标记期间禁止新write/start。不要另造相反锁顺序。无写入者后才关stdin；等待/close均在状态锁外。
4. **busy stop 不得返回旧成功**：stop 的 _lifecycle_lock.acquire(False) 失败时返回False，不返回历史 _last_stop_exited；完成值同时检查start_inflight/writer/reader/owner。_publish_ready 在同一个短锁下检查generation及stop/retiring，不能仅比较数字。保存对象快照的方式继续保留，不能旧owner清理新reader。
5. 原五项probe、三个新边界probe、blocked write / late cancel / active Job / close failure / PermissionError pending / 两stop+start 都应在一批针对性测试中覆盖。bootstrap用行为hook，不能退回源码字符串检查。核心测试线程都要finally释放/有界join/把异常交回主线程，不遗留非daemon线程的挂死测试子进程。

修完先读取代码逐项确认逻辑，再按自有硬超时监督执行root两脚本+targeted各一次，通过才联合gate一次。不要让旧版本probe后台任务继续存在；先检查具名旧进程，只有本轮自有任务可清理，安全API不绕过。最终状态370项旧报告已失效，生产没有通过，真实模型与下一切片仍禁止。更新handoff并STOP。

状态：CHANGES REQUIRED。已阅读最终 SafetyPolicy/helper alias/bootstrap/client/调用方及新增测试。具名权限、环境继承、复用已验证 Job、reader 退出前保留 owner 的方向正确，但当前不允许进入真实模型/下一切片。

Root tmp/codex_rvc_owner_probe_2026_11_02.py（不得修改）结果：
same_worker_repeated_protocol_reads=false；live_reader_retains_owner=true；idle_stop_avoids_pipe_write=true；stop_during_spawn_no_late_ready=false；spawn_interrupt_owner_preserved=true。均纯 fake，无模型/设备/正式记忆。

## 一次性闭合下面同一生命周期协议，不逐个只修断言

1. **持久 stdout reader 只读一条消息，导致基本功能回归。** _RVCStdoutReader._run 在首次 json.loads 后 break，poll_message 不会再读。启动取走 ready 后，warmup/convert 无法收到响应。改成每 worker 一个持续 reader；队列硬上限1，有限轮询/取消的背压；不静默丢有效响应、不每请求新建线程。EOF/close 必须使 poll_message 快速醒来，即使队列满也不能丢终止状态或等满45/60秒。用模拟真实交互的 queue pipe 测 ready→warmup→warmup/convert 多次，不靠 mock _read_worker_json 绕过路径。Root probe 的连续两次 warmup 必须 true。

2. **start_inflight 时 stop 仍谎称完成。** 当前等2秒后 _proc 仍 None 就 _last_stop_exited=True/returnTrue，但 spawn 后续还会返回。Root Event probe 已确认。无 proc 但 starting/retiring/reader/writer 未结束时必须 False 并保留该 client，阻止重建；无需等待2秒才返回这个事实。晚返回 proc 无论普通/控制流异常都先接管并按原 generation 回收，不发布 ready。_last_stop_exited 必须包含 start_inflight 与真实退出一致；不能先 True 再晚分配。

3. **生命周期发布与回收没有原子边界。** generation 检查、_proc=proc、启动reader、最后 _ready=True 分散在 _spawn_lock 外；stop 可夹在最后检查与发布之间。_try_release_process 又读取当前 self._reader/_stderr_thread，两个 stop 与新 start 交错可让旧 proc 的清理操作关闭新 reader。用现有短 lifecycle/spawn lock 保护所有权状态检查/登记/最终发布；start_inflight/preparing/stopping/retiring 都阻止新分配。stop 有 single-flight 准入（第二 stop 可快速返回未完成），不持短锁等待 join/IO。回收必须绑定同一个 proc+reader+generation 快照，最终仅在仍持有该 owner 时清引用/标成功；被旧调用捕获的 proc 不能碰新 reader。在 start Reader 线程之前先登记引用，失败/KeyboardInterrupt 也保留 owner；旧 reader join False 时禁止替换它。测试含 startup返回前停止、ready发布边界停止、两个 stop+新start，检查实际对象身份和结果，不只线程最终结束。

4. **stdin 写线程/操作仍活时 close 可卡死。** warm_up/convert 仍直接 write/flush；_try_release_process 只检查 stdout/stderr 后就同步 close(stdin)，若另一个线程正持有 TextIO 写锁会无界阻塞。记录单个在途写入的状态（用短状态锁，写IO在锁外，finally清状态）；stop 先杀自有 Job，writer未退时保留 owner、False、不 close、不再分配。已有小JSON请求可保留同步写实现，不能 per-write 新线程；开始写入之前也要原子检查 owner/generation 尚有效。补阻塞写且 close 会报错/等待的 fake，stop 不触发它的 close；释放写入后重试才 complete。原预算不放宽。

5. **取消后迟到响应不得成功。** warm_up/convert 在读取 payload 后没有再验证 operation epoch/owner generation；stop 也仅递增owner_generation。关闭/取消使旧请求无效后，即使已排队 payload.ok=True，也必须返回False。在最终成功返回前按同一窄状态协议检查当前 owner/epoch；不要清外部取消。用 Event 测取消与响应同时到达。避免新增两套互相矛盾的“ready”状态。

6. **不要引入可能让解释器无法退出的非daemon管道线程。** 新 stdout/stderr daemon=False，若异常路径仍未确认管道结束，main 返回后 Python 会等待这些线程，父进程不退出，Job 的父退出兜底也无法触发。保留单一 owner、明确 join/status 的前提下用 daemon=True 作解释器退出兜底；daemon 不计作清理成功。必须仍检查实际退出，不能以daemon属性让stop返回True。

7. **验证不得以源码字符串或过浅 fake 代替行为。** test_vc_params_deferred_until_convert 目前用 source.partition/assertIn，不满足任务。用 runpy/import hook 的缺token/open/assign失败分支证明真实脚本早期退出前没有 src.voice/numpy/torch/model import。增加 Job active但redirector退出、Close失败重试、PermissionError/KeyboardInterrupt pending owner 的有意义行为覆盖。当前测试缺这些任务原有要求。

保留 Native Job 已通过实现不改，只用别名。当前 root probe 五项全 true 之后 targeted 一次，再相关门禁一次。门禁至少覆盖原八模块（CPU staging、B、C1、C2b1、C2b2a、RVC lifecycle、owned_job、slice_a）加新RVC、security_round3a、security_round3a_review_fixes、mode_assembly_round2；120秒硬上限，UTF8与WAV825实时精确集合前后不变。使用安全监督器，不新增不限时后台命令。意外失败停止交接，不重复求绿。新增测试要显式 fake Job DI，不由 Popen callable身份猜测。

仅此持久RVC切片；不改声学参数/配置/生产超时/正式data/memory；不运行模型/main/设备/infer/probe切片；不提交推送。修改完更新ADR/handoff，STOP待Codex。下一步会独立真实无模型进程树验证，不把你的测试报告当最终验收。
