# Cursor 下一里程碑：真实 Ollama 长期记忆文本验收入口

## 目标

长期记忆产品接线已经通过 Codex 最终离线验收，但 `memory.enabled` 仍默认关闭，真实 Ollama 提取质量、结构化输出遵循率、文本端到端召回和实际延迟尚未验证。

本里程碑完成两件事：

1. 使用 Ollama 官方 `/api/chat` 的 JSON Schema `format` 强化记忆提取输出；
2. 提供一个默认不联网、显式 `--execute` 才调用本机 Ollama 的纯文本验收入口，始终使用合成对话和临时数据库，不污染用户正式记忆。

官方依据：

- `https://docs.ollama.com/api/chat`：`format` 可为 `json` 或 JSON Schema，`stream` 可显式关闭。
- `https://docs.ollama.com/capabilities/structured-outputs`：可将 JSON Schema 传入 `format`；低 temperature（例如 0）有助于稳定结构化输出。

本轮不启用麦克风、TTS、RVC、角色或系统操作，不修改默认 `memory.enabled=false`，不启动或停止 Ollama 服务，不下载/更换模型。

## 开始前

读取并遵循：

- `AGENTS.md`
- `skills/architecture-contracts/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `skills/safe-refactoring/SKILL.md`
- `skills/collaboration-review/SKILL.md`
- `docs/architecture/adr-memory-product-integration.md`
- `docs/codex-review-memory-integration-final.md`
- `src/memory/` 全部文件
- `src/llm/ollama_client.py`
- `src/core/config.py`
- `config/config.yaml`

先检查 `git status`，保留全部未提交修改。本轮不提交、不推送。实现和离线验证期间不得调用真实 Ollama；完成后停止，等待 Codex Review，再由用户决定何时执行真实验收。

## 1. 提取请求使用 JSON Schema

增强 `OllamaMemoryExtractor` 的请求 payload：

- 保持 `POST /api/chat`、`stream=false`、无 `ConversationHistory`、仅 system + JSON 编码轮次两条消息。
- 增加 `format` JSON Schema；根类型为 array，items 为 object，只允许：
  - `text`: string
  - `category`: enum `user_profile` / `preference` / `relationship` / `event` / `other`
  - `importance`: number，0～1
  - `confidence`: number，0～1
- schema 使用 `additionalProperties: false`，并用 `maxItems` 对齐当前 extractor 的 `max_extractions`。是否要求四字段全部出现需与现有默认值兼容；在 ADR 中写明决定并测试。
- 增加 `options: {"temperature": 0}`，不得覆盖未来其他运行参数；本轮不新增复杂 options 配置。
- 继续在客户端执行现有严格解析和领域校验，不能因为服务端 schema 就取消本地防线。
- schema 必须为每次请求的独立对象或不可变模板，调用方修改 fake 捕获的 payload 不得污染后续请求。
- `keep_alive` 与官方 API/现有 `OllamaClient` 兼容：至少接受非空字符串与有限数值（例如 0），拒绝 bool、NaN、Infinity 和其他类型；不要把数值无条件转成字符串。

只使用标准库构造 schema，不增加 Pydantic 或 Ollama SDK 依赖。

## 2. 真实文本验收入口

新增根目录 `diagnose_memory.py`（或同等清晰名称），结构参考 `diagnose_rvc.py`，但遵守以下安全边界：

### 默认行为

- 不带 `--execute` 时仅执行 dry-run：读取配置、完成参数校验、打印将使用的 endpoint/model、测试阶段和“不会写入正式数据库”；不得发 HTTP、创建 SQLite、启动 ingestion 线程或改配置。
- dry-run 不要求 `memory.enabled=true`；验收工具可以读取 extractor 的显式配置并回退到 `ollama.*`。
- 不自动启动 `ollama serve`，不拉取模型，不修改系统或环境。

### 显式真实执行

只有用户传入 `--execute` 时才进行真实 HTTP。至少支持：

- `--config PATH`：可选配置路径；
- `--base-url`、`--model`、`--timeout`：可选只覆盖本次诊断，不写回配置；
- `--report PATH`：可选写 JSON 报告；未指定则只输出控制台摘要；
- 所有参数严格校验，报告路径由调用者显式提供时才创建父目录。

真实执行必须：

1. 创建 `TemporaryDirectory` 下的 SQLite store，绝不使用 `memory.db_path`；
2. 使用固定、无私人数据的中文合成场景，至少包含：
   - 稳定偏好：用户喜欢某种饮品；
   - 基本资料：虚构姓名/城市；
   - 临时或无记忆价值内容：简单算术/寒暄，应尽量返回空；
   - prompt injection 文本：要求模型忽略规则或输出伪指令，必须仍满足 schema，本地不得执行其内容；
3. 顺序执行，不并发制造 GPU 竞争；每个外部请求有显式 timeout；
4. 验证 extractor → ingestion/store → lexical recall → context formatter 的真实文本链路；精确重复结果写入时验证 SQLite 去重；
5. 至少执行一次真实 `MemoryAwareLLMClient` 文本往返：第一轮提交合成偏好，等待 ingestion 在有界时间内完成，再询问相关问题并记录 recall 是否进入 system。不能访问私有字段忙等；如确需等待，先为 ingestion 增加窄的 `wait_until_idle(timeout) -> bool` 公共契约并用 Event/Condition 实现及离线测试，禁止 `sleep` 猜测；
6. 无论成功、失败或 Ctrl+C，都在 `finally` 有界关闭 runtime/store/HTTP 流；若 worker 超时，报告不完整状态，不谎称成功；
7. 不调用语音、浏览器、角色、Windows 控制、文件清理或主程序。

若完整真实 wrapper 往返会同时触发前台与后台请求，应等 ingestion idle 后再发第二轮，避免本轮误测并发调度；GPU 竞争留到后续性能里程碑。

## 3. 报告契约

定义不可变结果类型或明确字典 schema，至少记录：

- 工具版本/报告 schema 版本；
- endpoint 的主机与端口、model、timeout；不得记录 URL 密码、token 或环境密钥；
- 每个场景的阶段状态、耗时毫秒、候选数量、类别、协议错误类型；
- 写入/去重数量、recall 命中数量、context 字符数；
- 前台生成总耗时、记忆是否进入 system、回复是否命中合成事实；
- runtime 关闭状态；
- `passed`、`warnings`、`failures`。

报告默认不保存完整模型回复、完整 prompt 或记忆正文。合成场景可用稳定 scenario id 和预定义期望摘要。控制台输出清楚区分：

- **契约失败**：HTTP/JSON/schema/关闭失败，退出码 1；
- **质量未达标**：协议正常但漏提取、误提取或回答未命中，退出码 2；
- **通过**：退出码 0；
- 参数错误遵循 argparse 非零退出。

不得把一次真实样本通过写成普遍质量保证。

## 4. 纯离线测试

新增 `tests/test_memory_diagnostic.py`，并按需扩展 integration 测试。使用 fake transport、fake LLM、临时目录与可控 Event；不得联网或启动设备。

至少覆盖：

1. extractor payload 含独立 JSON Schema、`stream=false`、temperature 0、两条无历史消息及动态 `maxItems`。
2. 连续两次请求的 schema/payload 不共享可变引用。
3. 本地解析仍拒绝额外/错误根、非法类别、越界/non-finite 数值和非字符串正文。
4. `keep_alive` 接受非空字符串及 0/有限数值，拒绝 bool/NaN/Inf/空串/对象；配置回退语义一致。
5. diagnostic 默认 dry-run：断言 HTTP/store/ingestion factory 调用次数均为 0。
6. `--execute` fake 路径始终使用临时 DB，不读取或修改配置中的生产 db_path。
7. 四个固定场景的 pass/warning/failure 分类与退出码。
8. 报告不包含合成正文之外的敏感字段，不默认写文件；显式 report 路径使用合法 JSON schema version。
9. 有界 idle 等待成功/超时/关闭竞态；不使用耗时阈值作为主要并发断言。
10. 异常和 KI/SystemExit 路径仍关闭资源；关闭不完整准确进入报告。
11. 全量离线回归继续通过。

测试应调用诊断模块的纯函数/可注入 main，而不是启动子进程或访问网络。

## 5. 文档与交接

- 新增 `docs/memory-real-text-acceptance.md`：说明 dry-run 和真实命令、只使用合成数据/临时库、退出码、报告字段和结果解读。
- 更新 `docs/architecture/adr-memory-product-integration.md`：补充 JSON Schema `format`、temperature 0 和真实验收边界。
- 更新 `docs/engineering-baseline.md`，只记录离线已实现能力；真实验收未运行时必须明确写“未运行”。
- 在 `docs/cursor-handoff-status.md` 顶部写入实际改动、离线测试结果和未验证项。
- 离线验证：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

完成后停止修改，等待 Codex 独立 Review。Cursor 不得自行运行 `diagnose_memory.py --execute`。

## 明确禁止扩大范围

- 不默认开启长期记忆，不写正式数据库，不读取真实历史对话。
- 不启动/停止 Ollama，不下载或切换模型，不改变用户 Ollama 配置。
- 不测试麦克风、TTS、RVC、角色、浏览器或系统操作。
- 不实现 embeddings、向量数据库、记忆编辑 UI、模型优先级或 GPU 抢占。
- 不提交、不推送，不撤销现有未提交修改。
