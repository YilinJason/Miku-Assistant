# 默认流式音频预取独立评审

## Root 最终验收 PASS（当前权威结论）

生产独立 `turn_prefetch_root19_final_20261104g`：19项及子矩阵全部PASS；最终相关A_g 138、B_g 182全部OK。最后仅tests夹具修正后，Root源码确认patch内部实际关闭、aggregate.complete/TFM owned0证明、done标志在成功后发布、正常/取消wait状态断言；`turn_prefetch_fixture_root_final_20261104h` 68/68 OK。各组有重叠，不相加为唯一测试数。所有gate WAV826精确集合零差、无超时，指定compile/diffcheck通过。

实际证明：默认TurnSpeechPlayer→TTSEngine首句可在生产结束前播放；单producer/一个future preparing或queued；Edge6路径、Cozy4WAV正常峰值、32段后回零；两后端serial开关；empty回调顺序；Runtime/KBI/Exit来自prepare/source/plan的原对象；primary play错误不被cleanup替换；unknown start迟启/actual线程退出；old/new turn取消、预取消保留新interrupt、已busy/pending拒新分配；真实IO pin释放后可恢复；播放失败后的晚到MP3不进入WAV/RVC。无真实模型/音频设备新验证，不能把这些当真实性能或长期无泄漏保证。

AST对切片前快照：原Engine/Queue方法未删除，变更限本任务接线/准备取消/保存锁边界；CozyBackend原方法未变。默认模型/音色参数/权限未改，正式memory未访问、未提交推送。官方Cursor会话73a8e323全部STOP，无在途CLI/测试。允许保行为精简，随后最终性能；真实三模型压力仍未解决，长测仍排除。以下均为历史过程，非当前缺陷清单。

## 当前：生产Root19及相关138/182已PASS；仅新fixture最后三处修正

Root最终生产证据 `turn_prefetch_root19_final_20261104g` 19/19 OK；relatedA_g 138/138、relatedB_g 182/182 OK，均826精确集合零差。AST对切片前快照：未删除原方法，修改限Engine/队列接线与准备取消，CosyBackend类方法未变；指定compile/diffcheck通过。生产冻结，不再修改任何src。

**源码仍未落实的测试要求，仅tests/test_turn_audio_prefetch.py微修**：

- run_patched当前finally在两个with patch之外，且先恢复real_play再shutdown；必须在patch作用域内部的finally先_shutdown_harness，之后才恢复play与退出patch。结构是 `with patch1, patch2: try: fn() finally: _shutdown_harness(); restore_play()`，不能仍让with先退出。stage直接patch云/转换的测试也在退patch前完成回收。
- _shutdown_harness当前先置_shutdown_done=True再关闭资源，改为所有player/engine/TFM退出证明通过后才置True，失败保留重试；_require_quiet_engine与harness须校验aggregate.complete（stop_rvc=False），不只synthesis_idle。
- 未使用的wait_for_turn返回必须断言：正常first/serial/legacy等COMPLETED，新轮COMPLETED旧轮INVALIDATED；engine-only_signal_interrupt用例请模拟实际Core取消（对应turn compare_and_invalidate后再signal）并断言INVALIDATED，不以等待超时但忽略结果假绿。不得改变生产取消契约来迁就fixture。

只此测试与交接；唯一label scoped gate该模块+stream_playback/voice_session，STOP供Root最终fixture review。ADR在Root最终批准前不得自标Accepted；Root会统一更新最终实际图（不再使用过时request/StopRequest历史描述）。禁止raw测试/设备/模型/Rootprobe/其它重构。

## 前轮历史

Root `turn_prefetch_root18_review_20261104f`：18项全部PASS，WAV826零差。公开预取消补充 `turn_prefetch_root19_entry_20261104f`：19项仅1FAIL（见1）。相关A `turn_prefetch_root_relatedA_20261104f` 136项两FAIL（见3）；相关B `turn_prefetch_root_relatedB_20261104f` 182项一FAIL（见2）。两组WAV826零差，不是全通过。当前只做下面最终收口，不动已过调度/底层。

