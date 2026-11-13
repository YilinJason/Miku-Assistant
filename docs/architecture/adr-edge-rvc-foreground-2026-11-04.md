# ADR: 默认 Edge+RVC 走同一前台有界 Job

## 状态

Accepted（2026-11-04，host-memory Root 408 PASS 后放行）

## 背景

Cosy 已有 ``tts_foreground_bound_path`` / 单 Job pending；默认 Edge+RVC 仍在 ``ensure_tts`` / ``bound_tts_models_ready`` 上对已绑定 Edge 对象假 ready，且 ``--mode voice`` 装配期 ``run_coordinated_warmup`` 同步等 RVC。

## 决策

- ``TTSEngine.get_rvc_prepare_state()`` 冻结 ``RvcPrepareState``（enabled、inflight、attempt_completed、ready、固定 user_warning）；``prepare_rvc`` 仅在 try/finally 发布 attempt_completed，不在进入前伪完成。
- ``bound_tts_models_ready`` / ``app_tts_playback_ready``：Edge 且 ``edge_rvc_warmup_required`` 时须 attempt_completed 且非 inflight；失败完成仍允许 fallback 播报，经 ``VoiceStackLoadResult.warnings`` 携带 Nahida 未就绪语义。
- ``tts_foreground_bound_path`` = Cosy 或配置/绑定 Edge+RVC 预热；``cosy_foreground_path`` 保持仅 Cosy 含义。
- ``voice`` 装配 Edge+RVC：``start_ollama_warmup`` 仅后台启动，defer RVC；App ``request_*`` 经同一 Job 串行 ``ensure``（Ollama→RVC）。
- 本切片不做失败 RVC 显式重试/预取。

## 验证

- ``tests/test_voice_foreground_bound.py``、``tests/test_app_voice_activation.py``
- Root scoped gate label ``edge_rvc_foreground_20261104``

## 非目标

声学/30s 预算/Root 探针/正式 memory 变更
