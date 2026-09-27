# ADR：Ollama 结构化生成结果

- **状态**：已采纳（第六小步；兼容层 + 异常/流式边界补齐）
- **日期**：与 `docs/engineering-baseline.md` 本轮更新同步

## 问题

`generate() -> str` 把模型正常回复与调用失败都变成字符串。调用方只能靠是否以「错误:」开头猜测，导致：

1. 模型正常生成「错误: …」会被误判为失败（或相反：真实失败被当台词播报）。
2. Handler / TTS / CLI 无法在类型层面区分成功、空回复与失败，错误播报问题缺少可靠依据。

## 决策

1. 新增 `src/llm/generate_result.py`：`GenerateStatus` + 冻结的 `GenerateResult`（单一 `status`，另加互斥的 `text` / `error_detail`）。
2. 状态集合：`SUCCESS` | `EMPTY` | `CONNECTION_ERROR` | `TIMEOUT` | `HTTP_ERROR` | `RESPONSE_ERROR`（解析 / 结构 / 非字符串正文）。
3. 新增 `OllamaClient.generate_result(...)`：按异常与正文类型分类；**不**用文本前缀判断成败。
4. `generate(...)` **仅**调用一次 `generate_result` 再 `to_legacy_string()`。
5. 仅 `SUCCESS`（非空 str）提交历史；失败与空回复不改历史。
6. 保留非流式与流式聚合两条路径；不改请求协议、`done` 判定、窗口与存储容量。
7. **异常说明兜底**：`str(exc)` 为空（如无参 `Timeout()` / `ConnectionError()`）时使用稳定非空兜底；`GenerateResult.failure` 亦拒绝空 detail。
8. **`requests.exceptions.JSONDecodeError`**：在宽泛 `RequestException` **之前**捕获，归类为 `RESPONSE_ERROR`（该类同时是 `RequestException` 子类）。
9. **流式 `message`**：只要字段存在就必须是 dict；非 dict → `RESPONSE_ERROR`，不得静默跳过并提交部分回复。
10. **本轮不改** Handler、TTS、CLI；不加自动重试、新依赖或数据库。

## 兼容层文案（明确，非「完全不变」）

| 结局 | `to_legacy_string()` |
|------|----------------------|
| SUCCESS | 原文（可含「错误:」） |
| EMPTY | `""` |
| CONNECTION / TIMEOUT / HTTP | `错误: 无法连接到Ollama服务 ({detail})` |
| RESPONSE_ERROR（含 JSONDecodeError、结构错误） | `错误: {detail}` |

**相对第六小步前的变化**：

| 场景 | 以前 | 现在 |
|------|------|------|
| `message.content` 为 list/number 等 | 可能写入历史或异常字符串含糊 | `RESPONSE_ERROR`；兼容串 `错误: …` |
| 模型文本以「错误:」开头 | 字符串成功返回 | `SUCCESS`，兼容串仍为原文 |
| `requests.exceptions.JSONDecodeError`（如 `response.json()`） | 落入 `RequestException` → **连接失败文案** | `RESPONSE_ERROR` → **`错误: {detail}`**（有意修正误分类） |
| 无参 `Timeout()` / `ConnectionError()` | `str(exc)==""` 时构造失败结果可能再抛错 | 使用「请求超时」「连接失败」等非空兜底；兼容串仍带连接前缀 |
| 流式 `message` 为非 dict | 可能静默跳过并提交已聚合部分 | `RESPONSE_ERROR`，不提交 |

## 明确未解决

- **旧调用链仍只消费 `generate() -> str`**，错误被当作角色台词播报的问题**尚未解决**。
- 本轮只提供可区分的结构化依据，供后续接入。

## 替代方案

- 多个布尔字段（`ok`/`empty`/`error`）：可非法组合，拒绝。
- 立刻改 Handler/TTS：超出本小步，拒绝。
- 以「错误:」前缀判断：与模型台词冲突，拒绝。
- 继续把 `JSONDecodeError` 当连接失败：误导调用方，拒绝。

## 迁移与回退

- 新代码优先 `generate_result`；旧代码可继续 `generate`。
- 回退：删除 `generate_result.py`，将逻辑内联回仅返回 str 的 `generate`。

## 离线验证

```text
py -3 -m unittest discover -s tests -v
→ Ran 65 tests in 0.035s … OK
```

见 `tests/test_generate_result.py`（含空异常兜底、真实 `JSONDecodeError`、流式非 dict `message`）；原有用例继续通过。
