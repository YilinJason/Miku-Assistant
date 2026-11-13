# 最新：离线通过；真实共存未通过，禁止App接线

详见`docs/cursor-task-rvc-coexist-readiness.md`。真实报告8c91b8a59344：Ollama2.063秒、RVC76.812秒、Cosy32.063秒失败，0音频，全部回收。RVC独立实测CUDA正确，CPU cap4对照没有实质改善；不做无效调参。另有helper warmFalse仍报True的纯fake缺口，Cursor先修此契约，之后Codex诊断Cosy60秒已有上限，生产默认30不改。

# C2b2a Codex review：离线通过，进入真实共存验证

Codex独立复审ac1eec9e最终实现，root两个故障probe实际通过：superseded仅2次查询、快照getter异常固定失败、末取消不假成功、mapping不可改且proxy输入复制、legacy默认兼容、缺原子API零调用且不假fresh。确认single-flight finally释放及默认warmup兼容。

独立全量监督器`tmp/codex_c1_validation.py --full-c2a` exit0：1007 tests OK(skipped6)，106.096秒（总107.91秒），文件集合644→644，added/removed均空；git diff --check通过（仅换行提示）。Cursor234项报告未代替本次独立结果。

当前只允许固定短句真实三模型串行共存验证；不接App、不切默认。协调器不接管caller引擎，也不能强制取消既有RVC同步准备；运行中GPU竞争与设备长测仍未验证。下面为已收口历史。

# C2b2a Codex review：CHANGES REQUESTED

aced2b65交付223测试报告。基本阶段顺序/默认scope/RVC warm失败不ready已看代码，以下一次收口；禁止真实模型及App接线。独立`tmp/codex_c2b2a_review_probe.py`无网络/线程/文件副作用，覆盖下面行为，不可修改。

1. **预热代际没有原子绑定且会busy-spin**。start_warmup返回bool后再snapshot取generation有竞态，可把别人的下一轮成功冒充自己的失败轮。已有target代际被后续fresh替代时循环仍等旧generation，而新的_warmup_done已经set，wait即时返回，直到120秒可持续占满核心。增加窄原子代际获取API（例如`begin_fresh_warmup(timeout)->int`：在同一个启动锁内返回所创建/复用周期的generation；默认旧start_warmup返回bool保持兼容），getter不得在同一非重入锁里再嵌套上锁。协调器只跟踪该代际；看到不同代际立即固定superseded失败，零后续动作。不要读取客户端私有_warmup_done；用公开快照+最多50ms的可取消sleep/wait，避免已set事件造成忙轮询。Memory wrapper委托新窄API，不接触真实库。
2. **快照普通异常会裸抛**。snap_fn取target及loop读取不在try内，破坏固定失败结果契约；缺失/非法快照/属性异常统一固定失败标签，后续模型零调用。KeyboardInterrupt/SystemExit仍上抛让caller清理。禁止异常原文/路径进入detail/log。
3. **末阶段取消会假成功**。Cosy prepare返回True同时外部cancel已set，当前不做末次取消检查就ok=True。返回成功前再次检查cancel；不清外部事件，不转fallback。补阶段内取消/失败不前进。
4. **结果只是浅冻结**。stage_elapsed_sec实际dict可随意改，和冻结报告契约不符。使用真正只读复制/不可变tuple等，并给清晰序列化方式；最多固定3阶段，不能引用可变输入dict。所有耗时有限非负。warmup_timeout也须在动作前拒绝bool/非有限/<=0/超现有上限，避免inf无限等待；require_rvc参数严格bool。
5. **Memory wrapper破坏旧默认调用**。start_warmup(restart=False)仍总传restart=False，旧inner只接受timeout会TypeError。默认False保持旧kwargs形态；显式fresh用新API或明确支持的restart，不在内部TypeError后盲目再调用。补legacy inner与真实当前client/Memory wrapper协同测试。
6. **并发协调器要有准入**。当前函数可同时执行两条三模型阶段链；与Cosy进程级单例一致可用一个短状态锁/非阻塞single-flight gate（不新线程/不无限排队）保护整个显式准备调用，第二调用立即busy/零动作。所有异常/取消finally释放。不要声称这会排斥其他应用GPU，也不改普通运行时推理。补两caller事件屏障避免同时fresh/RVC/Cosy。

测试门禁又被加进永久unittest模块；请开发监督器放tmp/Cursor具名文件，标准测试模块仅测试入口。监督器120秒精确PID树、UTF8、文件集合双向零变化。超时单测不要time.sleep(10)留daemon线程，应事件释放并join，断言失败finally也正常收口。保持原测试不弱化；先新故障专项/root probe，再相关223门禁一次。compile/diff，交付停止Codex review。

## 3d7718dd 第二次独立评审：仅剩两个已复现边界，关闭后真实共存

根probe原6项全部通过；`tmp/codex_c2b2a_extra_probe.py`实际复现：snapshot_property_escaped=True ValueError；non_atomic_wrapper_claims_fresh_success=True calls=1。

1. `_safe_warmup_snapshot`仅包snap_fn调用，generation/finished/succeeded字段getter和int转换在try外；非法属性仍裸抛。把读取+严格验证一起包窄异常边界（generation正int非bool，finished/succeeded严格bool，非法固定snapshot_failed），不能bool('false')变成功。begin返回token同样严格验证，不int(True/'1')。普通异常固定失败，KI/SystemExit继续上抛。不读私有event。
2. MemoryAware.begin_fresh_warmup没有inner原子API时退回start+snapshot，重新引入首轮已要求修掉的竞态，也会用旧幂等成功冒充fresh。只委托inner原子API，缺失则返回0且零start/snapshot动作；默认start_warmup兼容不动。不要为支持不安全legacy造锁，因为锁不能保护inner其他caller。

小项一并：LocalVoiceWarmupResult.__post_init__无论是否MappingProxyType都复制dict后proxy，避免别人传进来的proxy仍被原dict改变。内部调用已有复制，纯防外部输入别名，无额外框架。

禁止改root probes；补同类断言。先专项，独立监督器228项一次/120秒/集合零差，compile/diff；保持没有App接线/真实模型/设备/真实memory/提交推送。交付停止。
