# Codex Review：记忆出处契约 Round 3 暂未通过

日期：2026-09-28  
结论：Round 3 已阻断候选正文中的未验证尾部和复合事实，但 `evidence_quote` 仍可从用户的疑问或不确定子句内部截取一个看似肯定的子串。请完成下面的 Round 4 最小修复后停下，等待 Codex 最终复验。不要运行真实 Ollama、主程序、设备或 live 库删除；不要提交或推送。

## 阻断项：证据子串脱离用户原句语境后可把问题变成事实

`validate_candidate_user_evidence()` 只确认 `evidence_quote` 是 `user_text` 的连续子串，随后只解析这个子串。模型可以去掉包围它的疑问词、句末 `吗/？` 或不确定前缀，使问题内部的文字伪装成陈述。

Codex 已在当前实现复现：

```text
user="我叫林小舟，你觉得我喜欢抹茶拿铁吗？"
fact="用户喜欢抹茶拿铁"
quote="我喜欢抹茶拿铁"
validate=True, should_skip_extraction=False
OllamaMemoryExtractor(fake transport) accepted=['用户喜欢抹茶拿铁']

user="我住在上海，你记得我喜欢咖啡吗？"
fact="用户喜欢咖啡"
quote="我喜欢咖啡"
validate=True, should_skip_extraction=False

user="我叫林小舟，我不确定我喜欢抹茶拿铁"
fact="用户喜欢抹茶拿铁"
quote="我喜欢抹茶拿铁"
validate=True, should_skip_extraction=False
```

整轮因为同时含一条合法自我陈述，所以不会被纯查询准入跳过；第二个候选随后能进入 store。这仍违反“用户明确陈述是唯一事实来源”。

## Round 4 最小要求

1. 验证证据时必须保留其在完整 `user_text` 中的语境。定位 `evidence_quote` 的实际出现位置，并检查包围它的原始子句/原子单元；不得只解析模型截短后的 quote。
2. 只有 quote 所在的完整用户单元本身是明确、可解析、极性一致的陈述时，候选才能通过。完整单元含疑问槽位、`吗/呢/是否/是不是/有没有`、疑问标点，或 `不确定/不清楚/可能/也许/大概/猜测` 等不确定语气时失败关闭。
3. 同一 quote 在原文出现多次时按位置逐一判断：至少存在一个明确陈述位置且能完整支持候选才可通过；不能因为另一个疑问位置而误配，也不能脱离边界拼接。
4. 保留合法混合句：`我喜欢桂花乌龙，你呢？` 中的 `我喜欢桂花乌龙` 可以通过；`我叫林小舟，你觉得我喜欢抹茶拿铁吗？` 中名字可以通过，抹茶候选必须拒绝。
5. 保留 Round 1–3 已完成的 assistant 隔离、极性、原子完整覆盖、逐项容错、schema required、只读审计和原子条件删除。

## 必须新增的离线回归

- 上述三个复现样例在直接验证、extractor fake 路径和完整 ingestion 临时 store 路径中均拒绝污染候选。
- 覆盖模型截掉句末 `吗/呢/？` 后提交 quote 的情况。
- 覆盖 `你觉得我喜欢…吗`、`你记得我喜欢…吗`、`我是不是喜欢…`、`我不确定我喜欢…`。
- 混合句中合法陈述候选保留，疑问/不确定候选拒绝；同一批合法兄弟项不受影响。
- 原有 371 项测试继续通过；live 库只读 count 保持 9，禁止 `--execute`。

## Codex 独立验证结果

- `unittest discover -s tests -q`：371 tests / OK
- `compileall`：OK
- `git diff --check`：OK（仅工作区 LF/CRLF 提示）
- live 库只读计数：9，未删除
- 真实 Ollama、主程序、设备：未运行

