# 后四项交付与恢复清单

用户授权 Codex 设计与严格评审、Cursor 官方 CLI 实现。每轮修复闭环后再进入下一功能。已授权必要代码/文档发送 Cursor 服务。当前分支 yilin_dev；保留既有未提交修改，禁止读取/修改真实 data/memory；无提交/推送动作。

## 顺序与完成条件

1. 前置 RVC 电流音：纠正有效参数矩阵 → Cursor返修 → Codex评审 → 24段真实RVC → 用户A/B选择。任何主观结论不能由频谱数值替代。
2. CosyVoice 正式接入：窄后端接口、隔离解释器、封存本地资产/安全能力、进程环境最小化、超时/取消/退出传播、资源预算、显式fallback；每个切片均评审。冷启动与模型共存风险先测量，不默认宣称实时。
3. 真实长测：先可重复的隔离测试库和固定非敏感场景，覆盖正常对话/取消/失败/重启/清理；再由用户参与30–60分钟麦克风/扬声器人工验收。记录CPU/RSS/子进程/GPU、热轮延迟、队列、回收；真实数据不纳入测试库。
4. 最终里程碑：跨模块独立评审、离线门禁和真实证据对照、退出与异常回收、记忆重启与维护正确性。明确仍需人工验证项，不能把自动化当作听感/设备验收。
5. 版本整理：整理文档与最终差异、检查敏感/模型/音频不被跟踪、准备可恢复里程碑。建立commit前核对授权和用户已有修改归属；不推送。

## 用量约束

用户指定的是Codex账户剩余额度到10%时停止，刷新后继续。早先未发现查询能力，现已发现只读`mcp__codex_app__get_usage_limits({})`。按rateLimitsByLimitId.codex各窗口100-usedPercent判断，null不当0；任一相关窗口<=10%保存阶段/会话/未闭环问题、停止Cursor和新开发，等待刷新确认。不得把上下文或Cursor额度误作Codex账户额度。最近读到短周期剩余24%、周77%（需继续实时查询）。

## 当前状态

### 最新有效状态（替代下方历史过程）

2026-10-18已由用户明确恢复开发，账户刷新（本轮首次primary剩97%、weekly85%）；不再处于旧10%停止状态。主Python3123.12.3、RVC/Cosy3.11.9核实。上轮官方CLI无残留节点。当前C2b2a性能收口，不进入下一部分。

Cursor2e2836a0完成初始化锁修复，root init/alias独立probe都exit0。239门禁失败2项，文件集合本轮起824→824；历史多出的测试文件不泛删。root有限EOF故障诊断第9次复现stop_failed=True且fake0.086秒就判存活，定位fake.wait不等线程便立即抛TimeoutExpired；startup超时用例则因_LineQueueStream空0.5秒错报EOF而提前失败，不可降低0.9秒断言。评审docs/codex-review-cosyvoice-fixture-contracts-2026-10-18.md。当前Cursor feacb619-c0db-421c-9971-cedd236bdb67 / exec81480只修专用fixture契约；无生产/设备/模型/真实库动作。交付后root严格复审+独立门禁，才实际默认30秒两模型CPU staging启动验证。


**额度停止：primary剩10%，weekly剩86%。已中断Cursor exec75291，停止新开发/真实测试，等待账户刷新。唯一恢复入口docs/resume-after-quota-2026-10-17.md；CPU staging初始化锁泄漏仍未通过，不得真实测试/接App。未提交推送。**


RVC准备结果修复b8b0d01b经Codex86项独立通过（见具名review）。真实60秒诊断仍失败：三模型e50897971109（RVC83.016/Cosy62.047秒），全部回收；去RVC两模型6f88b5abc6af也失败，shutdown/TFM当次incomplete不能称通过，进程退出后parent-watchdog回收需后续只读检查。首次无RVC诊断dddd5a6420f0是root观察器None错误，未加载模型，已修probe。

