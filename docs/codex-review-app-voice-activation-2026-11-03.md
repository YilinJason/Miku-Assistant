# App同步激活首版：CHANGES REQUIRED

## 里程碑561项门禁：仅两项single-flight回归，定点删除错误pre-check

Root561项101.485秒，2失败(skip1)，WAV826零差；`FinalF1CWaiterWakeTests.test_waiter_returns_promptly_after_shutdown` 和 `Finding1LifecycleTests.test_concurrent_tts_and_voice_single_flight`，见resource_milestone_nov3报告。

原因：ensure_voice_transaction在_reserve_lane等待前执行 `_fail_block`，把**另一个活跃加载者已登记的owner slot**当成失败pending，立即拒绝，无法等待成功，也无法被shutdown唤醒返回关闭原因。已存在拿票后的_fail_block重检，因此**删除拿票前这次owner/failed pre-check**，让活跃loading经过唯一lane等待；拿票后再区分真正pending/失败。不要改旧测试或把single-flight改成busy拒绝。closing快检查/已ready快返回可保留。explicit retry仍只在持票后清flag。

只定点该检查，Root现有supervisor60秒运行 test_storage_round2_review_fixes + test_app_voice_activation + test_mode_assembly_round2，原两Rootprobe各一次，STOP，别重跑100秒旧门禁。Root独立针对受影响范围后决定里程碑，不扩大功能。

## 第三次Root评审：基础9+异常/迟返6全过，但正常发布有P0回归

Root独立两probe全true（各约1.7秒、WAV826零差）。统一lane/slot架构方向正确，但补充普通成功路径直接确认：

- 新建TTS+STT ensure_voice_stack返回ok=True，**published_tts.closed=1**，激活时把刚发布的TTS关了。
- 已绑定未ready Cosy、缺STT，ensure_voice_stack返回ok=True，**build_stt=0且stt_engine仍None**。

### 定点修（不再重写整体）

1. **原子publication**：当前赋值app.stt_engine后又独立 `if need_build_stt and app.stt_engine is not None`，必将discard=True；owner还持有刚发布tts引用，随后retire把正式engine关闭。先在锁内判断closing/conflict，再一次提交所有需要的字段；成功move后清owner.tts/stt并摘owner_slot，禁止成功后再用新状态判断重复。只有真正未发布的owner可以retire。
2. **bound prepare不等于语音栈完成**：need_prepare_bound成功后只在没有缺槽时直接返回；需要STT则继续build并publishSTT，成功才ok。TTs已ready补STT失败不得永久封锁已有TTS重试，失败标志语义需限定（或明确explicit voice retry路径）。不要把prepare成功当STT存在。
3. **finally集中retire**：STT构造异常目前直接return只release_lane，未尝试retire新owner。把未发布owner清理放事务finally（锁外、只一次），所有return/exception共享；保留incomplete。cleanup异常须转incomplete并保留owner，不覆盖原KeyboardInterrupt/SystemExit或结构化错误。try_retire_unpublished_voice内 `_shutdown_engine`/状态getter的普通异常均False，不外抛；先登记owner再可能抛错。
4. **wait后重检失败封锁**：_fail_block只在reserve前，等待另一lane失败后snap.load_failed未检查，会自动重跑prepare；explicitretry也在拿票前清flag。改为成功拿票后短锁重检所有ready/failed/slot/closing，只持票者清retry flag。补两个并发bound prepare，首个受控失败，第二个不再prepare（计数1）。
5. **shutdown未知lane必须保留**：shutdown_retire_owner_in_lane对voice_loading=True但holder=None也必须返回incomplete不改lane，不能自造ticket覆盖未知inflight。删除voice_activation里未使用且无验证就clear引用的discard helper，避免保留危险旧入口。
6. 新增正常both-missing成功“不提前shutdown”、bound Cosy缺STT、上述sticky并发、STT失败锁外finally、cleanup抛错不mask原异常用例。别只跑之前9项；Root supervisor60秒包装unittest，不依赖pytest，不必另造重复supervisor。保留已有两个Root probe，targeted+probes一次后STOP。

