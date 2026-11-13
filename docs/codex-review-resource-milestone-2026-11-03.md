# RVC 与 App 加载/退出生命周期里程碑

**最终补充（2026-11-04）**：TFM完整deadline与LLM冷热内存准入已经Root独立通过，最后精简12/88与资源短循环3项也通过；代码及离线资源边界最终验收见docs/codex-review-performance-milestone-2026-11-04.md。下面相应“未完成”均是历史。当前主机约1.5GiB可用RAM/3.4GiB提交余量，真实快速拒绝已验证，未启动真实模型音频短测；此前三模型共存失败和后置长测限制保持，不能说全程序真实表现已最终通过。

## 2026-11-04 恢复后的里程碑补充

**最新补充：本轮精简与性能六切片Root通过**，见docs/codex-performance-progress-2026-11-04.md。legacy每回复线程、RVC CPU预算、Cosy stale retained、即时remove解pin额外3秒与stop丢wake均已关闭；下面相应待办已过期。预取路径上限/唯一worker/IO pin/身份/actualquiet保持，选定样本hash未改。**完整TFM deadline/HoL、LLM冷载保护和整体真实共存仍未完成**，因此整程序性能里程碑不算最终通过。未进行真实模型/设备/长测，未访问正式memory/提交推送。

**预取与等待板块里程碑已Root通过**：具名turn-audio-prefetch review顶部19独立+相关138/182+最终fixture68（有重叠），所有826精确集合零差。单Voice准备线程、一个future、Edge6/Cozy4路径、TFM owned硬上限/IO pin、旧轮取消及迟返回收、pending/retained拒新工作与truthful aggregate边界已复核；关闭不以daemon/启动进程/terminal冒充实际退出。没有新放行默认模型/权限/声学改动。可进入保行为精简，然后最终性能治理。

尚待最终性能板块：legacy非流式每回复线程累积、RVC CPU线程预算、LLM冷加载主机资源保护、TFM固定退避的清理延迟、Cozy retained失败后的无新分配回收推进审查、真实共存压力。协调器已有生产接线；不重复开发。以上不阻已通过的预取契约，但不得包装为整个程序性能完成、所有低配适配或真实长测通过。

最新：TFM资源治理已Root最终PASS（具名review/ADR），17独立边界、6持久IO断言、175相关回归全通过，WAV826零差。owned容量默认128/硬上限1024、pin删除意图自动推进、唯一清理线程迟启/退出证明、锁外身份claim、失败pending/耗尽/旧队列新身份等已闭环。下文“先修TFM”等为历史。允许进入预取生命周期基础→真实stream句间接线→保行为精简→最终性能治理，仍不表示真实三模型/设备/长期无泄漏已通过。

RVC显式失败重试/取消桥已Root闭环，见同日具名review。原进程树/持久IO/单次owned-run边界保留；Edge/RVC前台唯一job、失败缓存与明确显式重试、现代Ollama succeeded代际门槛、原子工作lease、永久退休fence、取消迟返强owner、Cosy就绪与STT补装配均完成源码与离线验证。最新38/85/131分组全PASS（有重叠）及前轮Root23独立边界证据，不以Cursor自报验收。无真实模型/设备新验证。

本里程碑可进入临时文件资源治理：静态确认TFM owned registry目前无硬上限、pin期间remove请求会丢弃、release不会推进删除、cleanup worker.start异常可能产生未知启动重叠。先修这些边界，再有界预取与独立精简。后续仍有Edge无界队列/600秒join、legacy线程、RVC CPU线程预算、LLM冷加载资源保护与真实三模型不足，不能把本里程碑描述为全程序性能完成。

## 真实诊断补充

- 实际RVC单独准备21.25秒；退出首轮47ms，真实Job+raw+stdout/stderr+stdin+pin全部完整，global client摘除，TTSEngine六字段aggregate.complete=True、TFM complete=True。证据 `tmp/codex_rvc_real_exit_2026_11_03.json` 与 `tmp/codex_rvc_real_exit_gate_nov3.json`，无音频设备/正式memory。
- 实际App三模型尚未通过：原11434不服务；独立临时11445已验证Ollama加载。真实health入口漏同步timeout60与policy cap30已修并Root18项独立通过（不放宽上限）。随后三模型RVC阶段98.672秒，Cosy在resource_admission阶段32.109秒超时，未达模型构造/生成音频；最少空闲RAM17944MB、GPU4474MB。对照RVC独立21.25秒，不能把共存性能当已解决。临时服务结束exit0，之前清理后GPU空闲14852MB且11445端口已关闭。
- 三模型高延迟/准入超时是下一等待/性能板块的实测失败，不能靠仅增加默认timeout宣称优化；保留30秒默认，后续允许明确标注的受控诊断配置。原固定smoke只检查local Cosy状态，不足以证明高负载下RVC aggregate完成，本次单独真实诊断已改查完整aggregate。

结论：本轮同步生命周期边界 PASS；允许后续前台等待与有界预取，不代表真实三模型/设备/长期泄漏通过。

Codex已独立复查：RVC实际模型进程Job归属、部分启动所有权、原生helper复用、reader/stdin结束、单次捕获与lease、持久convert latepin/重复stop/释放失败、全局client非阻塞发布、App单槽候选与generation lane、原子TTS/STT移动、候选失败保留、锁外回收、shutdown独立步骤及TFM门控。原危险重复分支已收敛；未改声学/默认模型/权限与30秒模型启动预算。

证据分层：单次469测试独立OK(skip1)；持久480独立OK(skip1)后，局部回归121独立OK与最后14专项/独立8边界通过；App69独立OK与Root9+6边界通过；里程碑561项101.485秒剩2 single-flight失败，定位为拿票前重复失败检查，最终仅删除三行后独立五相关模块95项1.609秒OK（包含原2失败、新App13矩阵、装配/关闭）。最后未重复未改的GPU/本地TTS测试，不能称“最终同一遍561全过”。各监督180/60秒内完成，WAV826精确集合零差。

Native Job真实Py311无模型进程树此前3/3证据保留；本轮未重写底层，尚未加载真实模型或打开设备。正式data/memory未访问，未提交推送。

后续明确未完成：Cosy前台冷启动有界反馈、实际ready失效、1段预取与取消所有权、Edge无界queue/旧daemon播报累积、文件pin释放后待删与总量上限、低配策略、保行为精简、短真实性能验证。30–60分钟真实长测按用户本轮要求不执行。以上不得包装为已完成性能或全程序无泄漏保证。
