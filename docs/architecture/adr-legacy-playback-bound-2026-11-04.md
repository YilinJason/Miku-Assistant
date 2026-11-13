# ADR：非流回复复用有界 TurnSpeechPlayer

## 状态

Accepted（Root独立5/5、相关162/162与最后测试迁移74/74，WAV826零差；docs/codex-review-legacy-playback-bound-2026-11-04.md）

## 背景

`process_text_input` / 流式非 `wait_for_playback` 路径经 `_apply_outcome_side_effects(use_legacy_thread=True)` 为每条回复创建 daemon `Thread` 直接调用 `tts_engine.speak`。TTS 持锁阻塞时线程堆积；`Thread.start` 失败时同步 `speak` 会阻塞调用方。流式路径已用 `_GenerationTurn` + `TurnSpeechPlayer` 有界队列与 CAS 取消。

切片前只读快照：`tmp/legacy-playback-root-before-20261104/assistant.py`。

## 决策

1. **`use_legacy_thread=True` 且传入 `turn`**：`_claim_speech_for_turn(turn, None)` → 投影文本 `enqueue(..., cancel_event=turn.cancel)` → `mark_production_finished`；**不** `wait_for_turn`、**不** 逐回复 `Thread`、**不** 同步 speak 兜底。
2. **`turn is None`（私有入口）**：记录简短日志并跳过播报；不得 claim/begin 覆盖活动轮。
3. **入队失败**：`_release_own_speech(turn)` + 无文本 warning；`DispatchOutcome` 与角色转发不变。
4. **claim/播放器 bootstrap 普通异常**：记录后跳过播报并回收已 claim 的 CAS；`BaseException` 不吞。
5. **`use_legacy_thread=False`**：保留同步 `tts_engine.speak`（`MikuAssistant.speak` 等）。
6. **未改**：`speech_queue.py`、预取/TFM/RVC、声音模型配置与权限；shutdown 仍 player stop/join → TTS → TFM。

## 验证

- Scoped gate label `legacy_playback_bound_cursor_20261104c`（窄修 c）：**162 OK**，~7.1s，WAV **826→826** → `tmp/codex_gate_legacy_playback_bound_cursor_20261104c_2026_11_02.json`
- 返修 b gate `legacy_playback_bound_cursor_20261104b`：160 OK
- 模块：`test_assistant_orchestration`、`test_stream_playback`、`test_speech_queue_lifecycle`、`test_voice_session`、`test_generate_stream`、`test_natural_companion_round2`
- `claim→enqueue→mark_production_finished` 同一 try；CAS 释放后 **`not isinstance(exc, Exception)` 原样 raise**；普通 `Exception` 记录并继续角色转发
- 行为测试：`OwnedThreadTask.start` patch 触发真实 enqueue bootstrap 失败（非 claim mock）
- 未跑 raw discover / Root probe

## 未做

提交/推送、正式 memory、真实模型/设备；Root 独立 probe 与下一性能切片。
