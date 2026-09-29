# 下一里程碑：存储边界、资源性能与最小权限

> 执行顺序：先完成 `docs/cursor-next-memory-grounding-fix.md`，再连续执行本文件的 Round 1。当前用户已授权两个阶段连续实现，无需在二者之间等待 Codex Review。

## 目标

在不重写现有对话/语音接口的前提下，使长期运行具备可证明的容量边界、模式相关的资源加载和最小权限。任务依据：`AGENTS.md`、`skills/engineering-quality/SKILL.md`、`skills/architecture-contracts/SKILL.md`、`skills/safe-refactoring/SKILL.md`、`.cursor/rules/system-safety.mdc` 与 `docs/codex-audit-storage-performance-security-2026-09-27.md`。

不要提交或推送。保留当前全部未提交的长期记忆和语音修改。不得启动真实麦克风、扬声器、RVC、Ollama、浏览器或系统电源操作。

## Round 1：有界存储与低干扰维护

1. 给 `ConversationHistory` 增加可配置的存储上限；保留现有 `request_window` 与构造兼容。每次完整提交后按完整 user/assistant 轮裁剪，不能留下半轮。默认必须有限，测试至少覆盖 10,000 轮后长度保持上限、请求顺序正确、实例隔离不变。
2. 为 `MemoryStore` 增加窄的维护契约和 SQLite 实现：有界计数、按明确规则 prune、返回删除统计。增加 `memory.retention` 严格配置，至少包含 `enabled`、`max_records`、`max_age_days`、`maintenance_interval_hours`、`preserve_user_confirmed`。默认不得删除 `USER_CONFIRMED`；超量时只淘汰允许淘汰的记录，并用稳定顺序处理相同时间/重要度。
3. 维护只可在 ingestion idle 且没有 foreground lease 时运行；禁止在一次用户请求、召回或每条写入的热路径执行全库 `VACUUM`。自动维护需有界批量；完整 `VACUUM` 只能是显式离线维护入口并记录可能耗时。
4. 调整 `FileCleaner`：启动时不得同步无界扫描；每轮需有最大扫描/删除数和单实例后台执行保证。延迟音频删除改用单一有界队列/Worker 或交给清理器，不能一文件一线程无限增长。
5. 不自动清理仓库根 `tmp/` 的任意文件。诊断目录只能在专用前缀、创建标记、固定父目录、超过保留期且无活跃所有者时回收；任何目录删除均须通过扩展后的 SafetyPolicy 白名单。保留现有失败证据语义。

## Round 2：按模式装配与资源协调

1. 让启动模式在装配前可知。`text` 模式不得构造 STT、AudioPlayer、TTS、RVC Worker；`voice` 模式可按需初始化；`interactive` 第一次进入 voice 时懒加载并返回结构化失败。保留 `MikuAssistant.from_dependencies` 和现有公开方法兼容。
2. RVC 健康检查/设备探测/Worker 预热不得阻塞纯文本启动。Ollama 与 RVC 预热需要按模式串行协调，禁止默认同时抢 GPU。
3. 增加明确资源配置（建议 `balanced` 为默认）：模型保活与退出卸载分开；退出只释放本程序拥有的模型/Worker，不停止用户的 Ollama 服务。所有真实 HTTP 行为用 fake 做契约测试。
4. 性能日志只记录 turn/session ID 与阶段耗时，不记录正文。保留冷启动与热启动区分；增加无设备 soak 工具，采样当前进程 RSS、线程数、子进程数和队列深度，不访问麦克风/扬声器/GPU。
5. 离线 soak 至少覆盖：10,000 文本轮 fake、1,000 次失败/取消、重复创建关闭、清理器长目录 fake。验收标准为对象/线程/子进程回到稳定上界，不使用“运行结束后 OS 会回收”代替关闭契约。

## Round 3：专用能力与隐私边界

1. 移除 RVC Worker、设备探测、健康检查中的直接 `subprocess` 调用；将 SafetyPolicy 显式传到整个调用链。把宽泛 `subprocess` 权限缩成固定目的/固定可执行文件与固定脚本的能力，拒绝 shell、额外参数注入、路径越界和非预期解释器。
2. 修复 RVC/STT 临时文件的直接删除旁路；只删除本程序创建且位于注册白名单的文件。注册表写入继续永久拒绝；系统电源继续双门（权限 + 用户确认）。
3. 增加本地端点策略：Ollama/记忆提取默认只允许 loopback；远程 HTTP(S) 必须显式开启并在启动时给出不含敏感数据的警告。拒绝 URL userinfo、query、fragment 和非 http/https scheme。
4. 明确云服务许可：在线 STT/TTS 分别配置是否允许发送麦克风音频/回复文本；启动语音前展示一次能力摘要。不得把 edge-tts / Google STT 描述为完全本地。
5. 增加日志隐私回归测试：默认日志不得含转写正文、prompt、回复、记忆正文、认证信息或完整敏感路径。数据库明文限制写入文档；本轮不要擅自安装 SQLCipher、修改系统 ACL 或写注册表。

## 必须验证

使用 Python 3.12，仅跑纯离线测试：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

新增测试必须使用 fake transport、fake process、临时目录和注入时钟。不得用源码字符串匹配代替行为测试。不要运行 `diagnose_memory.py --execute`。

## 交付要求

- 新增 ADR，说明保留策略、模式装配、资源所有权、权限能力和兼容迁移。
- 更新 `config/config.yaml`、`Config` 默认值、工程基线和 `docs/cursor-handoff-status.md`。
- 在 handoff 顶部列出实际修改、测试数量、未运行项目、剩余风险。
- 完成 Round 1 后停下等待 Codex 统一评审；不要自行扩到 Round 2/3。
