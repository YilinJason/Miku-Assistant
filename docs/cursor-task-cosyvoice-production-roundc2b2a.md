# C2b2a：独立三模型串行准备协调器（不接App loader）

读AGENTS/相关协作架构工程技能及C2b1最终评审。用户授权Cursor实现、Codex严格review；保留既有dirty改动。禁实际data/memory、真实模型/设备/主程序、提交推送。C2b1离线245独立通过+真实prepare17.953秒，热首句4.172/次句3.609秒，全部回收。只本切片，交付停止。

## 目标

增加可独立验证的同步协调器，供下一切片App在已有owner保护下调用。**不改assembly/assistant/CLI，不自动启动，不接预取，不改默认引擎/权限/阈值/RVC音色参数**。建议`src/core/local_voice_warmup.py`，使用冻结结果对象，固定stage标签、总/分阶段monotonic耗时，不含文本/路径/模型回复。

`prepare_local_voice(llm_client, tts_engine, settings: ResourceSettings, *, require_rvc: bool=True, cancel_event=None, ...) -> LocalVoiceWarmupResult`：

1. 仅明确Cosy引擎；非适用/缺失依赖返回固定跳过或失败结果，零副作用。该功能当前仅支持`serialize_gpu_warmup=True`、`warmup_order=ollama_then_rvc`、Ollama启动预热开启；若require_rvc则RVC预热也须开启。不支持的策略在任何动作前拒绝，不静默覆盖用户配置，不放宽资产/资源准入。仍复用现有ResourceSettings，无新profile枚举。
2. 顺序为**本轮Ollama空请求成功且结束 → RVC准备成功（require_rvc=False则跳过）→ Cosy prepare成功**。前阶段失败/超时/取消时后阶段零调用，无自动重试。不得并行抢GPU、不创建额外队列/Timer/模型线程。
3. 现有Ollama `wait_for_warmup=True`只说明event已结束，不保证`_warmup_ok`，也可能是历史很久前的成功。给OllamaClient增加窄、默认兼容的显式fresh warmup能力：例如`start_warmup(..., restart=False)`，默认旧幂等保持；restart=True仅在旧周期完成后新开**一个**空请求周期，已有周期未完成则复用等待，不能第二个并行请求/线程。新增只读不可变warmup结果快照/成功查询，协调器仅在本轮已结束且真正成功才继续。不能用ever model_touch_succeeded冒充本轮成功。新准备不读写会话历史；不新增HTTP endpoint、任意参数或扩大安全权限。Memory wrapper正常委托需兼容。
4. 等待Ollama时用一个绝对deadline（受settings.warmup_timeout上限约束）、短poll有界wait，以便cancel及时返回；不在状态锁内长等，不busy-spin。客户端wait接口异常/无可观察结果失败关闭。cancel在每阶段前后都检查；RVC原有同步调用的边界受其既有timeout约束，不能宣称中途可强制中断或给总预算假保证。协调器不接管注入engine生命周期，返回失败时由caller保留owner并回收，不能丢句柄/临时文件。
5. RVC `TTSEngine.prepare_rvc`目前health通过后无视warm_rvc_worker返回值，可能假报ready。限定修正：worker_enabled时实际worker启动失败必须使_rvc_ready=False/returnFalse，不能让新协调器把健康检查等同模型已加载。原fallback/health诊断接口不放宽、不改参数、不重构全RVC；相关fake回归。require_rvc=False不调用RVC，支持纯Cosy适配，不强制所有本地TTS都用Nahida。

## 测试

fake事件/可控时钟：阶段严格顺序；Ollama wait结束但失败时零RVC/Cosy；过去成功→fresh请求本次失败也零后续；已有进行中warmup绝不并行新线程；默认restart=False旧语义；fresh成功、线程start异常恢复可重试；取消/超时/异常不前进阶段、不清外部cancel；不支持profile组合零动作；RVC返回False阻止Cosy；Cosy失败固定结果；pureCosy跳过RVC；所有耗时有限非负；不触碰真实历史/设备/清理任务。

RVC相关用fake health/worker验证healthOK+warmFalse必须not-ready。保持既有Ollama/performance/资源协调/取消/关闭测试，不能以强制清registry或吞线程异常清理。fake TFM/client正常addCleanup且精确文件集合零增。

主Python312，先新增+ollama_performance+mode/resource+B/C1/C2a/C2b1回归，真正120秒测试树监督器、UTF8日志和外层返回、文件集合检查；新失败才重复，不无故全量。compile/diff-check，更新ADR/交接并停止。Codex通过后才实际三模型共存/首句验证；App loader owner接线与取消尚属C2b2b，不提前做。
