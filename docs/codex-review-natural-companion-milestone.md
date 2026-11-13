# Codex 里程碑评审：更自然的陪伴体验

- **结论**：PASS
- **日期**：2026-09-30
- **范围**：自然陪伴 Round 1（统一人格提示）与 Round 2（自然且隐私友好的语音投影）
- **前置里程碑**：安全硬化 Round 3 已通过

## 最终行为

1. `warm_balanced` 是仓库内固定、可审查、最多 2400 Unicode 字符的基础人格。默认启用，不接受任意持久化自定义 system 提示。
2. 基础人格与当前请求的动态 system 在唯一 `/api/chat` 消息边界合成为一条 system；历史、当前 user 与长期记忆的数据边界保持不变。
3. 文本显示、会话历史、长期记忆提交与角色扩展始终保留模型原文；只有真正送入 TTS 的副本经过 speech projection。
4. speech projection 会省略 URL、Markdown 目标、代码围栏和无意义朗读标记，同时保留 `(API)`、`(RVC)`、`(Python 3.12)` 等正常内容。流式 token 跨 chunk 时仍保持隐私边界。
5. 固定提示与原句读只在拼接边界合并，避免“链接已显示。。”等机械播报。

## 资源、卡顿与生命周期评审

- 人格层是固定字符串组合：无 I/O、无线程、无额外模型调用、无额外数据库访问；每请求只增加一个固定长度 system 前缀。
- projector 每个生成轮独占；输入与 hold 默认各自最多 16000 字符。URL / Markdown / inline code 使用 token kind + progress 续扫，累计工作量为 O(总输入字符数)。
- 15k URL 逐字符输入的独立探针约 `0.018s`、`scan_steps=15012`；修复前约 16 秒的 O(n²) 路径已消除。
- 空投影不增加语音队列 accepted、不创建无意义播报线程；TTS 末端仍做幂等投影，保护直接调用方。
- 自然陪伴改动没有改变 `TurnSpeechPlayer` 的有界队列、取消、背压、完成或 shutdown 所有权；RVC/Ollama 串行预热与资源协调仍由安全硬化里程碑契约控制。
- 完成、失败、取消和 shutdown 的 projector 状态均按轮隔离；活动轮只保留有界对象，不累计完整会话。

## 安全与隐私评审

- 人格提示为本地固定模板，不从用户正文或记忆正文生成高优先级规则。
- 长期记忆 JSON 继续标记为不可执行数据；人格层不降低 `SafetyPolicy` 或系统操作确认要求。
- 投影后的 URL 路径、query、fragment、凭据和代码 sentinel 不进入 fake TTS；超限与未闭合敏感 token 均 fail closed。
- 日志只记录配置类别、计数、耗时和异常类型，不新增用户正文、提示正文或记忆正文日志。
- edge-tts 与 Google STT 仍属于外部网络服务，分别受 `cloud_tts` / `cloud_stt` 显式权限控制；本里程碑没有扩大权限。

## 独立验证

- Round 1 专项：`24 tests / OK`
- Round 2 专项：`46 tests / OK`
- Round 2 相关回归：`123 tests / OK`
- 最终全量：`674 tests / OK (skipped=4)`
- `compileall`：exit 0
- `git diff --check`：exit 0（仅工作树 LF/CRLF 提示）

测试全部使用 fake、临时目录和离线故障注入。未启动主程序、真实 Ollama、edge/Google、RVC、麦克风、扬声器、浏览器或清理实扫；未读取、修改或删除 `data/memory`；未提交、未推送。

## 剩余人工验收

离线契约无法证明真实 Nahida 音色、声卡、网络、GPU 和环境噪声下的自然度。开发里程碑已经闭环；发布前按 `docs/natural-companion-live-voice-soak.md` 做一次 30–60 分钟真实语音 soak。
