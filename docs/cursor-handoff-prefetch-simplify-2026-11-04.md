# 交接：预取控制流保行为精简（Cursor → Root）

## 目标

在已验收 turn stream 预取语义不变前提下，精简 `turn_stream_prefetch.py` 与 `engine.py` 中可证明冗余的控制流；保留工作区全部其它 dirty。

## 生产改动

| 文件 | 内容 |
| --- | --- |
| `src/voice/tts/turn_stream_prefetch.py` | 移除 `_owner_start_attempted`、`_cleanup_blocked`、`_producer_cancelled`；producer 内层重复 fail 归并外层；`paths_released` / held  reconcile 行为不变 |
| `src/voice/tts/engine.py` | `speak_stream` 去掉无意义 `except RuntimeError: raise` |

## 删去的冗余责任（非格式压缩）

1. **只写 flag**：`_owner_start_attempted` 从未参与分支；join 仍在 `finish()`。
2. **死代码**：`_producer_cancelled` 无调用方。
3. **重复门槛**：`_cleanup_blocked and held` 与 `len(held)==0` 在现有赋值规则下等价。
4. **三层相同 handler**：producer 对 plan/prepare/track 的内层 except 与外层 `_fail_producer(..., release_credit=True)` 重复。

## 验证（Cursor）

| 项 | 结果 |
| --- | --- |
| Gate `prefetch_simplify_cursor_20261104a` | **146 OK**，~10.0s，WAV **826→826** |
| 报告 | `tmp/codex_gate_prefetch_simplify_cursor_20261104a_2026_11_02.json` |
| compileall | `turn_stream_prefetch.py`、`engine.py` OK |
| diffcheck | 两文件 OK |
| 快照 | 未改 `tmp/prefetch-simplify-root-before-20261104/` |

模块顺序：`test_turn_audio_prefetch` → `test_owned_thread` → `test_speech_queue_lifecycle` → `test_stream_playback` → `test_voice_session` → `test_audio_preparation_rollback` → `test_cosyvoice_production_slice_c1`。

未跑 raw unittest/discover；未改 Root 探针/监督器。

## Root 待办

- 源码 diff 对快照 AST/边界复审
- 独立复跑 Root19 保行为矩阵及相关组
- 通过后可将 ADR 标 Accepted；**不进最终性能切片**

## STOP

未提交、未推送；未触正式 `data/memory`、真实模型/设备/配置。