本轮只修这五处相互关联的提交/失败语义，不动RVC/模型/设备/预算/声学/后台线程/数据；源代码评审通过才进入扩展gate和短真实诊断。

## 第二次Root评审：基础9项通过，仍不能放行

Root新版独立probe9/9通过、1.703秒/WAV826零差。真实外层prepare阻塞已修，pending基本路径已接入。但是只6个正式测试且仍没有统一事务；assistant语音区扩展成600+行重复逻辑，以下源代码缺陷必须闭环：

1. **仍锁内retire**：ensure_tts publish的closing/duplicate分支、_ensure_voice_stack_both同分支在 `with _lifecycle_lock` 内调用try_retire_unpublished_voice。shutdown会阻塞在长回收上。只在锁内记录“不能publish”结果，退出锁再retire，所有分支统一finally处理。
2. **pending retire没有single-flight**：_retire_pending_voice_if_allowed在占_voice_loading前执行；两个explicitretry可以并发shutdown同一对象；shutdown开头又直接retire同一pending，未走lane。将**退休pending也放唯一同步事务内**，shutdown忙时只cancel并保持，不能和loader并发retire。占loading之后，失败/pending/ready必须再snapshot重检，避免旧snapshot allocate。
3. **候选仍仅栈局部**：constructor返回后仅candidate.tts赋值，没有App当前candidate字段；请在build之前登记一个空candidate容器到App（loading保护constructor），返回后填同一容器，再prepare。active/pending共享一个owner槽即可；成功publish后clear，失败只在strict complete后clear。不要同时保留两套会覆盖的pending；_park现有pending时对新candidate best-effort后丢引用，必须删除此危险分支，用单槽准入防止第二candidate。
4. **异常会跳过独立关闭**：shutdown在定义_run_step前裸调用try_retire_pending，shutdown或status getter抛OSError/ValueError会直接退出，cleaner/character/memory都不关闭。先记录owner再捕获清理失败为incomplete，经_run_step继续独立步骤；原错误不能被cleanup异常覆盖。`tts_shutdown_reports_complete` getter任意普通异常均失败关闭，不能仅catch RuntimeError。
5. **重复end-loading竞争**：ensure_tts发现bound未ready时先_end一次，退出锁后又_end一次再转prepare。间隙另一路可占lane，第二end清掉别人的在途标记。只有本次lane票据在finally释放自己generation，禁止manual重复end。这里与之前RVC票据问题同型。
6. **测试未遵循监督**：项目的Root supervisor是Python脚本包装unittest，不依赖pytest！不得以“无pytest”为由裸跑。按 task 的60秒监督运行新模块一次（可加原mode_assembly/assemblyrollback/assistant，若预算不够只targeted，由Root扩展），失败STOP。正式补constructor迟返、pending shutdown/status抛错、重复explicitretry/retire gate、publish前关闭的锁外回收与单票据结束。至少覆盖以上实际App路径，不用只patch helper绿灯。

### 简化算法（设计伪代码，不作为生产复制）

```
public ensure_* -> _ensure_voice_transaction(want_tts, want_stt, allow_retry)
  short lane lock: wait with one deadline; reject closing; reserve generation
  try:
    short lifecycle snapshot: bound/ready/failed/candidate
    if pending: if !retry reject; else retire outside locks; retain if incomplete
    if build needed: install EMPTY owner slot; build outside locks; fill same owner
    build STT as needed; prepare target outside locks (only once)
    short lifecycle: if still current and !closing publish and detach slot
    if no publication: retire outside locks; clear slot only if complete
    return structured result
  except original: retire outside locks retaining incomplete; preserve original KBI/SystemExit
  finally: only this generation releases lane, notify
shutdown: closing/cancel -> notify; if lane busy retain/cancel only;
          otherwise try-own lane to retire slot outside locks, catch failures;
          independent closes continue; lane/slot incomplete always block TFM
```

保留from_dependencies的缺槽不构造兼容、Edge旧行为、原public/private兼容wrapper可保留，但删除重复事务/unused discard helper；不要再增加6个helper却保留三套完整build/retire分支。当前仅同步所有权，不新后台线程/模型/设备/预算/声学/预取。按全状态表检查后targeted+Root probe监督各一次，更新handoff/ADR STOP。