1. **已取消调用无副作用**：speak_stream(cancel_event已set)虽不prepare，却仍在entry清掉现有Engine interrupt。入口在任何clear前检查外部取消；epoch锁内提交点也读取消（可给_prepare_turn_stream_entry加可选只读cancel参数），不要只在2秒reap前检查。旧请求不得清新信号。legacy speak的raise_on_busy=False语义需覆盖pending/retained两类guard，当前_assert_resources在try外导致与其文档相矛盾；new stream/save仍明确抛busy且不fallback。
2. **真实回归**：CozySaveTests.test_queued_save_cancelled_by_interrupt_does_not_dispatch要求“已取消”，现变“epoch已过期”。保留原公开取消提示（可加epoch说明），不改断言为新字样来掩盖兼容。
3. **fixture迁移，不是放松生产**：tests/test_assistant_orchestration.py:_assistant_tts_shutdown_shell手工__new__缺_turn_stream_session=None、_turn_stream_session_lock=Lock，两个shutdown传播测试因此失败。补惰性资源状态，保留原所有complete/incomplete/依赖清理断言。不要为__new__假对象改生产绕过实际owner证明。
4. **新测试生命周期收口**：test_turn_audio_prefetch harness没有关闭共享TFM，只查engine.synthesis_idle；run_patched在finally恢复真实play/prepare时consumer可能仍活着。统一ExitStack清理：release所有gate→player真实shutdown→engine.shutdown(stop_rvc=False) aggregate.complete→TFM actual complete/owned=0/worker not alive→最后退patch/临时目录。主测试body失败时也按此顺序完成，patch须覆盖actualjoin。所有wait_for_turn结果必须断言，不靠prepare_log有fresh就算完成；waiter错误主线程确认、finally实际join。stage持久测试须用第二段cloud-save已进入屏障后才让第一段play抛同一primary（当前可能第二段尚未起就假过），确保真正验证晚到阶段，并保持原异常is对象。
5. **既有离线fixture设备隔离**：相关B中确实打印pygame导入警告；tests/test_audio_preparation_rollback.py:EdgePrepareRollbackTests._engine构造TTSEngine前未隔离AudioPlayer，可能mixer.init。请在此模块setUpModule/构造前用unittest有界patch禁用_detect_backend，和新harness一样无设备。若后续要回归voice_naturalness_round1也先隔离其构造器；不改生产AudioPlayer。所有module patch必须自动恢复，不在import时永久patch。

只engine上述入口/提示窄修与相关tests、简短ADR交接；已通过OwnedThread/TFM/RVC及模型声学权限不改，禁raw/discover/并发/Rootprobe。先scoped gate定点新tests+assistant_orchestration+CozyC1（唯一label，<=120s），STOP供Root独立19+两相关组后再验收。

## 返修 e/f 历史

Root `turn_prefetch_root17_review_20261104e`：17/17 OK。里程碑按源码补充“播放已失败、另一段云保存仍在途”的真实Edge准备阶段用例：`turn_prefetch_root18_stages_20261104e` 18项仅1FAIL，WAV826零差。主播放原错误已正确返回、owner仍保留；但晚到的第二段MP3保存回来后仍进入MP3→WAV（实际也可能继续RVC），取消只是producer循环读到了，**适配器用的stream_cancel仍是外部Engine/turn view，不含session永久取消**。

仅做此窄修：复用已有_MergedSessionCancel，session提供只读合并视图/查询，不新建独立可clear Event；Engine Edge/Cosy prepare回调都用“外部+session永久取消”的同一只读view。Edge现有stream_cancel参数直接传该view；Cosy的cancel_event/interrupted也读同一view。这样播放异常、关闭session等无Engine interrupt的场景，异步保存返回后会停在下一阶段前并交回已建路径回滚；不启动多余WAV/RVC。避免增加更多旗标/队列/线程，不改通过调度与底层。

补对应持久真实Edge阶段测试（只fake云保存/格式转换/播放，无设备网络；错误与actualjoin主线程确认，finally release gate）；唯一label新模块gate后STOP，Root18+相关回归验收，不进下一板块。

## 返修 d/e 历史

Root `turn_prefetch_root17_review_20261104d` 与修正实际准入注入入口后的 `turn_prefetch_root17_entry_20261104d` 均17项2FAIL，WAV826零差。Root注入钩子原用旧legacy _admit，d改为_prepare_turn_stream_entry后已同步探针；现在确实在真实共同准入返回后触发interrupt，仍会prepare stale。另一项换轮后释放旧future，producer不能自行退出。两者根因同一：**producer只合并自己的owner_cancel与session Event，未观察run的外部cancel/Engine interrupt/旧turn Event**。其它15项及子矩阵（含source/plan三类异常、Cozy None取消、IO pin与unknown迟启）通过，不重写调度/已过底层。

