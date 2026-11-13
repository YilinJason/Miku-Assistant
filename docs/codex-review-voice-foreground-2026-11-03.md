# 前台有界 request：Root 分层 PASS（2026-11-03 恢复后）

Root 最终源码确认 creator/worker/actual-exit 分开，unknown bootstrap 不摘 owner、不让 TFM 提前闭；join True 与静止谓词一致。构造失败终态；start 尝试后异常保守 unknown（公开 Thread API 无法证明未 spawn，允许保持 incomplete 到实际 worker ack/结束或进程退出），不无限重试分配。调用方 KBI/SystemExit 传播，后台中断发布终态。CLI pending 文本过渡、旧 result 无 pending 兼容，失败 sticky、tts→voice 升级与只读 readiness 保留。

独立证据（分层，不声称最后同一遍全量）：
- 七模块156测试8.219秒OK，监督9.672秒，之后只有 join 返回值定点变化。
- 最终 Root interrupt4项1.422秒、foreground5项1.719秒、同步App9项1.391秒、shutdown6项1.609秒全部 exit0；覆盖真实OS bootstrap 延迟与 start 中断，未知不能伪 join，真实线程结束后回收。
- 每次 data/tmp/miku WAV 精确集合826零差。未运行模型/设备/main/正式 memory，未提交推送。

本切片 PASS 只代表前台等待有界及生命周期正确，不代表模型变快。下一切片先查实际三模型共存 resource_admission 超时；有界预取、精简、最终性能仍未完成。以下失败记录保留作评审历史。

## 第四次返修后最后一处返回值不一致（Root 确定复现）

owner 保留已正确。但 `join_foreground_loader_bounded` 在 thread.is_alive=False 分支仍无条件 return True；bootstrap unknown 不会 reap，却宣称 join 完成。Root 已在同一 bootstrap probe 增加 joinFalse 断言，最新 `foreground_interrupt_ack_root` 4 项 1 fail，1.407 秒。两处 join 返回值均须按本次 job 的完整 in-flight/unknown 谓词，不能只按 is_alive；返回 True 必须是真正静止且可 reap。不要长等 unknown/未start线程（直接 False），实际已启动则剩余预算 join。仅此定点、Root4+原5+原同步9/6探针，以及七模块已在 Root 独立运行。修完后 STOP，勿再增范围。

## 第四次 Root：真实 OS bootstrap 尚未确认的中断窗口

第三次三项大部分修好，但 Root 新独立 `tmp/codex_foreground_interrupt_2026_11_03.py` 4 项中 3 过、1 确定失败，1.422秒。实际创建 OS thread 后，Python Thread._bootstrap 尚未设置 _started 时，`Thread.start()` 的 `_started.wait()` 可被 Ctrl+C 打断。这时 is_alive=False **不能证明线程未启动**。Root 在 Thread 子类 _bootstrap 前加 Event gate，原 start 真正创建 OS thread，wait 注入 KeyboardInterrupt：当前 job terminal=True/thread=None、blocks_shutdown=False，晚到线程仍会执行，违反 owner/TFM。

只修最后这项：保存创建出的 thread；明确 constructor 未返回 vs start 已尝试。constructor 异常可安全终态；start 尝试后被 BaseException 中断、未确认 started/worker entered 时必须 start_unknown 保留且阻 shutdown/reap/new allocation，不能用 is_alive=False 当 quiet。worker entry ack 与 worker terminal 是独立票据；实际 worker 确认进入并最终结束后才能 reap/join，不能永远保持未知。可小范围登记 start_attempted / worker_entered 明确状态，不依赖 CPython private _limbo、无第二线程/线程强杀。

如果 start 调用在真正 OS spawn 之前就被中断，公开 API 无法证明未启动，允许保守 unknown/incomplete，CLI 中断将退出；不能谎报清理完成或自动再开任务。**构造阶段**未返回 Thread 可明确完成。Root pre-start SystemExit probe 将相应改为 unknown 保留（不把注入失败后无 OS 子线程的信息假定为生产可知）。普通启动失败应说明证据与可恢复限制，保守保留也不得无限分配。

