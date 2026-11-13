# RVC 共存准备契约：Codex 独立通过

Cursor b8b0d01b最终实现已逐项复审。helper现在在warm失败/成功但ready已失效/仅start后ready不真实时返回False；无重复spawn/retry，不变音色、超时、引擎。TTSEngine既有传播将_rvc_ready保持False，协调器不会继续Cosy。disabled/无policy/startFalse兼容。

Codex独立执行已审查`tmp/cursor_rvc_coexist_readiness_gate.py`：86 tests OK/0.862秒，exit0，644→644精确文件集合无变化。fake失败日志为预期故障测试，不代表真实进程失败。之前1007全量结果保留为上一版本基线，本次只针对窄修改跑受影响门禁。

这是结果正确性修复，不是75秒启动耗时的性能修复。真实共存仍未通过。下一步仅一次现有extended-cold60诊断，生产默认30秒保持；禁止App接线及声称性能达标。
