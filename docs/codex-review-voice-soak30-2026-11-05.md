# 30分钟App流式链路诊断：Root放行运行，结果待结束

官方Cursor会话25a13086-76c7-43b9-8174-5690b1f0c3c4已STOP，仅4个tmp诊断文件；没有生产源/config/vendor或正式memory改动。Root发现并集中返修了模块global编译错误、Timer丢owner/假quiet、DI App不拥有引擎而跳过显式engine.close、RAM不可观测未拒绝、非pristine state忙等及音频验证门槛。最终真正compile(source, path, exec)通过，非只AST。

放行范围：固定--isolated-ollama --soak-30，1800秒steady、15秒cadence、最多120 turn，末120秒不启新turn，至少60成功；父2010秒、回收10秒，420个保存样本但每5秒始终采样和判定资源门槛，普通诊断仍180秒/36记录。固定自有prefix.stop能触发owned finally，无任意URL/路径/额外权限。App真实stream dispatch/consumer/prefetch→Cosy→RVC→capture，固定合成输入，num_predict32仅诊断，生产模型参数不变。

Root实际证据：

- soak30_root8_20261105ad：8项通过，假时钟真实走满1800、慢turn不能虚报足量、fatal原对象与部分row、严格ok、flag/固定预算、40次后低commit仍中止、stop文件、RAM未知及采样异常均owned回收。
- soak30_harness_root10_20261105ae：10项通过，保留原RVC/故障/首段边界，新增实际90秒Timer启动后App fake抛KeyboardInterrupt，实际cancel/join后线程死亡，App先关、调用方持有engine独立关闭，guard/warmup/TFM仍完成、原fatal对象不丢。
- soak30_diag24_20261105af：既有监督器24项通过。各gate WAV826→826无增删。

真实运行label=soak30_app_nov5ag，Root已启动；父PID47724、隔离launcher3032、child23568。入口报告tmp/codex_real_soak30_app_nov5ag.json；进行中为.live.json，必要停止文件同prefix.stop。只有最终child/server/App/Timer/aggregate/TFM/warmup/guard实际quiet与steady/turn数同时满足才能写PASS。

范围限制：不录麦克风、不打开扬声器，仅前6个WAV留存，其余每个真实owned音频仍验证再清理；不保存模型回复正文。此测试不包括STT、声卡/驱动与真人听感，也不能证明全设备永久不泄漏。primary<=10%则Root写唯一自有停止文件并观察回收后暂停，不自动reset。

首尝试soak30_app_nov5ag于48.91秒失败，steady首请求15秒超时、0turn/0音频；隔离服务日志显示1576-token新prompt prefill被取消、未见OOM。所有App/Timer/engine/TFM/warmup/guard与parent owned树完整回收。Root诊断把首次实际生成强制压成15秒与生产60秒不一致，现仅首轮保留构造时生产配置原timeout，稳态仍15；不增加生产等待上限。原失败报告保留，不能计作30分钟通过。

最终本轮状态：soak30_app_cold_nov5ai在实际约6分钟、最近23轮时primary剩10%，Root自动写自有stop文件。父exit124/elapsed384.25/abort operator_stop，child/server/cleanup均True。不是30分钟通过。进程被owned监督回收，取消后的临时WAV尚未核对，不能据此宣称App/TFM graceful finally全部执行；下一轮先收口此边界。完整恢复入口docs/resume-remaining-voice-work-2026-11-05.md。