临时安装py-spy0.4.2在tmp/diagnostic-tools（不改运行环境/依赖），仅对本轮验证的实际子进程438696采样，无locals。tmp/codex_cosy_model_stack.json：CUDA storage.copy_→torch.load→CosyVoiceModel.load第66行；pinned vendor把checkpoint先CUDA、load_state_dict到CPU、再toCUDA，确认存在多余往返拷贝。派Cursor任务docs/cursor-task-cosyvoice-cpu-checkpoint-staging.md，仅构造作用域的三个固定checkpoint CPU staging；不改vendor/pin/参数/超时、不接App，收益未实测。当前Cursor d3604bbb-743b-4355-aa04-102750451b8a / exec30859，完成后须严格review+独立限定门禁再实际两模型诊断。当前账户最近剩21%，10%停止规则有效。


真实共存未通过，禁止C2b2b接线。8c91b8a59344实际Ollama2.063/RVC76.812/Cosy32.063秒失败，0音频，全部回收；第一次180秒树超时已停止。RVC隔离74.969秒，cap4对照74.453秒无实质改善，不写无效优化。Cursor已交付`warm_rvc_worker`契约返修（warm/ready假成功收口）+ fake门禁`tmp/cursor_rvc_coexist_readiness_gate.py`，**停止待Codex严格复审**；复审通过后才Cosy extended-cold60诊断（默认30不变）与真实共存复测。无活跃模型测试，无真实库读写。额度最近剩33%，仍需实时查询。


C2b2a已独立离线通过：ac1eec9e两边界返修后，root probes通过；全量1007 tests OK(skipped6)/106.096秒，临时集合644不变。评审顶部为权威。当前无Cursor；开始真实`tmp/codex_c1_real_gate.py --coordinated`固定两短句共存/首句验证，capture-only无设备、无真实memory。下一步成功后C2b2b owner接线。额度最新短周期剩47%/周92%，以实时查询为准。


C2b1已最终通过：独立245项/57.08秒，EOF确定性fixture修复后无泄漏；真实prepare17.953秒零synth，随后两句含Nahida转换4.172/3.609秒，完整回收，报告`data/test/voice-naturalness/cosyvoice-production-prewarm/59eaafcc50b8/report.json`。当前无活跃Cursor/模型worker。C2b1评审顶部为权威结论。下一步拆C2b2a独立三模型串行协调器→评审+真实共存→C2b2b App loader/关闭owner接线→C2c有界预取/长句预算/播放交接→D长测→最终里程碑与版本整理。

开始C2b2a任务`docs/cursor-task-cosyvoice-production-roundc2b2a.md`：独立串行协调器、显式fresh Ollama warmup结果、RVC启动失败不假ready；不接App。C2b2b须特别处理：新/未发布TTS在预热前先登记private in-flight owner；关闭设cancel并保留loader/pending owner直到实际退出，不允许先TFM.shutdown；已装配voice模式也要走显式准备而非早退假就绪；不在lifecycle锁内长等待。默认assembly不应无owner地启动Cosy，CLI已有voice_preflight.ensure_voice_stack可作为App完成owner绑定后的激活入口。后续设计不得直接把Cosy prepare插入旧assembly warmup造成rollback丢owner。

C2b2a首轮返修（六项 root probe 项）已闭合；Codex 第二次独立评审仅剩快照属性边界与 Memory `begin_fresh_warmup` 非原子回退两项 + mapping 别名小项，已由 Cursor 返修并加专项测试。`tmp/codex_c2b2a_review_probe.py` 与 `tmp/codex_c2b2a_extra_probe.py` 均 exit 0；`tmp/cursor_c2b2a_validation_gate.py` 一次 **234 OK**、~58s、`MIKU_SET_CHANGED=0`。**停止待 Codex**；仍不得真实模型共存或 App loader（C2b2b）提前接线。

CosyVoice **CPU checkpoint staging** 窄切片已交付：`checkpoint_loading` + runtime worker `_construct_automodel` 作用域；`tmp/cursor_cpu_checkpoint_staging_validation_gate.py` **236 OK**/~61s/644 集合零差。**停止待 Codex**；不得声称构造性能已通过，真实子进程收益由复审后实测。

C2a已独立通过：957 tests OK(skipped6)，90.02秒，真实监督器exit0，文件集合644不变；三个关闭状态probe通过，权威结论见C2a评审顶部。现在开始C2b1 `docs/cursor-task-cosyvoice-production-roundc2b1.md`，仅显式prepare API，不接装配/不自动预热。后续顺序C2b1复审+真实热首句→C2b2资源协调/loader owner→C2c有界预取/长句时长/播放交接→D真实长测→最终里程碑/版本整理。

