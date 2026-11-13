# 最终性能里程碑：代码与离线资源边界通过

2026-11-04。用户本轮范围是：关闭退出/回收与测试可靠性缺口，完成真实生产路径的等待优化，在不破坏行为的前提下适当精简，然后检查等待、低配资源保护和泄漏风险。30–60分钟真实语音长测另行进行。

实施由官方Cursor Agent CLI完成，Codex负责设计、每轮源码/调用方review和独立验证。前置Job/IO lease/未发布候选、预取、队列与线程owner闭环依据见同日具名review和docs/codex-review-resource-milestone-2026-11-03.md；本文件是其最新补充，旧文档中的相关“未实现”不再是当前状态。没有提交或推送，保留既有脏工作区；正式data/memory未访问。

## 当前结论

**本轮剩余代码、保行为整理及离线边界验证完成。** 未发现需要继续返修的确定性阻断问题。真实低内存拒绝已验证；**真实生成/音频短测与12B+RVC+Cosy共存仍未通过**，不能据此声称全程序在所有硬件上低延迟、绝不卡顿或长期无泄漏。

真实短测未启动原因：本轮最后短探针读到物理可用1542MiB、提交余量3368MiB，低于最基本的本地模型安全余量，更低于冷载三模型要求。只进行了只读采样和实际拒绝路径，没有模型HTTP或设备操作。不得通过放宽阈值、停止用户服务、改系统设置或偷偷换模型来使测试变绿。

## 跨模块资源复审

