# RVC显式重试：Root最终验收PASS

本切片已闭环。额度恢复后Root复审并让Cursor修正fixture finally/patch作用域、quietFalse二次清理假验证、完整engine cleanup与foreground结果确认。最终独立回归：定点38项（label rvc_retry_root_repo_final_20261104o）；所有权/IO/owned-run 85项（rvc_retry_root_ownership_final_20261104o）；最终26项+App/前台/装配/CLI/Cosy合并131项（rvc_retry_root_app_cli_final_20261104q）。分组存在重叠，不能宣称254个唯一用例；最终131包含最后修订的26项。所有报告均tmp/codex_gate_<label>_2026_11_02.json，WAV826精确集合零差，最长36.125秒无timeout。此前生产Root8+13+2=23边界PASS有效，恢复后只改tests/交接，不重复未改探针。源码审查无新的生产发现，无模型/设备实测、正式memory未访问、未提交推送。

允许进入下一资源治理切片；不代表真实三模型共存或长期无泄漏达标。下面保留各轮发现与修复依据作为历史。

最新Root独立验证：`tmp/codex_gate_rvc_retry_final_root8_20261104i_2026_11_02.json`、同前缀root13/root2，8+13+2全PASS、WAV826零差。源码AST对比切片前快照无函数删除，worker只改constructor/start及新增lease/fence，engine只改prepare/新增retry，无NativeJob/IO/合成播放关闭底层变动；编译与diffcheck通过。Cursor下一轮仅补持久回归与测试回收，Root独立模块回归后才最终验收。以下为已修问题历史及验证要求，不把历史FAIL冒充当前仍有同一生产故障。

## 最后返回边界及长期回归（atomic13已由Cursor通过，待Root）

当前Cursor f报告原8/atomic13/12+75+67通过。Root沿prepare_local返回后发现最后一个取消缺口：`run_bound_rvc_explicit_retry`在Cosy prepare_local后直接锁内发布，未再查cancel。独立completion probe两项（`tmp/codex_rvc_retry_completion_root_2026_11_04.py`）一FAIL一PASS，报告`tmp/codex_gate_rvc_retry_completion_root2_20261104g_2026_11_02.json`：local prepare内set cancel但returnTrue，动作仍ok并把app._local_voice_prepare_ok置True；True但metadata未完成的另一项已正确拒绝。须在local prepare返回后、发布临界段内和无app返回成功之前同样检查cancel。动作取消不发布、不继续阶段，保持generation/closing/同engine验证；不可把在途所有权丢掉。无需重写架构。

仓库测试仍12项，Root原8/atomic13只是tmp独立验收，不能替代持久回归。请把本轮已修的取消退休、策略切换及ctor取消fence、fresh零/一次调用、Cosy成功的实际App ready、缺STT继续装配、返回取消/未完成metadata等场景落在`tests/test_rvc_explicit_retry.py`自含可靠用例（不import任何tmp Root模块、不修改Root、不靠宽松Mock提前return）。非重复地参数化，可复用小fake factory降低样板。

线程测试还需在finally里实际join且断言notalive、入口wait结果校验、目标异常/终态主测试确认；foreground的release.wait(5)未校验，job缺失时if跳过断言不能通过。用明确Job非None、done/result验证，不凭daemon或超时自动放行。当前普通App测试也应关闭本次engine/app，不读正式memory。

先completion2+原8+atomic13+仓库新测试，唯一label全部通过后必要旧分组回归；compileall/diffcheck。完成STOP供Root独立验收，不进入下一板块。

## 当前最终修正清单：统一状态迁移及调用链（续审六项已有进展）

Cursor上一轮12+75+67 tests及原Root8/atomic6通过；Root在真实调用方复核后把atomic探针冻结为**13项**，最新独立报告`tmp/codex_gate_rvc_retry_atomic_root13_20261104e_2026_11_02.json`：6 PASS、7 FAIL，WAV826零差。d版本STT用例曾误传无此参数的public ensure签名，是Root fixture错误；e版本已改实际ensure_voice_transaction调用，稳定复现漏STT，不需更改public ensure签名。Root原8和atomic13不可由Cursor修改。

