# C1：TTSEngine可选本地CosyVoice适配

用户授权必要代码/文档发送Cursor官方服务，并由Cursor实现、Codex严格评审。读AGENTS.md、skills/collaboration-review、architecture-contracts、engineering-quality、相关ADR及B最终评审。保留巨大既有dirty工作区，禁止重置/提交/推送；不读改删真实data/memory，不启动模型/设备/主程序。本切片不改assembly/config默认，不进入C2。

## 已验证起点

B已通过Codex独立900 tests(skipped6)及真实cold/warm/idle3/3。默认startup30秒，热基础合成2.6秒；冷19秒左右、极冷缓存可能超时安全拒绝。单实例worker、有限文本/音频、安全输出owner、完整身份、取消/超时/idle退休均不可回退。用户选原01_warm_companion_rvc.wav作为听感基线，禁止改RVC参数或覆盖该文件。

## 实现范围

1. `TTSEngine(engine="cosyvoice", cosyvoice_config: Optional[dict]=None)`兼容扩展；用现有parse_cosyvoice_settings严格解析，且配置错误必须在AudioPlayer/TFM/RVC/client副作用前失败。仅显式cosy引擎创建CosyVoiceWorkerClient，共用同一个TFM与显式SafetyPolicy。构造仅检查local_tts_worker许可及封存profile等元数据，不启动模型或导入torch/CosyVoice；cosy构造不预热RVC，交C2资源协调。Edge/pyttsx3旧默认兼容。
2. 建议新窄适配器模块`src/voice/tts/cosyvoice_backend.py`负责本地owned WAV合成与client生命周期，避免engine继续膨胀。文本仅固定style_id=`warm_companion`，无自由instruct/路径/脚本/模型字段。TFM.create('.wav')预建在固定TMP_DIR（注意TFM默认是data/tmp，必须显式选生产worker的固定tmp且先有删除白名单），调用client.synthesize，cancel_event贯穿；成功直接WAV送既有RVC，不绕MP3、不进Edge、不额外归一化/滤波改音色。转换串行，失败沿用已配置RVC基音fallback语义但清楚标明是基础本地音频。
3. C1的Cosy播报采用**顺序分句、一次只持有一段音频**，暂不启用旧的无界预合成pipeline；不新建producer/Timer/每请求线程。最多3200投影后字符/32段；每段不超过min(120, settings.max_text_chars)，优先现有自然句界/逗号，再硬切无标点长句，不丢字、不截断。超整体上限在任何合成前明确拒绝。播放结束/异常/取消立即清理该段；构造全量音频列表不允许。顺序策略的句间等待限制明确记文档，C2再根据实测决定有界预取，不能宣称流式或无等待。
4. `speak`仅Edge检查cloud_tts；Cosy完全本地，不因cloud_tts=false拒绝。Cosy失败、取消、资源不足均**不自动fallback**至Edge或pyttsx3（本切片保守显式失败，默认fallback_enabled不扩大本地出口）。不能修改engine_type偷偷换引擎。local permission denied也不进入云/应急播放。
5. `interrupt`推进现有epoch并传本地client.interrupt，禁止晚到音频播放；下一显式轮可恢复。`shutdown`先标closing/取消→停止本地worker→确认本地请求/owner退出，再清理本实例owned TFM。incomplete必须保留client/TFM引用可重试，不能新建第二client、不能删除仍写的WAV。外部TFM生命周期始终由caller拥有。与speak并发/constructor失败也有明确owner，无无限等speak_lock。给C2提供只读可观察的本地关闭状态，不破坏既有shutdown(stop_rvc=...)调用。
6. `save_to_file`Cosy分支仅支持TFM已登记、固定tmp内的普通.wav目标（一个<=max_text_chars的清理后文本），失败/取消抛明确异常；未登记/错误扩展名/超长在发起worker前拒绝，且不删除调用者目标。成功是基础Cosy WAV，和Edge save_to_file同样不经过RVC。禁止走Edge或任意路径copy/export。使用现有owner复验契约；不增加宽泛adopt_existing。

## 测试/交付

fake AudioPlayer/Cosy client/RVC，全离线：配置先拒绝零副作用、构造零模型/Edge零本地client、cloud=false本地成功、固定style/WAV→RVC顺序、转换失败基音、长无标点硬切/极小max chars/32段与3200界不静默截断、每段播放后回收且owned数有界、取消/晚到无播放、失败零云/pyttsx3、关闭不完整保留TFM+client重试成功、speak/save与shutdown竞态、未owned save零修改等。

测试TFM正确addCleanup且先关闭client；不可制造新的miku临时文件净增长。专项用主Python312、进程树总超时120秒，留stdout/stderr，不反复跑全量。compileall及git diff --check；更新ADR C1当前行为与交接顶部，完成后停止等待Codex复审。返回文件清单、准确测试命令/计数/耗时与限制，不把fake当真实音频通过。
