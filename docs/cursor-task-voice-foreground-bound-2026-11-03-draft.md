# 本地语音前台等待有界（正式放行，文件名保留draft）

App同步所有权已Root分层PASS（561项剩2回归定点后三行删除，95项独立通过；Root9+6边界通过；RVC真实准备21.25秒退出47ms aggregate完整），详见里程碑review。现在只实施本切片。实际三模型仍因Cosy资源准入超时失败，记录为未解决，不能宣称ready或仅延长默认预算。复用已验证的同步事务，不复制第二套资源状态机。不涉及预取或声学，不改变默认Edge/30秒模型启动预算。无main/模型/设备/正式data/memory/提交推送。

## 采用增量接口，保留同步兼容

保留现有ensure_tts_for_playback/ensure_voice_stack同步语义与Root旧probe。新增窄 `request_tts_for_playback` / `request_voice_stack` 有界接口（可委托新小模块），明确Cosy时一个有App owner的线程运行已有ensure_voice_transaction；其他引擎走现有同步兼容路径。CLI和App自动播报路径调用request接口而非偷偷改变全部同步API；旧注入适配器未声明新接口时兼容调用ensure。

新建冷任务可立即返回pending；已有未完成任务也可立即pending，不必每次强等5秒。若提供短等待，严格不超现有foreground_warmup_wait_sec总deadline。不要创造第二套build/prepare/retire事务，不能在background thread直接不经lane调用模型。

## 目标与边界

本地模型冷启动仍可能数十秒，不能让CLI同步卡住。明确选Cosy时，一个有App owner的加载线程运行原同步事务，前台按现有foreground_warmup_wait_sec总deadline等待，未完成返回pending（非ready，非失败，不重建）。Edge/注入普通同步兼容，不给text无语音路径加载模型。

- VoiceStackLoadResult末尾兼容增加pending字段或等价窄状态；所有旧构造/ok/errors/warnings保留。最多一个App加载线程，未结束或starting仍保留引用；失败不自动重起，explicitretry先退休旧candidate。新增的线程是唯一在途任务，不每个ensure/每个回复新建。升级tts-only到voice-full请求不能并发第二套TTS。
- thread引用+starting票据在短锁内先登记，再锁外start；start异常/线程已开始但start抛错均真实保留/清理。真正join/确认结束才摘除，不能daemon即完成。状态锁不包wait/start/prepare/shutdown。
- shutdown设置closing/cancel并notify，waiters<300ms醒；不能晚发布成功。异步starting/loader alive/pending candidate任何未知都阻TFM关闭和non_memory_done，独立组件继续关闭。shutdown可有界重试，loader在退出后收尾不能丢资源，不额外spawn清理线程。
- 调用真实readiness：Cosy曾prepare成功但已idle退出时不能永久缓存True，需显式只读ready能力/失效通知，使用现有资源状态；不要为了查询启动模型/子进程。injected适配器兼容用明确声明API，无Mock特判。
- CLI interactive区分准备中/失败/ready，pending时文本可用，不反复打印错误或清掉用户auto-tts意图；后续输入时ready可恢复播报，旧回复不补播。voice preflight pending时不得开麦。standalone voice启动pending不能立即finally shutdown把加载取消：应提供有界可返回的交互路径/继续文本并可再次voice（如必要main路由处允许小改，不启动main测试）；避免创建等待input的后台线程或轮询忙循环。
- App所有播报入口（stream/tracked/legacy）必须确认ready，不能因为tts对象绑定就绕过pending同步启动模型。
- 日志只阶段/耗时/状态，无文本/记忆。错误真实，不把冷启动包装成ready。模型阶段预算原样，UI返回pending不代表worker已结束。

## 验证

Event fake prepare堵塞：前台有界pending；多次ensure单线程/单constructor；ready后ok且不重复prepare；失败sticky与explicitretry；start失败/关闭期间start与晚返回；线程必须join，TFM不能提前关闭；pending不启麦/不播旧内容/文本可继续；idle后ready失效；Edge旧路径与旧result构造兼容。保留root同步probe，若public改异步语义，Root将新增不改旧断言的独立接口测试。

targeted60秒监督一次，失败STOP。同步App/RVC前置不能回归；交付handoff/ADR后STOP待Root独立验收。
