# ADR：自然陪伴 Round 1——统一人格提示边界

- **状态**：已接受；Codex Round 1 F1–F2 返修及独立全量复验通过
- **日期**：2026-09-30
- **任务**：`docs/cursor-task-natural-companion-round1.md`
- **评审**：`docs/codex-review-natural-companion-round1.md`

## 问题

主对话缺少稳定、可验证的陪伴人格层：`LLMIntentHandler` 与流式路径以 `system=None` 调用模型，仅在长期记忆命中时由 `MemoryAwareLLMClient` 临时注入记忆 system。文本/语音入口与流式/非流式之间缺乏同一基础人格契约。

## 决策

1. **本地固定模板**：`src/core/companion.py` 纯解析 + 仓库审查过的 `warm_balanced` 正文；无 I/O、无线程、零额外模型调用。配置仅选 profile，禁止 `custom_prompt` 与从用户/记忆正文生成规则。
2. **严格配置**：`assistant.companion.enabled` 必须为 bool；`profile` 非空且当前仅 `warm_balanced`；未知值 fail closed。基础提示硬上限 2400 Unicode 字符，超限在装配副作用前失败。
3. **唯一合成边界**：`OllamaClient.base_system` 在 `_build_chat_messages()` 与调用方动态 system 合成恰好一条 `role=system`（基础在前，固定分隔 + 动态在后）。`None`/空保持原兼容。流式与非流式共用该边界；禁止 `/api/chat` 顶层 `system`。
4. **动态分隔语义（F1）**：分隔说明附加 system 与基础人格共同生效；仅其中**明确标为数据**的正文不可当作指令。不得把整个动态 system 降级为“不是可执行指令”。记忆 JSON 的不可执行数据边界仍由记忆层文案自行标明。
5. **构造期规范化（F2）**：`normalize_base_system` 在 `OllamaClient` 构造阶段执行：`None`/`""` 关闭；其余必须为 `str` 且 ≤2400；拒绝 bool/bytes/容器/超长；错误不含提示正文。不约束每请求动态 `system`。
6. **装配接线**：与 memory/resource 一样先解析 companion，再创建 HTTP/设备/线程；仅当构造器接受 `base_system` 时传入。
7. **记忆兼容**：记忆 JSON 与「不可执行数据」规则仍由 `MemoryAwareLLMClient` 作为动态 system 注入；与基础人格共存时原样保留一次。关闭 companion 时记忆行为不变；关闭 memory 时仍可有基础人格。

## 非目标

- Round 2+（情绪分类、动态改写、额外 LLM 调用、会话状态、温度/`num_ctx`/历史窗口/TTS/RVC/安全路由变更）
- 真实模型自然度验收

## 后果

- 普通对话获得确定性本地人格边界；不覆盖 `SafetyPolicy`。
- 诊断/测试传入的可信动态 system 规则不再被通用分隔语义否定。
- 离线提示契约通过 ≠ 真实模型语气已验收。
- 不新增日志正文（提示/用户/记忆正文）。
