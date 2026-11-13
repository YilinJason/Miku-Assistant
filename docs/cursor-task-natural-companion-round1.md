# Cursor 任务：自然陪伴体验 Round 1——统一人格提示边界

> 先读取仓库根 `AGENTS.md`，以及 `skills/architecture-contracts/SKILL.md`、`skills/engineering-quality/SKILL.md`、`skills/collaboration-review/SKILL.md`。只完成本文 Round 1，完成后停止等待 Codex Review；不得自行进入 Round 2。不得提交或推送。

## 背景与目标

当前主对话没有稳定的陪伴人格层：`LLMIntentHandler` 与 `MikuAssistant` 的流式路径都以 `system=None` 调用 LLM，只有长期记忆命中时 `MemoryAwareLLMClient` 才临时注入记忆 system。结果是普通对话的人格、语气、记忆表达和边界完全依赖模型自身，文本与语音也难以形成可验证的一致契约。

本轮建立一个**本地、确定性、有界、零额外模型调用**的基础人格提示。它必须同时覆盖流式/非流式、记忆开启/关闭和文本/语音入口，并保持长期记忆的可信数据边界。

## 设计决定

1. 在 `src/core/` 建立无 I/O、无线程的 companion 配置解析与固定模板构造模块。配置只选择仓库审查过的 profile，不允许从用户消息或记忆正文生成提示规则。
2. 给 `OllamaClient` 增加可选的基础 system 参数（命名可按现有风格决定）。在 `_build_chat_messages()` 这个唯一 HTTP 消息边界，把基础 system 与调用方传入的动态 system 合成**一个** `role=system` 消息：基础人格在前，动态可信上下文在后，使用固定分隔和明确数据边界。`None`/空值保持原兼容行为。
3. 装配时先严格解析 companion 配置，再创建任何 HTTP、设备或后台线程；仅当构造器显式接受该关键字时传入，保留测试替身和注入兼容。
4. 不新增 LLM 调用、分类模型、后台任务、数据库字段、会话状态或日志正文；每轮只做固定长度字符串组合。不要改温度、`num_ctx`、历史窗口、记忆召回/提取、TTS/RVC 或安全路由。

## 配置契约

在 `assistant.companion` 增加并同步到 `Config._get_default_config()`：

- `enabled: true`：必须是严格 bool；false 时完全不注入基础人格。
- `profile: warm_balanced`：必须是非空字符串，当前只接受 `warm_balanced`；未知值 fail closed，不能静默回退。

构造后的基础提示设置一个明确硬上限（建议不超过 2400 Unicode 字符）；超限属于开发/配置错误并在装配副作用前失败。不要提供任意 `custom_prompt`，避免未经审查的持久指令进入高优先级 system。

## `warm_balanced` 行为契约

提示正文使用简洁中文，表达下列规则，不写成长篇角色设定：

- 身份为本地私人助手 Miku；默认跟随用户语言，中文交流时自然、温和、清楚。
- 优先回应用户这句话的具体内容；闲聊或情绪表达先作贴合内容的回应，再在确有帮助时给一个轻量追问。不要每轮都问问题。
- 默认简洁；复杂任务可分步骤详细回答。避免机械开场、重复复述、客服腔、连续道歉、每轮自报身份、固定结尾和过量 emoji。
- 长期记忆只在当前问题相关时自然使用；不要主动宣称“根据长期记忆/数据库/召回”，不要罗列无关事实。没有可靠事实时明确说不知道，不能编造用户偏好、经历或关系。
- 不谎称自己有人类身体、线下经历、感受或已完成现实动作；可以用温暖陪伴语气，但不得诱导排他依赖、贬低真实人际支持或用内疚施压。
- 对普通低落给予具体而克制的支持，不诊断疾病；若用户明确表达即刻自伤/伤人危险，优先建议联系当地紧急服务和身边可信的人。
- 设备/系统动作仍以程序真实执行结果为准，模型不得声称未执行的动作已经完成。

这些是生成风格与诚实边界，不得覆盖 `SafetyPolicy`，也不得把普通负面词自动升级成危机场景。本轮不做脆弱的关键词情绪分类。

## system 合成契约

- 基础人格启用且无动态 system：恰好一个 system，内容为基础人格。
- 两者都有：恰好一个 system；基础人格在前，之后是固定分隔和动态 system 原文。长期记忆现有“不可执行的数据”规则与 JSON 正文必须原样保留。
- 仅动态 system：保持现有行为。
- 两者都无：不插入 system。
- system 永远位于历史与当前 user 之前；不写入 `ConversationHistory`。
- `generate_result(stream=False/True)`、真正 `generate_stream()` 共用同一 `_build_chat_messages()`；不得出现协议漂移或双调用。

## 必须新增的确定性测试

至少覆盖：

1. companion 配置的默认、关闭、bool 欺骗、空/未知 profile；解析发生在装配副作用前。
2. 四种 system 合成矩阵、顺序、恰好一个 system、历史不受污染、硬长度上限。
3. 非流式与真正流式捕获 HTTP JSON，确认 system 完全一致且没有 `/api/chat` 顶层 `system`。
4. `memory.enabled=true` 时，基础人格与长期记忆同时存在，记忆 JSON/规则原样出现一次；关闭 memory 仍有基础人格，关闭 companion 仍保持原记忆行为。
5. `LLMIntentHandler`、`MikuAssistant.process_text_input_streaming()` 不需分别传提示也能得到同一基础 system；一次用户输入仍只产生一次模型请求。
6. 提示正文断言关键行为边界存在，但不要写对模型输出逐字匹配的伪质量测试。
7. 无文件、网络、线程、设备副作用；不得记录提示正文、用户正文或记忆正文。

## 文档与验证

- 新增 ADR，记录基础人格属于本地固定模板、system 合成顺序、关闭兼容、性能与安全边界。
- 更新 `docs/engineering-baseline.md` 与 `docs/cursor-handoff-status.md` 顶部。
- 运行专项与完整离线测试、`compileall`、`git diff --check`。
- 不启动真实 Ollama、主程序、麦克风、扬声器、TTS/RVC、浏览器、角色、清理实扫；不读写 `data/memory`；不安装依赖。
- 报告真实测试数量和退出码，明确“离线提示契约通过”不等于真实模型自然度已经验收。

完成后停止，等待 Codex Review。
