# C2a Codex review：通过，可进入C2b1

## 当前有效结论

Codex已独立复查配置提前解析、固定PROJECT_ROOT元数据封存/可选失败回退、Edge工厂兼容及关闭状态wrapper。root probe三个场景(None/抛错/缺失API)均断言通过：TTS不假标完成、共享TFM保留、独立cleaner/character仍关闭；Edge None/无API兼容与恢复重试有行为测试。

独立`tmp/codex_c1_validation.py --full-c2a`监督器实际exit0：**957 tests OK(skipped6)**，90.02秒；180秒真实进程树超时、miku WAV文件集合644→644，added/removed为空。没有模型/设备/实际记忆读写。默认Edge及local_tts_worker=false保持。本切片不含本地预热或预取，不宣称主程序语音设备长测完成。

下面为已完成返修的历史依据。

95c9716d返修后：None及status读取抛异常已正确进入_run_step，独立清理继续，静态复核通过。尚有一个同类边界：明确engine_type=cosyvoice但status方法缺失/不可调用时，当前仍直接return视作完成；原review第1条“缺失/None”只做了None。请在`not callable(status_reader)`分支先判断Cosy，缺失抛固定RuntimeError；旧非Cosy无API照旧允许。补一项fake行为测试，再跑原24相关即可，不改其它模块。root probe已增加missing并assert，必须exit0。交付停止。

Cursor53c489e4已交付45项报告。代码装配线路、内层配置、元数据构造封存/可选fail-closed回退、Edge参数兼容已静态复核，无默认切换。主程序关闭状态检查有两项需修，禁止C2b。

1. `assistant.py`当前仅非None且complete=false才判未完成，Cosy返回None会标_tts_shutdown_done并关闭共享TFM。明确engine_type=cosyvoice时缺失/None状态必须未完成；旧Edge真实TTSEngine有该方法但返回None，需要保留旧Edge语义。未知旧替身无方法也保持兼容。方法返回明确incomplete的其它适配器也不能忽略。
2. status_reader()在_run_step之外，普通异常会逃出整个shutdown，阻止cleaner/character等后续回收。将TTS.shutdown+状态读取/校验组成同一个可失败action交给_run_step；状态读取失败视作TTS未完成，保留engine/TFM，其他独立组件仍按原流程关闭。重试状态complete后继续TFM，不重复已经完成的独立组件。不得吞掉错误然后假标完成。

独立`tmp/codex_c2a_shutdown_probe.py`纯fake可复现：None状态不能关TFM；读取抛异常不应逃出/阻断cleaner、character。Cursor可读跑不可改。补None/抛错/恢复后三步、Edge返回None/旧替身无API的行为测试。保持原45项。

仅assistant窄关闭wrapper与相关测试/交接。主Python312离线真120秒总超时，日志/外层退出/临时文件集合零增，compile/diff-check；不跑模型/设备/记忆/提交推送。交付停止待Codex独立复验。
