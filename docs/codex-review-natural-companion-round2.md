# Codex Review：自然陪伴 Round 2

## 最终结论（2026-09-30）：PASS

F1–F8 已全部关闭。Codex 独立复验确认：显示、历史、memory 与 character 保留原文，所有助手 TTS 入口只接收本地投影文本；跨 chunk URL / Markdown / code 不泄漏，16k 上限与 quarantine 保持 fail closed；增量扫描为线性有界，固定提示不再产生双标点。

- 复杂度探针：1k / 4k / 8k / 15k 逐字符 URL 分别约 `0.0018s / 0.0037s / 0.0079s / 0.0179s`，`scan_steps` 分别为 `1012 / 4012 / 8012 / 15012`。
- 独立专项：`46 tests / OK`。
- 独立相关回归：`123 tests / OK`。
- 独立全量：`674 tests / OK (skipped=4)`。
- `compileall` 与 `git diff --check`：exit 0（仅既有 LF/CRLF 提示）。

本结论只覆盖离线代码、契约、性能守门与故障注入测试；真实 TTS/RVC 音色、扬声器播放与自然度仍需 30–60 分钟人工语音 soak 验收。未运行真实服务或设备，未读写 `data/memory`，未提交、未推送。

---

> 以下保留各轮发现与返修要求，作为评审证据。

## F1 — P1：Markdown 嵌套括号和长 URL 会泄漏尾部正文到 TTS

独立复现：

- `[文档](https://evil.example/a_(x)?tok=SECRET_NESTED)` 投影为 `文档?tok=SECRET_NESTED)`；简单 `[^)]+` 在 URL 内第一个 `)` 提前结束。
- 长度超过 2048 的裸 URL 只替换前 2048 字符，余下 path/query（含 `SECRET_LONG`）作为普通正文进入 TTS。

要求：

1. URL/Markdown 目标解析改为确定性、有界、线性扫描或等价安全实现；支持 Markdown 目标内平衡括号、长目标、Unicode、query/fragment。不得用固定匹配长度后把余段当正文。
2. 对已识别但未闭合的 Markdown URL/裸 URL，隐私优先：在 `finish()` 前保持有界缓冲；到输入上限或本轮结束仍不完整时丢弃目标，不得回吐潜在 URL 正文。
3. 补嵌套括号、连续嵌套、可选 title、Unicode、恰好/超过旧 2048 阈值、未闭合目标的 sentinel 反例；fake TTS 参数中 URL、凭据、query/fragment sentinel 均为零出现。

## F2 — P1：增量 URL 在 chunk 边界失去 scheme 后泄漏

独立复现：

```text
feed("看 https://evil.example/a?tok=SEC") -> "看 链接已显示。"
feed("RET_SPLIT 好")                    -> "RET_SPLIT 好"
```

当前 projector 立即处理每个无换行 chunk，无法知道末尾 URL 仍会延续。强制 500 字分段、任意模型 chunk/句子边界或 scheme 自身拆分都能触发。

要求：

1. 增量 projector 对 URL、Markdown link/image、inline code、围栏起始符等跨 chunk token 保持最小必要状态；不得仅依赖调用方“通常传完整句”。
2. 对下列每一种输入，在**每个可能切分点**以及逐字符 feed 下，增量结果与完整投影语义等价，且 sentinel 不出现：裸 URL、`[label](URL)`、`![alt](URL)`、inline code、```/~~~ fenced code。
3. URL 终止后应及时释放前文供播报，不能把整段普通回复都推迟到 `finish()`；内部缓冲必须有硬上限，超限仍 fail closed。

## F3 — P1：非流式/公开播报入口仍绕过助手层投影

`_apply_outcome_side_effects()` 和 `MikuAssistant.speak()` 仍把 `strip_assistant_prefix()` 后的原文直接交给 `tts_engine.speak()`。生产 `TTSEngine` 会再次投影，但注入式/扩展 TTS 并不保证该实现；而且纯 emoji 仍会创建线程。独立 fake 捕获到：

```text
["https://evil.example/?tok=SECRET_PUBLIC", "😀🎉"]
```

这违反“所有助手 TTS 入口共用投影”以及“空投影不启动无意义线程”的任务契约。

要求：

1. `_apply_outcome_side_effects()` 在创建线程或同步 `speak` 前先 `project_for_speech`；为空则不创建线程、不调用 TTS。fallback 同步调用使用同一个已投影值。
2. `MikuAssistant.speak()` 同样先投影；为空安全返回。`_speak_outcome_tracked()` 保持已投影语义并补空值计数断言。
3. fake TTS 测试覆盖非流式 async、线程启动失败的 sync fallback、tracked、公开 `speak()`；URL/代码 sentinel 不进入参数，纯 emoji 调用数为 0。显示、历史、memory 和 character 仍保留原文。

## 复验门槛

- Round 2 专项、相关、完整离线测试通过；`compileall`、`git diff --check` 通过。
- 更新 ADR、交接状态与真实测试数量；不得声称真实语音自然度已验收。
- 完成后停止等待 Codex Review。

## Codex Review Round 2：F4–F6

F1–F3 的原始复现已修复，但独立边界探针又发现以下问题。仍属于 Round 2，不得进入新功能。

### F4 — P1：hold 超过 4096 后丢掉敏感前缀并回吐 URL 尾部

`SpeechProjector.feed()` 在 `_hold` 超过 `MAX_SPEECH_HOLD_BUFFER_CHARS` 时只保留最后 4096 字符。若 hold 的是长 URL/Markdown 目标，scheme / link 开头被删掉，剩余尾部随后作为普通文本输出。5000 字 URL 的 `SECRET_OVER` / `SECRET_CHUNK` 均被独立探针捕获。

要求：敏感 token 的有界溢出必须 fail closed。可以让 hold 上限与 16000 总输入上限一致，或维护显式 quarantine/discard 状态直到可信终止符；不得通过截掉前缀后重新解析未知尾部。覆盖单次和多次 feed、恰好边界、边界+1、总输入在 URL 中途耗尽，确保 sentinel 为零出现且内部缓冲始终有界。

### F5 — P1：大小写 scheme 与 angle/title Markdown 仍可绕过

- `HTTPS://evil.example/a?q=SECRET_UPPER` 原样进入投影结果，因为 `_startswith_scheme()` 大小写敏感。
- `[文档](<https://evil.example/a?q=x> "SECRET_TITLE")` 投影后仍包含 `"SECRET_TITLE")`；angle 分支只消费到 `>`，没有消费外层 `)` 和可选 title。

