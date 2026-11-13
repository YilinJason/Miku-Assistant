# Soak 第32轮播报失败：先补定位，禁止猜测修复

Codex 已复核 real soak30_full_nov5h：504.44秒退出1，31轮成功，第32轮生成音频已sf.info验证后消费者 RuntimeError，stage_playback_failed。所有App/TFM/Native Job退出已确认，RAM/commit无触发。保留原报告。

只修改 tmp/codex_app_real_voice_smoke_2026_11_03.py，并可新增一个tmp纯元数据异常观察helper。不得修改生产src/tests/config、超时、资源门槛、prompt、num_predict、队列、取消/回收或成功条件；不执行任何程序/模型/测试/设备，不提交推送。读取AGENTS及相关skills。禁止覆盖其他未提交修改。

要求：仅soak诊断在现有真实engine.speak_stream实例调用外加透明观察wrapper，捕获BaseException后记录并原样raise，不吞异常、不重试。记录bounded最多4条、异常链最多3层(循环保护)、traceback最多末尾8帧，只有项目相对file、function、lineno、异常类名、整数errno等元数据。不要记录异常message/args/locals/sourcecode/绝对外部路径/文本/音频内容；不要用repr(exc)。对项目外frame只给external标记，函数/类名可做字母数字下划线截断。输出phase=soak_speech_error到stderr并放入child报告speech_errors；stdout现有协议不变。正常调用参数/返回值/原异常对象/异常链不变，非soak完全不包装。helper可独立import无副作用，便于Root用fake验证普通/致命异常传播和隐私/bounds。正常路径不增加新线程/timer/IO，失败时不允许观察器故障覆盖原异常。无需生产日志改动。

完成只做AST检查并回报精确文件及实现，停止等待Codex复核。Root将离线验证后用新label跑真实链路，不把此前失败标记通过。
