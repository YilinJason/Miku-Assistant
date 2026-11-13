# 交接：非流回复有界播报（Cursor → Root）

## 目标

非流/非阻塞流式 side effect 复用唯一 `TurnSpeechPlayer`，消除逐回复 daemon 线程与 `Thread.start` 失败时的同步 speak 兜底；保留工作区其它 dirty。

## 生产改动

| 文件 | 内容 |
| --- | --- |
| `src/core/assistant.py` | `_apply_outcome_side_effects` 的 `use_legacy_thread=True` 改为 claim → enqueue → mark_production_finished |

## 测试 / 文档

| 文件 | 内容 |
| --- | --- |
| `tests/test_assistant_orchestration.py` | 移除 Thread/sync-fallback 断言；新增 `LegacyBoundPlaybackTests` |
| `tests/test_natural_companion_round2.py` | 投影入口改经 player + turn；移除 Thread 兜底用例 |
| `docs/architecture/adr-legacy-playback-bound-2026-11-04.md` | Proposed ADR |

## 验证（Cursor）

| 项 | 结果 |
| --- | --- |
| Gate `legacy_playback_bound_cursor_20261104c`（窄修 c） | **162 OK**，~7.1s，WAV **826→826** |
| 报告 | `tmp/codex_gate_legacy_playback_bound_cursor_20261104c_2026_11_02.json` |
| 窄修 c | `isinstance(exc, Exception)` 分界；teardown 去 wait_for_turn；owner.snapshot().quiet |
| compileall | `assistant.py` + 相关 tests OK |
| 快照 | 未改 `tmp/legacy-playback-root-before-20261104/` |

未跑 raw unittest/discover；未运行或修改 Root probe。

## Root 待办

- 对快照 diff `_apply_outcome_side_effects` 与关闭/取消契约复审
- 独立 probe（若任务清单要求）后再标 ADR Accepted
- **STOP**：不进 RVC/清理/LLM 冷载等下一性能切片

## STOP

未提交、未推送；未触正式 `data/memory`、真实模型/设备。
