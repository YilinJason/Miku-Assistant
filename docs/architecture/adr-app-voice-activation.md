# ADR: App 语音加载所有权与 prepare 接线

**状态**: STOP — 561 里程碑定点（拿票前 `_fail_block` 已删）；supervisor 三模块 + 两 probe 待本轮复跑  
**前置**: 持久 RVC IO lease Root PASS

## 决策

1. **唯一同步事务**：`src/core/voice_load_lane.ensure_voice_transaction`；公开 `ensure_tts_for_playback` / `ensure_voice_stack` 仅为窄 wrapper。
2. **单 owner 槽**：`MikuAssistant._voice_owner_slot`（构造前登记空 `UnpublishedVoice`，build 填同一容器；成功 publish 清空，incomplete 保留至 strict retire）。
3. **generation lane**：`_voice_lane_ticket` / `_voice_lane_holder` + `_voice_loading`；仅持票 generation 在 `finally` 释放；pending retire / explicit retry / shutdown retire **均经 lane**。
4. **锁规则**：lifecycle 仅 snapshot/publish；retire/prepare/build **锁外**；禁止 `_park` 第二候选。
5. **shutdown**：`_run_step(voice_owner.retire)` → `shutdown_retire_owner_in_lane`（lane 忙则保留；异常不中断 memory/cleaner/character）；owner/lane 未知则跳过 TFM。
6. **RVC require**：`tts._rvc.enabled`。
7. **bound prepare 后继续 STT**：prepare 成功不等于栈完成；缺 STT 继续 build/publish。
8. **finally 集中 retire**：未 `published_moved` 的 owner 仅在 finally 锁外 retire；cleanup 异常不覆盖原 KBI/SystemExit。
9. **shutdown lane**：`voice_loading` 时 retire 直接返回，不自造 ticket 覆盖未知 inflight。
10. **原子 publish + sticky**：`_atomic_publish_owner`；**仅拿票后** `_fail_block`（无拿票前 owner/failed 预检，并发经 lane 等待；shutdown 唤醒返回 closing）。

## 锁顺序（摘要）

| 阶段 | 锁 | 动作 |
| --- | --- | --- |
| 快路径 ready | lifecycle 短读 | 可跳过 lane |
| 占 lane | loader + Condition | generation 票据 |
| 快照 | lifecycle 短读 | closing/failed/owner/bound |
| pending retire | **无** | `try_retire_unpublished_voice` |
| build/prepare | **无** | assistant.build_* + warmup |
| publish | lifecycle 短写 | 赋值引擎 / 清 slot |
| 失败未发布 | **无** | retire；incomplete 留 slot |
| 释 lane | loader | 仅匹配 holder ticket |

## 代码

- `src/core/voice_load_lane.py`
- `src/core/voice_activation.py`
- `src/core/assistant.py`（字段 + shutdown + wrapper）
- `tests/test_app_voice_activation.py`
- `tmp/codex_app_voice_activation_supervisor_2026_11_03.py`（60s unittest 包装，**禁止裸跑**）

## 未做

- 非阻塞 ensure、main/真实模型/预取/提交
