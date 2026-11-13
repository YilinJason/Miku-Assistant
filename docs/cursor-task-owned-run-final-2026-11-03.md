# 单次 RVC 最终所有权收口（正式下发）

## 阻止被拒调用清除别人的 active 标记

Root 审查最新 execute：reserve被拒时 `rec=None` 仍进入finally `_end_execute()`，把**另一个请求**的 `_run_inflight=1` 清零。必须用本次成功reserve身份/布尔准入票据，只能拥有票据的调用在finally清除自身active。补：第一请求spawn gate → 第二拒绝 → 断言active仍真/retire仍False → 第一继续到capture gate（raw可已退出）→ 第三拒绝 → 释放第一，只有一个spawn且最终quiet。

public retire 的pending清理也应实际设置/清除 `_retiring`，当前只读该字段没有人设True。短锁获取独占清理票据（active时只cancel不清理），锁外kill/wait/join/release，再finally在同锁释放票据，防两个retire或新reserve同时操作lease。无新增线程/等待；private cleanup只由持run票据的caller或持retire票据的public路径进入。

这是同一协议最后的已确认状态问题；定点两处和测试，保持其他已过探针，不跑旧扩展gate，交付Root统一复验。

## 最后收尾：消除 begin/reserve 空窗口后执行 Root gate

Root 当前独立 final3/cancel2/contract7/status4 全 true。但静态状态检查确认：`_begin_execute` 只设active=1，尚未发布record；此时 public retire 看到record=None返回True，调用方可关闭TFM，而execute随后仍reserve/spawn。**begin必须原子发布starting record**，或明确记录未发布inflight取消并在reserve前拒绝；retire在任意run-inflight窗口不可True，closing请求必须传到晚分配路径。请用gate阻塞begin返回/reserve前（而非模拟不存在的状态）覆盖关闭不成功、释放gate后零spawn或已分配者立即回收、TFM仍保留。

优先合并冗余的begin/ensure保留单一原子准入；不要让旧Root admission probe对私有helper的hook妨碍正确设计。旧probe保留不改，有不可用hook请记录原因，以Root新成功+partial probe及正式行为测试为准。清理永远因_run_inflight=1返回False的stale helper（显式retire后才重试的策略可接受，写清楚），避免虚假的自动回收入口。所有owned.kill移到状态锁外；read/close异常应记录，不让线程抛出未处理异常伪装成功。取消必须在result成功前检查，不能rc0即成功。

完成这一点后不重复408旧门禁，只跑新targeted与Root现有probes各一次并交付；Root自己执行扩展gate。其他切片仍禁止。

## 最新 Root 复验：仍两项确认缺陷，定点修复

`tmp/codex_rvc_cancel_start_failure_2026_11_03.py`（不改）两项false：second_capture_start_preserves_original、active_retire_requests_process_termination。前次三个边界需同时保留通过。

- retire 对 run_inflight 直接 False 没发取消也没终止进程，应用退出可能仍等待90秒推理。短锁下snapshot并set stop_event；锁外kill已知owner；active期间仍返回False，不能清记录/unpin。creator尚未返回则记取消，晚返回owner登记后必须立即取消，不能继续正常成功。
- capturer记录在线程start之前已登记；第二start失败时_join_capturers对未start线程join抛RuntimeError，覆盖原异常。每个capturer在start真正成功后标记（start抛错但实际启动也需用ident/is_alive处理）；只join已启动线程、无启动无IO线程直接确认。失败清理必须保留原ValueError身份，即使join/close/query失败。不要用未使用started列表，去掉死代码。
- 测试仍8项且bootstrap仍源字符串，与交付所称行为覆盖不符。补实际runpy/import拦截join失败顺序矩阵、第二start失败、取消后不成功、lease/query失败。不要删除原6项包含pinned删除/非法参数/busy/存活Job的已有效行为覆盖，只增有用矩阵并保留。测试finally释放所有gate并join，不能主线程assert后留daemon/假线程。
- `_retire_stale_outside_lock` 从execute在_run_inflight=1时必拒：保留pending时可以要求显式retire，但接口/ADR需一致，不留永远不可达的自动重试helper；同一记录的两个retire互斥，锁外I/O，active reject也要cancel。

只修以上与测试，不开新功能；Root fault probes全通过后跑一次包含新owned-run、TFM存储与此前408相关范围的扩展gate（旧408未含新模块，不能当覆盖新模块的证据）。STOP待复验。

