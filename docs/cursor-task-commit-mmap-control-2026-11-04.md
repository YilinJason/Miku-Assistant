# Windows提交余量保护 + mmap控制变量（仅tmp，先fake）

三模型默认复测仍FAIL：Ollama56.078s、RVC82.078s无ready，应用内三次shutdown persistent=False；外层Native Job最终child/serverquiet全部True，总159.83s。报告tmp/codex_real_three_model_local_sdk_policy_root.json，子目录f139ac4f8d2f。此时树RSS峰2628.19MB，但PrivateUsage提交峰17712.32MB；两模型成功时private16904.6MB。当前系统TotalVirtualMemorySize78494132KB、FreeVirtualMemory19931888KB（事后样本，不是故障时值）。不能据此已断言分页/驱动根因。

官方依据：Microsoft PROCESS_MEMORY_COUNTERS_EX PrivateUsage是commit charge；系统提交余量用GetPerformanceInfo的(CommitLimit-CommitTotal)*PageSize，不能用地址空间ullAvailVirtual当提交余量。https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/ns-sysinfoapi-memorystatusex

Ollama已安装0.40.6日志明确默认windows_cuda禁用mmap；自身server私有提交约11168MB。官方源码server/sched.go说明显式UseMMap值可覆盖默认。https://github.com/ollama/ollama/blob/main/server/sched.go 。尝试use_mmap=True可能减少file-backed模型的私有提交，但未验证，不能先改生产默认。CUDA方面：RVC torch2.6/cu124本应默认lazy（12.3+），Cosy torch2.3.1/cu121不同，不盲调LAZY或归因为此。

## 范围

只改tmp/codex_windows_resource_sampler.py、tmp/codex_real_voice_supervision.py、tmp/codex_app_real_voice_smoke_2026_11_03.py和tmp fake。**不改生产src/Safety/配置/注册表/分页文件/驱动/用户服务，不启动真实模型，未提交推送**。正式memory禁读改删。读取AGENTS、工程质量/协作技能；所有副作用经SafetyPolicy。

1. sampler新增真实系统commit_limit_mb/commit_total_mb/commit_available_mb，GetPerformanceInfo一次轻量调用（明确ctypes arg/restype/struct布局，pages乘pageSize，API失败None）。保持RSS/private/RAM原字段、无GPU外部查询的循环路径。不要把这些不同量相减造错误物理占用数字。
2. supervisor preflight增加可观测commit_available>=12GiB；循环commit_available<4GiB立即按原有owner统一停止，unknown按稳定标签停止/跳过（不当0）。与物理RAM6GiB guard并列。final/live报告min_commit_available和测量缺失，fake注入含新的metadata；依然单总deadline/10秒cleanup，不新增采样线程或真实GPU循环查询。fake阈值边界和API失败不可观测覆盖。
3. 新增明确 **--ollama-mmap-control** 标志，仅isolated_ollama=True且无其它模式冲突可用（严格flags验证）。不写生产配置。父向固定smoke传受控标志/env，隔离URL必须127.0.0.1:11445；在诊断实例统一_apply_runtime_options wrapper中加options.use_mmap=True，同时保留已有options和keep_alive，warmup/chat同一设置避免模型重载；不得裸请求绕开OllamaClient/Policy。元数据记录仅固定bool与stage，不含用户正文。
4. 仅控制加载方式，模型/num_ctx/声学/30秒/keep_alive原值不变。报告mmap_control=True；服务日志应可核实不再默认disablemmap（Root之后核实，不能此刻称已生效）。保留原--isolated-ollama无标志基线，旧报告不覆盖。**本任务绝不真实测试模型，Root审批代码/fake后只一次新label对照**。

## 验证

mock WindowsAPI结构数据，验证pages计算、failureNone/错误值拒绝；现有sampler路径新字段不可伪。监督fake覆盖低commit/未知/足够、单次阈值触发不再继续spawn、回收报告mincommit。flags错误组合拒绝零spawn；用fake OllamaClient实际_apply_runtime_options/捕获发出payload（无网络）验证warmup/非流式/流式选项保留且use_mmap控制一致，不只测字符串。

Root6和原监督18、launcher4、main入口顺序保持。现有Root supervisor固定probe白名单执行，无裸subprocess新gate；不要改Root探针弱化断言，若新metadata契约使旧fake缺字段，明确兼容注入/新增测试，而不是unknown偷当足够。交付handoff后STOP。
