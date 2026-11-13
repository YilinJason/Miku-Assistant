# App 语音加载所有权与预热接入（正式放行，文件名保留draft）

持久RVC IO/全局归属已Root PASS，依据 docs/codex-review-persistent-rvc-io-2026-11-03.md 顶节。现在只做本切片：同步ensure接口的候选/关闭所有权 + 现有三模型prepare接线；前台非阻塞包装另行切片，禁止此轮新加后台加载线程。本文件取代旧C2b2b草案的执行顺序：先用fake闭环所有权和主程序接线，再由Root做受控短真实诊断，不把离线通过当真实共存通过。无main/设备/正式data/memory/提交推送，不更改声学、默认引擎或30秒默认启动预算。

## 当前问题与目标

assembly在App持有TTS之前预热；ensure看到任意bound TTS就True（Cosy可能未准备）；丢弃未发布candidate只shutdown后丢引用；锁内shutdown；关机可能在constructor/prepare仍在途时关闭共享TFM。现有三个ensure重复大量流程。所有权修复与接入prepare_local_voice共享同一生命周期，作为一个切片，预取另行实施。

## 设计

- 一个single-flight加载通道，一个cancel Event，一个generation；已发布TTS或一个未发布candidate，全程有owner；最多一个pending-retire candidate。constructor返回前由loading标志保护TFM；返回后先登记candidate，再prepare。保留现有Edge与from_dependencies“不自动构造”语义，旧入口/VoiceStackLoadResult导入兼容。
- 默认装配对明确Cosy只构造、不开任何模型；App绑定后ensure才调用现有prepare_local_voice（fresh Ollama→RVC→Cosy），require_rvc取实际配置。Edge沿用既有资源协调，避免重复Ollama预热。不得在assembly持有未发布candidate时运行模型预热。
- 绑定Cosy不可提前True。prepare一次成功记录ready，失败不隐式自动重试，显式allow_retry仅在旧pending真静止后有效。任何取消/过期不得发布成功。
- 合并重复ensure分支到一个内部加载事务或窄controller（不额外框架）。测试patch既有assistant.build_*入口保持可注入；执行build/prepare/shutdown/join时不持lifecycle/condition锁，锁只读写状态。不新增每次取消一个清理线程。
- 当前保留同步ensure接口，记录CLI可能等待冷启动为未完成项；不增加后台线程。后续非阻塞包装必须有主owner、前台有界和shutdown保护。此轮单独shutdown取消唤醒等待者<300ms，真实worker退出依原预算，不以等待者返回代表worker结束。
- shutdown先closing/cancel并notify；inflight loader/constructor/prepare/retire任何未知保留candidate与TFM，独立cleaner/character/memory仍关闭。晚返constructor也登记并回收，不能丢引用；incomplete可重试。只把真正complete的对象摘除，不根据waiter已取消判断worker结束。新ensure closing即拒绝。
- stage日志只阶段/耗时/稳定错误码，无正文；记录prepare结果供状态诊断。全局协调不允许多个模型同时预热。资源不足/不支持配置及时失败且文本可用，不自动换音色/云端/下载模型。

## 验证

fake Event构造/每个prepare阶段/发布前关闭；单flight计数；bound Cosy不能提前ready；失败STT回滚TTS；shutdown期间waiter快速醒且无晚发布；missing/False/exception shutdown状态保留pending与TFM；显式重试前回收；Edge、注入、text模式保持。线程全部有finally gate release+join，禁止源码匹配代替行为。按当前范围targeted一次，Root独立审查后门禁。无有界预取/声学调整。

验证必须经Root supervisor（新增tests.test_app_voice_activation模块，60秒上限；如涉及原Edge asyncio在正常Windows上下文），一次targeted后交付，禁止裸unittest或自加模型/长测。只更新本轮ADR/handoff，不覆盖Root current-status。
