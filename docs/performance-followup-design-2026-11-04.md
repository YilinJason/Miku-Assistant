# 性能收口设计依据（已实施历史）

**最新状态**：本文TFM deadline与LLM内存准入已实施并由Root验收，最终跨模块代码/离线资源里程碑也已通过。详见docs/codex-review-performance-milestone-2026-11-04.md；真实模型短测因当前资源不足未启动，真实长测按约后置。下面“需要先设计/未放行”等为实施前依据，不代表当前待办。

本文件是设计与待验证项，不是实现/验收。Root已通过的切片以具名review为准。真实30–60分钟soak仍排除，真实12B+RVC+Cosy共存仍未通过，不靠延长默认timeout或自动降音色/换模型掩盖。

## TFM完整deadline调度

解pin窄优化只消除即时remove的新请求固定3秒及空队列多半秒等待。worker取得长delay项后仍在该项等待，OS锁失败退避也会占住唯一worker，存在HoL。后续应单worker有界deadline队列：每项首次接受时单调时钟绝对due；到期前可被新更早任务/stop唤醒；失败退避变成下一due而非就地sleep。保留身份、同path新generation、pending/queued/inflight/耗尽责任及owned硬上限，不引新线程/Timer/无界堆，也不提前执行明确延迟。

实施前选择队列载荷迁移与原有Root探针适配方式，避免新堆中陈旧generation无限堆积；所有撤销路径清理deadline登记。测试至少：长delay不阻塞新due0；一个锁住文件不阻塞其它；重复schedule不延长/缩短已接受明确deadline；旧身份任务不能抹新intent；满队列保留有界pending；停机总deadline且actualquiet；公平性与无busyloop。此项不是简单缩短sleep，不能混入pin窄修。

## LLM冷载的主机内存保护

现有host_memory.py只保护RVC/Cosy启动，Ollama可以在voice之后首次/再次冷载12B。生产HTTP入口不止OllamaClient：还包含memory.ollama_extractor的post_chat；不能只保护对话而留下记忆提取触发冷载。必须复用同一受封存endpoint与资源策略，远端endpoint不以本机RAM作错误判定。

官方只读metadata：[/api/ps](https://docs.ollama.com/api/ps)给当前运行model、运行size/size_vram/context_length；[/api/tags](https://docs.ollama.com/api/tags)给本地model与文件size/digest。文件size不能等价当作运行峰值；resident只是采样、共享服务可能随后卸载，准入不是内存预留，也不能保证第三方进程不再抢占。

需要先决定并写ADR：冷载保守RAM/commit估算（含上下文与模型额外开销），常驻最低余量，元数据不可观测时的拒绝/提示，固定小超时与整个请求budget，缓存只可缓存同model/digest大小且有界，不能缓存“常驻许可”无限放行。不得默默换小模型、卸载/停止他人Ollama、新开任意进程或nvidia-smi权限、下载模型；可明确提示选轻量模型/关闭其它程序/串行使用。

所有metadata请求禁redirect/使用sealed URL/response finally close，不默认记录完整对话/记忆/路径。warm_up、generate_result、generate_stream和memory提取都要覆盖，keep_alive=0卸载不应被“加载准入”阻断。构造/导入不能联网；离线测试需显式fake资源与metadata，现有fake POST不应偷偷连真实端口。模型首次touch、历史提交、取消和错误结构保持。

实现前先针对注入接口做最小契约设计，不把所有HTTP重构成大框架；资源估算无法物理证明的限制如实保留。随后用低RAM/低commit/未知metadata/无驻留/已驻留/模型身份不匹配/取消/重定向/时间预算/记忆提取矩阵证明“拒绝时零模型POST、零历史与文件副作用”，然后再考虑受控真实短测。

## 里程碑

完成上述切片后对生产依赖/异常/取消/actualquiet/无界集合及CPU预算再做整体review；核对selected Nahida样本hash和默认参数未变化。性能结论拆成离线边界、真实短测、长测，三层不互相代替。优先使用已有诊断监督器，不直接启动模型、音频设备、清理或测试全仓。记录低配支持的实际模型与RAM条件，不承诺任何硬件绝不卡顿或绝无泄漏。
