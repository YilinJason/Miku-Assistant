# Codex strict review: Windows local TTS Job lifecycle — changes required

2026-10-18. Finalize this review against Cursor delivery f5b298e2; do not run models. Independent fake-only tmp/codex_owned_job_contract_probe.py confirms four failures. Review covers final helper/native backend/SafetyPolicy/runtime import order/callers, not just implementer summary. All items belong to the same ownership slice; no new features.

1. **No implicit fake backend.** SafetyPolicy.bind_process_runners auto-selects FakeOwnedWorkerJobBackend whenever a custom popen_fn is not subprocess.Popen. A perfectly real instrumenting Popen wrapper then loses containment, exactly prohibited by task. Keep real backend default regardless custom callable; test fakes must explicitly inject their fake backend. Move FakeOwnedWorkerJobBackend into test support rather than production; remove production register_redirector test-only hook where possible. Preserve existing caller compatibility for RVC/other runners. New test: real-like custom callable without explicit test backend still selects native containment.
2. **Close failure must retain truthful ownership.** _JobResources.close catches OSError and marks closed=True/handle=0; OwnedWorkerProcess._finalize sets returncode before successful close. Native close_handle ignores CloseHandle return value. Check native BOOL; failed close leaves handle+not-closed and poll/wait incomplete, retryable. Do not return successful terminal status on close failure. Once close succeeds, close exactly once. Nonblocking finalizer may best-effort swallow without corrupting regular state.
3. **CreateJobObject collision API is wrong.** Windows returns a VALID handle + ERROR_ALREADY_EXISTS(183), not a NULL handle. Detect collision immediately after Create, close only our newly opened handle, and reject BEFORE changing flags/spawn. Pure fake kernel32 probe reproduces current acceptance. Keep fresh unpredictable names; never terminate or modify existing Job. Verify last-error handling with ctypes(use_last_error=True) and correct signatures.
4. **Startup interrupt leaks Job handle.** spawn currently except Exception; fake popen_fn raising KeyboardInterrupt after create/setkill bypasses cleanup (root probe confirms). Use BaseException cleanup for owned resources then re-raise original, and retire a successfully returned redirector if later setup fails. Best-effort cleanup errors must not mask the original. No unbounded wait; do not lose a live owner or enable retry while unknown. No fallback uncontained spawn.
5. **Serialize handle transitions.** poll/terminate/kill/wait/finalize currently have no common short lock. Concurrent status polling can close a handle while another thread uses it in TerminateJobObject (handle reuse hazard). Use a per-owner short RLock across native handle/state transitions; wait polling releases lock before sleeping. Terminal state returns without querying/terminating handle0 or a closed/reused handle. Tests with Events for close-vs-terminate and concurrent repeated status/stop, no double close/invalid handle/native action after close. Never hold lock across long Popen.wait/IO/thread join.
6. **Bootstrap failure proof must be behavioral.** Existing test_missing_token_exits_before_barrier merely checks an untouched dictionary; success test only calls join helper. Replace/supplement with actual script entry via runpy/fake import hook so missing/open/assign/close/parent failures prove no src.voice/torch/numpy/model imports. Join success proves assign-current-process only + immediately close child's handle. Remove unnecessary injectable exit_fn and just raise SystemExit(2) on failure (no callback accidentally returning through invalid state); no tracebacks/token disclosure for expected containment failure. Keep importing worker effect-free and keep existing module-level helper patch points/tests meaningful.
7. **Complete bounded failure coverage in one batch.** Current fake backend must not hide native API conventions. Add native-close-failure, valid-handle collision, popen interrupt, failure after Popen returns, bootstrap open/assign/child-close, parent death/prejoin missing Job, raw-exited/child-active; assert exact handles/status and restoration. wait(NaN/inf) must promptly reject instead of an unbounded spin. No source string assertions or forced registry clears. Preserve root probes unchanged.

After code/tests, run new targeted offline tests then one bounded gate including original six modules + tests.test_owned_worker_job + tests.test_cosyvoice_production_slice_a. Include capability zero-spawn rejection and explicit fake DI, exact miku WAV824 set invariant. One compile/diff. STOP on unexpected failures, no repeated求绿/real models/main/devices/data/memory/commits/push. Update ADR and handoff with honest limitations; no claim of OS sandbox or speed fix. Root will independently re-review and run model-free Windows child/parent-exit proof next.

## ba46cd75 独立复审补充：第4项部分启动 owner 仍未闭合（未放行）

原四项root probe已实测通过，原六模块独立239 OK/62.545s、824不变。新的 tmp/codex_owned_job_partial_spawn_probe.py 在 Popen 成功返回后的真实Python行边界注入KeyboardInterrupt（不依赖行号/源码字符串），同时fake redirector停止未确认、Job Close暂时失败。结果 original_interrupt_preserved=True，但 live_partial_spawn_owner_returned=False。现有 _retire_redirector 等50ms后吞超时；spawn除清理尝试外丢弃raw/Job引用，调用方无从阻止下一轮分配。此为原评审第4项“未知时不得丢活owner”未实现，不是增加功能。暂停模型测试。

请按以下明确所有权协议修复本项，别再仅吞异常尽力清理：

