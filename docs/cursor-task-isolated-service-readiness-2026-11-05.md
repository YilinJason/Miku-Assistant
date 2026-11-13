# 隔离诊断服务API就绪门（仅tmp监督器，已放行）

Root两次元数据-only真实实验已确认：刚绑定11445后首个/api/ps超时（先默认、再改直连优先，两者都在第一个PS约2秒超时），随后ps/tags都立即200且models为合法list。不是模型加载或内存耗尽证据，是诊断只等TCP端口开、不等API就绪造成guard早拒绝。原三模型失败memleak_three_ext_nov5d与两个元数据报告全部保留，不得覆盖。

唯一允许文件tmp/codex_real_voice_supervision.py。保持生产代码、ModelMemoryAdmission前台2秒/每GET0.5秒、父总180秒/服务ready原15秒、所有资源线与owned回收不变。你不改Root fake/probe/gate或harness，不运行test/模型/网络，只AST后STOP。

设计：
- 新增固定loopback `isolated_metadata_ready(timeout_sec)` helper，只GET固定127.0.0.1:11445/api/ps，不加载模型，不使用代理/注册表/DNS主机名/任意URL。复用已有socket模块，通过HTTP/1.0请求并Connection: close；只读取最多2048字节直到首个CRLF，验证HTTP/1.x状态行精确200，不跟redirect，不需解析body。所有socket路径context/finally关闭。
- helper绝对wall预算min(0.5秒,传入剩余ready预算)：在connect/send/每次recv前用monotonic剩余设置timeout，循环有总截止与2KiB上限，不以每次read timeout冒充总预算，不增线程/进程。异常/超时/EOF/非法状态返回False，fatal自然传播。
- Supervisor构造添加显式DI `metadata_ready_fn: Callable[[float], bool] | None = None`，默认上述helper。Root会自行给全部fake Supervisor实例传入lambda timeout:True，避免离线测试误触真实网络；禁止根据fake/Mock/port_open_fn是否存在做生产特判。
- _start_isolated_server仍原15秒ready cap，只有port_open且metadata_ready(剩余budget) is True，且仍未超过总/ready deadline、owned server仍活着才允许返回/启动child；probe失败继续小间隔等待直至原cap，失败仍走已有总finally回收。不要将TCP变通成默认跳过或延长就绪预算。
- 写一个metadata-ready阶段到现有stage记录说明条件即可；不改报告label/任意argv通道/预检与模型策略。不更改已通过的extended_cold flag。

Root将复审diff、显式迁移fake DI并新增状态行/慢响应deadline单测，重跑监督器24与Root7，再只读metadata-only确认API就绪；最后才同条件重跑完整三模型短测试。没有这些通过前不要声称模型共存成功。
