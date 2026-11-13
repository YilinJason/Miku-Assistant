# 真实入口发现RVC health timeout契约回归：CHANGES REQUIRED

独立固定短句真实诊断：原11434未服务，后使用本次独有127.0.0.1:11445临时Ollama，模型预热26.5秒成功（context8192、size_vram8360946564），RVC health立即False，未加载Cosy/生成音频，owned cleanup complete，临时服务taskkill0。根因已读源码确认：health_check._can_import_rvc_python传timeout=60；新SafetyPolicy.run_rvc_probe固定max=30，effect前拒绝被except吞成“无法导入/请重装”。不是RVC环境缺包。

请只把health IMPORT_RVC调用预算对齐已有30秒cap（不要扩大权限/政策上限、不放宽超时掩盖）；搜索所有真实probe调用预算并保持一致。新增行为测试：真实health入口经实际SafetyPolicy+显式fake Popen/Job探测可以成功，校验实际无kwargs越界、不用patch policy.run_rvc_probe直接return0掩盖契约。错误预算仍零spawn拒绝；保持安全密封/固定argv。

读取AGENTS/engineering-quality/architecture-contracts。单次ownedrun/持久reader/其他App代码不动；不真实main/模型/设备/正式memory/提交推送。Root supervisor45秒 targeted health/owned能力模块一次，交付STOP；Root再决定一次短真实验证。已有离线生命周期PASS仍为离线证据，真实入口未通过不能声称三模型完成。
