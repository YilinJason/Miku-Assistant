# 语音包lazy导出：Root最终PASS（范围仅包导出）

返修已独立阅读五包源码及调用关系：真实属性缓存globals、dir保留globals并列出导出；无新增资源/线程或构造副作用。线程异常收集后重抛，join完成才比较identity；全部并发导入测试独立进程，消除原sys.modules污染。13条导出真实定义identity、ImportError/KBI/SystemExit原异常路径及重试通过。Root scoped gate lazy_exports_root_final：46 tests/13.150秒，OK(skip1)，总15.843秒；WAV826→826无增删；Root compileall/git diff --check退出0（仅既有LF/CRLF提示）。生产行为边界无新可确认问题，此小切片闭环。

不等于整个text启动减重：assembly仍显式导入TTSEngine。真实三模型/低配/连续音频等待仍未通过，mmap负面结果保留。下列初审问题是已修复历史。

生产范围保持五个包，未进入预取/装配重构。当前源码已避免包导入连带实现，但测试仍有以下明确缺口，Cursor完成初轮后统一返修。

1. __dir__只返回__all__会隐藏现有__name__/__file__/__path__及已导入子模块，改变普通模块可发现行为。应返回sorted(set(globals()) | set(__all__))，不访问lazy属性；新鲜解释器验证含原globals、全导出且无重依赖加载。ADR“__dir__不变”不准确，此前没有自定义函数。
2. 并发首次访问测试在启动线程前先from包import TempFileManager，实际已经预热，未覆盖首次并发。将所有并发首次访问放新鲜解释器，线程开始前只import包，明确实现模块未在sys.modules；用barrier同时访问，记录线程错误/确认全部join退出，再对实际定义做identity验证。
3. test_inprocess_concurrent_tts_engine_same_identity删除sys.modules里的真实engine，但其它包或回归测试可能仍持旧类，导致同进程类identity分裂/污染后续测试。删除此进程内删模块策略，迁至独立解释器，不清全局模块或篡改已有Root测试。
4. 真实identity测试未穷尽__all__（RVCHealthReport/ensure_rvc_ready及CosyVoiceShutdownStatus/WorkerClient/parse缺失）。使用显式预期导出表与真实定义比较所有名称，保持无构造/设备；导入失败重试至少覆盖原异常identity（包括KBI/SystemExit不吞），不要用宽泛except ImportError把其它错误当预期。导出成功缓存应遵循任务的实际模块属性缓存，优先globals()[name]=value，免去每次__getattr__访问及重复缓存结构；若保留独立cache需解释取舍，不能扩大为通用框架。

返修后Root独立运行lazy测试+现有相关回归，compileall/diffcheck。测试绿灯不代表三模型/低配/真实音频完成。
