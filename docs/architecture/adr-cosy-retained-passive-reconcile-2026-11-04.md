# ADR：CosyVoice retained 被动对账（TFM 已释放 / backend 仍计数）

## 状态

Accepted（Root独立5/5、相关107/107及最终模块14/14，WAV826零差；docs/codex-review-cosy-retained-passive-2026-11-04.md）

## 背景

`TTSEngine._assert_turn_stream_admission_resources` 在 `retained_output_count() > 0` 时拒绝 turn stream 工作，而 `CosyVoiceTtsBackend._release_retained_outputs` 仅在 `_gate_synth_allocation` 等路径运行。若 TFM 已在后台完成 owned 释放（文件已删或 revoke），backend 的 `_retained_outputs` 仍可能滞留，导致 speak/save 在到达 backend gate 前永久 busy。

只读快照（Root 比较用，不可覆盖）：`tmp/cosy-retained-root-before-20261104/`。

## 决策

1. **`CosyVoiceTtsBackend.reconcile_released_outputs()`**（public、被动）
   - 在 `_lifecycle_lock` 内快照 `_retained_outputs` 与 `_retained_revision`。
   - **不持 lifecycle 锁** 对每个快照路径调用 `TempFileManager.is_owned`（只读）；**不**调用 `remove`、retire、prepare、合成或 IO lease。
   - 收集已不再 owned 的路径；二次加锁时若 `revision` 已变则**放弃本轮删除**（保守，防并发新增被误清）。
   - `is_owned` 抛出的 `BaseException` **原样传播**；未提交删除前 retained 集合不变。
   - 不修改 `_pending_worker_retire` 或 `client.stop_failed`；实际合成仍走原 `_gate_synth_allocation`。

2. **`_retained_revision`**：凡向 `_retained_outputs` 的 add/update/`difference_update`（含 `_release_retained_outputs` 与被动对账成功删 stale）均递增。

3. **`TTSEngine._assert_turn_stream_admission_resources`**：TFM `pending` 门槛不变且仍最先；其后对 `_cosy_backend` 用 `getattr(..., "reconcile_released_outputs", None)`，callable 则调用；再检查 `retained_output_count()`。不调用 `_gate_synth_allocation`（含 2s worker retire）。

## 锁序证明（本切片选用）

- 与既有 `_release_retained_outputs` 一致：**快照/写回**持 `_lifecycle_lock`；**复核**在锁外调用 `TFM.is_owned`（TFM 短锁、无回调进 backend lifecycle）。
- 未在 lifecycle 锁内调用 `remove` 或 retire，避免与 TFM 删除锁序扩大。

## 替代方案（未采用）

- 在 admission 内直接调用 `_gate_synth_allocation`：会触发 worker retire 与 `remove`，超出被动对账边界。
- 被动对账异常时假设 retained 已清空：违反「异常原样传播」。
- 无 revision 的全量 prune：并发 `_mark_failed_output` 可能误删较新条目。

## 验证（Cursor，未跑 gate）

- 新增 `tests/test_cosyvoice_retained_passive_reconcile.py`（纯 fake ownership manager/client，无真实 TFM/文件；Root 建议与 C1/C2B2A/预取/回滚组同 scoped supervisor 串联）。
- `py -3.12 -m compileall`：`cosyvoice_backend.py`、`engine.py`、新测试模块。
- `git diff --check` 上述生产文件。

## 未做

Root probe / 监督器 / 覆盖报告；提交推送；配置、TFM 容量、LLM、正式 memory、真实模型设备。
