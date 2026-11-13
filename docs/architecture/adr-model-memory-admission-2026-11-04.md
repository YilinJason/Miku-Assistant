# ADR：应用级本地模型内存准入

状态：Accepted（2026-11-04）。官方Cursor实施后，Root已独立审查最终源码、默认装配与关闭调用方，并完成下述离线验证。前置TFM deadline已Root最终PASS。

验收证据：独立边界12项、新仓库21项、默认真实类接线/回滚/未知启动3项，以及原客户端47项、装配与关闭73项、记忆与网络65项分别通过；分组有重叠，不是全仓单次测试总数。所有gate均WAV826→826、无增删。资源采样为fake；没有启动真实模型/设备或访问正式data/memory。真实短测和跨模块资源里程碑另行记录。

## 范围与估算

默认assembly为本应用创建一个共享LocalModelMemoryAdmission（新窄module），注入OllamaClient及MemoryRuntime→OllamaMemoryExtractor→RequestsOllamaChatTransport。构造/导入零网络/线程/设备。低层可注入客户端默认guard=None保持库和测试兼容；默认应用入口始终注入，不给生产新增关闭开关。自定义DI工厂只在接受该kw时传参；不以测试注入绕开默认装配证明。diagnostic若直接构造低层客户端需显式注入并回收，文档说明保护边界，不盲改所有诊断脚本。

仅Windows loopback按本机RAM/commit判断；远端（已获原权限）与非Windows不额外本机检测。guard必须重算/校验sealed endpoint，伪造loopback kind不能让外部metadata GET。固定allowlist新增/api/ps，其它path/redirect仍拒绝。元数据来源为[官方ps](https://docs.ollama.com/api/ps)与[官方tags](https://docs.ollama.com/api/tags)，只GET不加载模型；不发show/pull/unload或启动进程。

每次准备模型POST前重新读RAM/commit和ps，不缓存驻留许可；无缓存/无无界map。model名称仅做末路径分量缺tag时补:latest，保持大小写，不能拿其它model/digest当许可。匹配ps的合法正size视为本次驻留采样；若无匹配则GET tags取同model严格正int文件size。models形状错/size bool负数/未知model/GET失败或不可观测一律拒绝，不发模型POST。响应close，body最大64KiB，列表/schema有界；不记录对话/完整URL/记忆。

保守工程估算（不是物理保证）：驻留保留2048MiB可用RAM与4096MiB提交余量；冷载RAM需求ceil(1.5*文件bytes/MiB)+2048，commit需求ceil(2*文件bytes/MiB)+2048。整数计算，不把文件size直接当峰值；快照数据非法拒绝。低于最低floor可零GET快速拒绝；metadata后重采样再决策。模型上下文/共享服务与第三方程序会影响真实峰值，guard不等于预留，不承诺所有低配都能加载12B。

## 等待/所有权

仅靠requests timeout不能给slow trickle绝对wall deadline，因此复用已验收OwnedThreadTask：每guard最多一个metadata owner，无全局线程池。前台总预算min(2秒,调用timeout)，完成必须actualjoin+result；timeout/cancel/start异常时cancel并保留owner，不启动替代线程。后续请求若旧owner未quiet立即resource_busy；严禁因超时后台仍返回允许就晚发模型POST。metadata worker在GET间/读取中检查cancel，requests connect/read小超时（<=.5s），64KiB硬上限；front始终有界，底层不可中断时如实保留至actualquiet。CPU/网络采样不需要新子进程/环境权限。

guard用短状态锁发布owner，start/join/HTTP都在锁外；require单飞，碰busy快速明确拒绝。close置永久closing、取消owner并有限join，返回严格bool实际quiet；失败保留owner可重试。AssembledParts拥有guard，rollback注册close；Core绑定并在shutdown独立关闭（不能依赖unload_on_exit开关/llm wrapper类型），non_memory completion包含guard.actualquiet，失败不假完成不阻其它独立回收。不存在guard的旧DI/手工__new__ fixture保守兼容。不要重写已有voice退出逻辑。

## 结果与调用

OllamaClient _request_post只对固定/chat且会加载的payload调用guard，覆盖warm_up/generate_result/聚合stream/增量stream。keep_alive=0且messages=[]的明确卸载请求不受加载准入拦截（不扩大其它请求豁免）。新增GenerateStatus.RESOURCE_ERROR，非流结构化失败/流恰一个FAILED，不提交历史，不标touched；稳定中文提示资源不足/不可确认/检测忙，可关闭其它程序或选择轻模型，不能误说连接失败。warm_up返回False且日志给稳定reason。fatal BaseException原对象传播。

memory transport在POST前调用同guard，拒绝映射MemoryExtractionTransportError(error_kind='resource')，不写入/伪造提取成功。原admission gate/用户事实契约不变。原远端策略、重定向拒绝、云权限保持，不新增通用subprocess或环境操作。

## 证明与交付

纯fake host/metadata/POST矩阵：warm/cold边界、坏size/schema、未知驻留/模型不匹配、低物理/低commit/不可观测、timeout/迟返无二次线程、closing/unknown start实际quiet、0额度卸载、远端与伪造kind、response关闭与体积上限、无历史提交/零模型POST、memory提取及defaultassembly共享和Core关闭重试。AudioPlayer构造前fake，正式memory禁止；全用fake store或独占临时DB。Cursor先源/tests+局部compile/diffcheck STOP，不运行测试；Root审fixture后独立gate与probe，之后里程碑/受控真实短测。
