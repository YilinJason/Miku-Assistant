# 隔离诊断launcher定点（仅tmp，先fake）

用户已明确取消周额度10%停工规则，继续工作；短周期remaining<=10%仍停，不消费reset credits。三模型复测在模型启动前失败，未进入真实模型准备；不是新SDK功能失败。仅修以下tmp工具，不改生产SafetyPolicy。

1. tmp/codex_isolated_ollama_launcher_2026_11_03.py 调 safe_subprocess_run(env=...)，真实sanitize_run_kwargs拒绝覆盖env。父监督器已经通过受控spawn env固定四个Ollama变量。把launcher做import-safe main，验证继承的四项值恰为127.0.0.1:11445 / 1 / 1 / 1m，失败快退；然后safe_subprocess_run固定EXE serve但**不传env**。不能放宽Policy、改裸subprocess、接收自由argv或临时修改用户服务环境。
2. tmp/codex_real_voice_supervision.py 在server失败/child未spawn的异常路径，finally summary给exit_code0/cleanup_completeTrue，实际CLI traceback退出1。记录原运行异常标签，public_exit/summary一致非0；cleanup_complete仍单独表示回收，不代表运行成功。KBI/SystemExit保留原异常，终态报告非0。未知资源quiet仍严格。

fake用真实SafetyPolicy+fake底层run验证launcher不含env、参数/继承值固定、错env零spawn；不要只mock safe_subprocess_run返回成功。监督器fake覆盖server已退出/未ready、child未spawn，报告不能0；原16+Root6保持。用现有Root bounded gate，禁止真实Ollama/模型/设备/main/正式memory。ADR/handoff后STOP，Root验收后再唯一label复测。