不要逐处补bool，使状态迁移统一成：拒绝不分配 → 接受独占且标inflight → 取消检查 → fence并真实quiet旧owner（不quiet保留）→ 再查取消 → 新准备 → 验证实际ready → lane检查generation/closing并发布所需就绪 → 若请求STT仍须走原事务装配 → final result；finally对**所有**取消/异常路径收口inflight和lease。

1. **退休后取消永久inflight**：retire期间set shutdown再True，retry handed_to_prepare=True；prepare入口merged取消直接return（尚未进入finally），inflight永久True且attempt_completed False。retry拥有全过程状态，在finally无条件收尾（或等价明确私有body），不靠handed旗标赌callee进入finally；失败/取消仍completed失败sticky，不能新请求自己清别人的inflight。原异常/KBI对象保留。
2. **fence分支未覆盖**：get_rvc_worker策略切换仍stop后mark；构造迟返cancel分支仍直接stop+清单例未mark。这两条是永久退休，同样须在stop前fence，quiet False强owner保留。复用统一窄helper，避免各分支复制不同顺序；普通client.stop/interrupt可重用不变。
3. **已取消仍启动Ollama及双预热**：run_bound先start_ollama再fresh，cancel已set也调用两者。fresh本身会开启/复用generation，无须额外start；若首个start很快完成，fresh会再冷/热请求一次。入口及fresh helper调用begin前检查cancel，等待/重试/本地prepare之间检查取消，任何已取消动作返回失败且不再启动后续阶段。无LLM skip旧语义不变，现代失败必须零retry；不要改非retry协调器。
4. **Cosy成功未发布**：lane在retry后提前return，_local_voice_prepare_ok仍False；同步ensure ok但app_tts_playback_ready永远False，foreground会出现已完成job却pending。成功后在app lifecycle锁内核对同tts+ticket+未closing再发布Cosy准备成功（沿用已有原子发布语义）；失败不置ready，不允许关闭后发布。
5. **完整语音栈漏STT**：same earlyreturn在want_stt=True且缺STT时跳过build_stt_engine。retry成功后应进入原事务继续装配所需STT；不重复warm健康TTS/Cosy、不重建现有STT，失败按原状态/ownership回收。完成请求只能在全部请求依赖真的满足时ok，不能无job永久pending。
6. **测试可靠性补充仍未处理**：见下方“新测试本身亦需收口”。新test_ollama_finished_not_succeeded_blocks_explicit_retry用了无合法state的MagicMock tts，会在“无需重试”处提前返回，是空验证。用真实失败metadata并断言begin/snapshot确实触达、retry零次。线程例均finally释放/收异常/实际join并is_alive断言，App/Engine有界关闭，不依赖wait超时自动放行。

额外源码核对：retry返回True后state应确认enabled+completed+ready且notinflight；不能只排除sticky/readfailed让未完成metadata变成功。取消后的warm ready也不能发布（prepare在warm返回与赋值处再核对merged）。这些与上述统一状态流程一起处理并覆盖行为测试，不另起板块。

先Root原8+atomic13+仓库新测试各独立唯一label，失败停定点修复；通过再RVC/App/foreground/Cosy/CLI相关分组。只当前切片，保留巨大dirty/全部权限和声学/正式memory禁读改删，无模型设备下载，Root探针监督器不改。完成STOP供Root源码及独立复测。

## 续审：Root8 PASS 仍有未闭合窗口（2026-11-04）

Cursor CLI 会话6285fbdf本轮完成9+75+67项回归，Root8独立PASS。源码复核另新增独立`tmp/codex_rvc_retry_atomic_root_2026_11_04.py`六项全部FAIL，报告`tmp/codex_gate_rvc_retry_atomic_root6_20261104a_2026_11_02.json`，全fake、WAV826零差。第三项探针随后仅调整为允许正确的直接原子lease实现（若不调用旧bool探测，不强求False），未改变所复现竞争事实。

