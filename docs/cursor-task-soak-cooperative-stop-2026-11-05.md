# 长测协作停止（已放行，诊断切片）

读取AGENTS、四repo skills、安全规则、resume-remaining-voice-work-2026-11-05.md。Codex设计/严格review和独立测试，官方Cursor实施；不运行test/model/device，只真正compile后STOP。仅允许tmp/codex_real_voice_supervision.py、tmp/codex_app_real_voice_smoke_2026_11_03.py、tmp/codex_real_voice_soak_loop_2026_11_05.py，新增tmp/codex_soak_stop_signal_2026_11_05.py。不改生产/config/vendor/旧tests/Root probe/gate/正式memory，不提交推送，不启动清理器或删除任何旧文件。

事实：前轮quota取消时parent立即NativeJob回收，进程全部quiet，但当前共享WAV828，前已验收826；两个新增文件仅时间吻合，归属未证明，Root保留并记录完整metadata baseline tmp/codex_wav_baseline_resume_20261105.json，禁止仅按时间删除。目标是下一次取消先执行App/Timer/engine/TFM finally，并区分graceful与硬兜底，不将进程quiet冒充文件清理。

## 子进程固定信号与取消

- parent仅soak模式在既有env_extra传MIKU_DIAG_SOAK_STOP_BASENAME=prefix.with_suffix('.stop').name；不是任意路径。新增小模块提供固定path解析与FileStopSignal、StopAwareEvent，不起后台线程。只接受ASCII basename codex_real_<1..48字母数字下划线>.stop或原固定默认codex_app_real_voice_isolated_nov3.stop；拒绝路径分隔符/盘符/..等。拼入给定project_root/tmp，子进程始终用实际PROJECT_ROOT，不从env取root。
- FileStopSignal为线程安全可调用signal：只探测该文件是否存在，不读内容、不写；false探测缓存不超过0.1秒，true永久锁存；缺失False，其它不可观测错误传播并走现有finally。可注入monotonic供Root fake；不识别fake类型。StopAwareEvent继承threading.Event，is_set为本地set或signal请求，signal为True后set自身；wait也有界小片wait复核signal，无新线程。
- pure loop模块增SoakStopped(RuntimeError)与SoakState.cancelled=False，run_fixed_soak新增stop_requested=None回调；loop每次调度/空闲轮与成功callback后复核请求，若停止则cancelled=True、complete=False、原样raise SoakStopped，finally仍更新elapsed/部分rows。不能把cancel当成功或改1800/15/120/60标准；无callback时原语义保留。
- smoke只long创建signal（要求env有效），接到App现有_voice_load_cancel的StopAwareEvent（prepare前）以及每轮cancel_event；run_fixed_soak传signal回调。run_soak_turn看到signal请求时raise SoakStopped（而非误报普通generation失败）；其它异常/fatal保持优先。现有Timer90、实际join、App→独立engine→TFM→warmup/guard/unload finally全部保留。取消也要落UUID部分report，SoakState.cancelled=True，并标signal请求；不新增日志正文。

## 父监督共享10秒收尾

- 若operator_stop，_run_shared_cleanup在其原cleanup_started与唯一deadline=started+10内，先给child最多6秒协作退出机会，再走已有owned回收与server回收，绝不能6+另10。时间用实际_remaining与mono，wait超时正常进入兜底，其它错误记录后继续；不得新加长时间sleep。
- 只有原child的_is_spawn_quiescent实际True才标cooperative_stop_child_quiet=True，不拿poll退出码或child自报代替。保留False/None区分；仍全程OwnedJob兜底。若raw进程已退出但owned Job不quiet，也应调用同一owner.kill，不得因poll已退出跳过残余Job。别碰用户服务或按进程名kill。
- 子进程可能先感知stop并退出，parent while poll先退出：在finally/derive前仍应识别本次自有stop文件，将abort_reason=operator_stop（不覆盖已有资源中止原因），保持取消非零/不能PASS。
- 只给operator_stop grace，低资源/超时/异常仍立即回收；TOTAL2010/短测180、EXIT10、ready15、420/36样本与RAM/commit/VRAM门槛不变。报告新增cooperative_stop_child_quiet，默认None。文件集合由Root前后manifest独立比对，不让parent删未知WAV或声称拥有未登记文件。

交付：精确diff、边界说明、4文件compile(...,'exec')后STOP，不执行任何test/model。Root会覆盖signal路径/缓存/取消wait、pure loop idle/active取消保留部分数据、父6+余量总<=10与raw已退Job未quiet、旧24/Root回归及真实取消短测；全部通过后才重新30分钟真实链路。源码尽量窄、清楚，无重复状态/无用helper，不将测试通过当长期内存保证。