| 边界 | 源码核对的实际约束 | 验证与限制 |
|---|---|---|
| RVC实际进程与IO | Native Job模型进程树、启动raw/模型解释器及reader/stdin/pin共同参与actualquiet；失败保留owner，单次run同policy单槽 | 前置具名review及真实单RVC退出证据保留，不以原始启动器退出冒充整个树结束 |
| 语音加载 | Core ensure→voice_load_lane→voice_activation→prepare_local_voice生产接线；单加载lane/候选，关闭取消，不重复起loader | 原独立矩阵和本轮73项装配/关闭回归；不再重复开发已接线协调器 |
| 对话播报 | 统一有界TurnSpeechPlayer，默认队列8、轮状态最多128；非流回复也复用单consumer | 原300请求burst与最终74相关回归；没有逐回复daemon Thread入口 |
| 跨句准备 | Edge/Cosy共用单producer，最多1个future（preparing/queued共用credit）；Edge最多6路径、Cosy最多4 WAV；当前播放释放credit后才向前取 | 独立19及相关回归已通过；pipeline_enabled=False保留串行路径，未替用户切配置 |
| 临时文件 | owned默认128、硬上限1024；ready默认64、硬上限1024；唯一清理worker，IO lease最多8，每lease最多2路径 | TFM deadline独立8、新/边界43、相关131及原所有权9/1/5分别通过；不能删除pin中或身份已变文件 |
| deadline调度 | pending保存绝对due与重试责任；只把到期任务放ready，OS锁失败改下一due；不会让长delay/锁失败独占worker等待 | 明确delay不提前执行，重复请求不重置首个deadline；耗尽后保留责任，只允许显式重试刷新预算 |
| LLM内存准入 | 默认assembly共享guard覆盖主client chat/stream/warm和独立memory transport；实际sealed endpoint；每次驻留重查，不缓存许可 | Root12、默认实际类接线3、新仓库21及相关47/73/65分别通过；库级guard=None兼容不代表独立诊断自动受保护 |
| 准入等待与容量 | 每guard最多1个metadata owner，前台min(2s,调用timeout)；GET connect/read各0.5s、body64KiB/512行；超时保留真实owner，不起替代线程，不晚发POST | 实际join而非terminal冒充完成；关闭严格True，Core失败可重试；无新全局线程池/定时器/无界map |
| 主机与CPU | RVC/Cosy启动前RAM+commit保护；LLM冷热保守估算；RVC BLAS/Torch计算线程max(1,min(4,CPU//2))/interop1，Cosy子环境上限4 | RVC不修改父环境，大小写变体不能绕预算；保护不是内存预留，不保证所有低配都能跑现有12B |
| 长期集合/维护 | 完整轮会话历史默认500轮；SessionTimer默认64记录；记忆摄入队列硬上限256；既有日志轮转、SQLite分批维护/前台互斥与FileCleaner扫描预算保留 | 本轮记忆/网络65项回归；未对用户正式记忆执行维护或删除；不以磁盘清理直接等同释放RAM |

关闭普通错误继续其它独立步骤；未回收资源如实标记并保留重试，speech未结束时不关闭其依赖。底层OS删除或第三方阻塞可能不能立即中断，保留所有权和实际状态，不用daemon属性作回收证据。

## 最后一轮保行为整理

官方Cursor会话dea96dad-162d-48fb-b3ea-ca9cf6f0c3bd已STOP。Root与tmp/final-clarity-root-before-20261104逐行比对：仅移除guard冗余_require_active（已由非阻塞_require_lock完整覆盖）、提取器无作用的except BaseException: raise，并纠正协调器已接线与关闭可重试的旧注释。没有改变HTTP/schema/估算/异常映射/关闭实现或权限。

其余精简已分别验收：五包lazy导出共用helper；预取控制流700→668行、engine减3行；TFM删除4个零调用私有遗留入口。新增生命周期与准入代码承担真实资源边界，不为行数删除保护或建立更大的框架。项目并未承诺总行数减少。

## 独立验证证据

所有测试只通过唯一监督器、唯一label顺序运行；失败报告保留，禁止复用覆盖。下列各组均exit0、未超时、共享WAV精确集合826→826、无增删。分组有重叠，**不是最终一次全仓测试总数**。

| label | 结果 |
|---|---|
| tfm_deadline_root8_final_20261104e | Root独立deadline8项通过 |
| tfm_deadline_root_repo_final_20261104e | deadline/bounds/pin-wake43项通过 |
| tfm_deadline_root_related_20261104d | 持久IO、预取、回滚、Cosy等131项通过 |
| model_admission_root12_final_20261104d | 内存准入Root12通过 |
| model_admission_root_repo_20261104d | 新仓库21通过 |
| model_admission_root_integration3_20261104f | 默认实际类接线、回滚、unknown-start3通过 |
| model_admission_root_clients_final_20261104g | 原生成/流/历史/system47通过 |
| model_admission_root_assembly_20261104g | 装配/Core/关闭73通过 |
| model_admission_root_memory_net_20261104h | 记忆与网络65通过 |
| perf_clarity_root12_final_20261104b | 最终整理后Root12再次通过 |
| perf_clarity_repo_final_20261104c | 最终整理后准入/记忆/Core88通过 |
| perf_milestone_root3_final_20261104a | 最终资源短循环3项通过，细节如下 |

Root最终短循环（tmp/codex_performance_milestone_root_2026_11_04.py）：

- 200次fake聊天/记忆交替POST：200次metadata GET全部响应close；同时metadata线程最多1，最后真实线程退出；GC后guard/OwnedThreadTask/Core对象均0。会话只保留设定16完整轮，测试不会读取实际对话或写数据库。
- 25次默认实际类装配→关闭：fake Store/摄入/网络，未构造音频设备；GC后25个Core weakrefs均释放，guard和owner数量回到0；TFM实际complete、owned0、worker dead；无GET/POST/数据库文件。
- 实际主机低内存拒绝：当时1542/3368MiB，约146.2ms返回RESOURCE_ERROR；零GET/POST、零metadata线程、零历史提交/模型touch。这是实际拒绝路径测试，**不是真实模型生成或语音时延**。

200次fake循环进程采样：线程1→1、子进程0→0；RSS40898560→41078784字节（约增加176KiB）。短样本存在分配器/缓存变化，不能外推为长期零泄漏；更有力的本次结论是owner实际退出与相关对象GC归零。

最后静态检查：src/scripts/tests共217个Python文件AST无错误；git diff --check exit0（仅23行换行提示）；选定Nahida样本仍382444bytes，SHA256=7C4AEE8B69428E4BBE3BB773CFC47EFAE1618EFB46B7B80EBAF1465739FE19BA。没有改选定音色或默认模型。静态检查不等于音频链路验收。

## 真实验证仍待资源条件

1. 用户释放足够物理内存与提交余量后，重新只读采样并按现有准入阈值决定是否放行。只使用已有受监督固定文本诊断、独占测试端口和owned Job，必须显式注入本次LLM guard并实际close，避免低层诊断旁路。不能直接运行旧裸客户端smoke。
2. 在安全条件下验证冷/热短句、连续多句、打断、重复启动关闭和实际模型树/临时文件回收；固定Nahida样本对照。记录阶段耗时、CPU/RAM/commit/显存峰值；未通过就保留失败，不只延长超时。
3. 30–60分钟真实语音soak按用户本轮范围继续后置。此前12B+RVC+Cosy真实共存失败仍是有效限制，未被本次fake通过或快速拒绝覆盖。

没有在途官方CLI/测试/模型任务；剩余工作是上述真实运行验收，不是未实现的代码项目。不要继续追加低风险无关重构以回避真实条件不足。
