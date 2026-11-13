## 2026-10-18 独立复审：代码及离线门禁通过，真实性能待测

Cursor 2e2836a0 修复初始化异常释放锁；feacb619 仅修复 EOF/启动进度测试替身的真实等待与显式 EOF 语义。Codex 阅读最终代码及调用方，初始化失败重入、旧别名委托独立探针通过；20 次 EOF 独立复现门禁通过。六模块独立回归 239 tests / OK（46.496 秒）；miku WAV 集合 824→824，零增删。未弱化生产超时/退出判据，未改 vendor、默认配置及音色。现在允许固定文本、受限生命周期的真实两模型诊断；尚未证明性能改善，不允许据此发布 App 自动接入或宣称不会卡顿。
# CPU checkpoint staging：Codex review CHANGES REQUESTED

初版d3604bbb独立probe `tmp/codex_cpu_staging_alias_probe.py` exit1实际复现：restored=True，但retained_alias_works_after_scope=False / RuntimeError。禁止真实性能测试直到修复和独立门禁。

`_patched_torch_load`依赖全局_ORIGINAL_LOAD/_ACTIVE，作用域退出后清None。库/其它线程若在scope内保存torch.load引用，scope外调用该引用会RuntimeError；下一scope期间调用旧引用更会错误采用新scope的owner/path/original。常见from torch import load或函数参数缓存即可触发，破坏“其他加载原样委托”。

改为**每次作用域独立闭包**捕获original和本轮owner/已解析路径/active状态。结束先令本轮active=False，再恢复torch.load；保留引用在退出后或新scope期间仍永久委托其捕获的original，不能读新scope全局/清None。非owner线程原样委托，不重试内部TypeError。进程级nonblocking锁可持整个scope确保重入/并发立即拒绝；初始化/Path解析/导入或构造异常须finally释放，不能留死锁。

补当前retained alias及“scope A保存alias→A退出→scope B active时同线程调用A alias仍原样不stage”的回归；other-thread延迟调用用Event屏障/finally join，不time.sleep。root probe不可改。保留其它准入/精确三文件/kwargs/positional/weights_only行为，不改vendor/音色/timeout/权限。

专项+既有tmp限定门禁一次/120秒/文件集合零差，compile/diff；交付停止Codex复审。无需全量与真实模型。

## 0bf1f996返修中独立复验

retained alias probe已通过。但`tmp/codex_cpu_staging_init_probe.py`实际失败：scope_lock_released_after_init_failure=False。`import torch`、original_load获取、_make_patched_load/Path.resolve仍在try前，初始化普通异常使scope锁永久占用，正是上一评审已要求的initialization finally边界。请把取得锁后所有初始化放外层try/finally（锁无条件释放），只在安装成功后执行deactivate/restore；不能未绑定变量掩盖原异常。补初始化异常后能再次进入scope的fake。保持别名闭包方案与精确路径语义。此问题修好后才最终门禁/真实测试，不以alias通过代替整体通过。
