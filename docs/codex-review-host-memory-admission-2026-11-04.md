# 主机内存准入：Root最终PASS（保护范围不等于共存性能达标）

Root最终源码/调用点/fixture审查及独立分组：RVC+host 101/34.032秒OK(skip1非Windows分支)、Cosy162/30.669秒OK、App/装配/权限日志142/16.597秒OK(skip2依赖项)、独立SDK buffer/失败API/真实metadata3/0.111秒OK，共408检查、3跳过；每组WAV826零差。编译/差异检查通过。独立真实metadata最后14284MB物理/9830MB commit，仅只读、无模型/设备。

ABI完整及固定offset已独立验证；RVC构造拒绝不保留auto，start/oneshot/Cosy spawn前检查、ready复用不过度拦截，prepare只捕获新增受控资源拒绝，普通异常仍传播。旧fake module setup用unittest.enterModuleContext还原；新host模块不固定真实API；fresh工具已用SafetyPolicy，参数化入口lowRAM/commit/unknown零spawn。slice_b原wetext噪声测试仅迁移旧调用到固定布局和repo_root，原stdout/stderr断言保留。此小项闭环，不声称动态外部进程下绝不内存不足，也不把保守阈值当不同模型的通用容量保证。系统余量不足，无追加真实重模型尝试。

以下为已修正审查历史。

## Root复审：ABI已独立通过，仍有测试迁移与Policy问题

Root自有独立SDK结构/真实metadata/失败API共3项已PASS，真实物理14984MB、commit10523MB（时间点），WAV826零差。构造raise与prepare仅捕获资源类型已读源码符合。继续一次修清：

1. Root gate host_memory_root_wiring_20261104跑新host+旧owned_worker，28项中6个ERROR，均旧RVC fixture未注入固定内存导致真实host_memory_commit_insufficient。不仅worker_lifecycle要迁移，库存所有创建RVC/Cosy fake client或一次性fake能力runner的相关模块。只加固定足够metadata fixture，不能删断言/改权限/生产识别测试。
2. **明确生命周期取舍**：禁止的是tests导入即永久patch；unittest setUpModule中enterModuleContext(patch_sufficient_host_memory())并注册模块结束自动还原是有效生命周期，允许用它避免机械改大量类（勿覆盖已有setup/teardown）。或每测试addCleanup均可。新host_memory_admission模块本身不能统一固定高值掩盖实际API测试。所有fixture都用固定样本，不靠当前机器条件。
3. 新test_host_memory_import_without_voice_graph直接subprocess.run绕过仓库SafetyPolicy，改已有safe_subprocess_run+明确Policy和固定命令/timeout。sentinel当前仅记录src.voice并return None，不阻重库；应对src.voice/NumPy/SciPy/torch/soundfile真实拒绝，检查零导入，别只测常量字符串。
4. 实际三条/四个启动入口只测低RAM，未完成低commit/unknown各路径零CUDA/零spawn合同；用参数化同一fake案例覆盖，不为数目复制大量测试。ready复用低余量照旧。API failure、无效page/commit/physical边界用SDK固定layout断言+fake，不依赖当前电脑。新快照纯数值拒绝bool/非整数/负值，不因int强转产生假可用数据。

请先修完以上再一次新host fake→RVC所有权/IO/oneshot→Cosy B/C1/C2 等相关gate（分组各<=180s、前台顺序，失败停），不能只选择容易绿的模块。Root自有探针不能改；Root会独立再跑，此时不启动模型。ABI原始问题及拒绝状态历史如下。

Root已中断CLI当前回合，保留所有已写改动。不要重做已完成其它切片，不碰Root探针/正式memory/系统设置，不启动真实模型。

## P1 ABI结构错误（已真实只读复现）

host_memory.PERFORMANCE_INFORMATION漏SystemCache/KernelTotal/KernelPaged/KernelNonpaged，PageSize偏移错误，结尾HandleCount/ProcessCount/ThreadCount应DWORD而非SIZE_T。生产真实read_host_memory_snapshot返回None，但同进程已验证tmp sampler返回实际commit余量10969.77MB（时间点），说明不是API不可用。

必须按Microsoft完整原始ABI：cb DWORD；CommitTotal/CommitLimit/CommitPeak/PhysicalTotal/PhysicalAvailable/SystemCache/KernelTotal/KernelPaged/KernelNonpaged/PageSize均SIZE_T；HandleCount/ProcessCount/ThreadCount均DWORD，保持顺序/对齐。GetPerformanceInfo arg/restype及cb传sizeof，lazy stdlib-only入口。异常/未知failclosed，不靠放宽unknown修复。

当前test_get_performance_info_page_math也复制了错误结构，自证错误。独立fixture按官方布局定义，断言sizeof/关键offset（Windows64 size104, PageSize offset80, HandleCount88；以SDK/实测确认数值，不盲抄本句），用真实ctypes buffer写完整bytes，验证生产转换。**注意本段offset估计需自行按字段重算：cb+pad=8，10个SIZE_T=80，PageSize为第10项offset80，尾DWORD起88，总104。** 边界/API失败/字段非法/boolean或负数注入快照拒绝完整覆盖。只读实际系统metadata可运行一次，不加载模型/设备。

## P1 构造拒绝后的设备状态错误

RVC __init__资源拒绝时把_device设原config device（常为auto），之后资源恢复start只再检查内存而不会resolve设备，可能把auto传给Config。按原任务，构造前检查失败直接raise ModelStartupMemoryError（此时无子进程/外部探针），禁止猜设备；TTSEngine.prepare_rvc只catch此**新增受控拒绝**类型，ready=False、_rvc_last_error与可见日志说明Nahida未启用及需释放资源/显式重试或重启；原普通异常/KBI保持传播。start复查仍False，ready复用跳过；不能清空旧owner。

## 覆盖/范围

旧fake必须用明确足够的快照fixture，不能依赖当前电脑余量，更不能生产识别Mock/禁用保护。新模块测试必须穿透三条实际spawn前路径，低物理/低commit/None各零CUDA探针及spawn；够资源原runner一次；ready复用低余量继续；异常及旧owner关门不变。不要通过调整原关闭断言掩盖问题。测试支持的patch需有明确生命周期还原（class addCleanup/contextmanager），不是导入tests时全局常驻patch。

修完只新host_memory模块fake先跑一次，失败停止；通过再相关RVC与Cosy回归一次。已有Root scoped gate限180s，前台等待，不detach。完整交接后STOP，Root独立复核。不声称阈值保护能保证外部进程并发分配时永不内存不足。
