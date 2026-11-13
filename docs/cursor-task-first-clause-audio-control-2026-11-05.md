# 首音频同文本对照接线（仅诊断，待前一源码离线通过后实施）

只允许tmp/codex_real_voice_supervision.py、tmp/codex_app_real_voice_smoke_2026_11_03.py。生产、config、tests、其它tmp冻结，无test/model/device执行权限，AST后STOP。

GateFlags增加first_clause_control=False，固定--first-clause-control，仅允许no_ollama=True且非rvc/torch/prepare_only；显式转发到smoke，报告标注。TOTAL180、EXIT10、MAX36、ready15、全部准入与finally原样。

smoke显式control时使用同一固定长句“今天如果你觉得有一点点疲惫，我们就先坐下来喝点水，再慢慢聊聊今天发生的事情。”，三个case依次baseline_before / first_clause / baseline_after。三者都用engine.speak_stream，baseline通过IncrementalSentenceSplitter默认None逐字符push+finish，first_clause通过first_clause_min_chars=12同样逐字符；不模拟LLM耗时，不含真实LLM/STT/音频设备。其余常规六句完全不变。

每case开始记录monotonic、清first_playable_ms。CaptureOnlyPlayer.play验证owned regular和sf.info成功后，在复制文件前首次记录该case首个可播放时间；每case记录实际len(text)、segment数、captures、first_playable_ms及total wall。不得继续写原13字符冒充实际长句。播放器仍构造前替换，不开启音频设备；所有片段必须完成，捕获数与实际计划片段数相等、重组TTS文本去首尾空白与原文本一致，不只首段成功就算通过。

保持精度FP32、原音色参数、语速、资源与超时不变；无cloud/新缓存/额外并发模型。原UUID report/finally/warmup/guard/TFM回收和fatal原对象保持。Root会复核差异、fake flags与既有harness/监督器后真实对照。只能称first playable capture时刻，不能称扬声器首声或端到端用户体验已验证。
