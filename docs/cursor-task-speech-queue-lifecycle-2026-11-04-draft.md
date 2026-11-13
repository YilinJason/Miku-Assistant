# 流式语音队列所有权与有界状态（已放行）

前置已满足：OwnedThreadTask Root独立10+相关81最终PASS（具名review顶部）。本切片不是最终音频预取；只修复默认stream逐句入口的可靠队列，为下一切片跨句预取提供安全入口。Cursor编写，Root独立review。读AGENTS及四个repo skills、SafetyPolicy、本地safety规则。巨大dirty保留，禁git回退、正式data/memory访问、真实模型/设备/下载/清理、提交推送。

## 已确认现状

- CoreAssistant._ensure_speech_player在turn锁内构造并赋值；现构造中Thread.start，如构造/start阻塞或异常对象还未登记，妨碍取消与回收。
- enqueue锁外queue.put后才锁内accepted++，消费可先完成；put与换轮/production_finished也非原子，可能注入过期队列/错误完成计数。
- _turns每begin_turn无限增加；只捕获Exception，target KBI/SystemExit会使消费者死亡而等待者永不获知。
- 默认stream生成每句分别speak，未来只在单次speak内部预取无法解决大多数句间间隙。

## 范围

仅src/core/speech_queue.py与新的tests/test_speech_queue_lifecycle.py、相关现有fake测试必要fixture和短ADR/交接。不改TTS/声音/默认模型/TFM/RVC/NativeJob/权限，不提前做音频预取/三模型预热。若确实需要Core窄调整，先说明触发点与兼容依据，不扩散重构。

## 设计

1. 构造仅登记一个惰性OwnedThreadTask owner，不构造OS Thread；第一次非空合法enqueue时才申请启动。对象在Core赋值后才enqueue，启动在turn/queue状态锁外。并发enqueue不能启动第二代，不为每轮创建线程。constructor/start异常保留原BaseException给发起者，同时所有既有等待者获知失败，owner保留供shutdown复核。启动未知/消费者致命退出后拒新工作；begin_turn不得假清失败重启死亡consumer。普通speak Exception仍可按现行为在新轮恢复，同一consumer线程持续存活。
2. 队列提交、accepted计数、校验turn/terminal/production_finished及出队在同一个Condition锁内原子执行。满时cond.wait释放锁并有限轮询取消，不持锁调用speak/factory/start/join。mark_production_finished后拒新非空文本，空文本维持无副作用成功语义。不得用empty判播放结束。可改私有Queue为collections.deque以消除重复锁/spaceEvent/STOP哨兵，手动len容量检查，不用maxlen静默丢句。
3. begin/CAS/invalidate仍保证只丢旧轮，不移除新轮。消费者获得item与校验state/置speaking必须在锁内连贯，speak锁外。状态更新/通知放在异常安全路径；普通Exception失败只污染自身仍当前轮，旧轮失败不伤新轮。KBI/SystemExit等BaseException原对象由OwnedThreadTask.error保留，所有尚未终结等待者明确FAILED（已STOPPED/INVALIDATED不改写），以后拒新工作，不能假装已COMPLETED或清标志继续接受。无原始文本/异常traceback日志扩散。
4. 只保留最近128个轮次状态（当前也计入），删除最老已终态；已有waiter持局部state引用仍能取得正确旧终态，未提前建立waiter的已回收ID返回既有INVALIDATED。不能悄悄删除活跃未终态；begin将前轮终结后再裁剪。可用普通有序dict，无额外历史列表。不要在本切片扩展用户配置。
5. shutdown严格有限非负timeout且<=10s（bool/None/NaN/inf/负值/大值拒绝且检查先于破坏性状态变化）。发布stop、唤醒所有背压/完成等待者、cancel owner，然后锁外同一有界budget join；creator未返回/unknown/bootstrap/target terminal但外thread活着均False且强owner不丢；之后可再次shutdown复核True。stop_accepting仍仅停止接收/丢弃pending，不假称底层声音立即停止；不增加实际音频interrupt副作用。
6. maxsize严格int非bool 1..128；poll_interval严格finite数值非bool，0.001..1秒，防无界队列/忙循环，验证在任何thread创建之前。当前默认8/.05及仓库.02兼容。不自动改Core配置值或默认语音。

## 持久确定性验证

构造及空文本/无效turn零thread；首次合法入队一consumer，重复/并发不新增；阻塞factory/start期间取消/shutdown有限返回且不误quiet，迟启后零speak；构造/start普通/KBI/Exit原异常与waiter可见失败，未知迟启保留owner直到实际exit；enqueue/finish/换轮屏障交错无accepted落后或幽灵项；队列满时取消和停止唤醒无丢句；普通/旧轮失败恢复兼容；fatal失败不挂waiter不新开consumer；terminal但外层alive shutdownFalse；重复shutdown实际joinTrue；10000轮状态<=128，旧waiter终态仍准确；invalid配置与预算矩阵。

所有gate.wait检查结果，worker异常主线程确认，finally release并实际join断言notalive；不用sleep猜测，不用daemon证明回收。通过现有Root scoped gate唯一label、前台顺序<=120s（本新模块+tests.test_owned_thread，再stream_playback/voice_session/generate_stream/assistant_orchestration相关组）。禁止raw unittest/discover/detach/并发；不改Rootprobe/监督器。静态编译指定文件/diffcheck，短ADR与报告交接后STOP，Root过关才进入真正跨句有界预取。
