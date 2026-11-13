# 隔离诊断服务就绪：Root验收通过

仅涉及tmp/codex_real_voice_supervision.py，不涉及生产启动、模型参数或资源准入门槛。原三模型尝试memleak_three_ext_nov5d在模型元数据阶段失败，不能算共存通过；两个元数据only实验表明第一个/api/ps请求超时，与是否使用环境代理无关，随后请求正常。因此修复诊断只等TCP、不等API就绪的竞态。

官方Cursor完成后Root审查并要求补齐callback返回后的绝对截止、服务实际存活、严格True与HTTP版本检查。最终会话afdafdb7-cafd-41ec-bb8d-f57f676ba102，已STOP。固定127.0.0.1:11445 GET /api/ps，最多2048字节状态行、每探测绝对预算<=0.5秒，socket所有退出路径关闭，fatal传播；总ready cap仍15秒、父监督仍180秒+退出10秒。不使用代理、注册表、任意URL、模型加载或额外线程。生产guard的前台2秒和单GET预算不变。

Root实际独立验证：

- readiness_root8_20261105k：8项通过，覆盖分段合法状态、错误版本/重定向/EOF/2KiB上限、慢滴流绝对截止、普通与fatal socket关闭、API未ready重试、返回迟到/服务死亡/非布尔真值不得宣布就绪。fake进程在finally实际quiet，句柄关闭后才删除独占目录。
- readiness_supervisor24_20261105l：原24项通过；所有fake构造明确注入metadata_ready_fn，无生产fake特判。
- readiness_harness_root7_20261105m：原Root7项通过。
- metadata_ready_actual_20261105n：真实隔离服务约3.05秒后ready，随后直连与默认会话的ps/tags四次均200，合法models列表；首个直连ps16ms。没有模型加载，owned服务回收完成。

上述各gate既有WAV826→826，无新增或删除。此结论仅放行诊断工具，不表示三模型共存或长时无泄漏已经验证；后续真实运行结果另见docs/codex-real-memory-short-test-2026-11-05.md。
