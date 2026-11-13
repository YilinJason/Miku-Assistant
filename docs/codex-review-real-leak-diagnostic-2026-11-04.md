# 真实短诊断：Root已完成放行验证（2026-11-05）

此前初稿的假共存、失败不回收和fatal丢失报告问题已返修。Root逐行复核最终三个诊断文件，独立real_leak_harness_root6_final_20261105d六项通过；扩展显式冷启动flag后Root7通过，既有监督器24和资源采样fake3通过。真实RVC三生命周期30次转换和双模型六句已实际执行，详细结果与失败保留见docs/codex-real-memory-short-test-2026-11-05.md。API就绪竞态另已Root8/24/7与真实元数据验证通过，见docs/codex-review-isolated-service-readiness-2026-11-05.md。

本文件以下保留当时初稿发现，作为修复历史，不再代表当前未放行状态。通过短诊断不等于长期无泄漏或生产冷启动达标。

# 真实短诊断初稿：Root未放行运行

## Root fake实际执行：尚余两个中断缺口

real_leak_harness_root6_review_20261104b：6项中4通过（正常3×10、prepare失败、convert失败、真实self句柄ABI/None）；RVC转换KeyboardInterrupt后cycle未进report、smoke engine.shutdown抛KeyboardInterrupt后没有调用LLM/guard close，两项真实失败。全fake、WAV826零差，未启动模型。

最终修复按结构统一而非特殊异常名判断：
- RVC每cycle一开始就把可变record（states/samples列表）加入总cycles，finally始终更新部分状态；顶层捕获BaseException存原对象，finally写UUID失败报告后原对象重抛。不要依赖run_cycle正常return后才append，否则fatal丢记录。普通受控失败仍退出非零且不启下一cycle。只保留一处engine.shutdown至多3次与manager回收的finally，删除正常路径再shutdown3次的重复块，不把最多3次变成6次。资源未知/engine存在但无status不能算quiet。
- smoke独立清理步骤也必须在任一步BaseException后继续其它必要close；记录step/type，第一个已有fatal_error原对象优先，不吞KeyboardInterrupt/GeneratorExit/custom。最后report后才raise保存的原对象。engine存在但shutdown=None时closed必须False；仅engine根本没构造才可视无资源。TFM仍服从真实engine/RVC quiet门槛。
- source只允许这两harness，Sampler/Rootprobe/监督器/生产全部冻结；禁止执行任何test/model/device。局部AST后STOP，Root重跑同6矩阵；两项都通过才放行真实短测。

官方Cursor会话05c4d612-a80d-4472-8f23-ef0e8dffc76c已STOP，Root已读三个最终文件。采样器ABI/不可观测None/复用现有process句柄符合范围，冻结。下列两个harness问题必须统一修复再STOP；仍禁止执行任何测试/模型/设备。

1. **不得改变共存条件**：smoke新增`llm_keep_alive=0 if isolated_ollama`会使暖机后立即卸载12B，再准备RVC/Cosy，得到假的三模型共存通过。恢复原cfg keep_alive，删除isolated_ollama_keep_alive_zero字段；不更改任何模型/语音/超时参数。在协调prepare成功后只读确认同model确实仍驻留，记录并断言；准备失败不伪造共存结果。显式keep_alive=0只在全部使用结束后对自有隔离测试模型发出。
2. **RVC失败也必须finally回收**：当前run_cycle finally只有engine=None，convert/prepare/sampler/删除失败都跳过shutdown并丢引用。改为每cycle真正try/finally独立回收engine(原最多3次)+manager(仅实际进程/IO quiet才清理)，普通回收错误记录不阻其它安全步骤，原异常不能被覆盖；失败不得进入下cycle。每个cycle的部分记录预先纳入总report，顶层finally exclusive UUID写出完整/失败状态，不论prepare失败、convert None还是cleanup不完整，不能只有成功才落报告。引擎/TFM未实际完成不能标True或仅engine=None。
3. **输出清理未推进则停止新分配**：每次manager.remove(output)后检查仍owned/pending/active leases/inflight，若未回收就停止转换进入同cycle finally，不继续下一轮分配。输入也须有finally责任，不能因中途失败遗留。固定源WAV只读/hash保持，勿直接删除/撤销owner掩盖。
4. **smoke finally独立关闭且避免晚加载**：当前engine.shutdown或unload等任一步异常会跳过llm.close_warmup/guard.close。需要独立步骤保证尽力执行并记录失败、整体不假成功；先close_warmup取消/实际quiet，再guard.close（必要有界重试warmup实际quiet），确认暖机quiet后才显式卸载隔离测试模型，避免先卸载后warmup迟返又重载。失败不吞fatal原对象，保留Root监督器收尾；capture/manager依赖仍按实际TTS/RVC quiet才清理。不要改监督器时限/资源线或调用taskkill清别的进程。

只两个tmp harness源，Sampler冻结，不改Root监督器/probe/报告/docs/生产。局部AST/compile后STOP，Root将用fake执行成功+失败路径检查，再运行只读sampler及现有监督器。不得自报真实模型通过。