当前Cursor session423a0495-6701-40f9-8c3d-d7a992a8756b / exec18522，尚未交付，必须严格review后才真实prewarm probe。root已备`tmp/codex_c1_real_gate.py --prepare-local`（真实RVC准备→本地模型prepare零synth→两句同句热合成，仅捕获无播放，180秒监督器），未运行。用户新链路试听问卷仍pending，不替代其原基线选择，不切默认音色配置。

C2b1最新覆盖上一行：423a0495交付后Codex确认prepare会对dead proc/reader/writer/fatal reader假报ready；返修d151a3c5已交付并停止，改为worker_reuse_ok+writer_alive检查；新增事件测试、无强清registry、正确120秒树监督器。Codex独立`tmp/codex_c2b1_ready_probe.py`已实际四项False/exit0，正在独立`tmp/codex_c2b1_validation.py`相关245项（下一tool结果会给exec ID），通过后才真实prewarm。当前无活跃Cursor。

最新：独立245 gate实际失败1项旧B EOF用例，文件集合644不变；单独复跑通过，fake stdout把0.5s无数据当EOF有时序依赖。C2b1仍未最终通过，真实prewarm未跑。已派Cursor修专用显式EOF fixture与C1测试线程预期取消异常捕获（无生产改动），当前会话和exec ID见本次工具返回；评审顶部完整说明。

再更新：9b8d1b4f仅fixture返修已交付，Codex复审与独立245门禁57.08秒通过，文件集合644不变。C2b1离线通过（评审顶部），开始`tmp/codex_c1_real_gate.py --prepare-local`实际模型准备→两句热合成捕获，无设备，180秒监督器。无活跃Cursor。C2b2尚未开始。

2026-10-17最新：额度接口连续读到刷新（primary used0→1%，secondary used0%），旧“剩13%”不再有效；按实时查询继续，10%停止线不变。C2a已由Cursor53c489e4实现，95c9716d与da8c3e5f返修关闭状态；当前Cursor均已停止。Codex纯fake`tmp/codex_c2a_shutdown_probe.py`三个断言场景（None/抛错/缺失API）通过，状态不完整时TFM不关闭且cleaner/character继续。正在独立全量门禁`tmp/codex_c1_validation.py --full-c2a`，exec40718，180秒超时，结果写`tmp/codex_c2a_full_validation.json`；**通过前C2a不记最终完成、不得C2b**。C2a评审见`docs/codex-review-cosyvoice-production-roundc2a.md`（顶部待补最终结论）。

后续建议继续窄切片：C2b1先client/TTSEngine显式prepare无音频/无自动调用（epoch/取消/关闭/单实例复用），独立复验后再C2b2资源协调与懒加载owner，最后C2c有界预取/中文长单句时长预算与播放取消交接。不能一次把预热、loader并发、生产者队列全改。已有Edge无界producer/关闭后晚到线程也须里程碑检查。真实C1采样说明首句模型加载与句间准备确实需优化，不能以fake计数宣称无等待。

C1已闭环通过（以下旧“仍返修”现为历史记录）：Codex独立274项/46.25秒、root owner probe全部断言通过、真实3组6段Cosy→RVC生成成功（监督器76.73秒）。本次TFM自有文件0、所有模型worker回收；miku WAV全局历史644不变。报告`data/test/voice-naturalness/cosyvoice-production-c1/1da48e17c2f1/report.json`有完整参数；短句冷19秒、热23–43字含转换5.8–8.3秒，实际播放/30–60分钟长测未跑。用户试听反馈pending；原基准音频未改。

下一切片C2a任务`docs/cursor-task-cosyvoice-production-roundc2a.md`：只装配可选后端与主程序共享TFM关闭状态，默认Edge/本地许可false；C2b预热/有界预取/长句时长预算尚未开始。C1最终评审顶部为当前权威结论。Codex账户最近短周期剩余13%，10%必须停止保存（需实时查询）。

B里程碑已通过：Codex独立900 tests OK(skipped6)，总81.76秒；真实cold/warm/after_idle 3/3（19.094/2.578/18.922秒），关闭与owned临时文件完整回收。详见B评审顶部。

