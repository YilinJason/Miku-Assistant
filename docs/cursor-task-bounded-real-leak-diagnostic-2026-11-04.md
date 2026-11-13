# 受控真实短诊断：只补诊断脚本，不改生产（已放行）

前置预热Root8/152和HTTP Root3/3/141已最终验收。继续检查真实RVC/可选Cosy模型资源趋势；不把fake或两句成功等同长期无泄漏。用户允许真实短测试，30–60分钟长测仍后置。正式data/memory禁读写删；不更改默认Edge、Nahida、模型配置/权限，不停止用户Ollama或其它进程。

官方Cursor仅写下列3个tmp诊断文件，Root审查和fake验证后才运行；你不能启动任何测试/模型/设备/网络：

1. tmp/codex_rvc_real_exit_2026_11_03.py：保留已有SafetyPolicy/TrustedRvcProfile/Native Job/NoDevicePlayer构造前替身、manager所有权。改为同一诊断主进程内3个串行engine生命周期，每cycle实际prepare→固定WAV转换最多10次→严格shutdown（最多原3次）→manager完整回收；失败立即退出，不继续新cycle。固定输入仅读data/test/voice-naturalness/cosyvoice-instruct/01_warm_companion_rvc.wav，复制进当轮manager.create('.wav')的已owned文件，绝不对原文件write/adopt/delete，记录原hash并验末尾未变。只调converter.convert_file，不调用播放或edge云接口；每次输出必须真实owned、用manager.remove及时回收，pin/pending时不丢责任继续分配。输入最后走manager.remove，TFM实际complete才下一轮。每cycle baseline/prepared/每次转换后/closed采样tree_sample(os.getpid(),include_gpu=False)，输出有界阶段JSON，记录RSS/private/thread/handle和真实job退出。报告名必须UUID唯一且exclusive open('x')，禁止覆盖旧固定tmp/codex_rvc_real_exit_2026_11_03.json！保留原监督器入口/flags不变，仍总监督180秒与资源中止线；诊断不盲增timeout或改模型参数。
2. tmp/codex_app_real_voice_smoke_2026_11_03.py：保留隔离11445/coordinator/CaptureOnlyPlayer及原默认参数。给实际OllamaClient显式创建/传入LocalModelMemoryAdmission（实际sealed_endpoint与同policy），构造时不联网；finally独立close_warmup和guard.close，严格True记录到report并加入末尾assert，不因TTS失败跳过。显式keep_alive=0只对本隔离测试加载模型，遵循原ollama_preloaded判断，不能改用户11434服务。prepared cases由2个改为6次同一固定短句，每次输出资源checkpoint；末尾成功计数以len(cases)为准，prepare-only仍0。只capture不播放/不麦克风。加入全链路baseline/prepare/每句后/cleanup后资源checkpoint，用已有sampler，避免dump正文/完整路径。
3. tmp/codex_windows_resource_sampler.py：在已有进程OpenProcess句柄内用正确Win64 ABI的GetProcessHandleCount(HANDLE, DWORD*)→BOOL补每行handles；失败/不可观测为None，不能0冒充。tree_handles只有全部已观测进程handles均可观测时才求和，否则None；保留unobservable_processes并使缺进程不可假完整。无需额外OpenProcess、不加子进程或外部GPU查询。其它采样字段/契约不变，GPU循环仍禁止外部命令。

不修改任何Root监督器/gate/探针/报告、docs或其它src/tests；不创建新监督框架、不安装依赖/下载资产/写注册表/清空系统缓存。不要运行这些文件，仅局部AST/py_compile与diffcheck后STOP，交回精确改动和未验证假设。Root将先fake/只读采样验证，再用现有tmp/codex_app_real_voice_gate_2026_11_03.py严格串行放行真实阶段。默认Edge+RVC与可选Cosy控制实验结论应分开，不声称测完所有模式。
