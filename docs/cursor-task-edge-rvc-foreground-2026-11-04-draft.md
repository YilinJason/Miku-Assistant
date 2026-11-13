# 当前等待切片：默认Edge+RVC也走有界前台请求（已放行Cursor）

host-memory准入已Root408检查PASS（skip3），本稿现已放行。真实默认仍Edge+RVC，不切Cosy/模型/Nahida/声学/预算。先读AGENTS、四skills、当前foreground/activation/lane/assembly/assistant/engine及CLI调用；保留全部dirty，无提交推送、正式memory禁读改删、不跑设备/主程序/模型。仅此切片，新增布尔/API输入严格校验，异常不新增泛catch吞掉；完成STOP，不提前实现失败重试或预取。

## 已确认缺口

voice_foreground.cosy_foreground_path仅Cosy；request_tts/voice的Edge分支同步ensure，冷RVC仍可能阻塞。--mode voice的Edge在assemble_default里同步run_coordinated_warmup，还未进入App owner就先等RVC。Core现成Edge TTS的early return，以及bound_tts_models_ready(Edge恒True)，会跳过后续准备。先解决这个事实，再开发预取，不能把Cosy有界请求当所有路径完成。

## 窄设计

1. TTSEngine提供只读、路径无关RVC准备状态（冻结小dataclass，含enabled、attempt_completed、ready、固定error标签/可见警告）。区分started/进行中与完成：现_rvc_prepare_done在调用前就True，不能当完成。新增inflight状态，prepare进入后try/finally发布实际完成；返回/普通异常/KBI都保持就绪准确，未实际开始的prepare不伪完成。已有sticky失败缓存保持，本切片不做失败重试重置。
2. App readiness：Cosy仍要求真实本地准备；Edge仅在资源配置要求RVC预热且真实引擎声明上述状态时，准备未完成不能判ready。完成但RVC失败按旧fallback配置允许原播报，但结果携带明确警告Nahida未就绪，不能声称目标音色成功。无RVC/未要求预热/无新状态API的注入legacy引擎保留行为，禁止Mock特判或读取任意对象私有flag。
3. 通用foreground选择条件扩至配置/已绑定Edge+RVC且要求预热。复用已有job/creator/unknown-start/generation/lease/预算，不重写线程框架或新建第二种loader。保留旧cosy_foreground_path兼容含义，内部可用更准确新helper，避免把函数名含义悄改给旧调用者。所有ready_fn/最终result转换同一状态规则，不能pending时因“已有Edge对象”返回ok。
4. assembly对于真实配置Edge+RVC不做RVC重预热；可只启动原Ollama后台预热不等待，App request再串行等待Ollama→RVC。不改纯文本/无RVC旧模式副作用，不把保留的sync ensure入口改后台。App/voice_load_lane旧Edge early return在存在未完成声明准备时必须进入同一bound prepare事务；不能重复构造已绑定TTS/STT，shutdown期间不发布，旧owner未quiet不启动其它重资源。
5. warmup_registered_tts_candidate的Edge路径按准备状态完成结果/警告；Ollama超时跳过RVC导致未开始时不能ok。原fallback失败完成与仍在准备明确区分。警告固定元数据，不持久记录路径/正文。现有强关闭六字段与TFM gate不放宽。

## 必要行为验证

固定高内存facts+Event fake：真实Edge/RVC配置首次request wait_sec短预算返回pending、后台仅一次准备；已有Edge对象不能提前ok；冷加载时text仍可用；语音模式assembly零RVC模型预热，App请求后串行Ollama→RVC；无RVC路径不新增线程。RVC失败已完成返回旧fallback语义且明确警告；Ollama仍busy跳过RVC不能ready；重复请求/关闭/未知OS start/晚返回依旧阻TFM与新分配。对实际TTSEngine.prepare状态与App接线穿透，不只stub ready=True。用fake同步ensure/CLI回归，原Cosy前台156级合同保持。标准Root scoped gate限时，真实线程全部join，WAV零差，无新timer。完成STOP待Root源码+独立验证，未真实测不承诺秒数。

下一独立切片才处理`tts`对失败RVC的显式重试：当前ready Edge会直接return成功，已有失败cache不重试。不得在本切片简单clear flag自动重启，必须实际旧worker quiet/同policy/无活跃转换后有界显式重试，且仍用同前台job。