开始C1任务：`docs/cursor-task-cosyvoice-production-roundc1.md`。仅TTSEngine可选本地后端、固定warm_companion、顺序有界单段、取消/关闭与owned WAV接口；暂不改assembly/默认配置、不声称端到端语音通过。C1复审闭环后才C2装配与资源协调。

C1仍返修：详见`docs/codex-review-cosyvoice-production-roundc1.md`顶部。当前Cursor a159cf50-7546-41df-82f0-d0bee6f47281 / exec46405，先修错误清理fixture，再成功路径OS延迟删除上限、continue_shutdown guard、重复retire拒绝。前会话c8dacdd2因监督器temp净增长仍反复回归而由Codex中止；当前data/tmp miku WAV644（旧540加测试缺陷新增，未泛删历史无owner文件）。不要把272子测试OK当整个门禁通过：监督器因为temp集合变化exit1。

独立`tmp/codex_c1_owner_probe.py`无文件/设备/模型副作用；`tmp/codex_c1_validation.py`120秒真实测试树监督器（相关B/C1/旧voice/security/stream，文件集合前后必须一致）；真实捕获探针`tmp/codex_c1_real_smoke.py`已准备但**尚未运行**，必须C1离线复审通过后才运行（真实Cosy→RVC、fake player仅捕获，不是设备验收）。C2还需预算化预热/共享TFM关闭顺序/未发布owner保留、模型共存资源实测，并复核实际播放与interrupt交接窗口；不得默认假称无延迟或无泄漏。

### 已恢复（本次优先）

额度已刷新：开始时短周期剩余99%、周74%。下文旧“已停止”是上一轮恢复依据，已由用户本次“继续开发”解除；10%停止线继续有效。

最新：启动诊断返修1689b38d完成，Codex独立23项OK。真实30秒probe：资源5.8s、WeText12.3s、Cosy导入2.6s，模型构造剩余时间不足；仅诊断extended-cold(60s上限、未改默认)另一次模型14.598s ready，之后output_rejected。已只读复现跨解释器原因：Python3.12/3.11同文件st_dev分别14415720814859885606/250453030，inode一致。

跨Python修复与派发owner竞态返修均已完成并独立复验（8项实际双Python测试、owner probe零spawn，评审见cross-python-identity）。真实默认30秒probe：cold成功19.672s、warm成功2.516s；空闲2s后sleep3s，旧worker仍teardown却被_ready复用，after_idle94ms失败。完整关闭/TFM回收仍成功。

当前Cursor session `9e15d26c-1a4a-47eb-9675-1900c7bf37bd` / exec `74125`，任务`docs/cursor-task-cosyvoice-idle-retirement.md`：父在新请求派发前识别idle退休/EOF、确认旧owner完整退出后再重建，不能放宽probe睡眠或静默重试已执行合成。交付后Codex严格复验，再默认30秒cold/warm/idle。C仍未开始。

用户已试听选定`data/test/voice-naturalness/cosyvoice-instruct/01_warm_companion_rvc.wav`：电流音最少且仍像Nahida。它是原始CosyVoice→RVC基线文件，**不是**rvc-artifact-eval的A/B/C/D后缀候选。报告确认CosyVoice300M-Instruct/中文女/warm_companion，但旧报告未记录完整RVC参数，不能只凭当前YAML猜当时参数；选定文件保留为声音基准，生产参数应用前需核对来源并保存显式配置快照。

