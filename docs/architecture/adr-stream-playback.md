# ADR：CLI 可选增量显示与分句播报

- **状态**：已采纳（里程碑；round-4 归属临界区与展示阶段修复后）
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步

## 问题

用户希望在模型仍在生成时看到增量文本，并在首个完整句子就绪后开始 TTS。须保留非流式入口，且取消/轮次/展示语义不得误伤新轮或误标生成失败。

## 决策

1. **非流式**兼容；关闭后拒绝新任务。
2. **流式**：入口拒取消/关闭；单次 handler；手动 `next` 前/回调后检查取消。
3. **轮次归属**：`_begin_generation_turn` 与播报 CAS 同在 `_turn_lock`；仅 `compare_and_invalidate(prev)`，**CAS 失败不得 fallback invalidate**。`_claim_speech_for_turn` 确认所有者后才 `begin_turn`。
4. **`ReplyStage`**：与 `allow_tts` 独立。CLI 读 `stage`：`PLAYBACK_FAILED` → 短提示不重复全文；`GENERATION_FAILED` → `[生成失败]`；不得用 TTS 许可猜测生成成败。
5. **历史**：仅客户端成功 COMPLETED 提交；取消未拉取 COMPLETED → 不变；已提交后播放失败 → `playback_failed(全文)`。
6. 分句 / 背压 / 过期 speak 隔离：见既有契约。

## 限制

- 阻塞网络读取期间取消无法立即打断。
- 底层硬打断与真机未测；STT 独立修改不在本里程碑。

## 离线验证

见 handoff：最近 **120 tests OK**（含 B/C 入口交错与 CLI 播报失败展示）。
