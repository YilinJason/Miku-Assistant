# Cursor 任务：安全硬化 Round 3C1——持久日志隐私与日志文件边界

> 状态：Cursor 已完成 Codex Review Round 3 F8–F11 返修，等待 Codex 复验。  
> 基线：同一未提交工作区中的 Round 3A、3B；Round 3B 已通过 Codex 最终离线验收。  
> 范围：只做 3C1。不得进入 SQLite 残留数据 3C2 或自然陪伴体验。

## 先读取

- `AGENTS.md`
- `skills/architecture-contracts/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `skills/collaboration-review/SKILL.md`
- `.cursor/rules/system-safety.mdc`（存在时）
- `src/core/logging_setup.py`、`src/core/paths.py`
- `src/search/chrome_search.py`、`src/core/handlers/search_handler.py`
- `src/extensions/character_interface.py`
- `src/llm/ollama_client.py`、`src/cli/voice_session.py`
- `src/voice/rvc/{worker,health_check,converter}.py`
- `config/config.yaml`、`src/core/config.py`

## 已确认风险

1. `assistant.logging.file_name` 直接拼到 `LOGS_DIR`，`../data/memory/long_term.sqlite3` 等值可让 `RotatingFileHandler` 越界写文件，甚至破坏记忆库。
2. 搜索查询与完整 URL、角色消息正文、音频/程序绝对路径、RVC 子进程 stderr/payload error、网络异常原文会进入持久日志。
3. `exc_info=True` 默认把绝对源码路径和异常链写入日志文件；本地调试有用，但不应成为默认持久化隐私面。
4. 轮转大小/份数没有严格类型和硬上限，错误配置可形成无界或异常磁盘占用。

## 设计目标与不变量

### A. 日志目标必须受限于 `logs/`

- `assistant.logging` 必须是 mapping；`file_enabled` 必须是真 bool。
- `file_name` 只允许单个 Windows 安全文件名：拒绝绝对路径、盘符、`/`、`\\`、`.`/`..`、NUL、尾随点/空格和 Windows 保留设备名（含带扩展名形式）。
- 最终目标解析后必须仍是 `LOGS_DIR` 的直接子文件；存在目标若为 symlink/junction/目录/非普通文件则 fail closed。不得为了通过验证删除或覆盖目标。
- 先完成全部配置和路径验证，再创建任何文件 handler；失败不得在越界位置产生文件。

### B. 持久日志容量严格有界

- `max_size_mb`：拒绝 bool、字符串、NaN/Inf、非正数；允许有限数字以保持已有配置兼容，硬上限 50 MB。
- `backup_count`：只接受真 int，范围 0..10。
- 文档明确最大持久日志上界约为 `max_size_mb * (backup_count + 1)`；现有默认 10 MB / 5 份保持。
- 重复调用不得重复绑定同一日志文件 handler。

### C. 默认持久日志不落用户内容和本机敏感细节

新增一个小而明确的日志隐私辅助边界（模块名自行合理选择），至少提供：异常安全标签（类型 + 可选 HTTP status，禁止 `str(exc)`/URL/body）和持久 handler 隐私过滤。

正常/错误路径的日志不得包含：

- 用户输入、搜索词、完整搜索 URL；
- 模型回复、记忆正文、角色扩展消息正文；
- 音频文件、Chrome、Python、模型/索引等绝对路径；
- RVC 子进程原始 stdout/stderr、worker JSON 原行或 payload `error` 原文；
- requests/远端异常原文中的 URL、响应正文或用户内容。

按调用点改为记录可诊断元数据，例如 `chars`、engine、stage、exception type、HTTP status、是否有音频、stderr 行数。面向当前用户的 CLI 返回/打印可以保留必要正文；本要求针对 `logging` 落盘路径。

必须覆盖并修复当前已确认调用点：

- `SearchIntentHandler` / `ChromeSearcher`；
- `CharacterInterface.send_message`；
- `OllamaClient` 的预热、生成、卸载、模型检查错误日志；
- `VoiceSession` 后台任务错误日志；
- RVC Worker 的无效 JSON、payload error、stderr tail/启动失败；RVC health 写 logger 时不得输出路径版 `summary()`；converter 不记录原始异常。

审计相邻本轮相关调用点；发现同类直接泄漏一并收口，但不要全仓库格式化或重构。

### D. traceback 默认不持久化

- 新增 `assistant.logging.include_tracebacks`，默认 `false`，必须是真 bool。
- 仅对文件 handler 生效：默认移除 `exc_info` / `exc_text` / `stack_info`；控制台行为不要因文件 filter 被意外改变。
- 显式 `true` 可为本机调试保留 traceback，配置注释要说明可能包含绝对路径和敏感参数。
- 即使显式开启 traceback，上述用户正文/查询/URL/原始 stderr 的调用点禁记规则仍成立。

## 离线测试要求

新增独立 `tests/test_security_round3c_logging.py`，使用临时目录、fake handler/fake HTTP/fake subprocess，不启动主程序、网络、浏览器、设备或真实 RVC。

至少覆盖：

1. 文件名表驱动拒绝：`../x.log`、`..\\x.log`、绝对路径、盘符、`.`、`..`、`CON`、`con.log`、`NUL.txt`、尾随点/空格、NUL；零越界文件创建。
2. 合法名称创建在临时 `LOGS_DIR` 直接子级；重复 setup 不重复 handler。
3. `file_enabled`、大小、份数、traceback 的 bool/数值严格校验和边界值；先验证后创建。
4. 现有 symlink/junction/目录/非普通目标拒绝（平台不支持创建 junction 时只跳过该平台专属用例，不能弱化实现）。
5. 用唯一 sentinel 分别注入搜索词、角色消息、远端异常 URL/body、RVC stderr/payload error、绝对路径；格式化后的日志记录中 sentinel 必须为零出现，同时保留 type/status/chars/stage 等诊断字段。
6. `include_tracebacks=false` 的文件 handler 输出无 traceback/绝对源码路径；控制台测试 handler 仍可收到原始 record。`true` 行为单独覆盖。
7. 用户可见返回契约、搜索 URL 构造、Ollama `GenerateResult` / stream 状态、RVC 回退语义不得改变。

## 文档与验证

- 新增日志隐私 ADR，明确“默认无正文日志”边界、显式 traceback 开关、容量上界、日志仍是本地明文且不等于加密。
- 更新 `config/config.yaml`、`Config` 默认、`docs/engineering-baseline.md` 与 `docs/cursor-handoff-status.md` 顶部。
- 运行 3C1 专项、相关现有测试、全量 unittest、compileall、`git diff --check`。
- 返回实际修改文件、测试数量、未运行项和剩余风险。
- 完成后停止等待 Codex Review；不得进入 3C2/自然陪伴；不得读取或删除 `data/memory`；不得提交、推送或安装依赖。
