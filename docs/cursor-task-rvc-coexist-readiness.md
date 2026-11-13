# C2b2a 共存阻塞：RVC 准备结果与实测

读AGENTS、协作/工程/架构skills，保留dirty。只修此已复现问题，不接App/预取，不改音色/引擎/权限/超时/线程数。真实data/memory禁止，模型设备测试禁止、提交推送禁止。

## 已验证

协调器1007项独立离线通过。但共存实际未通过：`data/test/voice-naturalness/cosyvoice-production-coexist/8c91b8a59344/report.json`：Ollama2.063秒、RVC76.812秒、Cosy32.063秒失败，0音频；关闭/TFM全部回收。首轮180秒超时，已精确停本轮进程树。不得把离线通过当共存通过。

独立隔离RVC+驻留Ollama实际cuda:0：74.969秒，其中worker load26965ms、warm infer33490ms；回收完成。仅诊断子进程env将CPU线程cap4对照74.453秒，load26260ms、warm34512ms，**没有实质改善，不将其作为性能修复写生产**。现仍默认Edge/权限false。

## 本轮唯一生产修复

`src/voice/rvc/infer.py warm_rvc_worker` 对worker.warm_up返回False仅warning后仍True。root纯fake已复现warmup_failed_but_prepare_succeeded=True。该路径若超时终止worker，会向新协调器报ready导致后续继续占GPU，是明确契约缺口，真实这次warm成功不等于不存在。

worker_warmup_inference开启时：warm_up返回False应返回False；成功也必须核对实际worker.ready（含alive），禁止复活/重试/假ready。worker_warmup_inference关闭时保持只启动准备，但startTrue之后ready不真实仍False。保留disabled/policy missing/startFalse既有行为。无需新增线程/状态/配置。更新“预热仅优化总返回True”过时注释与相关文档。TTSEngine已根据helperFalse置_rvc_readyFalse，无需拓宽改动。

测试 fake：startFalse零warm；warmFalse返回False；warmTrue但readyFalse返回False；warmTrue且readyTrue成功；warm关闭且readyTrue不warm；关闭但readyFalse失败。穿透TTSEngine/协调器确认RVC失败零Cosy调用，不以mock默认truthy冒充ready。无需真实进程。

先新增及tests.test_rvc_worker_lifecycle、test_cosyvoice_production_slice_c2b2a、test_ollama_performance、mode_assembly_round2；用tmp具名120s监督器/UTF8/文件集合644双向零变化，失败只修具体失败，不重复全量。compile/diff，交接后停止Codex评审。后续Codex会单次诊断Cosy现有extended-cold60上限的启动阶段，**生产默认30秒保持**，测量成功前不下一部分开发。