先读 AGENTS 与 collaboration-review/architecture-contracts/engineering-quality，检查 dirty workspace，保留已有修改。Codex 设计/评审，Cursor 实施。当前仅本切片，不进入预热/预取/大重构，不提交推送，不启动 main/真实模型/设备，不访问正式 data/memory。主 Python312，RVC/Cosy 独立311；核心 Native Job 与持久 reader 已验证，禁止重写。

## Root 新确认

`tmp/codex_rvc_final_boundaries_2026_11_03.py`：正常一个请求成功且只分配一次=true；partial handoff 阻新分配=false；errors-only text=false。脚本纯 fake，所有线程 join，无子进程。Root 原探针不得修改。

本机 Python3.12 subprocess.Popen.__init__ 明确 `self.text_mode = encoding or errors or text or universal_newlines`，因此当前 errors-only bytes 测试预期错误，改成符合真实语义，codec handler 仍须 effect 前验证。

## 按统一状态协议修，不继续叠补丁

1. 独占执行记录：从准入到 execute finally 完成之前必须有显式 run-inflight 标志；只在同一短锁下 compare-and-reserve。活跃新调用直接 busy，不杀另一活跃请求、不覆盖其记录。只有调用已经结束而留下 pending 的记录可以锁外重试清理。新准入/显式 retire/清理回调须协调；严禁锁内等待进程或线程。
2. **partial owner 登记先于 creator ack**：当前 inner finally 把 starting=False，然后 outer except 才 take/adopt，其他调用可看到“空的已静止记录”并覆盖，pending Job 丢失。把 take/adopt 与 creator-ack 顺序固定，在所有 startup 成功/失败分支均先接管已分配 owner/capturer，再在受锁状态提交中 ack；原异常身份保留。内存分配/属性访问/第二 capturer.start 抛错也覆盖。
3. public retire 在 creator 未 ack 或 execute 在途时必须不能报整个 run 已静止：可发 stop、终止已知 owner，但返回 incomplete，保留记录/lease；不得持锁等待 active caller。caller finally 做有限清理后清 run-inflight；只有 raw+Job+capturer 真正结束才解 lease。retain 未知/释放失败供重试，查询错误 fail-closed。retire/new-admission/finally 不能并发释放同一 lease 或误判新记录。
4. capture 仅在成功 start 后 join，必须实际 join 后验证 is_alive。记录读/关闭失败，不用 thread event 替代真实结束。进程已退出、stdout仍堵塞时不成功；poll超时不跨过现有总预算，也不能附加两次完整 shutdown 等待。清理错误不覆盖原异常。异常 attr 必须附在实际抛出的同一个实例上，去掉给临时异常 attach 后丢弃的重复代码。
5. kwargs 与 Python 兼容：encoding/errors 隐含 text；bytes 默认不变；非法 codec/errors/finite timeout 在分配前拒绝。保留64KiB两路尾与可观察截断 metadata。环境继承与Job私有覆盖保留。保留旧状态构造兼容。
6. App 旧注入 policy 未声明新 API 的兼容处理：用静态声明检测，真实 policy严格检查，两API声明不完整/不可调用视失败；完全未声明的新接口保留旧路径。Engine stop_rvc=False 不回收共享 run。关闭失败不跳过独立 cleaner/character。

## 必须的行为覆盖

- 第一请求在 spawn gate，第二请求零分配被拒，释放第一后成功，线程全部 join。
- partial exception 在 owner 接管前 gate，第二请求拒绝；接管后故意 Job close failure 保留记录，再释放故障后可回收；原异常身份不变。
- public retire 与 caller/新请求竞争，creator未完不得完成；capture第二线程start失败；EOF迟到/读错/关闭错；lease释放和查询失败；invalid kwargs零分配。
- 原 fixed probe bootstrap 与实际 infer main 用 runpy/import hook 验证 join fail 发生在重依赖前（禁止只 assert 源码）；不加载真实torch/RVC。
- 更新错误预期的 errors-only 测试。不要只证明两个都失败的 spawn_count=0。

Root new probe +旧7/status4 + targeted各一次，通过后单次相关<=180秒 normal Windows门禁；保留825/826集合精确差异验证。失败立即说明，不重复后台求绿。必要时正常Windows执行避免已确认沙箱asyncio.socketpair死等；不得改生产绕过。

返回文件/验证/风险，更新handoff/ADR；不要改Root current-status，STOP待独立评审。
