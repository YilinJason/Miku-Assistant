# TFM资源治理：Root最终验收PASS

最终源码审查完成，当前无未关闭生产发现。Root独立：stale1、bounds9、claims5、加强errors2（含KBI/ValueError/EIO矩阵）全部通过，labels tfm_complete_{stale_root,bounds_root9,claims_root5,errors_root2}_20261104p；旧持久IO fake probe已隔离构造时的硬件事实，6个断言全True（tfm_complete_persistent_root_20261104p）。最终相关模块175项/16.875秒OK，报告tmp/codex_gate_tfm_root_accepted_contracts_20261104p_2026_11_02.json，包含最后27项仓库TFM用例。所有WAV826精确集合零差。代码编译/diffcheck通过；SafetyPolicy所有函数AST对比前置快照无改变，仅TempFileShutdownStatus增加兼容默认字段；选定Nahida参考音频SHA256保持7C4AEE8B69428E4BBE3BB773CFC47EFAE1618EFB46B7B80EBAF1465739FE19BA。

旧security3a模块漏host-memory module fixture已由Cursor补齐且Root175中独立通过，不等用户释放内存、不放松生产准入。原生进程/IO/Safety权限未改，未读改删正式memory，无模型设备/真实长测、未提交推送。允许下一预取生命周期切片。OS单次不可中断操作仍是现实限制，不能把离线PASS表述为全硬件无卡顿/长期无泄漏保证。以下保留各轮问题及证据历史。

## 最终出队路径：过期项结束了整个worker，还撤当前新身份pending

Root独立加强异常2+bounds9+claims5以及174项相关模块均PASS（tfm_last_*_20261104l / tfm_root_final_contracts_20261104m），权限函数AST对比切片前无变化，Nahida参考WAV hash保持原值。最终源码复核定位worker出队身份不匹配分支：无条件_pending_cleanup.pop(key)且break退出while，而非只丢弃本条过期任务。

独立确定性重现：tmp/codex_tfm_stale_queue_root_2026_11_04.py，报告tmp/codex_gate_tfm_stale_queue_root_review_20261104n_2026_11_02.json，1 FAIL、WAV826零差。用Event卡住首条worker；另一个旧文件已排队，前台先安全删除，再adopt新inode同路径并排新任务，外加第三个文件。释放worker后旧任务让worker停止，后面文件永远未清，且旧项清了新身份的pending。

只修这条出队生命周期：**先**验证本队列项当前身份，再改queued/inflight/pending标记；过期项只处理自己的task_done/旧意图并continue消费，不结束worker，不清同路径新身份的pending/queued标记或active claim。每条get的task_done准确一次，过期项不等待延迟、不冒充在途。避免把break盲改continue而继续误清新身份。补同样Event控制的持久回归。

先Root新stale probe+仓库模块新label监督器，过后Root9/claims5/异常2及必要组，失败停。旧174证据保持但不能代替修改后复核。只这一分支及测试，禁止改Root/权限/正式memory/真实模型设备/git回退/提交推送，不进入下一板块。交接STOP供Root最终验收。

## 最终接口一致性：失败删除仍未计入pending（原任务已要求）

Root当前9+5+异常出口旧矩阵均独立PASS（labels tfm_accepted_*_20261104j）。复核供预取使用的pending_cleanup_count接口，发现只有锁文件/pin路径登记意图，永久OSError(EIO)及KBI/ValueError出口虽保留owned但pending=0；RVC失败隐藏输出可能因此对后续准入不可见。这属于原任务“pending含删除失败”的遗漏，不需新框架或所有权group。

Root加强既有异常probe，仅增加失败仍owned时pending=1及一个EIO子场景（代码文件同名，现2项/3失败），tmp/codex_gate_tfm_pending_error_visibility_root_20261104k_2026_11_02.json。旧异常同对象/claim释放/pin和最终shutdown断言均保留。

本轮只统一合法删除请求意图：正常remove已接受确切owned身份后，成功撤销时清意图；永久OSError/未预期异常/取消保留当前同身份owned+pending，标耗尽或明确阻止pump自动重试，不能重置预算/自动另开线程。锁占用仍走现有有界重试；拒绝权限/陌生路径不获得新删除授权。所有原异常同对象传播和claim finally不变，明确新remove/shutdown可再推进。记录必须遵循刚修的pending属于owned同身份不变量，不复活旧队列/替代物。补持久矩阵，先Root加强异常2+仓库模块新label监督器，再Root9+claims5和必要相关组，不改Root文件、不扩其他范围。完成STOP，Root最终复审后再预取。

## 当前最后问题：已撤销身份的旧队列项重造幽灵pending

