# 流式播报队列独立评审

## Root 最终验收 PASS

独立 `speech_queue_root9_review_20261104b`：9/9 OK（两个cancel参数及多类异常子矩阵）；仓库fixture最终c源码已核实先读False再放屏障，未改通过的生产。Root最终 `speech_queue_root_final_contracts_20261104d`：123 tests/5.222s OK，含最终11、OwnedThread17、stream_playback/voice_session/generate_stream/assistant_orchestration。WAV826精确集合零差，无超时；指定compileall与diffcheck通过。

源码复审：惰性单owner登记、队列/accepted原子提交、mark_finished后拒绝新项、锁内二次取消与失败准入、普通错误可恢复/致命错误通知且拒绝重启、旧轮不污染新轮、128状态裁剪与实际waiter局部引用、shutdown预算先校验及unknown/outer-alive不误quiet均有证据。日志仅类别无原堆栈。尚未接音频预取；不声称默认语音句间等待或真实三模型性能已经改善。本切片闭环，下一准备事务回滚再单独评审。

## 以下为历史

## 当前：Root9 已通过；ObservedCancel fixture 微修待最终 gate

Root已复跑 `speech_queue_root9_review_20261104b`：9/9 OK。仓库 `ObservedCancel.is_set` 须先 `value=super().is_set()` 再 `checked.set()` 再 `return value`（与 Root probe 一致），避免读值前已取消时锁外 True 侥幸通过。仅 tests + 交接，不改生产/Root 脚本。

## 返修 b 历史（Root 已验收生产）

### 初稿历史（Root9 两 FAIL，保留证据）

Root scoped gate `speech_queue_root9_review_20261104a`：9 tests，两个取消参数子例FAIL，其余通过，WAV826精确集合零差、无超时。Root独立探针不许修改。已证实构造零Thread、unknown迟启/终态但活线程不误quiet、fatal waiter通知、1万轮<=128等基本边界有效。

1. **取消准入竞态（已复现）**：enqueue只在Condition锁外检查cancel_event/extra_cancel；Root持cond阻住入队，等锁外读False后设置取消再释放cond，两个参数均仍返回True并可能启动consumer/说出已取消文本。进入同一Condition后必须再次校验全部stop/failure/turn/terminal/production_finished/两个取消信号，再append+accepted原子提交。取消在此线性化点前已置位应零Thread/零accepted；线性化点后取消由既有turn invalidation处理，不保证即时打断已开始音频。不要复制更多散落的判断，尽量一个锁内准入来源。
2. **日志隐私违背本切片任务**：普通speak失败仍 `exc_info=True`，虽然err字段用exception_log_label，附带traceback仍会携带原始异常文本/路径。移除此原始异常堆栈输出，仅阶段标签及异常类别。补logger捕获assert不泄露自定义异常中的用户句子/路径，不新增正文日志。
3. **仓库持久测试不足/回收确认**：大多数finally裸 `player.shutdown` 忽略False；blocking_start失败路径finally只release，不join creator/owner，creator异常未由主线程确认；trim waiter测试没有真正waiter、手动抹掉INVALIDATED后调用私有_complete，并非并发waiter证明。请统一有界gate+BaseException结果收集+finally release与真实join/quiet断言，针对未知未bootstrap线程在finally显式resolve迟启并收口。constructor/start异常三类原对象、unknown迟启保留owner、terminal但外层alive、fatal KBI/Exit通知waiter/拒重启、两个cancel锁准入屏障应加入仓库持久矩阵。用OwnedThreadTask注入factory，禁止全局patch Thread.start。构造零Thread用构造前patch owner工厂证明，不在构造后换owner来宣称原构造零副作用。生产未涉及其它模块，不应改通过的OwnedThreadTask/TFM/RVC。

该切片只队列，先修上述具体点、运行唯一label scoped gate前台顺序，静态编译/diffcheck+ADR交接后STOP。Root独立9及相关模块复跑通过后再讨论音频预取。禁raw测试/discover/detach/并发/正式memory/真实模型设备/清理/提交推送。
