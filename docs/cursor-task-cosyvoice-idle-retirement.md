# B真实smoke：idle退出窗口不能当ready

跨Python修复与owner竞态已独立复验通过。最新默认30s真实smoke：cold成功19.672秒，warm成功2.516秒；idle_timeout诊断值2秒，sleep3秒后process_alive仍true（正在teardown），after_idle94ms失败；最终shutdown/TFM全部回收。`tmp/codex_cosy_runtime_smoke.json`保存证据。

根因：client._ensure_worker_started只用_ready+poll判就绪，不知道worker已到idle截止、主loop已退出；Python/模型释放阶段proc仍活，stdin无消费者。不能只改probe多睡几秒来掩盖生产同一窗口。

设计最小生命周期修复：父记录此worker最后确认ready/合成完成的monotonic活动时间；在新显式请求派发前，若已到/接近本worker idle退休时间，先有界回收旧owner（terminate/kill/wait/join仍同deadline），确认proc+reader+writer全部退出才cold-start。reader已EOF/停止也不能_ready直接复用。idle判定可保守略早（固定小guard，不能越过正在执行请求），避免时钟/协议传输小偏差；warm连续请求应继续复用，无每次重启。父的活动元数据按worker代际重置。

只在新的显式请求提交文本前处理已退休worker，禁止对已经可能执行的合成静默重试、禁止fallback、禁止两个模型并存。保持stop_failed/registry引用保留、取消/epoch/closing语义，不能为idle清掉未确认退出句柄。所有新等待含取消/关闭和总预算，不持全局请求锁长等。

建议同时检查`reader.is_alive`，以及已排队的正常退出通知（若选事件方案须严格版本/限长/语义测试，避免不必要扩协议）。优先父侧已有生命周期与idle设置，不启新Timer/每请求线程，不改vendor、权限、默认timeout或默认RVC/config。

测试：idle2秒后旧proc仍alive但已不消费stdin，父不向旧worker发送合成；完整退出后才spawn第二，第二成功；连续warm不重启；stop失败不spawn且保留owner；reader EOF不当ready；取消/close发生于退休等待有界返回。用事件/可注入时钟，不依靠大量sleep。测试TFM/no orphan清理保持。

Cursor仅client/相关窄helper与fake测试/文档，主Python312离线90s专项，交接后停Codex评审；不跑真实模型/设备/记忆，不进入C/提交推送。Codex复验后重跑cold/warm/idle真实smoke。不要改变probe的3秒等待作为唯一修复。
