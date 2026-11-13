# 内存泄漏专项检查

范围为D:\Miku_Assistant_LLM。Codex设计与独立评审，官方Cursor实施；保留dirty，未提交推送，未访问正式data/memory或停止用户服务。真实模型短测仅在Root评审和资源准入后运行，不打开音频设备。以下区分实际复现、已修复和未验证，不能以fake通过保证全程序长期无泄漏。

## 已复现并修复：后台预热资源滞留

Root旧版探针llm_leak_root3_before_20261104a复现：默认装配的预热仍阻塞，Core.shutdown却标记整体完成；Thread.start实际启动但确认抛错时，原线程引用被丢掉，下一次可再起线程。此路径未被前轮关闭预热的fake短循环覆盖。普通请求timeout可能使其之后结束，但退出时如实持有和确认资源的契约已经违反。

官方Cursor修复后，Root独立8项（llm_warmup_root8_final_20261104c）及8模块152项（llm_warmup_repo_final_20261104f）通过，WAV826零差。100次真实线程/fake暖机请求后client weakrefs=0、线程1→1。已覆盖发布至start窗口、启动确认丢失、close并发、取消迟返、实际退出与终态区别、单次准入和factory失败不忙等。详细设计与返修历史见docs/codex-review-llm-warmup-leak-2026-11-04.md。

## HTTP回收：Root最终通过

**额度恢复后已完成验收**：Root原复现3项、fatal独立3矩阵与相关141项均通过，WAV826零差。详情docs/codex-review-http-response-release-2026-11-04.md。旧暂停记录为历史，不再将其当当前状态。

原计数fake与源码证明warm_up、非流生成/聚合流、探测、退出卸载及memory transport有响应未显式close路径，现已由消费方finally回收，redirect恰一次，普通关闭错误不遮盖原fatal。requests默认非流消费可能已归还连接，因此尚不能称其为已证明持续增长的socket泄漏。官方Cursor修改后Root已独立验收。

## 系统占用与归因限制

前次快照：物理总64618MiB，可用约1384MiB；CommitTotal79095MiB/Limit82571MiB；非分页池6262MiB、分页池4486MiB。进程工作集合计14305MiB，含共享重复且不能与其他统计直接相加，仍有大块占用未归属。没有Python/RVC/Cosy模型进程，Ollama相关工作集合计约122MiB；不能断言上述线程缺口造成了全部系统占用。

本轮Windows报告新的开机时间2026-11-04 19:16:41（America/New_York），之前为10月29日。恢复后可用物理内存约47.6GiB；三个间隔10秒样本：available48432→48380→48372MiB，commit23485→23575→23578MiB，nonpaged1368→1370→1369MiB，paged1219→1220→1219MiB，均没有模型进程。采样时Cursor开发CLI与普通桌面应用仍在运行；这是短基线，不能用小幅变化判泄漏。

重启清除了旧状态，旧差额仍缺RAMMap物理内存分类/驱动池标签证据。需要在再次异常时保留Use Counts、进程/内核池和提交量的时间序列，才能进一步归因；本轮不安装驱动、不修改注册表/页面文件、不使用强制清内存或重启命令。微软参考：https://techcommunity.microsoft.com/blog/askperf/mystery-memory-leak-where-did-my-memory-go/1675369 。

配置现状：ollama.keep_alive="30m"、resource.ollama.unload_on_exit=false。模型在服务中继续驻留属于现有缓存策略，不等同泄漏；不擅自卸载共享模型或停止用户服务。

## 真实模型短测已完成（2026-11-05）

详细证据docs/codex-real-memory-short-test-2026-11-05.md。RVC三次独立生命周期、每次10转换通过；关闭后RSS约111.5MiB/private约1046.7MiB平台稳定，IO/模型Job/TFM均quiet。Cosy+RVC固定六句通过；三模型准备后确认12B仍驻留，六句音频生成并完整退出通过，父树峰值RSS9373.86MiB。进程退出后物理可用49092.53MiB、提交余量44559.86MiB；没有复现逐轮或逐句持续递增及退出大块残留。两项生产回收缺口已修复，不能据此把历史约60GB占用确定归因于Miku。

默认30秒首次Cosy冷启动失败保留；后续六句通过用了明确extended-cold诊断，不更改生产超时。三模型准备55.969秒、短句约4.3–5秒，等待体验尚不能认定达标。诊断早期TCP/API竞态已由官方Cursor修复并Root8/24/7和元数据only通过，不把诊断缺陷当生产泄漏。

## 后续长期验证边界

本轮短测已比较加载、重复短句和关闭前后资源。长期多轮、真实打断、重复音频设备开关和低配实机仍需独立时间序列，区分启动缓存平台期与逐轮持续增长。诊断裸客户端必须显式注入并回收新guard和close_warmup；不得直接运行旧smoke绕过准入。30–60分钟真实语音soak按既有约定后置。本报告不把系统级高占用认定为已解决，也不声称真实长期无泄漏。
