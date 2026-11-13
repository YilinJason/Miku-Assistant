# CosyVoice 孤立标点分段：确定性修复

真实soak30_full_nov5h在第32轮chars=1的Cosy请求16ms失败，是否与标点同因尚未实际确认。独立确认的生产缺口：plan_cosyvoice_segments('。')返回['。']，而vendor/CosyVoice frontend.text_normalize过滤is_only_punctuation，inference_instruct无产出后worker_output.collect_inference_speech raises empty_inference。Root门禁punctuation_red16_nov5l中唯一失败为此期望[]/实际['。']。trace_nov5k已由Root主动协作停止，646.42s，41轮已完成、全quiet；不视作30分钟通过。

范围：只改src/voice/tts/cosyvoice_backend.py与新增tests/test_cosyvoice_punctuation_segments.py；保留工作区现有改动。Root前快照tmp/punctuation-root-before-20261105/cosyvoice_backend.py。读取AGENTS及safe-refactoring/architecture-contracts/engineering-quality/collaboration-review相关内容。

最小修复：在Cosy计划中既有总字符及分段上限校验后，过滤仅由Unicode P或S类别及空白组成的片段（包含全空白/空串）。保留有实际文字/数字/组合标记等其他类别的片段原文和标点，单字好、英文/日文/阿拉伯文、数字/混合符号文本都不得丢失。不要修改通用split_for_speech、sentence_splitter、SpeechProjector、展示/历史/记忆、Edge策略或vendor，也不调整worker失败判定。不要过滤后再做上限来绕过原3200字/32段拒绝。线性扫描、常量额外状态，无新线程/依赖/模型调用，无新配置。注释解释与Cosy normalizer丢弃纯标点的契约。

回归需覆盖：纯标点/引号/emoji符号/空白不合成；普通文本和单字/数字/各文字原样；超限仍报原错误；有界speak_stream fake执行['你好。','。','再见。']时只合成/播放有效片段，chunk done仍每原chunk一次（含空计划），实际关闭quiet/TFM无owned/pending/inflight/lease，生产普通/致命合成失败仍原样向上传播，不把真错误当无语音。采用既有fake fixture，不启动模型/网络/设备。

Cursor只写代码、AST/compile检查，不执行测试/模型/设备；不读写正式data/memory，不提交推送。完成停止供Root严格评审和顺序门禁，再用新label重新真实30分钟。
