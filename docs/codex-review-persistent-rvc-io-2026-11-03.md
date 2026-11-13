# 持久 RVC IO首版：CHANGES REQUIRED

## 最终结论：本切片 PASS（下方为历史问题）

Root复查单一非阻塞全局准入gate、IO pin短锁占位/锁外登记、未知状态保留、原raw/reader回收与public完整静止谓词分层、重复stop、latepin和释放失败重试。Native/reader启动/单次ownedrun未重写。统一480项90.520秒OK(skip1)，随后局部stop修复121项38.361秒OK；最后仅完成谓词补全后Root14项0.424秒OK，两独立boundary6+pin-stop2全true（各15秒监督），WAV826精确集合零差。证据分别 persistent_union_final_nov3、persistent_stop_final_nov3、persistent_complete_final、persistent_boundaries_accept、persistent_pin_accept gate报告。

不把早先480项当最后14项新代码的直接证据，以上为分层验证。大板块尚需App候选所有权，真实模型/设备/长测未执行，预取未开发；本切片已可放行下一项。

## 最终完成谓词仍漏lease失败（Root已复现）

Root最后121项相关测试38.361秒OK/WAV826零差，但补充纯fake：raw poll=0、_try_release_owner清_proc后TFM.release抛OSError，**stop=True且token仍存在**。`if self._proc is None: _public_stop_succeeded()`把失败重新变成功。

请只扩展**统一完成谓词**：public成功须 `_proc is None`、无start/pin/convert在途、无stdout/stderr reader活跃、无stdin写、**无未回收IO token（含release/query失败）**。可复用 `_convert_io_blocks_new_pin` fail-closed；也检查token实际存在（失败应保留）避免token被忽略。任何一项未知False，last_stop_exited=False。所有成功分支走此谓词，不能只检查converting/pin。它仅观察状态，不额外等待/分配/删文件。private raw-cleanup语义保持。

新增“有raw并已退出，但lease release失败→stopFalse且global保留→解除故障→retryTrue”测试，与原无raw释放失败均覆盖。其他已过规则不动，不重写；targeted persistent模块35秒+两Root boundary一次并STOP。Root最后只独立新targeted，不再重复100秒未变范围。

## 最新重复stop故障：须统一raw与caller两层完成

Root纯fake复现：设置真实 `_converting` 在途，首次stop=False且raw已清；**再次stop=True**，caller仍在途。`binding is None` 分支漏 `_converting` 检查，global会摘掉晚pin持有者。Root还看到首次stop在清_proc后因private返回False继续拿旧binding循环约2秒，属于无用等待。

定点两层契约：`_try_release_owner` 只表示旧raw+Job+reader+已登记lease静止，可清_proc并返回True，即使caller仍在途；last_stop_exited在caller/pin仍在途时仍False。由 `_stop_locked` 所有成功分支（包括binding=None）统一检查caller/pin，仍在途及时False、保留globalclient；不要拿已清旧binding再等两秒。释放lease失败必须False，可重试。补firstFalse/secondFalse/clearcaller/thirdTrue，且旧raw-cleared断言保持。单次targeted+两Rootprobe，不跑旧全量；Root记录当前fullgate结果后再决定需要哪些验证。

## 扩展Root门禁：480项中两处兼容回归

Root正常Windows独立480测试95.831秒，2失败(skip1)，WAV826零差。错误见 `tmp/codex_gate_persistent_union_nov3_2026_11_02.stderr.txt`：WorkerPipeTests.test_active_interrupt_terminates_worker_but_client_can_be_reused 与 F17LifecycleMatrixTests.test_rvc_worker_timeout_and_cancel，均已确认raw退出却 `_proc`未清。不要删旧断言或改成“保留即可”。

区分**已静止进程句柄**与**仍在途caller/晚pin所有权**：active cancel终止后，在pin不在途时可以推进原 `_try_release_owner` 回收raw+reader并清_proc，但只要caller仍在途，stop与last_stop_exited仍False，globalclient仍保留供晚pin/重试。pin在途时保持快速False不等锁；未确认reader/Job不清句柄。_try_release_owner不要将清_proc等同整个client回收完成，释放pin失败时_last_stop_exited无条件False（现在清_proc后用owner_still_current判定会漏设False）。

保持不放松pin/caller保护，补“raw清理完成但caller在途stopFalse，晚pin后下一stop清理”的行为；TFM release/query与锁忙未知不伪造成功。仅定点修上述回归与测试，执行三相关模块的35秒监督+Root两probe各一次，STOP；Root之后统一门禁。

## 第三轮 Root 独立 pin/stop 竞争：仍一项失败

专项现9项自报通过；Root新 `codex_persistent_rvc_pin_stop_2026_11_03.py`（15秒监督）2.219秒：stop_during_pin_returns_incomplete_without_waiting=false；最终回收=true。pin持有_convert_lease_lock调用TFM.pin的IO，stop随后阻塞在解pin获取同一锁。不能仅用快速fake manager掩盖。

定点修：用一个pin-inflight准入标记，短锁先占位，锁外执行TFM pin，成功无论代际是否已取消都先登记token/mgr，再确认可派发；finally结束pin-inflight。stop见pin/convert caller在途时仅取消/终止已知进程，及时返回False，不能等待pin锁、清掉globalclient或假报last_stop_exited。pin晚返回保留token直到owner确认静止并重试回收，原经理不得重绑。不要为此新起清理线程或重写reader/Native。

