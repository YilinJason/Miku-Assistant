# ADR：Turn stream 预取控制流保行为精简

## 状态

Accepted（Root源码、独立19/19、相关146/146；docs/codex-review-prefetch-simplify-2026-11-04.md）

## 背景

默认 stream 跨句预取已在 `docs/architecture/adr-turn-audio-prefetch-2026-11-04.md` 验收。本切片仅删除经静态与现有 gate 可证明的冗余状态与重复异常路径，不调整单线程/future1、Edge6/Cosy4 上限、EOF/credit/取消/IO pin/epoch/TFM pending 门槛。

切片前只读快照：`tmp/prefetch-simplify-root-before-20261104/`（比较用，不可覆盖）。

## 决策

| 删除项 | 理由 |
| --- | --- |
| `_owner_start_attempted` 槽与写入 | 只写无读；`finish()` 仍对 `_owner.join` 实际执行 |
| `_producer_cancelled` | 仓库内无调用；取消仍经 `_MergedSessionCancel` / `prepare_cancel_view` |
| `_cleanup_blocked` | `paths_released` 终判仅依赖 `held_paths` 与 TFM pending/busy；`is_owned` 复核与 reconcile 不变 |
| producer 内层 `next_work` / `_prepare` / `_track_paths` 的 try→`_fail_producer`→return | 与外层 `except BaseException` 等价；仍 `release_credit=True`、EOF 在 `_fail_producer` 后由 finally 置位 |
| `TTSEngine.speak_stream` 中 `except RuntimeError: raise` | 无变换重抛，直接传播 |

未改：`OwnedThreadTask`、TFM、RVC、Cosy backend、权限/模型/音色配置；未删公开 API 或 Root 探针。

## 验证

- Scoped gate label `prefetch_simplify_cursor_20261104a`（146 tests，~10s，WAV 826 零差）→ `tmp/codex_gate_prefetch_simplify_cursor_20261104a_2026_11_02.json`
- `py -3.12 -m compileall`：`turn_stream_prefetch.py`、`engine.py`
- AST：`TurnStreamPrefetchSession` 仅移除未使用的私有 `_producer_cancelled`；`TTSEngine` 类集合无变
- `git diff --check` 上述两文件退出 0
- `turn_stream_prefetch.py` 净减约 **32 行**（700→668）

## 未做

真实设备/模型长测、性能切片、提交/推送。Root19 与相关146已由Root独立完成，WAV826零差。