1. `infer._client_idle_for_retry`/`worker.work_mutex_held_by_operation`仍然只是拿锁再放锁的观察，观察返回后新操作可接受工作锁，随后retire仍stop。应当在global gate内非阻塞**持有实际工作lease**，在确认idle、登记永久退休fence之前不放开准入；busy零stop。未知/缺工作互斥拒绝，不用私有flag替代。保持stop有界，不在状态锁内长join。lease用try/finally释放；旧bool helper不再作为安全准入。
2. `_stop_client_quiet_if_global`、`stop_rvc_worker`、策略切换/cancel清单例分支都在stop成功后才mark；stop False的旧ref仍可复活。永久退休fence须**在stop之前**登记（显式retry busy拒绝不能mark），quiet False仍保留强引用且之后可继续stop。取消构造分支亦不可清掉未标记的旧ref。常规client.stop/interrupt保持可复用。
3. `worker._start_locked`仅入口检查globally_retired；慢metadata/path/preflight过程中退休后，最后spawn准入未重查，Root精确在resolve路径时mark，仍触发spawn Mock一次。把不可逆fence并入受spawn锁保护的最后准入（及现有generation/发布有效性判定）；不改NativeJob/IO底层，不持全局gate跨模型加载。
4. `warm_rvc_worker`缺warm_up返回后的cancel检查，warm内部cancel且返回True会报告True且不quiesce。补精确owner取消回收；False/异常路径也不能漏取消所有权。不得停foreign全局对象。
5. `run_bound_rvc_explicit_retry`用了旧`_wait_ollama_warmup`；实际`OllamaClient.wait_for_warmup`只说明done，不说明成功。finished=True/succeeded=False仍执行retry并ok。现代client应复用现有fresh generation+finished+succeeded有界轮询（可公开窄helper），Edge/Cosy相同；cancel贯穿等待，取消前不启动预热。无LLM保持skip；有client缺可验证成功API不能凭wait True或缺API当成功，不能伪造snapshot。勿借此修改正常非retry协调器全局语义。保留旧测试兼容时给fake明确真实契约，不放宽断言或模型失败门槛。
6. `TTSEngine.retry_rvc_preparation`退休阶段未发布inflight。接受独占工作后应在退休前设置inflight，所有return/异常finally恢复；内部prepare须明确处理此入口的自有状态，不能因自己设置inflight而短路，也不能提前暴露无inflight窗口。失败仍sticky，新epoch取消不清掉，KBI/原异常传播不改。

请一次闭合以上问题并补仓库行为用例，再先Root原8+新增6及新测试，失败即停；通过再原有分组回归。Root探针/监督器不可修改，不做下一板块，不启动真模型设备、不碰正式memory。既有源码和未知dirty保留，禁止git回退类命令。交接写真实证据并STOP供Root复审。

### 新测试本身亦需收口

`tests/test_rvc_explicit_retry.py:test_late_get_worker_cancel_skips_start_and_warm`的`gate.wait(2)`结果未校验、线程异常未回传、join后不查is_alive、失败无finally释放。`test_foreground_explicit_retry_short_budget`只等待超时自动放行，未release gate/观察job终态/实际join且不shutdown assistant。不能把这些9项绿灯当严格线程回收证据。用Event明确进入/释放，线程target收集BaseException由主测试重新抛出（不吞），finally释放、真实join+is_alive断言，所有App/engine在finally有界关闭并验证相关状态；失败fixture也不残留。前台测试断言pending且notok，不以宽松`pending or warnings`代替。本轮补用例不可重复这些问题。

Root自有probe tmp/codex_rvc_retry_root_2026_11_04.py **8项全部FAIL**，0.038秒，WAV826零差，报告tmp/codex_gate_rvc_retry_root_ownership8_20261104_2026_11_02.json。全Fake且包含实际RVCWorkerClient的旧引用检查，无真实模型/设备。首版probe有一项Root fixture错误导入，已改自含warmup API并重跑，当前8项均是源码合同问题，不把那次fixture错误算生产发现。原task中的Cosy对称重试属于本轮范围，不接受“另开子项”遗漏。

## 一次修复的范围