global.stop在convert尚未结束时不可True并摘除client：否则旧caller可能晚登记pin，而global已丢owner。即使raw/reader已静止，也要把caller在途合入stop完成条件（conversion finally后可重试）。caller内部cleanup允许推进，但未结束前last_stop_exited仍False。新增在pin gate、stdin gate、晚返回下的get/rebuild拒绝与最终回收测试。只targeted35秒+Root两boundary各15秒一次，STOP。不重跑旧大gate。

## 第二轮：已解除自锁，修fixture与非阻塞gate后再验

Root独立 `persistent_root_boundary`（15秒监督）1.469秒，WAV826零差：首次pin/释放失败阻stop/重试/并发get及时拒绝均true；stop构造中即时False=false（实际抛Unavailable）、allthreads/errors=false。当前不是通过。

**不要按上一交付建议削弱kill/terminate。** 进程及reader已经确认退出时正常释放pin是正确行为，timeout测试想保留pin，就让fake真的保持未知（受控survive_terminate+survive_kill、reader gate），finally再让其退出。不许为错误fixture删生产kill。Fake wait应raise `subprocess.TimeoutExpired`，不要裸TimeoutError。

1. 专项的delayed API改成现有delay=；所有设置infer全局用patch context/finally恢复原值，不能交叉污染teardown；持pin的测试最后必须解pin并shutdown确认complete。TFM两不同路径refcount2。成功payload通过真实client读取接口fake明确终态；未知测试fake真的未退出。相应失败大部分是fixture，先查实际API不要盲改生产。
2. 简化全局协调：**一个非阻塞生命周期准入gate包整个get/rebuild/stop事务**（这是准入gate，不是状态读写mutex，不允许任何阻塞acquire），任何调用无法立即try-acquire：get抛既有Unavailable，stop返回False。gate持有者才可constructor/停止/发布，IO在状态锁外；finally释放。无需现在两段release再acquire的复杂竞态。这样构造/stop在途其他请求即时拒绝、旧client保留、一次构造、不会并发stop旧client时又复用它。状态查询不用此gate、不等待。所有真实未知strictFalse；不要多次完整stop预算。
3. 保留Root probe不改；以Root的boundary和新成功cycles/终态失败/未知pin/释放失败重试/旧reader未退拒绝重建行为为依据。测试非阻塞并发第二get允许既有Unavailable，不能要求两个都成功才能单构造。

只修这些与已明确缺陷，targeted35秒监督一次+Root boundary15秒一次，失败STOP，交付。不要重跑100秒旧gate或开新功能。

Root在Cursor首版测试尚未交付时确认死锁并中断执行68826。实际自有 `py -3.12 -m unittest tests.test_rvc_persistent_io_lease -q` PID832132→823988阻塞；中断后复查原launcher已结束，无进程被额外终止。禁止把本轮作为通过。

1. **P0 必现自锁**：worker._pin_convert_io持 `threading.Lock` 再调用同样持锁的 _convert_io_blocks_new_pin，首个token=None时必走此分支。不要机械改全局RLock；删除该锁内冗余检查或明确区分locked helper。逐个画锁顺序，补首次pin正常返回的有界测试。
2. **pin释放失败被谎报完成**：_try_release_owner先清_proc/readers再调用void _release_convert_io_if_owner_quiescent，不检查失败就True；_stop_locked无binding也相同。即使进程结束，lease尚未回收也必须stop False/last_stop_exited=False，可重试保留token/mgr。helper返回严格bool，token失败不能清，空binding也必须重试并验证。
3. **全局锁围住可能长IO**：infer get/stop用blocking with _worker_lifecycle_lock，内部constructor/interrupt/stop可能阻塞几秒甚至探测超时，其他线程全部排队。采用短状态锁+inflight事务/保留old引用、IO锁外，或明确独立非阻塞生命周期准入gate（所有入口try-acquire，busy即时Unavailable/False，不作为长持状态锁）。构造中stop不能True，新get零构造；publication比较同一old/gen，不能丢旧reader/pin未退owner。
4. **双stop与弱状态**：_shutdown_existing_client always interrupt后stop，活跃interrupt内部已经stop，不能无理由二次完整预算；沿现有was_converting/last_stop_exited真实bool协议，False/未知保留。不要用truthy变成真实成功。保留旧fake None兼容仅确有旧契约时可说明，真实client必须bool。
5. **测试fixture错误**：TFM.active_io_lease_count是路径refcount，两不同路径应2，不是1。timeout测试fake terminate立即退出且无reader，按真实行为可立即解pin，不能要求凭空保留；用受控ignore-kill或reader gate模拟真的未知状态。client._reader=None时生产_read_worker_json不读_fakeProc.stdout，不能仅往StringIO写JSON就宣称成功。所有TFM/lease/client/gate在finally清理，保留lease的测试末尾必须释放后shutdown，不能带pin离开TemporaryDirectory。不要删原回归测试，更新fake契约应真实。
6. **所有测试必须有监督**：不要裸跑无timeout unittest。用Root `tmp/codex_scoped_test_gate_2026_11_02.py --label persistent_targeted --timeout 35 --modules tests.test_rvc_persistent_io_lease ...`（模块regex已允许），正常Windows context若涉及原Edge asyncio；只targeted一次，失败停止报告。Root扩展gate最终执行。bounded线程join不可只daemon。

仍限本切片；不重写已验证Native、reader/启动协议，不碰单次模块/声学/默认/main/模型/设备/data/memory/提交推送。先按完整锁图和状态表自行检查，再交付；Root独立复验后下一块。