Root读实际调用方与6项测试，独立fake probe7.718秒：pending_candidate_blocks_new_allocation=false；额外观察两次prepare阻塞3秒，shutdown在外层lifecycle锁等待。没有放行真实模型或下一切片。

## 确认缺陷

1. **P0 锁内prepare/锁反转**：ensure_tts、ensure_voice、_ensure_voice_stack_both多处在 `with _lifecycle_lock` / `_voice_loader_lock` 内直接return `_ensure_bound_local_voice_prepare()`。RLock内层退出不释放外层，模型prepare整个在外层锁下；并发shutdown不能set cancel，另一路还可能重入非重入loader锁自锁。所有routing只能锁内取snapshot，**退出全部锁后**调用/等待/prepare/shutdown，不能凭helper自己锁外就宣称锁外。Rootprobe已加fake_shutdown_does_not_wait_for_prepare_lock，shutdown总耗时也须<300ms（fake shutdown本身无IO）。
2. **未实现候选所有权**：App没有任何candidate/pending引用；仍局部tts→_discard_unpublished_tts(仅shutdown不验status)→tts=None，旧资源就丢了。新模块的_UnpublishedVoice/discard/retire未实际接入且无complete检查，是死代码。必须实现真实保留，旧候选未complete时阻新allocation；pending纳入shutdown/TFM保持与重试，不可仅写helper名。
3. **失败bound自动重热**：_ensure_bound_local_voice_prepare不设/不看_tts_load_failed，失败后每次ensure重跑三模型。保持失败封锁，只有显式allow_retry且旧资源可安全重用/已回收才重试；closing/cancel不清失败事件重开。已有TTS只缺STT路径也必须先准备Cosy，不能绕过ready。纯Edge返回兼容。
4. **RVC选择错误**：require_rvc当前由settings.rvc_warmup_on_voice_start决定，它是调度开关，不是实际RVC启用。取engine实际配置/绑定converter enabled（真实TTSEngine有_rvc），或App封存cfg，明确参数传helper；RVC未启用必须不调用prepare_rvc，启用但预热配置不支持则真实失败。不能因为rvc_warmup=false就跳过用户要求的音色并报三模型ready。
5. **验证缺口**：6个测试多数直接helper/patch准备，没有验证真实App.ensure路径与constructor/STT失败/candidate未完成/锁外真实prepare。返回的状态图也无retained pending，不符合任务。不要用六项绿灯宣称候选闭环，更不能下一步就真实smoke。

## 收敛实现，勿再复制分支

建议三个公开ensure入口为窄wrapper，统一一个内部同步加载事务，保留旧入口可注入与VoiceStackLoadResult导入。状态仅一个loading lane、一个cancel、一个current-generation、一个candidate/pending owner（最多1），已发布tts保持。开始短锁确认closing/failed/pending并占loading；constructor返回先登记候选；build/prepare/retire在锁外；发布短锁再确认closing/generation；finally清loading并notify。

候选shutdown后用现有显式status契约严格检查，缺失Cosy状态/异常/False保留，legacy Edge允许既有语义。不要因_stt构造失败先把tts=None；先retire，未完成则保留pending。shutdown不持lifecycle等loader，loading/pending任何未知均不能关闭TFM或设置non_memory_done，但独立cleaner/character/memory仍执行。pending清理/explicitretry也走单flight，不分配第二对象。constructor尚未返回用loading保护TFM，晚返对象登记并回收。不要新增后台线程。

Root脚本 `tmp/codex_app_voice_activation_2026_11_03.py` 不可改；真正调用ensure与shutdown，提供真实fake warmup API（无模型）。增加fixture在finally释放gates/join与statusflip后重试，包括失败pending pre-shutdown再次ensure不得allocation。改动前自行走全状态/锁表。

仅本切片修复；targeted60秒监督一次+Rootprobe20秒一次；失败STOP，不重跑旧大gate，不碰模型/main/设备/data/memory/提交推送。更新ADR/handoff并STOP待Root。
