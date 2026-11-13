# 非流回复复用有界播报所有者（2026-11-04 已放行）

## 问题与范围

assistant._apply_outcome_side_effects 的 use_legacy_thread=True 每回复创建 daemon Thread，阻塞在 TTS speak_lock 时积累，启动失败还同步 speak 阻塞请求。改为复用现有 TurnSpeechPlayer 与 _GenerationTurn 的队列/取消/退出责任，不新造队列、池、每条线程。生产只 assistant.py 和必要既有 tests/docs；不改播放器/预取/TFM/RVC/声音模型配置和权限。保留巨大 dirty，禁正式data/memory、git回退提交推送、真实模型/设备/清理。先读AGENTS四skills及安全规则。

这是有意行为修复：异步非流回复也遵守当前轮最新有效，旧轮未播废弃；关闭可见且有界。非流文本返回、角色转发和TTS许可/投影不变。use_legacy_thread=False 的原同步入口保留。生产两个调用都传turn；私有入口turn=None要明确保守处理，不能绕过所有权擅自新建代次覆盖活动轮。

## 设计

- 当 use_legacy_thread=True，用 _claim_speech_for_turn(turn,None) 确认代次并复用唯一player。投影后的单条文本enqueue，cancel_event=turn.cancel，然后mark_production_finished。不要wait_for_turn，不能退回同步speak或另外Thread。
- enqueue在claim锁外，传永久cancel；新一轮开始时已有的CASinvalidate保证旧轮提交失败/迟返不污染新轮；无需无限阻塞队列满（当前新轮单条）。不能在_turn_lock内调用prepare/play/等待消费者。
- enqueue失败只释放本轮CAS并做简短无文本日志；原始回复照常返回/角色转发，不能吞BaseException或伪造完成。bootstrap普通异常可以记录并回收本轮后跳过播放，致命异常原样传播，不能同步兜底。
- shutdown继续使用现有_speech_player stop/join→TTS→TFM顺序，失败保留重试。不得另有失去所有者的worker。
- 避免复用_speak_outcome_tracked后却等待：提取小型共同入队helper仅在明显减少重复时做，不要为精简扩大迁移。

## 验证

更新原test_assistant_orchestration中依赖每条Thread或同步fallback的旧断言，以可观察行为代替mock线程调用数。旧thread补丁会污染全局threading.Thread，禁止用它跑真实OwnedThreadTask；必要用注入fakeplayer做普通入口测试，实际单consumer压力用真player+fakeTTS。

至少证明：阻塞第一个fake speak时连续数百次非流输入立即返回、只有一个实际consumer、待播有限、旧轮不复活而最新轮能播；旧轮生产迟返不污染新轮；启动失败无同步speak/替代线程；use_tts=False和allow_tts=False不建player；角色转发及投影保持；关闭超时不假报完成，解阻塞后实际join和shutdown重试成功。所有Thread异常由主测试断言，事件finally释放，实际join后退出patch与临时目录；无设备初始化。

主Python312通过tmp/codex_scoped_test_gate_2026_11_02.py唯一labels顺序前台显式modules<=120秒，禁raw/discover/并行/修改运行Rootprobe。先读涉及fixture，FakeAudioPlayer构造前patch。最小相关：assistant_orchestration、stream_playback、speech_queue_lifecycle、voice_session、generate_stream。compile/diffcheck、ADR和handoff后STOP，Root独立review通过前不进下个性能切片。
