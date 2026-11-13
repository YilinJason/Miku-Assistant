# ADR：自然陪伴 Round 2——显示原文 / 播报投影双通道

- **状态**：已接受；Codex F1–F8 已关闭，最终离线复验通过
- **日期**：2026-09-30
- **任务**：`docs/cursor-task-natural-companion-round2.md`
- **评审**：`docs/codex-review-natural-companion-round2.md`

## 问题

屏幕、历史与记忆应保留模型原文，但把同一原文送入 TTS 会朗读 URL、代码围栏、Markdown 标记，并可能把 `(API)` 等正常括号误删为颜文字。流式多段 `speak` 无状态时，跨 chunk 的 fenced code / URL 会泄漏；助手非流式入口可能把原文交给注入式 TTS。跨 chunk 对 hold 从头重扫会呈 O(n²) 卡顿；固定提示与源句读可能叠成双标点。

## 决策

1. **双通道**：`DispatchOutcome.text` / `on_delta` / 历史 / 记忆 / 角色消息保持原文；仅真正送入 TTS 的文本经本地 speech projection。
2. **单一实现**：`src/voice/speech_projection.py`——线性有界扫描（平衡括号 Markdown 目标、长 URL 整段吞掉不回吐）+ 每轮 `SpeechProjector`（跨 chunk hold：URL / `![` / 链接 / inline code / 围栏）。
3. **接线**：流式入队前投影；`_apply_outcome_side_effects` / `speak` / tracked 在调用 TTS 前投影；空投影不建线程、不调用 TTS、不增加 accepted。`TTSEngine.speak` 末端仍幂等投影。
4. **隐私**：未闭合目标在 `finish()` 丢弃 URL 正文；假 TTS 参数中不得出现 URL/凭据/sentinel。
5. **有界 / fail-closed（F4）**：输入与 hold 缓冲硬上限对齐（默认均为 16000）；先投影再判定溢出；超限进入 quarantine，整段丢弃直至可信终止符，**禁止**截掉前缀后重解析未知尾部。
6. **scheme / Markdown 目标（F5）**：`http(s)://` 识别大小写不敏感（含跨 chunk 前缀）；angle destination 消费完整外层 `)` 与可选 `"..."/'...'/(...)` title；title 内 `)` 不提前结束。
7. **裸 URL 自然断句（F6）**：常见中文句读与成对结束符终止裸 URL 并保留为正文；Markdown 目标扫描不套用该规则。
8. **增量线性（F7）**：hold 维护明确 `token kind` + `progress`；已确认裸 URL / Markdown dest / inline code 时只扫描新增字符，不得每次从 scheme 重扫。`scan_steps` 可断言 1k→8k 线性有界；墙钟仅作宽松防死循环。
9. **提示句读（F8）**：`LINK_TIP` / `IMAGE_TIP` / `CODE_TIP` 与紧接源句读合并为单一自然句读；问号/叹号优先；普通句号不重复；不全局改写正文。

## 非目标

- 通用 Markdown 渲染器；真实语音自然度验收；改变 `TurnSpeechPlayer` 背压/取消/shutdown。

## 后果

- 嵌套括号与长 URL、chunk 边界、hold 溢出 quarantine、大小写 scheme、angle/title、中文句读断 URL、增量线性、提示句读合并、助手全入口投影契约可离线验证。
- 离线投影契约通过 ≠ 真实播报自然度已验收。