此外你新增 test_creator_start_worker_blocked_until_release 创建的外部 requester 未保存/join、lambda 异常未记录；必须 finally release gates、保存两类线程并确认结束/传播 fixture 错误，不以 daemon 当测试通过。补真实 bootstrap gate 可确定测试，Root probe 四项不可删除/改弱断言。其余三项/5边界/155回归保持。STOP 待验收。

## 刷新后第三次 Root 评审：三项定点返修

已独立跑 5 项 foreground 边界全部通过，1.421 秒；151 项 App/CLI 回归 8.382 秒，1 error，WAV 826 零差。尚未 PASS，禁止开始预取。

1. **P1 creator 中断留下永久在途**：`_creator_start_job` 只 except Exception，Thread 构造或 start 抛 KeyboardInterrupt/SystemExit 时，finally creator_done=True，但 terminal=False、thread=None。Root fake constructor KeyboardInterrupt 复现 blocks_shutdown=True，实际没有线程。所有 BaseException 都必须 finalize creator；未启动应 terminal failure + done；已启动/未知应保留实际 thread/票据直到真实结束。调用者 KeyboardInterrupt/SystemExit 必须按原语义重抛，但不能丢 ownership；不吞异常伪成功。补构造前中断、start 前中断、实际 start 后中断（worker 由 Event 阻塞）的可确定测试，finally 释放 gates 并确认 join。
2. **P2 CLI 回归测试过期 patch 点**：151 项唯一 error 是 tests.test_voice_session.VoiceStreamTurnTests.test_stream_entry_flags_from_run_voice_session 仍 patch 已移除 ensure_voice_ready。更新测试到当前 check_voice_preflight 窄接口，仍验证实际 run_voice_session 的 flags 路由，不能删除断言或仅导回未使用符号骗过。重新一次相同七模块 gate。
3. **P2 旧注入 result 兼容不完整**：text_session 两处 elif load.pending 仍直接属性读取，_request_tts fallback 可返回旧 result 无 pending。统一用真实 bool getattr(..., False) 或窄 helper，补实际 run_text_session 初始 auto_tts 与显式 tts 重试两个路径，旧结构无 pending 时显示失败并可正常退出；不能只测试 getattr helper。

保留已过五项与原同步 Root9+6，daemon 只兜底、所有模型/Safety/正式 memory 边界不动。只改本切片，交付 ADR/handoff，STOP 待 Root。Root gate 输出必须用 Python -X utf8，避免报告中文错误时 cp1252 二次报错；这不是产品错误。

## 第二次Root评审（新独立3项已过，仍未PASS）

Root `foreground_root_boundary` 2.078秒3/3：失败不自动重启、实际start后抛错仍保护TFM、最终回收；WAV826零差。但源码仍有以下确定窗口：

1. `_reap_finished_job_locked` 对job.thread已赋值但**start尚未真正调用**，is_alive=False直接摘job；完全忽略start_ack。构造/start在途时绝不可reap。用creator_done（finally成功/失败都ack）明确票据；只有creator已ack且实际thread结束（或确认未启动）才能join/reap。worker terminal不等于creator完结。补start gate阻在real_start之前、并发request/shutdown不能清job/闭TFM/新开线程。
2. daemon兜底当前只在`app._lifecycle_closed()`为True才使用，但正常request已拒绝closing，所以正常在途job都是non-daemon，关闭后无法改daemon，仍可阻Python退出几分钟。为这些受追踪loader**从创建时**设置daemon=True作为进程退出兜底，保持Job/TFM/incomplete真实，不能以daemon当完成；不得closing新起线程。NativeJob父退出杀模型是既有兜底，不新增强杀线程。
3. notify_foreground_shutdown只interrupt正式tts，遗漏owner_slot.tts（未发布候选中可能正RVC预热）；短锁snapshot两对象，去重、锁外interrupt，不清owner；先wake waiters再做有界取消。候选无interrupt只能保留incomplete，不阻塞等constructor。
4. _fg_wait_outcome先reap再取result，已完成失败job被清后返回pending，错误结果丢失。保留本次job局部结果/缓存终态，再reap；失败及时返回失败，ready仍只读确认。explicitretry清缓存只在新Job实际准入时，不得在旧job仍active时清app._tts_load_failed；该flag仅同步lane持票者通过allow_retry处理。
5. 背景KBI/SystemExit捕获后finally再raise会抛thread traceback，终态已发布应作为background失败保留，原同步ensure中断语义仍不变。无新清理线程/无额外loader。

