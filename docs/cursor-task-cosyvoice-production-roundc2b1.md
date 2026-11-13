# C2b1：显式本地模型prepare（不自动调用）

用户授权Cursor官方CLI实现，Codex设计/严格复审；读AGENTS及相关协作/架构/工程技能、C1/C2a最终评审。C2a独立957 tests(skipped6)通过，C1实际3组6WAV通过。保留既有dirty修改；禁读改真实data/memory、真实模型/设备/主程序、提交推送。只本切片，完成停止复审。

## 目标与接口

真实C1短句冷首音频19秒，热短句预估3–4秒，当前TTSEngine没有显式本地加载入口。先补窄effectful prepare API，供下一切片资源协调器调用，**本切片不接assembly/resource_coord/CLI，不改默认/权限/timeout/idle**。

1. `CosyVoiceWorkerClient.prepare(*, cancel_event=None) -> bool`：显式许可校验后，复用既有epoch、_begin_request/_end_request、_ensure_worker_started和idle退休、progress快照。只有确认ready、同epoch、非closing、未取消时True。忙/退役/stop_failed/closed/权限拒绝即False；并发busy立即拒绝，不长时间抢锁。任何成功/失败/异常均释放本次请求状态；开始的启动失败/取消按既有有界停止方式回收或明确保留owner，不能假解registry。已可能执行的请求不重试。本API**零synth请求、零文本/音频输出、零TFM.create**，只有明确调用才加载模型；已有健康worker可复用。不要把它当无副作用readiness polling/heartbeat。
2. `CosyVoiceTtsBackend.prepare(*, cancel_event=None) -> bool`：进入C1现有在途生命周期，先处理既有失败worker/待删owner gate，不得绕过两阶段回收；复用client.prepare。关闭时能看到正在prepare的在途操作，进程未退出前保留TFM。没有新增Timer/每请求线程/新全局registry，也不能为了prepare放松B/C1锁与owner边界。
3. `TTSEngine.prepare_local_tts(*, cancel_event=None) -> bool`：只有Cosy适用；其它引擎False/跳过且零localclient调用。与speak/save共用短准入，不等待持有中的speak_lock（忙即False）；沿现有epoch准入方式，新的显式prepare可清除旧engine interrupt，但不能清新epoch/外部cancel。将外部cancel与engine interrupt/shutdown合并为只读is_set token（无需线程），传至client；外部事件不可clear。构造不调用它、prepare不启动RVC/播放器/Edge，不更改engine_type。失败/取消零fallback。仅固定阶段/耗时日志，不记录正文/路径。

## 验证边界

用B现有fake子进程/事件屏障验证真实客户端：prepare只spawn一次且无synth行/无TFM创建、第二次健康复用、idle退休后确认旧进程退出才新建、忙合成时prepare立即拒绝、prepare期间synthesize拒绝、close/interrupt/外部cancel/timeout/资源错误/权限拒绝，不出现晚到ready/假complete。Popen延迟返回沿B owner语义；不声称可中断OS原生阻塞。

门面fake验证：Edge零调用、Ctor零prepare、RVC/AudioPlayer/Edge零副作用、同代新显式prepare清旧cancel/排队旧epoch被拒绝、合并外部cancel不被clear、prepare与speak/save互斥、关闭在途prepare不关TFM且重试可完成。TFM/client addCleanup，正常生命周期回收不强制清owner/registry。不要仅用MagicMock truthiness冒充关闭完成。

主Python312先新专项+B/C1/C2a及mode/resource/stream回归，真正120秒进程树监督器，精确外层退出与miku文件集合零增。新失败才再跑，不反复957全量。compile/diff-check，ADR记录prepare是显式effectful、未接装配、冷热区别；交接后停止Codex review。Codex通过后才跑真实“prepare→第一句热合成”捕获，再下一切片预热协调。
