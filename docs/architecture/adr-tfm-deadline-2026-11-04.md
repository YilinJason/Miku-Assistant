# ADR：有界 pending deadline + 仅到期 ready 队列

状态：Accepted，Root源码/fixture、独立8、仓库43和相关131等通过，详见docs/codex-review-tfm-deadline-2026-11-04.md。真实语音与模型共存尚未因此通过。

## 选择

保留单worker和既有有界Queue；未来任务仅存在owned硬上限约束的pending字典，ready只装已到期任务。pending用具名小记录（identity、due_at单调绝对秒、remaining_attempts、attempts_made）替代含糊相对delay tuple，不引第二heap/map或Timer。ready可保留原私有四字段载荷以减少迁移，但worker以当前匹配identity的pending记录为权威，不重复累加delay。

登记只在新意图或已耗尽的显式重试时计算now+delay，重复请求保持首次意图和预算。即时remove在已有合法明确delay之前仍可同步删除（保留原行为）；pinned即时remove不缩短已有pending明确delay。最后pin释放只唤醒/pump到期项，不能重启完整delay。ready满时未来任务不占容量；已到期未入队项留在有界pending，下次worker进度继续pump，按due和稳定插入顺序公平取项。

每次worker处理最多一次OS删除。成功沿用原身份claim/commit；可重试锁失败在同记录设now+2*attempts_made并减少剩余次数，释放inflight/claim后让出线程；非锁OSError/致命异常/耗尽保持原失败intent与显式重试fence。IO pin不可占claim，暂不能删除仍保留intent，不忙循环。一次性不可中断OS删除无法保证其它任务即时处理，必须如实披露。

## 等待与关闭

循环clear wake→锁内pump到期项→取ready→仍空则以最近可处理due计算wait，没有候选可wait Event不限时；所有stop前后检查与通知不丢。由于外部删除claim释放的唤醒责任必须全覆盖，可保留最大0.5秒安全巡检（不能高频忙轮询）；不得在某一项delay/retry里sleep。idle扫描时逾期但pin/claim/queued阻挡的条目只跳过，须继续找最近eligible due；仅无eligible且仍有pending才退回0.5秒巡检，避免首条blocked遮挡较近future项。

保留shutdown stop+wake→锁外实际join→总deadline内精确owned清理/ready drain，未完成pending/owner/IO仍可重试。停止后不能新建worker或开始批量任务；terminal不等于actualquiet。原unknown start/creator fence和锁外OS claim不重写。

## 兼容与范围

只temp_files.py、必要相关tests/docs。公开create/adopt/remove/remove_delayed/pin/release/shutdown及容量含义保留。pending_delayed仍为ready队列数（未来任务由pending_cleanup_count可见）。私有pending载荷和等待钩子可迁移，所有原行为断言保留，Root独立探针只能Root适配。不要顺便改未知启动实现/权限/其它语音模块。

公开remove的历史参数目前实际采用固定前台/退避策略，本切片不改该兼容事实。remove_delayed的有效有限非负delay、严格非负int重试预算按至少1次执行；原对bool/NaN/inf/负值的非明确定义行为作为配置健壮性修复在副作用前拒绝，不引额外上限或新配置。所有正常调用点与既有测试数值保持。

## 验收

必须独立证明future不挡due0、锁退避让出worker、pin/明确绝对deadline、重复预算不重置、容量1与owned上限下公平且无过期增长、旧身份任务不撤新intent、异常原样/失败可见、stop-before-clear/总deadline/unknown启动与实际退出。Cursor先源+tests+局部compile/diffcheck后STOP，Root审fixture再运行受监督gate与独立probe。真实模型设备/正式memory不触碰，真实语音性能不从文件测试推断。
