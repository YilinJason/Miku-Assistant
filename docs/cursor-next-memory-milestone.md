# Cursor 下一里程碑：长期记忆的领域契约与 SQLite 持久层

## 目标

当前 `ConversationHistory` 只负责单次进程内的短期对话上下文，不能承担长期记忆。本里程碑建立一个独立、可替换、可离线验证的长期记忆底座：领域对象、存储接口、SQLite 实现、格式版本和故障契约。

本轮**不把记忆接入提示词，不调用 LLM 自动提取，不做向量检索，也不改变现有对话行为**。完成后由 Codex 独立评审，再进入“提取服务 + 检索注入”的下一里程碑。

开始前读取：

- `AGENTS.md`
- `skills/architecture-contracts/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `skills/safe-refactoring/SKILL.md`
- `skills/collaboration-review/SKILL.md`
- `src/llm/conversation_history.py`
- `src/llm/ollama_client.py`
- `docs/architecture/adr-conversation-history.md`
- `docs/engineering-baseline.md`

先检查 `git status` 和当前差异。工作区包含大量已验收但未提交的语音、STT、流式回复和性能优化修改，必须全部保留；不要格式化或重写无关文件。本轮不提交、不推送。

## 1. 模块边界

在 `src/memory/` 下建立独立模块，命名可合理调整，但职责必须清楚：

- 无 I/O 的领域契约：记忆条目、类别、来源、写入/错误结果及 `MemoryStore` Protocol。
- SQLite 适配器：只负责持久化、查询和 schema 管理，不负责调用模型、拼提示词或判断一句话是否值得记住。
- 模块导入不得创建数据库、目录、线程或连接；只有显式构造存储实例时才允许打开指定数据库。
- 不修改 `ConversationHistory` 的窗口、提交、共享、清空和快照语义。短期对话历史与长期记忆保持两套明确边界。
- 不在 `OllamaClient`、`MikuAssistant` 或 `VoiceSession` 中装配长期记忆；本轮没有产品行为变化。

## 2. 领域契约

使用冻结 dataclass、Enum、Protocol 或项目可支持的等价窄类型。至少表达：

- 稳定 ID。
- 非空正文。
- 类别，例如用户资料、偏好、关系、事件和其他；未知类别必须有兼容处理，不能导致整库无法读取。
- 来源至少区分 `USER_CONFIRMED` 与 `MODEL_INFERRED`。持久层必须原样保存来源，不能把模型推断自动升级为用户确认。
- `importance` 与 `confidence` 的合法范围及含义；非法值在写数据库前失败。
- UTC 创建/更新时间；允许注入 clock/ID factory 或提供等价确定性测试点。

公开返回值不得暴露可被调用方原地修改后污染存储状态的内部可变对象。正文规范化只用于精确去重键，不得悄悄改写用户可见正文。规范化至少处理首尾空白、连续空白和大小写；中文和 Unicode 内容必须保留。

为预期存储错误定义清楚的项目异常或结构化结果，例如无效输入、重复项、存储不可用和不兼容 schema。不要吞掉 `KeyboardInterrupt` / `SystemExit`，不要用空列表伪装数据库故障。

## 3. SQLite 存储

仅使用 Python 标准库 `sqlite3`，不新增 ORM、向量数据库或依赖。至少支持：

- 新库初始化与 `PRAGMA user_version`（或等价）schema 版本。
- 新增、按 ID 读取、有限列表、显式更新、删除单条、清空全部。
- 列表有确定顺序、合法上限，不能无界一次加载全部记录。
- 精确规范化重复项不得产生第二条记录；重复时返回现有条目或明确冲突结果。不要在存储层做语义合并。
- 所有 SQL 参数化；写操作使用事务，失败不得留下半写入。
- 数据库关闭可重复；关闭后调用行为明确。
- 两个不同数据库文件相互隔离；显式打开同一数据库文件时可以观察到已提交数据。
- 重新打开后数据与 schema 版本仍正确。
- 遇到高于当前支持版本的数据库时非破坏性失败，不得自动降级、清表或覆盖。
- 日志不得默认记录完整记忆正文、原始对话或敏感内容。

为以后异步提取预留安全使用方式，但不要提前建立后台线程。若实现持有单连接，访问必须串行且生命周期明确；若按操作建连接，则事务与关闭语义同样要写清楚。

默认生产数据库路径可以提供一个纯路径解析函数或配置建议，但本轮不要在启动流程中实际创建用户数据库。测试数据库一律位于 `TemporaryDirectory`，不得写入仓库 `data/`、用户目录或真实配置路径。

## 4. 纯离线测试

新增 `tests/test_memory_store.py`（可按职责拆分），不得连接 Ollama、麦克风、音频、RVC、浏览器或系统操作。至少覆盖：

1. 领域不变量：空正文、非法重要度/置信度、非法时间或破损来源不能进入存储。
2. 中文、emoji、引号、换行和类似 SQL 的正文可安全往返。
3. 首尾/连续空白及大小写规范化后的精确重复不会新增第二条；原始展示正文规则明确。
4. 新增、读取、更新、确定性分页/限制、删除、清空和幂等关闭。
5. 重新打开持久化；不同文件隔离；同一文件的已提交数据可见。
6. 更新/事务中途失败时原记录保持；使用 fake/patch 制造故障，不损坏真实文件。
7. 高版本 schema 非破坏性拒绝；当前 schema 初始化幂等。
8. `USER_CONFIRMED` 和 `MODEL_INFERRED` 往返不混淆，更新不会隐式升级来源。
9. 并发边界若被实现，使用 Barrier/Event 等确定性同步验证，不用 `sleep` 猜测。
10. 现有 `ConversationHistory` 和全量离线测试继续通过。

使用主环境 Python 3.12：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
git diff --check
```

不要运行名字可能相似但会启动真实设备/模型的脚本。

## 5. 文档与交接

- 新增 `docs/architecture/adr-long-term-memory-store.md`，说明短期历史与长期记忆的边界、选择 SQLite 的原因、schema/version、来源真实性、去重策略、故障语义、隐私边界与回退方式。
- 更新 `docs/engineering-baseline.md`：只记录本轮真实完成的持久层能力和实际测试结果；不得写成已经接入对话、自动提取或语义检索。
- 更新 `docs/cursor-handoff-status.md` 顶部：修改文件、实际命令与测试数量、已知限制、需 Codex 重点复审的事务/schema/隐私边界。

完成后停止修改并等待 Codex Review。不要自行进入记忆提取、提示词注入或 UI 阶段。

## 明确禁止扩大范围

- 不接入真实 Ollama，不设计或调用自动记忆提取提示词。
- 不把全部聊天记录直接落库，不默认保存语音转写。
- 不安装 embeddings、分词、ORM、向量数据库或其他新依赖。
- 不修改当前模型、上下文窗口、系统提示词、流式回复、STT/TTS/RVC、UI、3D 或 Quest。
- 不启动 `main.py`、真实设备、浏览器或系统操作。
- 不撤销既有未提交修改，不提交、不推送。
