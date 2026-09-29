# 存储、性能与安全审计（2026-09-27）

## 结论

当前实现已有日志轮转、三类运行时文件清理、有界语音/记忆队列、集中式 `SafetyPolicy` 和较完整的关闭流程，但还不能声称已经具备长期无人值守运行所需的资源边界。

本机现场数据很小：`logs/` 约 231 KiB，仓库 `tmp/` 约 33 KiB，真实验收库 20 KiB；历史 23 个 `%TEMP%/miku-memory-diag-*` 目录合计约 460 KiB。它们不会解释此前的整机卡顿。更可信的高负荷来源是模式无关的 TTS/STT/RVC 初始化、RVC 同步预热、Ollama 模型加载，以及 `keep_alive=30m` 带来的显存长驻。磁盘占用与内存占用需要分别处理。

## 已有保护

- `RotatingFileHandler`：单个主日志 10 MiB、5 个备份，轮转上界约 60 MiB。
- `FileCleaner`：每 10 分钟检查 `data/tmp`、`logs`、`data/test`；默认分别保留 1 小时、1 天、72 小时；删除受白名单限制。
- TTS/RVC 临时音频正常路径会立即清理，文件占用时有有限次重试。
- 长期记忆默认关闭；启用时 ingestion 队列容量 32，召回候选与注入字符数有上限。
- 流式播报队列容量 8；HTTP 流式响应在 `finally` 关闭；RVC Worker、记忆线程、清理定时器都有关闭入口。
- 注册表写入在代码层永久拒绝；电源操作还需独立权限与确认；文件删除只允许已注册目录。
- LLM 日志主要记录阶段耗时，没有默认记录完整 prompt/回复正文。

## 已确认缺口

### 存储和内存

1. `ConversationHistory` 请求时只取最近 10 条，但内部 `_messages` 只增不减；长会话会持续占用 Python 内存。
2. SQLite 记忆库只有 CRUD，没有最大记录数、保留期、定期 prune 或维护入口。普通 `DELETE` 也不会自动缩小数据库文件。
3. 自动清理不覆盖仓库根 `tmp/`、`data/memory/` 或系统临时诊断目录。历史 23 个诊断目录目前不会自动回收；最新修复已保证新验收不再增加。
4. `FileCleaner.start_auto_cleanup()` 会在启动线程中同步扫描目录；目录很大时会拉长启动。每个无法删除的音频还可能各自创建一个延迟删除线程，没有全局并发上限。

### 性能和资源生命周期

1. CLI 在创建 `MikuAssistant` 后才按 `--mode` 分流；默认装配总会创建 TTS 和 STT。即使 `--mode text`，仍会做 RVC 健康检查、设备探测、Worker 加载与可选推理预热。
2. RVC 预热在 TTS 构造期间同步发生，随后又启动 Ollama 后台预热；当前没有按模式或 GPU 资源协调预热。
3. `ollama.keep_alive=30m` 会在对话或提取后保留模型。它能降低下一轮冷启动，却会长期占用显存；这是配置行为，不应误报为 Python 内存泄漏。
4. 已有关闭逻辑较强，但还缺少长时间 soak 的 RSS、线程数、子进程数、CPU/GPU 采样和明确阈值，因此不能承诺“绝不泄漏或卡顿”。

### 安全和隐私

1. 审计时主配置开放了系统控制、电源、自动浏览器、浏览器启动和环境变量修改；本轮已改为默认关闭。
2. STT 曾在 INFO 日志中写入完整识别文本；本轮已改为只记录字符数。
3. RVC Worker、RVC 设备探测和健康检查仍直接调用 `subprocess`，没有全部经过 `SafetyPolicy`。RVC 转换失败与 STT 临时文件也存在直接删除路径。它们违反仓库的统一安全边界。
4. 当前 `subprocess` 是一个过宽的全局布尔权限。RVC 需要启动固定解释器/脚本，不代表其他任意子进程也应获得授权。
5. Ollama 与记忆提取器接受可配置 URL；没有“只允许 loopback，远程地址需显式许可”的产品级边界。若配置被改为远端，完整对话或提取内容可能离开本机。
6. 默认 `edge-tts` 和 `speech_recognition` 会使用外部网络服务，分别可能发送回复文本和麦克风音频。这是隐私边界，需要在配置和启动提示中明确，不应称作完全本地。
7. SQLite 记忆是普通明文文件。SafetyPolicy 能限制程序行为，但不能防止同一 Windows 用户或恶意软件直接读取文件；当前没有数据库静态加密保证。

## 本轮即时修正

- `system.windows_control_enabled=false`
- `security.permissions.system_power=false`
- `security.permissions.browser_launch=false`
- `security.permissions.env_modify=false`
- `search.auto_open=false`
- 上述缺省值在 `Config`、装配 fallback 与 `SafetyPolicy` 中保持一致
- STT INFO 日志不再保存完整转写，只记录长度

保留 `security.permissions.subprocess=true` 是为了当前 RVC 可用性；在后续专用能力白名单完成前，它仍是明确的安全债务。

## 验收边界

离线单元测试可以证明容量、权限和关闭契约，不能证明真实模型、声卡、驱动和 GPU 在所有场景都无卡顿。完成后还需一次用户主导的 30～60 分钟真实语音 soak，记录冷启动、热启动、首 token、首音频、RSS、显存、线程和子进程曲线。不得在自动测试中启动真实麦克风、扬声器、RVC、Ollama、浏览器或系统操作。
