# Commit/mmap诊断：Root fake/边界 PASS（仅tmp，实验未代表生产）

## 唯一真实控制实验：FAIL，禁止推广生产默认

Root执行隔离11445的prepare-only，报告tmp/codex_real_three_model_mmap_commit_root.json；服务日志确认load_mode=mmap。Ollama预热120.016秒超时，未进入RVC/Cosy阶段，总129.23秒。树RSS峰7713.28MB，最低物理余量9610.56MB、提交余量7831.33MB；观察器已降为约13.64MB private。应用首轮六字段/TFM完整（本次未启动RVC），外层child/server quiet与cleanup_complete全部True，cleanup2.03秒，taskkill超时由原NativeJob回收。不把退出成功当作加载成功，不据此断言驱动/分页根因。正式mmap/model/keep_alive/声学配置保持不变。

对照默认隔离测试tmp/codex_real_three_model_local_sdk_policy_root.json仍FAIL：Ollama56.078秒，RVC82.078秒无ready，应用内persistent关闭未完成，外层Job最终完整回收。系统提交余量与物理RAM需分别预算，后续资源板块尚未闭环，不提前宣布三模型流畅。

Root独立顺序/小批fake电池全部通过：sampler3、mmap5、轻量导入1、监督24、Root资源6、实际main入口4、Policy launcher4；WAV826零差。采样器已stdlib-only，Root轻量探针实际private13.05MB/RSS20.88MB/4线程、观测commit_available19458.1MB；原真实gate观察器约1035MB私有提交，测量进程结构不同，不能把全部差值归给单个库。未知/低commit fail-closed，实际报告仍区分run_error/cleanup_complete。

tail已seek+read(max_bytes)，mmap wrapper warmup/chat/stream覆盖、保持既有options/keepalive，错误端点拒绝。原16+新tests已独立跑，Root实际main断言保留（Cursor曾重写到helper，Root补回真实main测试）。未改生产默认/系统设置；仅放行一次隔离mmap控制实验。下一步必须读取实际服务加载日志、逐阶段耗时、commit及退出，不预判根因或宣布功能完成。

官方来源和背景见任务文件。未放行真实模型。主防护结构基本正确，但先统一收口以下问题，不扩scope/不改生产：

1. **P1 观察器自己导入重库**：sampler `from src.voice.cosyvoice.worker_resources import available_physical_ram_mb` 触发src.voice.__init__→TTSEngine→RVC postprocess→NumPy/SciPy。此前真实采样器本身private约1035MB/35线程，改变被测提交压力。Root新独立codex_sampler_import_root_2026_11_04.py拦截重库导入，确定失败于src.voice，0.453秒。不应只说“无模型导入”：元数据工具须stdlib-only。复用新GetPerformanceInfo的PhysicalAvailable*PageSize同时获得物理余量与commit，保留query_system_commit_mb旧tuple接口包装；不要另引语音包/SDK。API失败/无效None，不伪造数值。所有fake保持，Root新probe不可改弱。
2. **P2 有界tail仍全文件读**：read_bounded_log_tail先path.read_bytes()再截断，未真正限制读取。改二进制seek文件尾最多max_bytes再read(max_bytes)，处理文件缩短/缺失/OSError固定标签，不完整读进内存。用受控file-like spy或大稀疏fixture验证read调用有上限，不创建巨量真磁盘数据。
3. **P2 mmap test未覆盖承诺**：目前2项只warm_up+非流式、payload没既有options；补真实OllamaClient.generate_stream发出payload后正常迭代/close、已有options保留且keep_alive不变、disabled不变、错误模式零安装。helper明确拒绝非隔离11445端点（仅tmp实验的边界），不改OllamaClient生产。不要用真实网络或只mock wrapper返回。
4. **验证/范围**：fake_complete_sample测试工厂放tmp测试support即可，实际采样/unknown路径不能调用它。此前Cursor修改Root6 fixture补commit字段可说明，但不删原断言/不重写Root入口测试。跑samplerfake/mmapfake/监督22/launcher4/Root6/实际main顺序及Root元数据轻量probe各一次，必须统一exit0再STOP。不并行detach或重复新supervisor；所有调用已有Root gate。GetPerfInfo(fake exception)也必须None/稳定错误，低commit/unknown均真实停止。

无GPU/模型/用户服务/正式memory/分页文件/注册表/配置改动。Root之后单次唯一label的mmap控制实验才可运行。
