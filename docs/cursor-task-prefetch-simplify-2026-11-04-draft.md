# 已验收预取控制流精简（2026-11-04 已放行）

用户要求保持性能/功能的适当精简。本切片不增加功能或改取消/关闭语义。Root设计和独立review、Cursor生产与tests。读AGENTS四repo skills及安全规则，保留巨大dirty，禁git回退/正式memory/真实模型设备下载清理/提交推送。

## 范围

仅src/voice/tts/turn_stream_prefetch.py、engine.py的确实未用import/无意义重复捕获（如except RuntimeError: raise）、必要现有测试/doc。不能改OwnedThreadTask/TFM/RVC/CozyBackend/权限/声音/模型配置。准备单线程、future1、Edge6/Cozy4路径、所有原BaseException身份/发布先后、IO pin/unknown迟返/epoch/pending门槛不变。

## 已确认可删重复

- _owner_start_attempted只写无读，可删除槽/初始化/写入，但owner.join必须始终实际执行，不能重新加入ack猜quiet。
- _producer_cancelled无调用，可删；保留实际使用的统一只读取消view。
- _cleanup_blocked的唯一判断是`flag and held_paths`，后面仍返回`len(held_paths)==0`，其结果完全由held集合已有信息决定；可移除该冗余flag及赋值，保持TFM pending/cache门槛和is_owned复核。
- producer已存在整圈outer except/finally，内部对next_work/prepare/track重复try→fail→return可以归一到该outer处理。保持主错误优先、credit释放、EOF在错误发布后、late item清理与actual join状态不变，不删第二次取消检查来凑行数。
- 可适度去除consumer/串行分支中“存错误→层层return→外层再raise”的重复传递；保持同一异常对象和finally资源责任，不把quiet当成功。不要引入更大策略/工厂框架，也不改EOF协议或底层线程实现。

先rg确认所有私有项确实无外部调用，保留必要注释。报告净行数和删去的冗余责任，而非机械压缩格式。只做能由现有强边界基线证明的精简，不能为行数删安全检查。

## 验证

主Python312+scoped gate唯一label前台顺序<=120秒：新预取、OwnedThread、SpeechQueue/stream/voiceSession及音频回滚/C1必要组；禁止raw/discover/并发、运行或修改Rootprobe/监督器。已有Root19完整保行为矩阵由Root独立复跑，源码AST对切片前快照确认边界；错误普通/KBI/Exit、播放失败late edge、unknown、pin、cancel均不得变绿假证明。无需新增镜像实现测试；有缺口先说明。compile/diffcheck与简短ADR/交接后STOP，不提前进入最终性能。