1. **取消后的未发布所有权**：get_rvc_worker构造迟返发现cancel、_shutdown_existing_client=False时直接return client，未发布到任何持有处。必须在持gate的创建流程先登记本次client/对应key，或保留等价强owner；stop未quiet不能丢引用。旧client已完整退休时先清旧global再构造，避免构造取消/异常后仍留下已关闭旧单例。返回cancel False不等于资源quiet，_quiesce失败引用也应可重试回收。不要用扩大无界list补洞。
2. **idle判定缺原子工作互斥**：_client_idle_for_retry只读converting等flag，在工作_lock已被接受但尚未发布converting的窗口仍stop。用实际client现有工作互斥非阻塞lease（可窄method封装在client），与global lifecycle gate的profile校验/stop闭环；busy立即False无stop副作用，未知idle也不能当idle。不要在状态锁内长join或做新的锁嵌套，保持现有停机有界且可重试。
3. **退休旧引用还能复活**：stop_rvc_worker清global后，已拿到真实client旧引用仍可start触及spawn。区分可复用pause/interrupt的stop与全局永久退休/shutdown_checked；永久退休有不可逆generation/closed fence，start/convert/warm不再创建资源。普通stop/interrupt后重用旧契约保留。全局退休前登记fence，stop不完整继续持有并可再stop，不是强行标quiet。不能改NativeJob/IO lease底层来掩盖。
4. **退休→重建之间切换policy**：retry retired后普通prepare→get_rvc_worker可能遇到新foreign policy/key，普通factory会stop它。显式retry必须在get/rebuild时仍限制同policy/key或空闲预期代际，遇foreign直接失败，不借普通factory替换逻辑停别人。可用窄的禁止替换模式/expected owner ticket，不要求把耗时model start放在global gate里；不要TOCTOU先检查再外部普通get。保留普通非retry调用的既有策略切换语义。
5. **串行Ollama门槛和Cosy遗漏**：lane._run_bound_edge_rvc_retry直接retry，绕过serialize_gpu=True的Ollama等待。Edge/Cosy显式重试都应走现有协调次序，Ollama未完成/失败时零retire/零新warm；Cosy绑定的失败RVC必须选择retry方法，而不是再调用sticky prepare_rvc，之后再准备本地TTS。Core/协调器传显式retry意图，默认旧接口仍兼容；不靠catch TypeError再调第二次。没有LLM可按旧skip语义，绝不伪造成功snapshot。
6. **动作失败假成功**：_run_bound_edge_rvc_retry忽略retry返回，始终voice_stack_load_ok；Root实际False返回却得到ok=True。明确区分普通fallback可播放与本次显式RVC重试成功：失败动作返回失败，后台job.result/前台wait/最终转换不能被fallback-ready覆盖。退休/重试阶段应准确发布inflight；显式请求在途仍pending，完成失败不出现无job永久pending；健康/无RVC复用和普通fallback+warning保留，TTS已健康而STT在加载的旧独立就绪行为不能回退。

保留新cancel bridge对get前后/start后/warm前后的检查，不能停新foreign全局client；新epoch/取消不被清掉。Root fake真实client stale reference的spawn是Mock（只计数），不能把它看成可运行真模型的授权。不得修改Root8 probe来变绿。

## 验证与交付

先读AGENTS/四skills、当前task、该review及源码，保留巨大dirty，禁止git checkout/reset/restore/clean/覆盖旧文件回退。Root快照只是初始基线，不是新实现。正式memory禁读改删，无设备/真模型/下载/系统设置、声学/预算/权限/NativeJob不改，未提交推送。

新增参数化行为测试覆盖上面每条与同policy idle→实际quiet→一次新prepare成功、stop不完整后可继续cleanup、取消迟返零start、健康复用、不自动重试。先Root8+新重试测试一次，失败STOP；通过再RVC所有权/IO/owned-run、App/CLI/Edge/Cosy/c2b2a分组现有Root scoped gate各<=180秒前台一次。唯一新label，报告不可覆盖，不detach。写交接及真实JSON路径后STOP待Root，不提前预取/精简。
