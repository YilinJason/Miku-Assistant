# Codex Review：记忆出处契约 Round 5 暂未通过

> 后续方案已收敛到 `docs/cursor-next-memory-ingestion-trusted-boundary-final.md`。执行该最终任务，不再单独实施本文原先的局部 Round 6 字符串补丁。

日期：2026-09-28  
结论：Round 5 已把完整包围单元的严格 claim 接入最终写库授权，但 value 对齐仍允许“候选值是用户值的任意子串”。因此句尾否认会被错误当成值的一部分，随后被概括成相反的肯定事实。请完成 Round 6 的最后一项最小修复后停下，等待 Codex 最终复验。不要运行真实 Ollama、主程序、设备或 live 库删除；不要提交或推送。

## 阻断项：自由子串概括可丢掉句尾否认语义

`unit_strictly_supports_fact()` 已严格解析完整单元，但 `_claim_supports_fact()` 最终调用 `_values_compatible()`；只要候选值完整出现在用户 claim 的 value 中就通过。开放的 `(.+)` 会把句尾否认也吞进 value，随后子串规则把它丢掉。

Codex 已复现以下输入全部错误返回 `True`：

```text
user="我喜欢抹茶拿铁是假的"
fact="用户喜欢抹茶拿铁"
quote="我喜欢抹茶拿铁"

user="我喜欢抹茶拿铁这件事只是玩笑"
fact="用户喜欢抹茶拿铁"
quote="我喜欢抹茶拿铁"

user="我住在纽约是谣言"
fact="用户住在纽约"
quote="我住在纽约"

user="我认识Alice是你编的"
fact="用户认识Alice"
quote="我认识Alice"

user="我叫林小舟是假名"
fact="用户叫林小舟"
quote="我叫林小舟"

user="我的测试代号是银杏4729是错的"
fact="用户的测试代号是银杏4729"
quote="我的测试代号是银杏4729"
```

这说明“完整单元解析”仍没有实现值的严格对齐。

## Round 6 最小要求

1. 写库授权中的 value 对齐改为保守等价：规范化后必须相等；禁止任意 `fact_value in user_value` 或反向子串关系。
2. 规范化只能处理不改变语义的差异，例如首尾空白、明确的句末标点和拉丁字母大小写。不要删除中文语义字符、限定词、括号内容或句尾描述；审查 `_norm_value()` 当前把 `的` 作为可 strip 字符的行为。
3. 用户谓词与候选谓词可以使用现有受支持的不同表述，例如“我最喜欢的饮品是 X”与“用户最喜欢喝 X”，但两边解析出的核心 value 必须等价。
4. quote 仍须为原文连续子串；完整包围单元仍须严格解析为恰好一个 claim；Round 1–5 的全部防线保留。
5. 若产品以后需要“安全概括”，另行设计显式、可枚举的转换规则；本轮不要用自由子串恢复它。

## 必须新增的离线回归

- 上述 6 个句尾否认样例在 validator、fake extractor、完整 ingestion 临时 store 路径全部拒绝。
- `我喜欢抹茶拿铁`、`我不喜欢抹茶拿铁`、`我住在纽约`、`我叫林小舟`、测试代号等合法原子事实继续通过。
- 候选比用户值更长和更短都拒绝；只允许保守规范化后的相等值。
- 不得破坏同一批合法兄弟项、极性检查、原子覆盖、语境定位、逐项容错、只读 ID 审计和原子删除。
- live 库只读 count 保持 9，禁止 `--execute`。

## Codex 独立验证结果

- `unittest discover -s tests -q`：383 tests / OK
- `compileall`：顺序独立复跑 OK
- `git diff --check`：OK（仅工作区 LF/CRLF 提示）
- live 库只读计数：9，未删除
- 真实 Ollama、主程序、设备：未运行
