# 预取生命周期基础（已放行，先独立验收再接线）

TFM已Root最终PASS（具名review顶部），本切片服务于下一流式句间预取，统一新producer/消费线程的生命周期，避免继续复制不一致的启动/回收旗标。读AGENTS、architecture-contracts/engineering-quality/collaboration-review/safe-refactoring。巨大dirty保留，禁止git回退。正式data/memory禁读改删，无真实模型/设备/下载/清理、权限/配置/声学/NativeJob/现有TFM/RVC底层改动，未提交推送。

## 窄范围与API（请保持名称供独立probe）

只新增stdlib模块src/core/owned_thread.py、tests/test_owned_thread.py和简短ADR/交接。此次不接App/TTSEngine/TurnSpeechPlayer或重构旧实现，不引框架/池/监控线程。下一切片立即用于预取，不为未来XR做抽象。

`OwnedThreadTask(target, *, name, thread_factory=None)`：target同步接收一个threading.Event取消信号；构造仅存状态，不创建/启动任何Thread。thread_factory仅用于构造未启动Thread，None在start时动态取threading.Thread（非定义时绑定）；固定daemon=True，但不得据daemon判quiet。

- start()->bool：单生命周期只允许一次启动尝试，第二个并发/重复start返回False且零新Thread；正常start返回True只表示start已ack，**不表示任务完成或成功**。构造和start普通/KBI/SystemExit异常均以原对象抛出。调用前已cancel直接False、零Thread。
- cancel()->None：仅置永久Event，不clear，不做任何其他副作用；在途target须自行配合，不能宣称已停止。
- join(timeout=2.0)->bool：只返回实际quiet，不代表成功；timeout严格有限非负int/float（bool/NaN/inf/None拒绝），建议硬上限10秒（超出ValueError，禁止无界join）；Thread未确认启动不得join未启动对象，返回False保留owner。构造/start未返回时也不能被锁拖住。已经started或已进入/实际alive的线程可锁外有界join。
- snapshot()->OwnedThreadStatus：frozen dataclass，只读严格bool字段creator_pending、start_unknown、entered、terminal、alive、quiet。alive如实观察；调用不启动/等待/清理模型。不在持状态Lock时再调用会取同Lock的public方法，避免复刻TFM死锁。
- error只读property：target或启动失败的原BaseException（可None），不字符串化；target内BaseException被wrapper保留并发布terminal，消费者之后检查error，不能把quiet等同ok。异常上抛/捕获不得丢已有owner。

## 状态与证明（最重要）

1. start在**Thread factory之前**锁内预留creator_pending/单次attempt，使构造阻塞时snapshot/join/cancel仍快速返回；factory/start/target/join均不包状态锁。caller会先登记OwnedThreadTask对象，再调用start。
2. 仅Thread构造失败、或构造已返回且cancel在**任何OS start尝试前**被确认，属于known-no-start，可quiet=True。检查cancel后到start准入使用单一可说明的线性点；cancel之后target wrapper入口仍检查取消，迟到不调用target。
3. 调用Thread.start之前登记start_attempted并强持有线程。start抛出后即使is_alive=False/ident=None也属于unknown，不能假定未bootstrap，quiet=False且拒第二次start。没有entered/terminal证明不得伪造终态。
4. wrapper在调用用户target前发布entered，finally发布terminal；target异常保留原对象，无额外日志/线程。wrapper返回时不能摘线程引用，因为Thread可能仍真实alive（测试会在外层wrapper阻住退出）。
5. 已尝试start的quiet必须同时满足creator已返回、entered+terminal已证明、actual thread not alive。unknown迟启在cancel后进入wrapper应零target调用，真实退出后可quiet=True并收口start_unknown；不能unknown永久不消失，也不能未进入就“join没活着”宣布结束。未start的新对象无资源可quiet=True。
6. 不维护无界历史/retired列表，不复用一个实例做第二代；已知quiet后上层可丢owner建新实例。error最多一份、线程/target引用不在未quiet时丢失。不访问任何GUI/模型/设备或磁盘。

## 确定性矩阵（repo与Root独立）

正常单次/重复并发start、构造不启动线程、cancel-before-start零factory、target忽略cancel时join短预算False；constructor阻塞及constructor原异常、start阻塞未ack、start抛普通/KBI/Exit（含实际thread已启动但start仍抛、与未可见后才bootstrap两类）；terminal后实际Thread仍alive不能quiet；unknown迟启cancel零target且实际join后quiet；target普通/KBI/Exit原对象可观察且quiet与成功分开；join非法预算拒绝。所有gate.wait结果检查、线程BaseException主测试确认、finally release并实际join/notalive。禁sleep猜时序、禁daemon当回收。

先现有Root scoped gate唯一label新模块<=120秒前台，失败停定点修，禁止raw unittest/discover/detach/并发；不改Root探针监督器。compileall指定模块/diffcheck、ADR与实际JSON交接后STOP供Root严格源码/独立probe验收，未过不接入流式预取。