只定点以上窗口，保留旧3项、原同步Root9+6与standalone/upgrade覆盖。targeted60秒监督一次，Root probe20秒一次，交付STOP，不扩展新功能。默认30秒、实际三模型共存超时仍未解决。不改Root current-status。

Root完整读voice_foreground与App/CLI调用方，尚未放行下一切片。当前22项绿灯不足，以下为实际状态缺陷：

1. **线程未退出就摘引用**：worker在返回前清_fg_loader_thread、_fg_starting，之后还set共享done。新请求可启动第二线程，shutdown漏计旧线程；旧done还可能唤醒新任务。保留线程ref直到实际is_alive=False并join确认，由creator ack starting；worker只发布本次结果到本次event，不能摘自身ref。建议一个小Job记录（thread/start_ack/result/done/kind/generation），避免8个共享字段跨代复用。
2. **partial start失败丢活线程**：Thread.start实际启动后抛错，except无条件清ref；Thread构造本身抛错还在try之外，会留下starting永远True。把constructor/start全纳creator协议，先保留job，若ident/started已存在则保留未知在途线程，不能丢；原同步事务负责资源。线程里的KBI/SystemExit目前直接raise不finally发布，starting/event永久悬空；后台应记录终态失败并finally唤醒（原同步ensure仍保留中断语义）。
3. **失败后仍可能前台长等/反复建线程**：_fg_request看到_tts_load_failed就直接调用同步ensure，可能在另一个仍retire的lane等30秒；线程Exception未置appflag时下一request又自动新线程。Cosy request永远不绕过有界状态机；active即pending，终态失败缓存sticky，explicitretry才能新任务，禁止自动重起。返回结构化已有错误，不能同步等lane。
4. **升级误ready**：worker只copy一次kind，运行中tts→voice升级不补STT；_fg_wait_outcome无条件return stored.ok，使语音栈无STT也ready。单worker完成升级目标（可以再走一次原同步事务补STT），或明确继续pending；任何ok必须以本次请求的只读ready_fn确认，不能只认上一任务ok。
5. **真实startup voice路径无效**：cosy_foreground_path先检查autoload，默认--mode voice的autoload本来False，因此仍同步阻塞；bound Cosy的显式request也应有界，缺组件且禁止构造的注入路径则按同步guard立即失败。不要用autoload作为所有后台准备的总开关。
6. **standalone pending立即退出**：run_voice_session在ensure_voice_ready=False直接return；src/cli/app.main随后finally.shutdown取消加载，没提供文本/交互过渡。按任务接入pending区别失败，在standalone时保留主进程、提供文本交互/再voice入口，禁止开麦和旧回复补播。测试实际路由（fake main/voice_session，无设备），不能只测preflight。
7. **非daemon未完成可拖住程序退出**：第三方constructor/同步RVC准备不可马上取消时，non-daemon让Python退出等待最长几分钟。shutdown先唤醒前台，锁外对已登记candidate/正式TTS发既有interrupt取消；保留Job/TFM至确认线程结束。可将daemon作为**进程退出兜底**（Native Job父退出杀模型已有真实验证），但绝不能以daemon当回收完成，所有status仍incomplete且不新分配。不要强杀Python线程/os._exit或新增清理线程。
8. **readiness与兼容**：只读getter异常/非bool返回fail-closed，不bool(truthy)伪ready；旧注入result可能无pending字段，CLI用兼容getattr(...,False)真实bool，避免新属性破坏旧适配器。保留同步ensure与Root9+6原断言。

先按完整生命周期表修同一模块，不开预取/新性能功能。补start构造失败/启动后异常、worker中断、升级STT、缓存失败不新线程、autoloadFalse boundCosy、standalone pending文本过渡、关闭所有迟返字段与实际join测试。使用现有Root supervisor60秒（stdlib unittest，无需pytest），一次targeted+Root老probes，失败STOP。原默认30秒/声学/模型/已验收RVC不动。正式memory/main真实运行/设备/提交推送禁止。更新ADR/handoff，STOP待Root独立评审。
