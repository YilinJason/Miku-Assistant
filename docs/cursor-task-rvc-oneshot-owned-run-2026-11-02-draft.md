# 单次 RVC infer/probe 归属（待上层传播放行后执行）

执行前提：持久 worker 与上层关闭传播均由 Codex 独立放行，并收到Codex明确下发。本文件名保留draft以免破坏引用；正式下发时按本任务执行。用户要求资源回收与鲁棒性，不能把持久worker修复扩大描述为全部RVC启动入口已经覆盖。

## 当前事实

SafetyPolicy.run_rvc_infer / run_rvc_probe 仍用 _run_fn（subprocess.run）；Windows timeout 杀redirector后可能仍有实际解释器，run的Windows错误清理还可能等不到pipe EOF。infer.py调用方捕获超时，converter随后会清理输出；正式关闭必须能判断剩余进程/IO是否真的退出。

## 建议最小协议（待正式任务收口后实施）

1. Windows两个具名入口复用已验证的owned Job helper，模型/解释器/脚本/IO路径身份验证与权限在任何分配之前。非Windows兼容；不回退通用subprocess。infer脚本和固定probe片段在重依赖前加入Job；probe代码只能由枚举固定生成，含固定repo路径与stdlib bootstrap，禁止用户代码拼接。
2. 一个SafetyPolicy只保留一个单次run owner记录（包括starting/pending，非阻塞single-flight），在Popen/IO线程启动前登记。超时/取消/BaseException以有限预算杀自有Job、确认redirector+Job+捕获线程退出；无法确认保留owner并拒绝下一次分配。关闭/重试 API 返回严格bool，不新建无限reaper，不因异常丢owner/替换原控制流异常。
3. 固定运行预算保持当前probe30s、infer现有配置90s，拒绝NaN/Inf/bool/非正数，有限清理grace显式说明，不通过改模型timeout掩盖问题。stdout/stderr捕获有硬上限（例如各64KiB尾部）且持续排空，最多2个被owner持有的daemon reader，不能无界communicate收集第三方日志；CompletedProcess的code/text/bytes/check语义保持当前实际调用兼容。任何截断只记录元信息，不写原文到日志。
4. 失败时仍可能写入的input/output必须保留TempFileManager所有权：增加小的有界IO lease/pin机制（精确已登记路径、有限owner/路径数、引用计数，不扩大白名单），remove/shutdown不能删除仍被worker使用的文件。只有进程与IO完全结束才释放lease；未知时不强清。此机制也适用于持久RVC取消未知时，避免晚写入把已删除路径重建成无人管理文件。无需新后台清理线程。
5. 引擎/App关闭需要同时聚合单次run状态；即使当前没有已发布TTS，装配/探测中的policy owner仍可被关闭重试发现，不能只检查全局persistent client。与现有loader closing/TFM guard协调，普通借用组件/共享策略的所有权不得扩大。
6. 重新限制RVC能力runner kwargs：当前sanitize只拒绝shell=True，executable/preexec_fn等不能覆盖固定解释器/bootstrap。按实际调用所需IO/timeout/encoding参数建明确边界，其他执行覆盖在零effects时拒绝。保持测试runner显式Fake DI，不从callable身份猜测。

## 验证

fake覆盖正常多次执行、超时且redirector先退/Job仍活、capture满/EOF/reader仍活、partial spawn与KeyboardInterrupt、close失败重试、busy拒绝、IO lease解除前后、caller shutdown不假成功、固定argv和override零spawn拒绝；不能只断言源码包含bootstrap。

先Codex逐项设计/审查再下发，Cursor实现后针对性一次+有界相关gate一次。最后用固定无模型脚本实际验证Py311树退出与输出捕获上限（不借密封能力执行任意脚本），再允许真实三模型共存测试。不得读取正式data/memory、运行main/音频设备、修改声学/默认引擎、提交推送。

## 具体接口与实现范围

- 建议私有 src/core/owned_rvc_run.py（stdlib + 已证明owned Job helper），由SafetyPolicy在所有权限/资产/参数验证后调用；不要改Native Job API逻辑，也不引入第三方框架。一个policy一条记录：spawn在途、proc、两个捕获reader、stop event、IO lease、最终结果；在任何effect前登记，各reader先登记后start。状态锁不跨IO/join。
- SafetyPolicy提供 `retire_rvc_runs(timeout: float = 2.0) -> bool` 和 `rvc_runs_quiescent() -> bool`。前者只停止这个policy的当前单次run，可重试；后者纯快照，不启动资源。creator尚未ack或pending IO未退不可True。并发run立即busy拒绝，不无限排队。清理失败保持record，下一次run前先回收，仍失败则零新分配。
- 捕获各最多65536字节/字符尾部，持续排空的chunk<=4096；最多2个daemon capturer归owner持有，实际join完成才算退出。普通短输出与CompletedProcess text/bytes/check语义保持；截断只提供稳定元信息，绝不写原始stderr/用户音频内容到日志。不要使用无界communicate读完整模型日志。
- TempFileManager增加显式 `pin_io_paths(paths)` 返回有限IO lease/token，最多8个owner、每owner最多2个已登记精确路径；重叠路径引用计数。lease只允许显式release，无GC自动解pin。pinned文件的remove/延迟worker/shutdown均不得删，仍保有所有权；shutdown.complete必须包含active lease==0。只有run/reader/creator完全静止才释放；启动前拒绝且无effect时立即释放。不要扩大删除白名单/registry权限，不新建reaper。
- 本轮给单次infer接lease；持久convert未知取消下的同类文件生命周期可用同一机制小范围接入，但不能重写已证明reader/Native核心。明确记录是否覆盖；没覆盖不得说完整文件生命周期已闭环。
- TTSEngine聚合状态扩展单次RVC完成旗标（可加带True兼容默认的rvc_subprocess_complete字段，Engine真实状态必须显式赋值），`complete`同时包含它。App在关共享TFM前检查/回收自己的policy run，即使当前TTS未发布；loader在途guard保留。旧注入policy无新API时按旧路径，真实policy必须纳入完成条件。接口声明检测继续stdlib静态语义，不恢复Mock特判。
- fixed probe代码/infer bootstrap必须行为测试证明重依赖前join；generic execution override kwargs（executable/preexec等）在任何Job/lease/spawn前拒绝，不能覆盖固定程序。当前真实产品调用的capture/text/encoding/errors/timeout/check兼容，不开放任意命令入口。测试runner显式fake后端；Windows不再靠run_fn假装进程树回收。

主Py312、RVC独立Py311。范围包括上述core、RVC固定入口/调用方、TFM/状态传播、针对性tests/ADR；不变更音色、模型、Ollama上下文、默认引擎、生产加载预算；不接预热/预取/candidate。运行前读 AGENTS/architecture-contracts/engineering-quality/collaboration-review。

先语义targeted（含正常执行、满捕获、超时树仍活、pending BaseException、lease/remove/关TFM、None/非法状态、busy、override拒绝）一次；再相关有界gate一次。使用已验证安全监督器，正常Windows上下文，不超过180秒，精确WAV集合前后不变。意外失败停止报告，不后台无限重试，不为fake加1秒特判/放宽生产超时。更新handoff/ADR，STOP待Codex，不覆盖Root当前状态文件，不提交推送。

尚未下发时本文件不改变源代码、不表示实现，也不授权跨过当前切片。
