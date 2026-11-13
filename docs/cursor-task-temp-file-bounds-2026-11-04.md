# 临时文件资源治理（已放行，预取前置）

RVC重试已Root最终闭环，见同日review及资源里程碑。用户授权Codex设计/严格review、官方Cursor CLI实施；当前巨大dirty保留。读AGENTS/四skills、src/voice/audio/temp_files.py、相关安全/IO消费者及现有测试；snapshot tmp/tfm-bounds-root-before-20261104只是Root审查基线，禁止checkout/reset/restore/clean或覆盖旧文件回退。禁止正式data/memory读改删、真模型/设备/下载/清理现有数据、声学/模型预算/权限/系统设置/NativeJob/IO核心改动；未提交推送。

## 现状与本切片范围

- _owned无硬上限；单一队列有界不能约束保留的文件。
- remove/remove_delayed遇pin直接返回，release只减refcount，原删除请求消失。
- _ensure_worker在锁内start，异常时只看is_alive可能错判未启动，产生第二个cleanup worker；shutdown无法观察未知启动。

仅TFM及必要兼容状态契约/仓库测试/ADR/交接。不要同时预取、改Edge流水线、重写Cosy/RVC或资源配置体系，不引框架。

## 必须保持的资源契约

1. **所有权硬上限**：新增可选keyword owned_capacity，默认128、允许严格int 1..1024（bool/float拒绝），不能配置无限。create在mkdir/touch之前同现有registry锁内准入，满抛明确RuntimeError子类，既有owned保留；adopt_existing相同上限且满不修改外部文件，同一仍有效owned身份重复adopt不耗新名额。并发不能超限；成功安全回收后可重新分配。拒绝不能偷偷创建/丢登记/扩白名单。只新manager参数，不改全局profile。
2. **pin期间保留精确删除意图**：remove及remove_delayed在身份/权限合法但仍pin时，保存与owned身份绑定的待删记录，记录集以owned_capacity为硬界（owned子集）。最后一个lease释放后自动推进现有单worker删除，无须下一次手动remove；前几个ref释放不得删。release_io_lease路径只做短状态/非阻塞入队或signal，**不执行文件删除/重试睡眠/新Thread.start/join**。可以在最初请求删除时准备唯一worker；无权限/身份变更不能绕过安全删除。
3. **有界且不丢意图**：重复remove幂等、不能同path排无界重复；队列满仍保留精确owned/待删意图，由同worker在有名额时推进，不能只说留给外部扫盘。重试有次数/退避上限，耗尽仍owned且状态真实，不能无限自动忙重试。可提供pending_cleanup_count等只读窄接口（包含等待pin/队列满/删除失败的待删owned），供后续预取准入；不要暴露字典或靠queue.empty判空闲。
4. **唯一cleanup worker有明确owner**：预留引用在start之前，creator返回/worker进入/终态与实际Thread退出分开；OS bootstrap未知、start未返回或可能迟启时不可再分配第二worker。真实start/join和等待不能包在registry/worker状态锁里。constructor失败可释放无资源slot；start不确定保留并使shutdown complete=False，后续迟启看stop立刻退出，join后可再次shutdown收口。不用daemon或is_alive=False证明未启动；不新增watchdog/每文件线程。常规始终最多一个清理worker，失败/重试无堆叠。stop不能在迟返start处被清掉。
5. **关闭与身份**：现有TempFileShutdownStatus字段兼容（如必要新增默认字段表明start_unknown/creator_pending，语义说明，不把未知谎报alive）；complete须含真实worker quiet、无owned/待删/IOlease。在总预算内shutdown，不能在pin释放路径同步长删；关闭后release仍可维护状态供再次shutdown，但不得新建worker。文件被替换/链接/权限撤回时绝不删替代物；清理记录同步撤销旧身份，不能幽灵占满容量。保存现有SafetyPolicy边界，OS单次不可中断元数据操作的现实限制如实说明。

## 验证（全部fake/仅fixture临时目录）

先新增行为测试：容量validation/create/adopt/并发/删除失败后不超限与回收恢复；重复adopt幂等；remove及remove_delayed被2个lease pin，首release文件仍在，末release后worker自动删且主线程没有os.remove/Thread.start/sleep；queue容量1溢出多文件自动推进、去重与计数真实；权限/替换/symlink不删（权限不足symlink可skip），重试耗尽不丢owner/不忙转。

Thread构造失败、start已可能bootstrap但is_alive=False、start未返回时并发remove/shutdown、迟启stop、worker终态但Thread仍alive等控制Event用例。线程fixture收所有BaseException、finally释放并真实join/notalive；未运行/未知不能算quiet。确认registry锁外start/join，至少一个实际线程验证关闭后无线程残留。无真实设备/main/Ollama/清理器。

先新单模块现有Root scoped gate（唯一label、<=120秒、前台），失败停修；通过后TFM安全/存储/持久IO/owned-run/Cosy c1与c2b2a相关模块分组<=180秒顺序，WAV826零差，禁止unittest discover、detach或改Root监督器/探针。编译/diffcheck，ADR写上限/状态/取舍，handoff顶部实际JSON路径，STOP等待Root严格源码与独立边界复审，不进入预取。
