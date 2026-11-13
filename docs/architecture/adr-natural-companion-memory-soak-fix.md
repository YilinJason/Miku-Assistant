# ADR：真实语音 soak 返修——记住前缀、单字事实与失败关闭召回

- **状态**：Codex 最终复验通过（PASS）
- **日期**：2026-09-30
- **任务**：`docs/cursor-task-natural-companion-memory-soak-fix.md`
- **评审**：`docs/codex-review-natural-companion-memory-soak-fix.md`

## 问题

真实语音验收中，用户说「请记住我喜欢香草甜筒」后重启再问冰激凌偏好，仍召回旧的抹茶答案。Codex 独立复核另发现：合法单字中文值被当成占位、弱领域查询跨对象召回全部偏好、`skip_reason` 可把任意正文写入日志。

## 决策

1. **F1 单字与记住前缀**：`_value_tokens` / CJK run 允许单字值（茶/猫/狗）；占位与标签值（什么/饮品）继续拒绝。白名单句首「记住」前缀与槽位句式不变。领域词允许作 slot 名（`我喜欢的饮品是…`），仍禁止作 value。
2. **F2 失败关闭召回**：查询派生 `generic` / `concrete` / `domain slot`。仅真正无对象的「我喜欢什么」走有界偏好 fallback；单字是具体内容（茶≠抹茶内嵌）；弱领域查询只按规范事实 slot 命中，无 slot 且无具体交集则空。不引入「拿铁=饮品」词表。
3. **F3 skip_reason allowlist**：ingestion 仅记录 `no_safe_fact_unit` / `eligibility_skip`；未知或非字符串映射为 `unknown`，禁止对任意对象 `str()`。

## 非目标

- 不读写真实 `data/memory`；不做模糊语义冲突覆盖；不放宽防污染边界；不改队列/关闭/retention/网络/TTS。

## 后果

- 单字偏好可写入；领域问句不再污染跨对象偏好；日志原因码不可夹带秘密正文。
- 离线契约通过 ≠ 真实语音 soak 已复验通过。