Cursor在delete-errors轮说Root pinned replacement“偶发，复验通过”；不能用复跑绿灯关闭未知失败。Root检查原探针确有“异步尚未quiet就立刻断言pending=0”的时序问题，已改为release后先shutdown真实quiet再断言（不改安全/所有权断言），并在失败信息打印状态。改后稳定复现生产错误：tmp/codex_gate_tfm_replacement_root_diagnostic_20261104g_2026_11_02.json，status=complete False、owned=0、pending queue=0、inflight=0、worker_alive False、lease=0、pending_cleanup=1，两个startup旗标False。即旧身份已撤销且线程确实结束，仍幽灵pending。

原因：旧队列项在is_owned撤销文件身份后被worker出队，关闭分支_requeue_locked→_note_pending_cleanup_locked无current-owned身份验证，重新写入一条无owned的待删记录；shutdown的owned循环再也不会访问它。

本轮只收口这条不变量：pending必须是当前owned且同身份的子集。_note/_requeue/_enqueue/pump对过期身份/无owned旧任务直接丢弃或撤旧任务自身，不重造pending、不误删替代文件、不撤当前新身份，不释放仍实际在途的claim。停止时旧队列不必为了保留意图而复活已撤销对象。补Event控制旧项在身份撤销后进入stop分支的持久用例；不靠sleep/循环重跑制造绿灯。原Root9安全例只强化先证明quiet，Root文件禁止Cursor修改。

先Root9+新增仓库用例单模块新label监督器，过后必要相关组；只修改此状态不变量及测试，别重构别处。ADR/handoff真实证据后STOP待Root最终复审，无预取/模型设备/正式memory/权限变化/提交推送。

## 最后异常出口：同步删除claim未在所有异常下释放

claims续审已由Cursor修复并报Root9+claims5+仓库22通过。Root源码检查`_perform_os_remove_outside_lock`只有OSError和成功分支释放claim，foreground remove也缺整个已接受claim范围的finally。独立tmp/codex_tfm_delete_errors_root_2026_11_04.py（1项含KeyboardInterrupt/ValueError两个subTest），报告tmp/codex_gate_tfm_delete_errors_root_review_20261104a_2026_11_02.json，两个均失败：原异常虽传出，但后续同owned路径仍PermissionError“删除进行中”，shutdown不能完整收口。

本轮只修这个异常出口：把确切claim的释放收口到涵盖身份复核/真实删除/提交的统一try/finally，OS删除可能已经结束/失败时也不遗留claim；原KBI/SystemExit/普通异常对象仍传播，owned身份按实际文件状态保留供再次安全回收，不能直接丢owned或改成成功。避免各分支重复pop以及claim=False时误释放别人。worker/shutdown若已有等价finally保持幂等。将同矩阵加入仓库持久回归，验证后续pin可接受、最终shutdown真实complete。

先新Root异常probe+Root9+claims5+仓库模块顺序新label监督器，每项失败停修；过后必要安全/持久IO/Cosy回归。不要趁机重构其他逻辑，不改Root探针监督器；无真模型/设备/正式memory/权限扩大/提交推送。交接真实JSON后STOP供Root最终源码复审。

## 删除claim续审：Root独立5项中3 PASS/2 FAIL，剩余身份竞争需闭合

Root新probe tmp/codex_tfm_claims_root_2026_11_04.py，报告tmp/codex_gate_tfm_claims_root5_review_20261104b_2026_11_02.json：3PASS/2FAIL、WAV826零差。a版Root fixture先join仍正常循环的worker导致清理断言盖住原错，Root已改为先请求shutdown再join；b版2个生产失败稳定且fixture实际quiet。致命start原对象传播、构造失败release零start、已证实退出worker明确请求恢复均独立PASS，不再改它们。