只修一个一致取消来源：run在启动owner前保存只读外部cancel，并同session永久Event、owner.cancel Event合并；consumer、producer拿credit/拉下句/prepare前与后/put_ready都读这一来源。已取消的run不启动prepare；prepare适配器也应观察同一永久来源，不只共享Engine Event（Cosy的cancel_event/interrupted可直接合并，Edge私有_prepare可增加向后兼容的可选只读cancel参数/检查，不得改RVC worker/TFM底层）。取消晚返后producer自己清自己的输出并退出，不等旧播放放行才感知换轮。

Core首句yield也在锁内确认turn_id/旧turn取消，锁外yield；将dequeue得到的旧turn Event固定传给迭代器，不读新轮Event。已有source generator/用户iterator不能强停时仍保留owner，不能清引用伪quiet。

源码小缺口顺带统一：Cosy wrapper对playable None不应另写一套清理（现在只except Exception，KBI/Exit会跳过剩余路径并丢temp_paths）；直接把None+temp_paths交给session已有统一finally/逐路径责任，非取消None由session报错，取消也保留原资源状态。无需为此新增分支框架。

仓库补真实Engine中断在准入后与old/new turn Event交错、迟返自清矩阵，所有模型/AudioPlayer均fake，finally实际shutdown(stop_rvc=False)/TFM complete先于目录移除。唯一label scoped gate顺序，STOP供Root重跑17与相关回归；不跑Rootprobe、不改Root脚本、不进下一板块。

## 返修 c/d 历史

Root `turn_prefetch_root12_review_20261104c`两FAIL；其中pin case的3秒截止恰等于已验收TFM固定3秒退避，Root已修夹具为6秒允许调度开销，**未改生产TFM/放宽预取上限**。新增确定性准入/中断交错后 `turn_prefetch_root14_review_20261104c`：14项3FAIL，WAV826零差、无超时。真实句间预取、唯一prepare线程、Edge6路径/Cosy4WAV、两后端串行、32段回零、empty按序回调、future三类异常、play原异常优先、unknown启动保留owner与IO pin释放后可恢复均已PASS，不重写调度主体。

### 本轮返修清单

1. **busy不应假成功（已复现）**：_admit_turn_stream_work捕获RuntimeError返回False，speak_stream正常return，无法取消的旧prepare仍活着时新调用无异常。新stream/save应明确报告资源忙RuntimeError（或窄类型），epoch过期/取消可与busy分开。既有speak兼容日志返回可保留，但不能自动fallback抢资源或让Core误记播放完成。
2. **中断原子性（已复现）**：_admit只在epoch锁内检查，返回后caller另取锁clear；窗口中新interrupt被清掉，Root会prepare旧请求。把epoch确认与本次clear置于**同一次锁段**；caller不得再次clear。旧session有界回收在该锁外完成。run/producer准备前也必须观察外部cancel/Engine interrupt+session永久cancel，不得只有consumer检查，prepare回调须使用只读合并取消。
3. **pending准入（已复现）**：已有TFM pending时producer仍prepare，consumer仅break且正常返回。先在新owner/start/prepare前拒绝资源忙，producer每次准备前也检查，不能只consumer事后break；包括Cosy retained/retire未解决输出。不要启动新infer或静默假成功。
4. **outer producer异常（源码确认）**：_source.next_work/plan/_track_paths异常不在当前prepare try内，只被OwnedThreadTask.error存储；consumer只查_prepare_error且看到EOF即正常结束。统一捕获producer整个循环异常（或统一读取owner.error），原对象传播，plan限长/上游iterator Runtime/KBI/Exit不得变正常EOF或无限wait。无需再加分散error旗标。
5. **迟返/None路径（源码确认）**：_put_ready遇cancel直接放弃但没有对该prepared item自清；需在交接失败时producer自己逐路径请求回滚并复核，保留未释放部分。Cosy prepare wrapper不能在playable.play_path=None时先throw丢失playable.temp_paths，应将所有权交给session或backend原fence；_play_item的None分支也应统一finally清理。主异常优先、pending真实保留，不改通过的TFM/RVC/worker底层。
6. **离线设备隔离和持久测试**：仓库_EnginePlayerHarness直接TTSEngine构造会调用AudioPlayer._detect_backend里的pygame.mixer.init，即使之后mock play也可能打开真实设备。必须在构造前patch AudioPlayer或其探测入口，杜绝设备探测；engine.shutdown(stop_rvc=False)避免触碰用户全局worker，TFM/producer/player均实际complete且先于临时目录关闭，patch作用域覆盖实际join。新增上述准入/真实producer source失败/迟返None矩阵，不再用fake TTS自测调度。