要求：scheme 识别大小写不敏感，跨 chunk 前缀同样成立。Markdown 目标扫描必须消费完整外层括号，正确处理 angle destination、反斜杠、平衡括号以及单/双引号或括号 title；title 中包含 `)` 时不能提前结束。目标/title 全部不进入 TTS。为上述组合逐切分点和逐字符补 sentinel 反例。

### F6 — P1：裸 URL 吞掉无空格中文后文

`见 https://example.com。谢谢` 当前投影为 `见链接已显示。`，`谢谢` 丢失。`_is_url_body_char()` 把中文句号及其后文本都当 URL，损害自然表达。

要求：常见中文句读和成对结束符应终止裸 URL，并作为普通正文保留，至少覆盖 `，。！？；：、）】》」』`；Markdown 目标内仍由目标扫描器处理 Unicode 路径，不套用裸 URL 终止规则。覆盖每种句读后的中文正文以及 URL 自身 percent-encoded/Unicode path 的保留隐私行为。

## Round 2 第二轮复验门槛

- 先运行并记录 `.cursor-agent-tmp/codex_probe_speech_round2.py`，上述 `SECRET_*` 均不得出现，`谢谢` 必须保留；再跑专项、相关、完整离线测试、`compileall`、`git diff --check`。
- 更新 ADR/交接；完成后停止等待 Codex 复验。

## Codex Review Round 3：F7–F8

F4–F6 原探针已通过。最终性能/自然度复验仍发现以下问题，必须在本轮收口。

### F7 — P1：跨 chunk 敏感 token 每次从头扫描，呈 O(n²) 卡顿

当前 `SpeechProjector.feed()` 每追加一个 chunk 都从 `_hold` 起点重新调用 URL/Markdown/inline 扫描。独立实际计时（逐字符 feed 同一裸 URL）：

| URL body | elapsed |
| ---: | ---: |
| 1,000 | 0.065s |
| 4,000 | 1.001s |
| 8,000 | 4.727s |
| 15,000 | 16.544s |

这不是固定测试阈值问题，而是算法复杂度错误；模型小 chunk 或被强制分段的长 token 会占满 CPU 并卡住播放链路。

要求：

1. 为 hold 增加明确 token kind 与增量扫描进度，或等价地只检查**新增 chunk**；已确认处于裸 URL 时，在未见终止符前不得从 scheme 重扫。Markdown label/destination/title、inline code 也不得对全部历史 hold 每字符重扫。
2. 总处理时间应为 O(总输入字符数)，允许固定数量状态；不得用 sleep 或只放宽计时阈值掩盖。
3. 补确定性复杂度证据：可注入计数器/扫描步数并断言 1k→8k 的累计扫描步数线性有界；墙钟仅作宽松防死循环守门，不作为唯一证明。逐字符、1/7/500 字 chunk 的完整投影等价与隐私反例必须保持。
4. 状态仍按生成轮隔离；取消/finish/新轮清空；16k 输入/hold 硬上限和 quarantine fail closed 不得回归。

### F8 — P2：固定提示与原句读形成双标点

`见 https://example.com。谢谢` 目前为 `见 链接已显示。。谢谢`；ASCII 句点/问号、图片提示也可能形成 `。. / 。？` 等不自然组合。

要求：固定 `LINK_TIP` / `IMAGE_TIP` / `CODE_TIP` 后若紧接源句读，应只保留一个自然句读：原问号/叹号优先，普通句号不得重复。不要全局篡改普通用户/模型正文，只在固定提示拼接边界处理。覆盖中英文 `。.!！?？,，;；:：`、无标点和跨 chunk 情况。

## Round 2 最终返修门槛

- 记录修复前后的扫描步数/宽松墙钟探针；15k 逐字符不得再出现秒级平方增长。
- 专项、相关、完整离线测试、`compileall`、`git diff --check` 通过；更新 ADR/交接。
- 完成后停止等待 Codex 最终复验。
