# Codex严格评审：关闭结果传播（dc7bfe56）

## 最终结论：本切片 PASS（2026-11-02）

Root已复查最终轻量core/shutdown_status模块、Engine严格bool、自有TFM写入者条件、App静态声明接口/legacy兼容与loader guard。生产无unittest.mock导入/特判，Core不再依赖具体TTSEngine类型；getter/字段非法不会被洗成成功。Engine旧导出名保持兼容。

Root四项故障probe全true，独立13模块408 tests OK(skip1)，84.018秒（监督器85.375秒），120秒上限，825 WAV精确集合零差。执行于正常Windows上下文，未跑模型/main/音频设备/正式data/memory，未提交推送。

结论只覆盖持久RVC关闭结果向上层传播；单次infer/probe、candidate生命周期与新预热/预取仍未实现，整体退出板块尚未最终里程碑放行。下面为已修复历史。

## 返修复审：严格bool方向正确，禁止生产Mock特判

已读最终函数与App分支。请保留严格bool与正常False重试逻辑，仅做这组契约整理：

- src/voice/tts/engine.py 与 src/core/assistant.py 新增 `from unittest.mock import MagicMock, Mock` 及类型特判，属于测试专用通行逻辑，必须移除。生产不得认识Mock类型。
- 当前 engine_declares_aggregate_shutdown_status 只 inspect.getattr_static(type(engine),...)，漏掉对象显式赋值的真实Adapter API，且把明确声明但不可调用的值当“没有API”而放行。**固定算法**：对 engine对象本身做 inspect.getattr_static(engine, name, sentinel)，只要实际存在就视为声明，随后getattr/callable/状态验证失败关闭。不要对Mock/具体引擎做类型判断。这样未声明的动态__getattr__（包括默认Mock）自然不算API，显式提供的属性（包括instance方法/None）必须尊重。
- Legacy local_cosyvoice_shutdown_status 同样先检查静态声明，避免默认Mock动态伪造Cosy接口；engine_type=='cosyvoice'缺接口仍拒绝。普通旧Adapter没有任何声明时保留旧None-return shutdown兼容。
- 将只读status dataclass/字段常量/接口检测/验证移到轻量 src/core/shutdown_status.py（stdlib-only、无voice/model import），App从此模块取helper。Engine导入并可保留现有名字的re-export兼容，不让Core依赖具体TTSEngine实现模块。不要重排其它功能。
- verify先验证全部字段都是bool，再判断全True；不要在发现第一个False时格式化尚未校验的其他任意对象。错误只包含稳定类别/字段名。

增加 class-declared、instance-declared、明确noncallable/None、无声明动态对象、明确Mock接口受尊重 的行为测试；不源码字符串匹配、不改root4项probe。所有真实状态字段仍严格True才成功。

仅做上述小整理，生产行为不扩大/不绕过任何失败。targeted+root4probe各一次，再含App模块402相关gate一次，120s正常Windows上下文/WAV集合不变；意外失败停止交接。不要进入下一切片。交接STOP。

状态：CHANGES REQUIRED。已读最终Engine/App/status与调用方、原有注入兼容。关闭失败保留TTs/TFM、speak忙不关依赖、loader guard方向正确，未发布candidate与单次infer/probe仍明确未覆盖。

Root tmp/codex_shutdown_status_probe_2026_11_02.py（不改）四项全false：
1. stop_rvc_worker返回object()，bool(...)使聚合complete=True。
2. 本地backend.shutdown_local返回complete='yes'，也被当作成功。
3. 其他明确实现get_shutdown_status的Adapter返回所有False，App却因不是具体TTSEngine而忽略新API并标记完成。
4. 真TTSEngine getter返回TtsShutdownStatus(complete=1,其余True)，App仅if not complete接受整数1。

请一次最小闭合：
- Engine中 stop结果仅 `is True` 才确认；本地status.complete与自有TFM status.complete也只接受真正True。None/缺字段/非bool均未完成。_publish只产生bool旗标，不把任意truthy输入洗成成功。
- App按**明确声明的窄接口**检查，不依赖具体TTSEngine类型/导入。可用inspect.getattr_static确认对象/类型真的声明get_shutdown_status，避免MagicMock.__getattr__自动伪造API；声明了但不可调用/异常/None/非法状态都不放行。验证完整字段（complete/local_tts_complete/persistent_rvc_complete/synthesis_idle/temp_files_complete）均为真正bool，且全部True才成功；不要只信外部complete。
- 旧Fake没有声明新接口时保留旧Cosy路径；现在test_magicmock_tts_does_not_require_aggregate_status显式设置get_shutdown_status又要求忽略，是错误的契约。改成验证**未显式声明**的动态MagicMock属性不算API；明确提供的接口必须被尊重，包括自定义Adapter的False/None/异常。
- dataclass仍作为只读数据，Root故障注入允许构造malformed状态用于验证App，别通过修改rootprobe或去掉测试绕开。

补有意义的True/False→重试、非bool/缺字段/None/抛错、自定义Adapter与无API旧Mock覆盖。原四项probe须全true；不改production超时/声学/reader/Job，不补预热/预取或candidate。先targeted+probe一次，再包含 test_assistant_orchestration 的相关gate一次（原RVC gate漏了新App测试模块，须补进去）；正常Windows环境、120s、精确WAV集合。意外失败停止报告，不重复求绿。不读改删正式data/memory，不提交推送。交接STOP。
