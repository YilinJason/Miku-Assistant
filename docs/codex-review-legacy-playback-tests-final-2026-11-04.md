# 非流播报最终测试兼容：Cursor 已迁移

Root生产5/5（含300 burst与4类fatal）及相关162/162均PASS，WAV826零差，**生产冻结**。

## Cursor 迁移（structured_dispatch）

- `tests/test_structured_dispatch.py`：成功路径改 `TurnSpeechPlayer` + `PlaybackStatus.COMPLETED` + `addCleanup(_shutdown_assistant_with_player)`；失败/空路径去掉 `Thread` patch，断言 `_speech_player is None`
- 删除全仓已无引用的 `_immediate_thread_factory`（`test_assistant_orchestration.py`）
- Gate `legacy_playback_structured_dispatch_20261104`：`test_structured_dispatch` + `test_natural_companion_round1` → **33 OK**，WAV826 零差 → `tmp/codex_gate_legacy_playback_structured_dispatch_20261104_2026_11_02.json`

**STOP**：待 Root 复审此迁移并独立复验后切片验收；不进下一切片。

## 历史要求（初稿）

Root生产5/5（含300 burst与4类fatal）及相关162/162均PASS，WAV826零差，生产冻结。全仓rg发现tests/test_structured_dispatch.py:178仍使用_immediate_thread_factory，patch src.core.assistant.threading.Thread会同步执行实际消费者循环而挂起。此旧断言必须迁移，不能仅保留这项兼容工厂或因为没进本轮gate忽略。

只改该文件涉及非流TTS的用例：去Thread patch/逐回复创建断言，实际player+fakeTTS等待本轮COMPLETED，finally或addCleanup调用已复审的_shutdown_assistant_with_player。allow_tts=False两项也无需Thread patch，验证未建player且文本/角色契约不变。清理无消费者证明也必须actualquiet；不得运行旧同步factory触发挂起。全仓rg确认已无其它依赖旧行为的调用，再考虑删除确已不用的_immediate_thread_factory（不是必须）。

生产不改，受监督仅structured_dispatch与natural_companion_round1必要组唯一label、compile/diffcheck、handoff后STOP。Root复审此测试迁移并独立复验后验收，不进入下一切片。