- 在任何Popen调用前保留一个OwnedWorkerProcess/资源owner（可支持raw尚未绑定，捕获固定argv；无raw时stdin/stdout/stderr=None）。最好先分配owner状态再创建Job/绑定handle，避免控制流中断落在已创建Job却无owner区间。之后将成功返回的redirector原子绑定到此owner，不依赖后续构造/测试attach回调；生产不需要测试后端attach_redirector hook，测试关联放显式fake Popen工厂里。
- 失败时用有限deadline向该owner发kill/确认raw+Job都退出；**不允许先丢owner/清handle再等待**。关闭失败或退出未知时保留owner，在原异常（包括KeyboardInterrupt/SystemExit，必须保持原类型/身份）上附 `_miku_owned_worker_process`，然后重新抛原异常。可定义窄take helper保证只交接一次；不要新建全局孤儿registry/后台线程。Root partial probe检查此属性；保持root探针不改。
- CosyVoiceWorkerClient._start_worker_poll 捕获BaseException时先接收并持有这个pending owner到现有_proc生命周期，再保持普通错误/权限失败的既有语义、控制流异常继续上抛。记录not-ready；未知退出时使用现有stop_failed/关闭重试边界阻止重启，不加自动重试，不假成功，不扩展资源预算。后续shutdown必须能回收此owner，raw=None亦可安全poll/wait/kill，成功清理后通过现有registry/engine生命周期才可新建。
- Add deterministic fake tests：Popen返回后中断、cleanup timeout+Close失败owner交接、raw未返回但Job close失败、client接收owner后拒绝重启/关闭重试回收；成功close时原异常照样抛而无残余owner；异常身份不被cleanup异常覆盖。已有锁/原四probe/原场景必须保留。不要用未触发变量当bootstrap测试：原实现已有runpy missing-token行为证明，可保持。
- 为root独立验证保存真正八模块监督器 tmp/cursor_owned_job_validation_gate.py（原六+Job+slice_a，120s/exactWAV set），不要只在一次性内联命令临时改列表。执行一次、compile/diff，交接STOP，无模型/设备/data/memory/提交推送。

## f25ca603 独立复审：剩余三条同一协议分支未闭合

根本owner交接设计保留；不再重构。root原四项+post-Popen三项均实测通过（root仅将四项probe构造调用改为等价具名参数以适配未发布ctor，未改断言）。tmp/codex_owned_job_remaining_paths_probe.py独立确认以下三项全False；请只修完这些已声明协议分支并一次验证。

1. Client._start_worker_poll 的 PermissionError先行except直接returnFalse，根本不调用take_pending，丢失附有owner的PermissionError。统一BaseException入口先接管任何有效pending owner，然后PermissionError仍固定日志+False，其它异常原身份上抛。测试用 PermissionError附同一真实fake-owner证明client._proc保持引用且stop_failed阻止再spawn，后续shutdown回收。
2. OwnedWorkerProcess.poll 对_redirector is None直接None，导致raw未返回但Close失败而交接的owner永远不能通过常规shutdown重试完成。No-raw也应查询Job，active>0/unknown/Close失败仍None；active0且成功close则返回固定失败退出码（例如2）。没有创建任何Job和raw时可视为失败终结，不能无限等。必须由client现有poll/wait/kill路径可回收，不只私有spawn_cleanup能关。
3. create_named_job成功返回后、_bind_job_handle行前被打断时，except orphan_handle分支Close失败仍吞并丢句柄。像已返回redirector一样，将已返回的orphan_handle先纳入既有owner再走同一有界清理/异常交接；不能直接Close失败就丢，避免双绑定/双close。Root trace注入不依赖源码行号；原KeyboardInterrupt身份保持，异常owner必须持有handle7，恢复close后可关闭。

顺带确保_bounded_spawn_cleanup / 最终quiescent查询任何cleanup异常都不替换原异常；将异常视为未知并保留owner。take helper只接收本类型owner，交接一次（不是扩大动态协议）。不要改变模型/默认/预算、fake原流程、权限、生产启动器等其它东西。

保留所有root probe（含remaining_paths）不改；fake新三项先一次，然后保存的八模块gate一次/120s/824集合不变，compile/diff，ADR/handoffSTOP。通过Codex复审与无模型真实Job验证前不跑模型、不进入下一功能。不读改删data/memory，不提交推送。

## 21332fae 独立复审：真实Native正常路径通过，最后两个异常转移修正

root三组probe与八模块独立288 OK(skip1)/71.009秒/824零差。真实无模型Native Job三项全部通过：自定义真实runner停止4进程47ms，自然退出156ms，父退出5进程31ms；模型0，45秒监督器总1.547秒。架构已获真实验证；不改正常路径，不增加新机制。

Root tmp/codex_owned_job_transition_probe.py再确认两处现有语句的错误：

- create返回7→_bind_job_handle成功→orphan_handle尚未清0的行边界中断。except重绑触发RuntimeError后直接close(orphan_handle)，但owner已经持有同一个7；结果关闭真实句柄却保留owner.handle7，之后query永久unknown。修正：except检查resources.job_handle==orphan_handle时仅清局部orphan标记，绝不能Close仍由owner持有的相同句柄；尚未绑定时接管再清标记；不静默吞真正冲突/丢第二个句柄。主正常路径不变，root此probe不改。
- _bounded_spawn_cleanup最终_is_spawn_quiescent()的query抛错未被捕获，会替换原spawn异常，也丢pending owner。将整个cleanup/quiescence普通/控制流异常视为unknown，原异常对象保持并附owner后重抛（不是重新造异常）。已有关闭失败返回False语义保持。root模拟query OSError显示此问题，不能只用close OSError测试代替。

请仅修两处并补对应fake，运行所有root probes不修改（contract4/partial3/remaining3/transition3），真正八模块gate一次/120s/824不变、compile/diff、交接停止。Native真实普通路径已测，不需要你跑模型或设备。无data/memory、提交推送。此切片完成前不下一个功能。
