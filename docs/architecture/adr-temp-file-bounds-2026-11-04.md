# ADR：临时文件（TFM）有界 owned 与单 Worker 生命周期

## 状态

Accepted（2026-11-04，Root完成最终源码评审、17项独立边界+6项持久IO断言及175项相关回归，见docs/codex-review-temp-file-bounds-2026-11-04.md顶部）

## 背景

owned 登记无硬上限、pin 期间 remove 丢意图、release 不推进删除、cleanup worker 在 start 异常/迟启时可能重叠，且 registry 锁内嵌套 worker 锁导致必然死锁。

## 决策

1. **owned_capacity**（默认 128，严格 int 1..1024）：在 mkdir/touch 前于 registry 锁内准入；满则 `OwnedCapacityExceededError`，不创建、不 adopt 外部文件；同一有效身份重复 adopt 不耗名额。
2. **pin 待删**：remove/remove_delayed 在 pin 时写入 `_pending_cleanup`（规模不超过 owned 子集）；末次 lease 释放后在 registry 锁内入队并 signal；**不**在 release 调用 `_ensure_worker` / Thread.start。
3. **有界队列 + 去重**：…
4. **删除 claim**：…；**pending 不变量**：…。**过期队列出队**：dequeue 后先校验队列项 identity 与当前 owned；不匹配则仅 `_clear_pending_for_obsolete_queue_item_locked` + `task_done`，**continue** worker 循环，不得 `break` 终止 worker或无条件 `pop` 同 key 上新身份 pending。合法 `remove` 已接受且身份仍 owned 时，永久 `OSError` / 未预期 `BaseException` / worker 取消或重试耗尽须 `_record_failed_delete_intent_locked`（`pending_cleanup_count` 含该意图，`max_retries=0` + `_retry_exhausted` 禁止 pump 自动重试）；成功删除或 `_commit_delete_if_identity_locked` 清 pending 与 exhausted；显式 `remove`/`shutdown` 可 `discard` exhausted 后再推进。
5. **单 Worker owner**：…
6. **关闭**：…

## 取舍

- 后台 `os.remove` 在 registry 锁外执行；持 claim 期间其它线程仍可读状态，shutdown 预算内可返回 `complete=False` 而不等待 IO。
- 重试耗尽后保留 owned + pending 意图，避免无限忙循环；需再次 remove 或 shutdown 清理。
- 未知 bootstrap 在迟启 resolve 前 `complete=False`，避免假 quiet。

## 验证（fixture / fake）

- 仓库：`tests.test_tfm_bounds`（27，含 `StaleQueueWorkerTests`、失败删除 pending、GhostPending）
- **过期队列出队（本轮）**：…；**queued 标记**：仅身份匹配的合法出队才 `_queued_paths.discard`；过期项不得动同路径新身份的 queued/inflight/claim（`processing_item` 守卫 finally）。
  - 闭合 queued 顺序：`tmp/codex_gate_tfm_stale_queued_discard_{root,repo}_20261104o_2026_11_02.json`
  -  prior：`tmp/codex_gate_tfm_stale_queue_root_20261104n_2026_11_02.json` 等
- **失败删除 pending 可见性**：
  - `tmp/codex_gate_tfm_pending_visibility_errors_20261104k_2026_11_02.json`（Root 加强 delete-errors 2 项）
  - `tmp/codex_gate_tfm_pending_visibility_repo_20261104k_2026_11_02.json`
  - `tmp/codex_gate_tfm_pending_visibility_root9_20261104k_2026_11_02.json`
  - `tmp/codex_gate_tfm_pending_visibility_claims5_20261104k_2026_11_02.json`
  - 回归：`tmp/codex_gate_tfm_pending_vis_regress_storage_20261104k_2026_11_02.json`（45 OK）；`tmp/codex_gate_tfm_pending_vis_regress_pio_mod_20261104k_2026_11_02.json`（14 OK）；`tmp/codex_gate_tfm_pending_vis_regress_cosy_20261104k_2026_11_02.json`（65 OK）
  - Root persistent boundary probe 本机 `host_memory_commit_insufficient` 未跑通：`tmp/codex_gate_tfm_pending_vis_regress_pio_b_20261104k_2026_11_02.json`（exit 1）；`sec3a` 同因 1 FAIL：`tmp/codex_gate_tfm_pending_vis_regress_sec3a_20261104k_2026_11_02.json` — 与 TFM 改动无关，Root 可在资源充足环境复跑
- 历史幽灵 pending：`tmp/codex_gate_tfm_ghost_pending_root9_20261104h_2026_11_02.json`、`tmp/codex_gate_tfm_ghost_pending_repo_20261104h2_2026_11_02.json`
- Root 加强前失败对照：`tmp/codex_gate_tfm_pending_error_visibility_root_20261104k_2026_11_02.json`（3 FAIL，已闭合）