1. **adopt忙检查被幂等早退绕过**：adopt_existing先_capture有效owned就return，后面的_path_delete_busy_locked根本没执行。Root在OS删除已进入时adopt同path仍成功。busy/claim检查须先于重复adopt返回；pin/adopt都不能接管待删身份。
2. **duplicate remove不看已有claim**：remove仍直接在registry锁内os.remove，无busy准入，另一线程可二次删除同文件并再次卡住全局锁。重复remove对已有claim/inflight应安全幂等返回/保留同意图，不启动第二OS删除；正常同步remove也以同一窄claim helper锁外IO，保持同步返回语义，不能为了兼容保留这个竞争。claim helper不能覆盖同key既有claim，最终释放只属于本次确实取得的claim，不能在claim=False的finally误pop别人。
3. **撤销登记提前释放在途资源slot**：_revoke_locked直接pop_delete_claims/discard_inflight_paths，而os.remove尚在外面阻塞。Root owned_capacity=1，在删除途中外部移走fixture文件→is_owned撤销登记→create居然再获名额。撤销文件身份只撤销_owned/本身份待删，不撤销仍在执行的claim/inflight；实际执行者finally才释放。容量应计算owned与保留claim的去重资源集合（硬界），否则可以反复撤销再创建绕过上限。保持外部替换物不被当owned删，旧完成不动新身份。加对应持久行为用例。
4. **fixture精确收尾**：WorkerControlTests.test_late_start_sees_stop_and_exits的allow_loop.wait(2)未校验、主路径无finally、最后用sleep轮询推断结束。改成Event握手、目标BaseException主线程确认、finally release→捕获本次真实Thread→join+notalive；不要只扫描名字或循环猜。WorkerTerminalAliveTests用实际wrapper的终态/结束Event而非sleep猜，所有异常路径finally退出。前述Task与review已多次要求，请本轮一次完成，不仅修新增测试。

本轮冻结Root9和claims5（Root刚修自身cleanup后冻结，不准Cursor修改）；先两probe+仓库模块新label监督器前台硬超时，失败停，过后必要安全/IO/Cosy组，ADR/handoff真实JSON后STOP。源码状态尚待验收、不进入预取，无真实模型设备/正式memory/权限/系统设置/NativeJob改变，无提交推送。

## Root续审：原8项独立PASS，阻塞删除时有界关闭FAIL

6551daf2会话已STOP并声称原8+16新测试及旧组通过。Root实际独立9项报告tmp/codex_gate_tfm_bounds_root9_review_20261104a_2026_11_02.json：8 PASS、1 FAIL、WAV826零差。第9项`test_blocked_worker_delete_does_not_block_bounded_shutdown`在os.remove处Event阻塞，shutdown(timeout=.03)因worker持registry锁无法返回，.7秒内仍未返回；finally明确释放并join后才结束。该探针为Root后来补充，Cursor原8报告保持有效历史，不称其已测第9项。

### 本轮必须闭合

1. **后台IO拖住全局锁**：worker的`with self._lock: _can_delete_locked; os.remove; revoke`把OS删除包在共享锁内。取消/状态/lease释放/shutdown全被阻塞。为具体owned身份取得有界删除claim后在锁外执行实际删除，完成再短锁提交；shutdown预算到时看到claim/inflight返回incomplete，不等IO锁。pin/adopt/remove不能越过正在删除的claim（pin先接受则删除延期，删除先接受则新pin/adopt拒绝忙），同路径新身份/旧队列完成不能误删/撤销新对象；claim所有权与总量也有硬界且未退出不丢。保留正常remove同步兼容，不为过测试删掉文件身份复核或扩权限。OS单调用本身不可取消如实说明，但后台IO不得阻塞其他线程取得状态锁。新增并发pin/adopt/重复删除/关闭行为测试。
2. **release仍可能启动线程**：现release在入队成功后调用_ensure_worker；健康worker时恰好不spawn，但若最初remove的Thread构造失败后才释放pin，会在IO lease释放路径创建并启动新线程。release必须只状态/put_nowait/signal，不调用可能启动线程的函数；最初删除请求负责worker准入。构造失败时保留pending/owned及可观察失败，可由后续明确删除请求/shutdown重试，不能在release隐式启动。加构造失败→release的零start用例。
3. **BaseException吞掉**：_ensure_worker except BaseException设置unknown后return，仍吞KeyboardInterrupt/SystemExit。标记并保留owner后原对象上抛；普通start异常按明确契约处理，不能用生产吞致命异常满足fake。补KBI/SystemExit对象相同、unknown owner保留/迟启可回收用例。
4. **已终态dead worker的新请求恢复**：_ensure_worker进入_slot_taken前未调用_maybe_reclaim；终态且actual notalive的旧owner_start_attempted永远阻新worker，只有shutdown才reap。在新准入时先reap有充分证明的已退出owner，再检查，未知/终态但仍alive绝不reap。不要自动无限重启，只有明确新请求触发；已耗尽pending不可由pump自动续命。
5. **测试可靠性仍不足**：tests/test_tfm_bounds.py里的WorkerControl/TerminalAlive用time.sleep猜线程结束、未校验gate.wait、没有finally release+实际join，直接手工置terminal不代表“target返回但线程尚活”。改Event控制真实Thread wrapper在target终态后仍阻塞，确认shutdown incomplete/不建第二worker，再释放finally join；未知bootstrap用可迟启的真实wrapper证明，不制造terminal。新blocked IO测试主线程也收BaseException/finally解除并join。确认case teardown真的quiet，不凭daemon/全局thread name扫描当唯一证据。

