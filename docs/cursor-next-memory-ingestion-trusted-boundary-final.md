# 最终任务：把长期记忆写库权收回本地确定性边界

日期：2026-09-28  
优先级：P0  
目标：结束对模型生成 `text` 的逐轮补丁。模型只能从本地预先生成的安全候选单元中做选择；最终写入的事实正文、类别与来源全部由本地代码从用户原文确定性生成。

## 为什么改方案

前五轮已分别修复助手侧来源、极性、复合候选、疑问语境和未解析前缀，但共同根因一直存在：`OllamaMemoryExtractor` 仍接收模型生成的 `text`，验证器再尝试证明这段自由文本可信。自然语言可以无限增加条件、引用、否认、反讽和前后缀；继续扩充黑名单或子串规则无法形成封闭安全边界。

最终不变量必须是：

> SQLite 中每个新事实的全部语义文字都可追溯到一个本地识别的完整用户原句单元；模型输出不能创造、删减、改写或概括事实文字。

## 要实现的结构

### 1. 本地先生成 `UserFactUnit`

在发送任何提取请求前，本地把 `user_text` 分成带位置和终止符的不可变单元。建议字段：

```python
@dataclass(frozen=True)
class UserFactUnit:
    unit_id: int
    start: int
    end: int
    text: str
    terminator: str
    claim: PredicateClaim
```

要求：

- `user_text[start:end] == text`，不得重建或拼接；
- 保留终止符及其语义。不能像当前 `iter_clause_spans()` 一样丢掉 `？/?` 后再把问句视为陈述；
- quote/单元不得跨标点或原子边界；
- 单元必须完整解析为恰好一个受支持的用户 claim；
- 疑问、未知槽位、条件、引用、否认、猜测、不确定、未解析前后缀一律不成为候选；
- 暂不理解的自然语言返回无候选，不能猜测。

先支持现有已经有回归的谓词族：名字/身份、测试代号、喜欢与负向偏好、居住与负向居住、认识与负向认识。新增语法必须同时新增正反例。

### 2. 本地生成规范事实，不再信任模型 `text`

为本地 claim 提供唯一 renderer，例如：

```python
render_claim(PredicateClaim(...)) -> str
category_for_claim(PredicateClaim(...)) -> MemoryCategory
```

要求：

- renderer 只转换已识别的谓词框架与所有者，不允许自由摘要；
- claim 的 value 必须完整保留，不得用子串取短、补长或删除限定；
- 规范化只允许首尾空白、明确的句末标点和拉丁大小写等无语义变化；不得 `strip("的")` 或删除中文限定词/括号内容；
- 模型返回的任何 `text`、`fact`、`value`、`category` 都没有写库授权；优先从 schema 完全移除这些字段，并保持 `additionalProperties=false`；
- `ExtractedMemory.text` 必须由 renderer 构造后才创建。

### 3. 模型只能选择本地候选编号

请求模型时发送本地已验证候选的只读列表，例如：

```json
{
  "candidates": [
    {"unit_id": 0, "text": "我最喜欢桂花乌龙"},
    {"unit_id": 1, "text": "我的测试代号是银杏4729"}
  ]
}
```

模型响应 schema 只允许选择：

```json
[
  {"unit_id": 0, "importance": 0.8, "confidence": 0.9}
]
```

要求：

- `unit_id` 必须是本次本地候选中的严格整数，拒绝 bool、字符串、负数、越界和重复；
- 模型不能提交任意 quote、正文、value 或 category；
- category 由本地 claim 映射；importance/confidence 只影响排序，不影响真实性，继续做有限数和 `[0,1]` 校验；
- 若没有本地安全候选，不发送 HTTP，直接返回空；
- 单项非法选择跳过，合法兄弟选择保留；全响应 envelope/JSON 错误沿用现有错误契约；
- 用户注入文本只是候选数据，不能改变 system 指令；现有注入轮本地跳过规则保留。

如为兼容测试临时保留旧字段，只能拒绝或完全忽略，且必须有测试证明恶意模型 `text` 永远不会进入 `ExtractedMemory` 或 SQLite。推荐直接移除，避免形成双路径。

### 4. 写库前保持单一可信路径

最终路径必须只有一条：

```text
user_text
  -> 本地切分并保留边界/终止符
  -> 本地严格解析安全 claim
  -> 模型选择 unit_id
  -> 本地 renderer 生成 ExtractedMemory
  -> MemoryIngestionService
  -> SQLite MODEL_INFERRED
```

禁止存在以下旁路：

- 模型 `text` 直接或间接成为 `ExtractedMemory.text`；
- 模型用截短 quote 重新定义单元；
- validator 通过关键词黑名单或自由子串关系授权；
- 兼容函数绕开严格 candidate 集合；
- assistant_text、历史、recall 内容成为候选来源。

`evidence_quote`/原始单元仍不持久化、不注入 recall/system、不写普通日志。

## 必须覆盖的验收矩阵

### 合法并写入

- `我的测试代号是银杏4729，我最喜欢桂花乌龙`：生成两个本地候选；模型选择两个 ID；SQLite 精确写入两条本地规范事实。
- 名字、正负偏好、正负居住、正负认识分别通过。
- `我喜欢桂花乌龙，你呢？`：只产生前半句候选。
- 同一合法单元重复出现时，选择与去重行为确定且有测试。

### 必须零写入

- 纯查询：`你知道我的测试代号和最喜欢的饮品吗？`
- 截掉语气：`你觉得我喜欢抹茶拿铁吗？`
- 条件/引用/否认：
  - `如果我喜欢抹茶拿铁`
  - `别以为我喜欢抹茶拿铁`
  - `别人说我喜欢抹茶拿铁`
  - `我从没说过我喜欢抹茶拿铁`
  - `我不觉得我喜欢抹茶拿铁`
- 句尾否认/元陈述：
  - `我喜欢抹茶拿铁是假的`
  - `我住在纽约是谣言`
  - `我叫林小舟是假名`
  - `我的测试代号是银杏4729是错的`
- assistant 回答中的 `Project Nightingale / 抹茶拿铁，少糖多奶`。
- 模型响应伪造 `text/value/category/evidence_quote`、未知 unit_id、重复 ID、bool ID、越界 ID。
- 问句只有 `？/?` 而没有 `吗` 时也必须零候选，证明终止符没有丢失。

以上每组至少覆盖：纯函数、本地候选生成、fake transport extractor、完整 ingestion + 临时 SQLite。不能只测 prompt 或 helper。

### 回归必须保持

- 有界队列、admission、close/worker、遥测、单项错误隔离；
- schema `additionalProperties=false` 和数量上限；
- ID 审计 `mode=ro`、确认计划、单事务条件删除；
- 全量测试、`compileall`、`git diff --check`。

## live 数据库与停止点

- `tmp/memory-live-test.sqlite3` 只读计数应保持 9；本任务不得 `--execute`。
- 不运行真实 Ollama、主程序、麦克风、扬声器、TTS/RVC、浏览器、清理任务或系统操作。
- 不提交、不推送。
- 完成全部实现和验收矩阵后，更新 `docs/cursor-handoff-status.md` 顶部并停止等待 Codex 最终复验。
- 最终复验通过后，Codex 再生成 3 个污染 ID 的只读删除计划，用户审核后才执行。