请只修当前这些边界，已通过预取调度不另起炉灶。唯一label scoped gate顺序，禁止raw测试/设备/模型/Rootprobe修改运行。完成STOP供Root扩展矩阵复审，不进下一板块。

## 返修 b 历史

Root扩展 `turn_prefetch_root8_review_20261104b`：8项/异常子矩阵7 FAIL，WAV826零差；首句现在能播放、串行与None路径错误通过，但**真实预取仍未实现**。下面新设计必须完整实现，不再用自造fake TTS调度替代生产证明。

### 返修 b 的具体问题与唯一调度方案

1. run仍由consumer遍历chunks并逐段 `_consume_prefetched(segment)`，play完才请求下一段；后台只是RPC换线程，首段play阻塞时第二段根本不会prepare。Root真实路径/三类future异常/迟返case都在second gate失败。请删除_request队列和consumer发请求逻辑：**producer自己拥有chunks迭代器，惰性拉取并计划/prepare**。循环为“拿1个credit → next惰性段 → prepare并登记路径 → ready单槽”；consumer循环为“take ready成为current（前一current已清理）→释放credit允许一个future →play →finally清理并确认路径 →回调”。serial在当前线程用同一惰性段源，不开producer。没有第二套prepare执行者，没有整轮flat，没有consumer请求队列。EOF/异常终态独立event，不向满ready塞哨兵。
2. play抛错后当前cleanup完全没执行；finish只置cancel/join，不处理ready/held/迟返，_cleanup_blocked也从不恢复。Root原play异常后owned=1，pin释放后文件仍owned、late shutdown incomplete。所有离开路径必须finally逐路径请求TFM清理，原主异常优先；清理后用is_owned复核，只保留确实未释放路径；finish/下次准入/shutdown推进同一固定有界集合的回收，不能永远sticky也不能清掉未释放记录。producer迟返cancel后自己负责交接失败项回收，actual exit和全部路径释放共同决定session可摘。准备失败隐藏路径还要看TFM pending/Cosy retained fence，不能仅held集合。
3. `finish`因 `_producer_started`尚False而跳过owner.join：start异常/未知迟启会被误quiet并丢session。无论start有无ack都用已通过OwnedThreadTask.join/snapshot判actualquiet；不要重新复制/猜测底层状态。Root probe已扩充第9项unknown迟启三类，待下一次复跑，不得改Root代码。
4. 准入必须覆盖 `.speak`（含single/Cosy）、`.speak_stream`与保存的新分配入口；现speak在clearing interrupt之后、仅多句分支检查旧session，单句/Cosy/保存可绕过旧producer/路径owner而抢资源。旧owner未quiet或路径/pending未释放时任何新音频准备都不得启动，也不应自动fallback另一个引擎；不改公开接口/默认音色，使用共用小型准入检查，正确先后顺序是检查旧owner→epoch确认→清本轮信号。prepare必须观察session永久cancel（可只读合并），而非只靠可clear的Engine Event。
5. 仓库多数预取测试仍 `_PrefetchProbeTTS` 自己实现“预取”，生产TTSEngine只测serial，51项因此假绿；daemon waiter异常主线程未检查，finally shutdown返回也被忽略。必须以真实TTSEngine+TurnSpeechPlayer、只替换prepare/play和本地fake后端证明上述时序/峰值/失败/unknown/旧轮取消；fake TTS仅用于legacy接口测试。所有target异常收集给主线程、gate结果检查、finally真实join/complete、TemporaryDirectory后退出。不得只mock未来音频已经ready来自测逻辑。

