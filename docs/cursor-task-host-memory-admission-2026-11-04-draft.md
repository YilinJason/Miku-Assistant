# 当前资源切片：Windows模型冷启动物理RAM/commit准入（已放行Cursor）

RVC预检切片已Root117+最后异常合同39通过，本稿现已放行。用户优先电脑不卡死，不暗换12B模型/Nahida/预算，不修改注册表/分页文件/用户Ollama服务，不下载。最近只读available RAM约16GB、system commit headroom约12.6GB，早前两模型成功树private约16.9GB，当前三模型不可假定装得下。无真实重模型试错。

## 设计

新增stdlib-only src/core/host_memory.py，lazy Windows GetPerformanceInfo读取同次快照PhysicalAvailable与(CommitLimit-CommitTotal)，单位MiB，明确ctypes arg/restype/结构DWORD+SIZE_T，PageSize>0、limit>=total、物理available<=total等；API不可观测返回None，非Windows明确unsupported并维持原平台行为。无NumPy/语音/torch/GPU外部进程/采样线程/循环/psutil新依赖。参考tmp sampler但生产不得import tmp。

窄检查函数check_model_startup_memory(kind)→冻结结果(allowed,reason,available物理/commit,required值)，strict bool/整数校验；不把地址空间当commit。已ready/re用模型不因低余量拒绝，只有新启动前检查。初版内部保守阈值：RVC至少物理4096MiB且commit14336MiB（估计10GiB启动提交+4GiB系统余量）；Cosy至少物理8192MiB且commit10240MiB（估计6GiB+4GiB）。这是本地测量支持的安全起点，不是所有模型内存上限/保证；记录ADR取舍。unknown Windows fail-closed、非Windows保持旧行为，不加隐藏禁用开关或大配置矩阵。不修改LLM生成/默认模型；只保护本项目能控制的新增语音模型启动，外部进程变化仍可能竞争。

## 接线

RVCWorkerClient构造在resolve_rvc_device外部CUDA探针之前检查，避免先重导入torch才拒绝；start真正spawn之前复查，已有ready路径不检查。资源拒绝使用稳定类型ModelStartupMemoryError或等价，只捕获这个新增受控拒绝类型后False并记录路径无关原因，原普通异常/KBI仍按旧合同传播，不能再泛catch Exception。_rvc_last_error清楚提示资源不足及释放其它应用后显式重试/重启；不得伪ready。RVC一次性路径在CUDA探针/推理spawn前同样检查。Cosy客户端_start_worker_poll入口在spawn/reader前检查，资源不足False并稳定错误标签；ready复用不受影响。保留原owner/未知退出/IO lease/Safety边界，不因拒绝清掉旧owner或改关闭语义。不要为新保护修改已通过底层NativeJob。

只采样同步元数据，无后台监控线程，不等待资源变多、不重复冷启动风暴。当前RVC失败sticky重试另切片处理，此次日志明确需显式重试/重启，不假装它已实现。现有Edge fallback配置不暗改，但资源拒绝必须有清楚可见警告，不能把原音色当Nahida成功；Cosy禁云fallback保持。

## 测试

fake WinAPI真实结构写pages，验证计算、边界、失败/未知/非法值、不支持平台，fresh sentinel保证零重库导入。三个实际调用路径低RAM/低commit/unknown均零模型spawn且RVC零CUDA探针；足够则原能力runner一次，ready重用在低余量仍复用。部分owner未quiet仍优先旧门禁，不可被memory足够绕过。旧相关fake测试应统一提供可观测固定足够metadata（精准patch helper，不修改生产识别Mock/测试特判），不要依赖当前电脑空闲量让测试偶然绿。只迁移相关fixture，保留原退出断言。

读取AGENTS/四skills，全部生产/测试由Cursor，Rootreview。保留dirty，正式memory禁读改删，未提交推送。运行已有Root scoped gate前台顺序（新host_memory_admission+RVC/本地TTS相关），无全量discover/真实模型/设备/清理。旧fake统一在test support通过精准patch common read_host_memory固定足够样本，不改生产检测Mock或加测试模式。Root自有tmp探针若需同样fixture由Root自行迁移，Cursor不能改。禁止宽泛修改测试断言以变绿。返回改动与精确测试证据后STOP。

依据：https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-performance_information （CommitTotal/Limit为pages，PhysicalAvailable不同量）。