去掉ADR中“Accepted但待Root”矛盾状态：尚未Root验收就是Proposed/待验收，不能把Cursor跑Root探针等同Root独立验收。保留旧16+原8证据，新增case到仓库持久回归。先现有scoped gate新label跑Root9+新单模块各<=120秒，失败STOP定点修；通过再必要TFM安全/IO/Cosy相关组，禁止raw unittest/detach/并发/修改Root探针监督器。只本切片，权限/声学/正式memory/NativeJob等边界不变。完成真实JSON及交接STOP供Root严格复审。

当前官方Cursor会话b3d48d55-ea02-4c97-a0e3-c43d0527bdee被Root中断。其违背任务先跑监督器要求，直接开启两组raw unittest（06:28:54的2036188/2035928及06:31:03的2036372/2038576），挂起数分钟；Root只读CIM确认后中断CLI，SafetyPolicy精确身份脚本复查两树均已不在，无需追加kill，报告tmp/codex_tfm_hung_reclaim_20261104.json。不要重新raw启动/开启第二组赌通过；今后只用现有硬超时监督器，失败/超时立即停。

## 源码确认问题（不能以测试绿灯绕开）

1. **必然死锁**：_worker_lock=Lock；_ensure_worker持有它再调用_worker_slot_taken，后者再次获取同锁。任意首个延迟/被pin删除请求即可卡死。改成清楚的locked helper/单次获取，不靠增加线程或超时规避。调用Thread.start/join仍在registry和worker状态锁外。
2. **结束尚未退出就丢owner；未知永远不能收口**：worker finally清_worker/清_start_attempted并清_entered，此时Thread还alive；新请求可创建第二worker，shutdown可假quiet。未知start则未知旗标永久True，即使迟启已实际退出也无法close成功。每一代独立entered/terminal事件不在finally清入场证据；保留引用，只有creator_done+已终态+actual notalive（含未知迟启的证明）时由观察/回收侧摘除。无证据未bootstrap的未知继续保留，不凭isaliveFalse猜。异常标记必须保留，KeyboardInterrupt/SystemExit不被except BaseException:return吞掉。
3. **关闭准入窗口**：_ensure_worker只在worker锁外看stop/closed，进入锁后不复核；shutdown可能先完成，之后仍分配新worker。预留owner与关闭须线性一致，锁内检查最新stop/closed，迟返start不可清stop。不要引入registry→worker与worker→registry交叉锁顺序。Thread构造失败无资源可释放slot，真实start未知则保留。
4. **队列未去重**：_enqueue/_offer/_pump仅排除inflight，不排除已queued。重复路径可塞满64份，甚至路径早删完后每份仍先等3秒，阻塞别的清理。queued/inflight/等待pin/耗尽状态应统一、各路径最多一份候选，map以owned容量为界；出队先识别失效任务再等待。旧身份项不得覆盖新同路径身份的pending记录。
5. **重试并未有界**：最后失败将retries写0，pump仍反复排它，for range(max(1,0))每次仍试一次。耗尽路径应保留owned+待删但不自动再排；仅明确重试或shutdown推进。累计预算/退避不能被pump或重复remove偷偷重置。溢出路径仍自动在队列有空位时推进，不能用屏蔽pump掩盖。
6. **状态/线程语义**：delayed_worker_alive和_observable_alive在creator_pending/unknown时直接False，即使真实线程已alive，不是真实观察。alive如实观察，未知另用新字段，complete含所有证明。原daemon=True被改为False无必要，会让退出主进程因清理器卡住；保留daemon兜底同时严格真实quiet，不能把daemon当回收。新增status字段保留向后兼容。
7. **release应更轻**：leased path本来已canonical，_offer再resolve造成IO；release只内存状态/put_nowait/signal，不做路径解析、stat、Thread.start/join或同步删除。pending变动全部受registry锁保护（目前_requeue_locked被worker在未持锁时调用），绝不可造出pending不属于owned的幽灵记录。

Root独立probe已准备：tmp/codex_tfm_bounds_root_2026_11_04.py（容量/并发、双pin最后释放、queue overflow/去重、替换文件、unknown迟启等）。Cursor不得修改Root探针监督器。先修上述统一状态流程，补仓库确定性用例（含stop在ensure锁外检查之后置位、worker terminal但Thread仍alive、重试耗尽无自动再排），Root probe与新测试各通过后再相关旧分组；每组唯一label、硬120/180秒、前台顺序、失败停止、不detach。不要运行真实模型/设备/现有清理/正式memory，不改其他板块；保留巨大dirty与原权限，禁止git回退。完成ADR/handoff真实JSON后STOP供Root源码复审。