- RVC artifact F1-F4经过Cursor逐次返修与Codex独立评审，工程复验通过；111项相关/773项全量通过。
- 真实矩阵24/24、90.4秒、无Worker/临时目录残留、基线12音频哈希不变，显存回到运行前约1.66GB。
- 已向用户展示同句基线与A/B/C/D试听，等待电流音/音色选择。用户再次要求继续后，推进可独立验证的可选CosyVoice后端切片，生产默认/音色选择继续保留。不得假称人工已经通过或选定候选。
- 当前切片A设计见`docs/cursor-task-cosyvoice-production-rounda.md`，总设计见`docs/architecture/adr-cosyvoice-production-backend.md`。
- 切片A已返修复审通过，独立相关74项OK(skipped=1)。当前进入B：`docs/cursor-task-cosyvoice-production-roundb.md`；未接TTSEngine/未切默认引擎/未运行生产worker。
- B初版已交付但Codex严格复审发现B1-B6，返修测试又发现B7 fixture线程泄漏。会话37dbffd6已返修交付（报告818项OK），但Codex隔离探针确认取消等待、dotdot路径、NaN输出、生成期间替换文件、inbound schema和满队列fatal仍未闭环。完整清单在`docs/codex-review-cosyvoice-production-roundb.md`顶部；继续B返修，禁止C和真实模型。前会话84fc3569中断的4个测试子进程已清理；测试须有进程树总超时。
- 后四项（生产集成、30–60分钟真实长测、最终跨模块验收、版本整理）仍未记为完成。
- 表达型CosyVoice诊断6/6已通过，用户反馈RVC更自然但有电流伪影。
- B离线独立859项OK(skipped5)/59.34秒、final probe通过、compile/diff-check通过。受控真实smoke启动失败，固定协议model_lock_busy：旧版固定model锁文件7个NUL字节，新版只接受1字节。失败均worker/reader/writer/TFM关闭完成，GPU约1.5GB基线，没有加载模型。
- 旧锁迁移已由Cursor `8a1e1800-6705-4757-9742-c861e5bde130`交付并停止。Codex独立检查model_lock实现，独立11项Windows实际锁测试OK(skipped1)。受控runtime自己持锁把7NUL迁为1字节，未删除/重建锁。
- **当前未闭环**：锁问题解决后，真实生产worker冷启动30.094秒仍未ready，被deadline终止。`tmp/codex_cosy_runtime_smoke.json`记录cold=false，worker/reader/writer退出完整、TFM owned/pending/inflight=0。GPU回到1525MiB，空闲14537MiB；没有真实音频生成成功。不能把旧eval6/6当新runtime通过。
- **B startup 诊断（Cursor 2026-10-17）**：已实现有界 `startup_progress`（7 stage×begin/end，硬上限14事件；仅 stage/phase/elapsed_ms）。Worker 用 `_PROTOCOL_STDOUT` 与第三方 `redirect_stdout_to_stderr` 分离；`cosyvoice_import` 与 `automodel_construct` 分阶段且导入期 stdout 重定向。父 `CosyVoiceWorkerClient.startup_progress_snapshot()` 不可变快照；progress **不**延长 `startup_timeout_sec` 默认30s。用户主观偏好试听：`data/test/voice-naturalness/cosyvoice-instruct/01_warm_companion_rvc.wav`（**未**改 RVC/config）。离线 **110 OK (skipped=2)** / ~35s，`tmp/cosy_startup_progress_stderr.txt`，RC=0。**停**在 Codex 严格复审；复审通过后才由 Codex 跑真实 fixed-text cold smoke 读 stage 定位根因。
- **恢复先做**：Codex 复审 startup_progress 切片 → 真实 smoke 按 stage 证据修根因（仍不先调默认 timeout）→ 真实smoke通过才能C。
- **恢复后其余顺序**：C接TTSEngine/装配（可选后端、默认Edge保留、取消/队列/资源协同）→严格review→真实固定文本→D隔离记忆库与30–60分钟语音/资源长测→最终跨模块review→版本整理。人工RVC去电流音选择仍待用户，不能代替听感验收。
- **已按用户额度约束停止**：最后只读查询Codex短周期剩余10%，刷新2026-10-17 12:44:03 America/New_York。当前无活跃Cursor、无模型worker；无提交/推送，真实data/memory未读改删。临时miku WAV仍528，未净增长。恢复先查额度，刷新后继续。
- 下一项任务设计已保存`docs/cursor-task-cosyvoice-startup-diagnosis.md`，尚未发给Cursor。先实施有界固定startup阶段诊断→Codex复审→真实smoke定位根因；不得直接放宽timeout或进入C。
- GPT-5.6 Sol调用被Cursor月度额度拒绝（刷新提示2026-11-10），未设置额外付费，Composer仍可运行；该额度不是Codex账户额度。已通过async请求用户提供Codex当前剩余额度，暂无回复；无法自行读取。

## CLI恢复经验

当前机器Cursor旧持久会话恢复易静默卡住；使用新会话流式输出验证实际工具活动。Auto对明确返修多次长时间重复分析，Composer 2.5已成功实现F2/F4；每轮仍须Codex审查。当前无活跃Cursor修改会话。不要把CLI result usage当成Codex账户剩余额度。
