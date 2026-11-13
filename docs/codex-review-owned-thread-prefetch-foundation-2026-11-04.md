# OwnedThreadTask 独立评审

## 当前：Root 最终验收 PASS

返修 e 源码已统一外层except，OS commit在start前、duplicate不伤creator、取消永久、unknown无ident捷径、terminal与实际退出分开；无额外模型/设备/进程。Root独立 `owned_thread_root10_final_20261104e`：10/10 OK（含真实Thread在start返回后下一行注入KBI，原对象保留且存活期间非quiet）。Root仓库/调用方基线 `owned_thread_root_repo_stream_base_20261104e`：81 tests OK，含最终17、stream_playback/voice_session/generate_stream。两组WAV826精确集合零差；无超时。不是将之前20或16声称为最终项数。

源码与fixture已复审：factory/start/join锁外，有限join验证先于快路径，所有权强引用不因terminal丢弃，target错误供消费者读取。已去全局Thread.start污染及sleep猜时序，creator错误主测试检查、finally实际quiet复核。小模块允许下一切片使用，不要求立即迁移已验收TFM/RVC/NativeJob。后续队列/音频预取与真实硬件性能仍未完成，不能由本helper通过推断整程序无泄漏或语音流畅。

## 返修 c 历史：Root10仍1 FAIL

Root新增真实线程+`sys.settrace`一次性注入：OS start已返回后的下一条OwnedThreadTask.start行抛KeyboardInterrupt。监督器 `owned_thread_root10_review_20261104c`：10项9PASS/1FAIL，原对象已抛给caller但 `.error is None`。commit提前后不会再假quiet；但当前仅内层 `thread.start()` try捕获异常，外层只有finally，故后续ack路径的中断仍丢错误。请**去掉分散的内层except**，一个外层except覆盖factory/commit/start/ack：记录原异常，若已commit标unknown，然后原样raise；finally仅当前受理者清creator。这样known constructor失败与OS unknown同一套逻辑，不继续添加分支补洞。仅可根据entered+terminal+实际退出收口unknown。

fixture返修 c仍有具体缺口：三个target的release.wait未检查；多个finally的task.join返回忽略；并发racer仅all(results)没有len=4，子线程异常可能以空结果通过；constructor/snapshot/迟启等creator target直接task.start，异常只进excepthook而主测试不检查；未知迟启测试finally不保证creator实际join。请统一所有在途creator/racer捕获BaseException并由主测试确认，finally释放gate+实际join确认notalive/task.join=True；正常路径断言error=None、结果数量，不借超时静默退出。避免再提交“所有wait带断言”但源码仍有裸wait。

以上两项修好后只跑唯一label仓库gate；Root会自行复跑10（禁止raw对照Rootprobe）。原草案下一queue任务尚未放行。

## 返修 b 历史

Root重新运行 `owned_thread_root9_review_20261104b`：9/9 OK、WAV826零差。但源码仍未满足原任务第3条：`_os_start_committed` 应在调用 `Thread.start` **之前**同一锁内登记并强持有Thread，而非返回/抛错后才置True。当前若start返回后、状态锁重新获取前发生KeyboardInterrupt，finally清creator，os_committed仍False，正在跑的线程会被quiet判定当known-no-start。请把cancel准入检查/owner登记/OS commit放同一个锁段，之后锁外start；外层except保存所有异常并依据commit保守unknown，finally仅受理者收creator。不能在known-no-start分支忽视实际alive事实。异常保持原对象，不清已有target错误来伪造成功。

仓库测试可靠性另未收口：`test_start_blocks_unacknowledged` patch全局Thread.start后用被patch的starter.start先在主线程阻塞3秒，wait超时未检查；`test_unknown_late_start_cancel_zero_target`类似且sleep猜时序；factory/start gate多处wait不确认，starter/racer join无notalive断言，没有finally放行/实际join。请统一使用已保存_REAL_THREAD构造外层creator、仅对受测Thread注入_DeferredThread与屏障，所有gate结果检查，启动异常捕获到测试主线程断言，每个在途测试finally release/cancel/实际join/notalive。未实际启动的unknown若需结束必须resolve迟启并确认退出，不把daemon/timeout当证据。消除sleep等猜时序；目标忽略cancel可用release gate+join短预算False。每个target错误三类以及constructor三类可subTest，不只单个普通异常。

ADR尚有旧“ident=None仅join可收口”错误描述，请删掉并明确无entered/terminal/实际退出证明永不能收口。再次唯一label监督器跑新模块，STOP后Root独立复审。

## 初稿历史（已由 b 修复，保留证据）

Root监督器 `owned_thread_root9_review_20261104a`：9 tests，5 failures（含同一矩阵3个子例），无超时，WAV826精确集合零差。报告 `tmp/codex_gate_owned_thread_root9_review_20261104a_2026_11_02.json` 与 stderr。不是仓库20项通过即可验收。

1. `start` 在受理前也进入 finally：重复调用返回False却清除另一调用的 `_creator_pending`。阻塞factory期间重复start后pending必须仍True；只有受理者有权结束creator。将准入放在所属try/finally外，禁止靠重复补旗标。
2. `_start_attempted` 混淆一次受理与实际OS start尝试；`join` 把unknown且ident=None当未启动清除未知，`_compute_quiet` 又用该条件宣布quiet。严格区分 single-use claimed / OS-start-committed。constructor异常、确认取消且未commit才known-no-start；commit之后必须entered+terminal+actual not alive+creator返回方可quiet/消unknown。禁止用ident=None或瞬时is_alive=False作未知启动失败的退出证明。
3. factory异常没有记录 `.error`；start的KeyboardInterrupt/SystemExit被排除。所有原BaseException记录且启动调用原对象继续抛出，quiet不能掩盖失败。
4. snapshot先复制部分状态，再重取锁读取新状态并混用旧thread，可能产出不一致快照。用一份一致状态计算quiet；避免公共函数锁重入，factory/start/target/join不在状态锁内。
5. target错误应按任务契约仅捕获原对象供消费者查询，finally terminal，不额外重抛到threading.excepthook打印原始错误；启动调用的异常仍原样抛出。这不是吞成功状态，消费者必须检查error。

建议最小实现：一次受理标志、实际OS尝试标志、creator_pending、unknown与entered/terminal；统一一个snapshot/quiet判定，不为不同测试复制不同条件。cancel永久，线性化start准入说明清楚。snapshot与join都不能清除尚无进入退出证明的unknown。

返修同一窄范围；补仓库对应矩阵（特别并发重复start、constructor异常三类、unknown迟启三类、terminal但外层thread alive），每个测试finally真实release/join且主线程确认异常/退出。禁止改Root probe/监督器以迎合实现。先唯一label scoped gate `tests.test_owned_thread` <=120s，compileall指定文件+diffcheck，更新ADR/交接后STOP。Root复跑独立9及源码review之后才决定接线。禁止raw unittest/discover/并发/detach、无真实模型/设备/清理/正式memory/提交推送。
