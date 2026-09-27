# Codex 独立评审：语音流式轮流说话（Round 4）

## 结论

Round 3 代码修复通过独立审查：无终结事件耗尽会按生成失败收口，部分播报后不会重开 STT，无 TTS 时可安全恢复；136 项纯离线测试实际通过。当前只剩工程基线中的旧措辞与旧测试数字，修正文档后即可验收本里程碑。

## 已独立验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
Ran 136 tests in 4.403s
OK
```

另单独重跑 `tests.test_voice_session`：16 项通过。全部使用 fake；未启动真实麦克风、扬声器、RVC、Ollama、浏览器或系统操作。`git diff --check` 通过，仅有 Git 的 LF/CRLF 提示。

## 只需修正文档

修改 `docs/engineering-baseline.md`，不要改业务代码或测试：

1. 第 7 行仍写“`不解决播报取消或端到端流式`”，与 6.9–6.10 已完成能力冲突。改成明确的历史范围说明，例如“审计起点尚未解决；后续已由 6.9–6.10 渐进补齐应用层取消、增量显示/分句播报与语音轮流说话契约；底层硬打断仍未实现”。
2. “7. 已验证”仍记录 `Ran 128 tests in 2.989s`。更新为本轮实际 Python 3.12 命令及 `Ran 136 tests in 4.403s / OK`，并说明包含无终结事件耗尽、无 TTS 对照和 Event 驱动的取消/队列清理测试。
3. “8. 阶段结论”仍写“128 项 unittest”与仅 `round-1`。更新为 136 项，并准确覆盖语音 Round 1–3 修复；继续保留“不能视为真实音频链路验收”的限制。
4. 更新 `docs/cursor-handoff-status.md` 为“文档收尾完成，等待 Codex 最终验收”，记录实际命令、136 项和明确未验证项。

保持 README/ADR 当前准确限制，不把真实设备、底层硬打断、回声消除或 barge-in 写成已完成。

## 收尾结果

Cursor 原生窗口在本轮最终收尾时未被桌面控制接口暴露；Codex 因此直接完成上述三处纯文档修正，没有修改业务代码或测试。修正后差异检查通过，本里程碑完成最终离线验收。

## 约束

只修改上述文档；保留全部既有未提交修改、STT 与 RVC Worker。不要提交、推送，也不要启动真实设备、RVC、Ollama、浏览器或系统操作。完成后停止修改等待 Codex 最终验收。
