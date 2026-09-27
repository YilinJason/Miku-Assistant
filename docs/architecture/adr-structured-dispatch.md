# ADR：消费结构化生成结果（Handler / 路由 / 助手）

- **状态**：已采纳（第七小步）
- **日期**：与 `docs/engineering-baseline.md` 本轮更新同步
- **关联**：`adr-generate-result.md`（第六小步提供 `GenerateResult`）

## 问题

第六小步已能区分模型成功与调用失败，但 Handler / 路由 / 助手仍只消费 `generate() -> str`，错误提示仍会被 TTS 播报并转发给角色。

## 决策

1. 新增 `src/core/dispatch_outcome.py`：`DispatchOutcome(text, allow_tts, allow_character)`。
2. `IntentHandler.handle_outcome`：默认将字符串 `handle` 结果包装为**原副作用语义**（系统/搜索本轮不重新分类）。
3. `LLMIntentHandler` 覆盖 `handle_outcome`：调用一次 `generate_result`（或兼容 `generate`）；`handle` 委托该入口，避免重复请求。
4. `IntentRouter.dispatch_outcome` 为结构化分发；`dispatch` 委托它。
5. `MikuAssistant.process_text_input` 按 `allow_tts` / `allow_character` 决定是否起播报线程与 `send_message`；返回值仍为展示字符串。
6. 映射规则（**不用**「错误:」前缀判断）：
   - `SUCCESS`（含模型正文以「错误:」开头）→ 展示原文，允许 TTS + 角色
   - `EMPTY` → 展示 `""`，禁止 TTS/角色
   - 失败类 → 展示 `to_legacy_string()`，禁止 TTS/角色
7. 不修改 Ollama 协议、历史提交、流式能力、播报线程模型；不改系统/搜索处理器逻辑。

## 工作区说明

`src/voice/stt.py` 的未提交修改来自**独立任务**（语音模式识别失败静默吞掉的修复），**本轮保留且不纳入实现或验收范围**。

## 明确未解决

- 取消/turn_id、端到端流式、错误文案产品化仍未做。
- 仅有 `generate()` 的替身客户端无法区分「连接失败返回的错误串」与「成功台词」；生产 `OllamaClient` 走 `generate_result`。

## 替代方案

- 在助手内用「错误:」前缀过滤：与模型台词冲突，拒绝。
- 立刻改 CLI 专用 API：本轮保持 `process_text_input -> str` 即可。

## 离线验证

```text
py -3 -m unittest discover -s tests -v
→ Ran 74 tests in 0.033s … OK
```

见 `tests/test_structured_dispatch.py`；原有测试继续通过。