请一次按此调度方案实现并覆盖矩阵，只当前切片，不修改已通过OwnedThread/TFM/NativeJob/RVC底层；保留dirty，禁git回退/正式memory/真实模型设备/下载清理/提交推送。测试仅scoped gate唯一label前台顺序，新模块+必要相关回归；禁止raw unittest/discover/并发/Rootprobe，STOP交Root。

## 初稿历史（此前3项2FAIL）

Cursor会话73a8e323-f207-4ee9-b30a-bec19f997a35已STOP（exec46880结束），自报49项不代表验收。Root独立 `turn_prefetch_root3_review_20261104a`：3项2FAIL，预取/串行两条真实TurnSpeechPlayer→TTSEngine路径均在首句播放前超时；首段控制异常原对象通过（注意此用例尚未证明真正producer分支）。全部finally完成、监督器未超时、WAV826精确集合零差。

### 必须统一改设计，不能逐个加补丁

1. **整轮收集回归**：session.run先遍历全部chunks建立flat列表，生产未结束就不会开始首段prepare/play，丢失流式首句优势且正文列表不受音频credit约束。改惰性处理，不收齐整轮；首句入队时即能开始准备/播放，等待之后文本不得阻已准备首句。
2. **credit顺序死锁/推理并行**：当前先dispatch next，再take当前ready。第2轮试dispatch第3段时第2段仍占credit、尚未被take，正常流程无法推进。首轮同时dispatch第2段+主线程_prepare_sync第1段，也并行了两个合成/转换。建议**唯一producer负责所有非串行prepare**，按credit惰性拉取原句/分段；消费者只take→play→cleanup，在前段清理完成、下一段成为current后释放一个credit。只需一个ready队列，不需要work队列/丢job哨兵。serial分支同一惰性分段但不启动producer。
3. **Condition跨yield持锁**：speech_queue._turn_sentence_iter在with _cond内部yield nxt.text。改为锁内取出/记账/notify，锁外yield，禁止把状态锁交给合成/播放/回调。首句也须验证仍属本turn；turn_cancel必须在取item的同一锁段绑定旧轮Event，不能解锁后读到新轮取消Event。所有过期路径不能播旧句或偷新句。
4. **路径责任丢失**：session.finish无论quiet与否清空_held_paths，正常cleanup finally也不检查是否仍owned就difference_update；producer晚返/ready未消费无明确cleanup。强持有最多current+future，迟返自清，删除失败/unknown/IO pin/隐藏失败pending必须阻新session。只有producer实际quiet且本session全部路径实际释放才可摘owner，不得仅snapshot.quiet丢session。清理异常不得取代主play/prepare异常，不能把主错误捕获后在cleanup异常路径丢掉。
5. **配置/取消门槛**：Cosy _turn_stream_prefetch_enabled硬True忽略pipeline_enabled=False；修为串行开关对两后端有效。speak_stream当前先clear Engine interrupt再检查上轮session，可能恢复上一迟返prepare的取消信号。先有界复核并回收旧session且路径quiet，才允许新epoch准入/清信号；prepare也须观察session永久cancel，不仅共享可clear的engine Event。
6. **完成/空句/失败**：当前遍历flat期间在producer/计划侧提前on_chunk_done；应由消费侧按原句完整播放+清理后顺序回调，空句也只能按序完成。None播放路径若非取消应报失败，不能假完成。新测试应先enqueue首句、等first_play（尚未mark_production_finished），再给后句；否则只在full list或结束后开跑会假绿。

## 续作顺序与证据

恢复后先读本review+原task+实际源码；只交Cursor返修当前板块，Root严格源码复审与独立探针。新增实际producer KBI/Exit、credit满取消/迟返、old/new turn交错、actual thread terminal≠退出、删除失败与原异常优先、Cosy串行等矩阵；现Root3只是最小接线证明，不能作为全部验收。仓库fixture保持TemporaryDirectory、回收先于目录移除、主线程确认BaseException和实际join。禁止raw测试、并发或修改Root探针/监督器。保持正式memory/模型/Nahida/权限不动，不做精简/下一性能板块。

当前源码是未通过的工作区初稿；不能建议用户将其作为已验收版本实战。切片前快照在tmp/turn-prefetch-root-before-20261104（仅评审比对依据，禁止整文件回退覆盖既有改动）。未提交推送。
