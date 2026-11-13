# 本轮性能收口与恢复依据

2026-11-04，Root设计/官方Cursor实施/Root独立评审的闭环记录；主额度<=10%时停止，周额度忽略，不自动启用reset credits。各切片均在官方CLI停止后独立测试。**本轮剩余代码、精简与离线验收已完成**；最终依据docs/codex-review-performance-milestone-2026-11-04.md。未提交/推送，正式data/memory未访问，未启动真实模型/音频设备或长测；只完成真实主机低内存拒绝短测。

## 已闭环

| 切片 | Root证据 | 作用 |
|---|---|---|
| 惰性导出去重 | 独立3+仓库17 | 五包重复解析集中，保留lazy/cache/error行为 |
| 预取保行为精简 | 独立19+相关146 | 删冗余状态/未用方法/重复捕获，预取减32行、engine减3行 |
| 非流回复有界播报 | 独立5+相关162/最终74（重叠） | 300连续请求单consumer，不再逐回复Thread，启动失败无同步兜底；fatal原样与关闭重试 |
| RVC CPU预算 | 独立6+新仓库13+所有权66+最终安全生命周期60（重叠） | 三路径固定1..4计算线程/interop1，BLAS只注入child env，父env不变且Windows大小写不可绕过 |
| Cosy retained被动对账 | 独立5+相关107/最终14（重叠） | 已回收输出不再永久busy，revision保护并发新增，失败retire/stop_failed仍保留 |
| TFM解pin等待 | 独立5+相关78+原所有权9/旧身份1 | 新即时remove解pin后不额外等3秒，Event及时处理与停止通知竞态修复，明确delay和锁退避不变 |
| TFM完整deadline调度 | 独立8+deadline/bounds/pin43+相关131，原9/1/5 | 绝对due、到期ready、OS锁退避不占worker，长delay不再队头阻塞，容量/身份/耗尽责任保持 |
| LLM冷热内存准入 | 独立12+实际类接线3+新仓库21+原47/73/65；最后整理复验12/88 | 默认主聊天/流/预热及记忆transport共享单guard；RAM/commit与严格有界metadata、前台2秒上限、实际quiet关闭 |

各gate共享音频精确集合均826→826、无增删。每项具名codex-review与ADR是详细证据，不能把有重叠测试相加成全仓单次通过。TFM单个独占目录真实删除样本2.217ms，不代表端到端语音耗时。

## 本轮资源里程碑判断

语音准备仍单线程+future1，Edge最多6临时路径/Cosy最多4 WAV，TFM owned上限及唯一清理worker、IO lease、actualquiet和失败重试所有权保留。非流旧线程堆积入口已移除；新helper均stdlib导入且不在import打开设备/加载模型。CPU预算不改全局环境、Ollama服务、模型/音色/权限。选择样本01_warm_companion_rvc.wav仍382444bytes，SHA256=7C4AEE8B69428E4BBE3BB773CFC47EFAE1618EFB46B7B80EBAF1465739FE19BA。

代码及离线边界最终通过；最后Root3矩阵还验证200次fake交替请求和25次实际类装配/关闭后对象回收、线程1→1、子进程0→0、所有metadata响应close。217Python AST及diffcheck通过，样本hash未变。但**全程序所有低配/资源争抢/长期无泄漏不能承诺**：12B+RVC+Cosy真实共存此前失败；静态和fake回收不证明真实长时表现。

## 仍待真实运行验证

1. 当前主机只剩约1.5GiB可用物理内存、3.4GiB提交余量，真实拒绝约146ms、零GET/POST已验证。资源不足时禁止加载模型，不通过改阈值、结束用户服务或偷换模型绕开。
2. 资源释放并重新采样达到准入后，做受控固定文本真实短测：冷/热、连续多句、打断、退出与重复重启，记录阶段耗时及RAM/commit/显存峰值。旧裸客户端诊断需显式注入并回收LLM guard后才可运行。
3. 默认12B和选定Nahida音色保留，之前共存失败仍有效；30–60分钟真实语音soak按约后置。

不存在待实现的上述代码切片，也没有在途CLI或测试。继续时先读docs/current-delivery-status-2026-11-04.md和最终里程碑；docs/performance-followup-design-2026-11-04.md为已实现设计历史，不能再当待放行任务。保留工作区，不回滚现有修改。
