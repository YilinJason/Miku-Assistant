# ADR：默认 stream 跨句音频预取

## 状态

Accepted，Root最终源码及独立验收通过；证据见docs/codex-review-turn-audio-prefetch-2026-11-04.md顶部。

## 决策

- TurnSpeechPlayer在同一turn只调用一次TTSEngine.speak_stream。原句按Condition锁内取出、锁外yield，每轮固定永久取消Event；legacy不声明能力的适配器保留speak入口。
- 唯一OwnedThreadTask producer惰性拉取原句并计划分段，拿credit后准备；单槽ready队列。consumer取为current后放一个future credit，再播放并finally逐路径回收。没有整轮flat或请求工作队列。pipeline_enabled=False在Edge/Cosy均串行，不开producer。
- current+future正常上限：Edge6路径（4WAV+2MP3），Cosy4WAV。每个原句保留Cozy既有3200字符/32段/单段120限制；不把整轮音频或正文收集到列表。
- 成功完成原句才由消费侧按序回调，空句也按序完成。EOF与错误有独立终态；原BaseException保持，quiet不等于成功。
- creator/start未知、实际producer未退出、held/pending/retained输出任一未释放时保留原owner并拒新分配。finish/shutdown短join<=2秒，不能以daemon或target terminal推断退出；已过期或已取消调用不能清较新interrupt。epoch确认与clear同锁提交。
- Edge/Cosy prepare共同观察外部取消、Engine interrupt与session永久取消；播放失败后晚到的保存结果不再执行后续WAV/RVC。迟返输出仍由producer/TFM IO pin及Cozy retire fence保护，不强删写入中路径。
- stream/save资源忙明确失败；legacy speak保留拒绝方式。保存取消仍提供“已取消”提示。共享TFM pending采取保守门槛，可能暂时等待其它STT清理。

## 验证与边界

Root独立19及相关138/182、最后fixture68均通过（有重叠）；WAV826精确集合零差、编译/差异检查通过。fixture禁止真实设备探测，patch和临时目录覆盖实际回收。未进行真实模型/语音长测，不能据此承诺物理设备提速或长期无泄漏。默认12B模型、Nahida/style与权限不变。后续保持行为精简及资源性能工作独立验收。
