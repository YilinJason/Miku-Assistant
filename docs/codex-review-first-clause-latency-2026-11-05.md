# 首自然分句：Root源码、离线与真实首音频对照通过

生产仅sentence_splitter.py及assistant.py构造调用，新tests/test_first_clause_latency.py；不改模型、音色、生成提交、显示、取消、队列和并发上限。默认splitter None完全保留旧行为；App opt-in阈值12、最多一次合格逗号切分，与更早硬句末/overflow比较边界。Root发现并修复初稿“先刷硬句末导致delta粒度改变行为”、测试中文前缀不足阈值问题，收敛冗余kind与分支。

first_clause_repo_20261105z：6模块125项通过（分句/stream/取消/dispatch/编排/预取）；Root clause_control_root9_20261105aa九项通过，独立枚举固定文本全部双分块位置并对旧None基线比较；clause_control_diag24_20261105ab监督24通过。全部WAV826零差。官方生产会话fb6b4c33-cef1-41b0-a3d4-75a39b7fb27b与诊断0e0fee79-de68-43e3-a6ec-02fbeca8a8c5已STOP。

真实first_clause_audio_nov5ac，同FP32/30秒/Cosy+RVC/有界预取，同38字固定长句，baseline→新策略→baseline，均speak_stream与capture-only：

| 策略 | 首个可播放捕获ms | 完整合成ms | 音频段数 |
|---|---:|---:|---:|
| 原策略前测 | 10000 | 10000 | 1 |
| 首分句 | 4422 | 11172 | 2 |
| 原策略后测 | 9812 | 9828 | 1 |

首音频较两侧平均降低约55.4%，完整合成增加约1.26秒。保留此取舍，不把首段提前宣称整段全面提速。两段音频4.38+8.78秒，固定文本重组与全部4个captured WAV匹配；未测扬声器起声、实际播放间隙或主观听感。本对照没有模拟LLM耗时，也不等于端到端对话延迟。

父峰RSS6887.63MiB，最低物理41803.1MiB/提交29929.34MiB；aggregate/TFM全部complete，owned/pending/inflight/leases均0，child/server quiet、cleanup_errors为空。报告data/test/voice-naturalness/cosyvoice-production-first-clause-control/bb5bef22f15b/report.json。已接受首段等待优化，随后进行独立30分钟App流式链路资源长测。
