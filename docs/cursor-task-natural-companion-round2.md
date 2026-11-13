# Cursor 任务：自然陪伴体验 Round 2——自然且隐私友好的语音投影

> 先读取根 `AGENTS.md`，以及 `skills/safe-refactoring/SKILL.md`、`skills/architecture-contracts/SKILL.md`、`skills/engineering-quality/SKILL.md`、`skills/collaboration-review/SKILL.md`。Round 1 已由 Codex 最终复验通过。只完成本文 Round 2，完成后停止等待 Codex Review；不得提交或推送。

## 问题与目标

显示文本、会话历史和长期记忆应保留模型原文，但朗读同一原文会产生明显不自然和隐私问题：当前 `sanitize_for_speech()` 只去 emoji、星号和一个过宽的“颜文字”正则。

- Markdown 链接会把 URL（含路径/query）送到云 TTS 并逐字符朗读；代码块、围栏、标题、列表符号也会被朗读。
- `_looks_like_kaomoji()` 把 `(API)`、`(RVC)`、`(Python 3.12)` 等正常括号内容误判为颜文字并删除。
- 流式回复以多个 `tts.speak()` 调用提交；无状态清理无法识别跨 chunk 的 fenced code，可能继续朗读代码正文。
- 流式句子在进入队列后才由 TTS 清理；若 emoji/标记清理后为空，助手仍可能把本轮标成“已开始播报”，影响取消与恢复监听的状态判断。

本轮新增一个**纯本地、确定性、有界、可增量**的 speech projection。屏幕显示、模型原文、历史、记忆提取和角色扩展消息保持不变；只改变真正送入 TTS 的文本。

## 设计契约

1. 在 `src/voice/` 建立单一 speech projection 实现：
   - 纯函数处理完整文本；
   - 每个生成轮独占的增量 projector 处理流式片段，状态至少包含 fenced-code 开/关和本代码块是否已发提示；
   - 无 I/O、无线程、无网络、无日志正文、无额外模型调用；空间只随当前输入片段增长，不累计完整回复。
2. `TTSEngine.speak()` 继续在最末边界执行完整文本投影，保护直接调用方；投影必须幂等。流式助手在入队前使用每轮 projector，避免跨片段代码泄漏和“清理后为空仍计为播报”。
3. 所有助手 TTS 入口共用投影语义：流式队列、非流式自动播报、tracked 播报和公开 `speak()`。屏幕 `DispatchOutcome.text`、`on_delta`、`ConversationHistory`、memory submit、character `send_message()` 必须仍是原文。
4. 不改变 `TurnSpeechPlayer` 的所有权、背压、取消、完成或 shutdown 契约；空投影片段按成功跳过，不增加 accepted，不标记 `speech_started`，不启动无意义播放线程。

## 投影规则（保守、可逆范围清晰）

- Unicode emoji、emoji shortcode、确认是颜文字的括号片段：删除。
- 正常括号信息必须保留，至少涵盖 `(API)`、`(RVC)`、`(Python 3.12)`、`（可选）`、版本号和普通英文短语。颜文字判断必须需要明确的表情符号证据，不能只凭“CJK 占比低”。
- Markdown emphasis、标题、引用、列表标记：只去格式标记，保留可读正文。
- `[标签](URL)`：只读标签；`![alt](URL)`：输出一个固定短提示（例如“图片已显示。”），不得朗读 URL。
- 裸 `http://` / `https://` URL（含 query/fragment）：替换为固定“链接已显示”提示；URL 本身不得进入 TTS 参数。
- inline code：去反引号，保留短内容（例如 `Ctrl+C`）；不要猜测或改写语义。
- fenced code block（``` 或 ~~~，仅按行围栏识别）：代码正文不朗读，每个代码块最多输出一次固定“代码内容已显示。”；围栏可跨任意流式 chunk。未闭合围栏到本轮结束仍安全抑制，不把下一轮带入该状态。
- Markdown 表格分隔行不朗读；普通表格行保留单元格文字并用自然停顿分隔。
- 合并多余空白，保留必要中英文标点。投影为空时返回空字符串。

不要写通用 Markdown 渲染器，也不要用可能灾难性回溯的宽泛正则。对单个片段设置明确输入硬上限或证明调用方已有上限；超限要有确定性有界处理，不能无界缓存。

## 流式接线细节

- projector 必须在 `_GenerationTurn` 或 `process_text_input_streaming()` 的本轮局部创建，绝不能跨轮共享。
- `splitter.push()` / `finish()` 得到的片段先投影，再 enqueue。若投影为空，返回成功但 `speech_started` 保持 false。
- 同一代码块的“代码内容已显示”只能入队一次；取消/失败/新轮后状态丢弃。
- TTS 末端重复投影不得把固定提示删除或重复添加。
- 成功生成但投影全空时，生成仍是 `ReplyStage.OK`、历史仍提交、显示仍完整；播报等待应安全得到 skipped/completed 语义，不能超时或谎称有音频。

## 必须新增的确定性测试

1. 正常括号反例与多类颜文字；中文、英文、数字不被误删。
2. Markdown 链接、图片、裸 URL（含凭据/query/fragment sentinel）、强调、标题、引用、列表、inline code、表格；TTS fake 捕获值中 URL/sentinel/围栏/代码正文为零出现。
3. fenced code 围栏和正文逐字符、任意 chunk 切分；提示 exactly once；未闭合、相邻多个代码块、新轮重置。
4. 纯 emoji/纯格式投影为空：不 enqueue、不启动播报线程、不把 `speech_started` 置真；生成/显示/历史/角色仍保留原文。
5. 流式与非流式对同一完整回复得到等价 spoken 内容（允许因已有分句产生列表差异，但拼接语义一致）。
6. 投影幂等；大输入完成时间与输出/内部缓冲上限的确定性测试，不做长 sleep 性能测试。
7. 取消、消费者失败、播放完成、shutdown 与现有语音测试不得回归。

## 文档与验证

- 新增 ADR，说明“显示原文 / 播报投影”的双通道契约、URL/代码隐私收益、流式状态所有权与限制。
- 更新工程基线和交接顶部。
- 运行专项、相关、完整离线测试、`compileall`、`git diff --check`。
- 不启动真实 Ollama、主程序、麦克风、扬声器、edge/Google、RVC、浏览器、角色或清理实扫；不读写 `data/memory`；不安装依赖。
- 明确离线投影契约通过不代表 Nahida/RVC 音色或真实语音自然度已验收。

完成后停止，等待 Codex Review。
