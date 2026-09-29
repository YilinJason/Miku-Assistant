# Cursor 任务：修复事实单元的整段原子性（最终阻断项）

## 结论

新的 `unit_id` 可信边界方向正确：模型已经失去 `text`、`value`、`category` 和 `evidence_quote` 的写入权。但当前实现仍未通过最终验收，因为 `src/memory/fact_units.py` 会先按并列连词拆分，再分别接受可解析的前半段。这样会丢失同一标点段末尾的疑问、否认或未解析语境。

当前可复现的错误：

```text
我喜欢咖啡而且我住在纽约？     -> 错误候选：用户喜欢咖啡
我喜欢咖啡并且我住在纽约？     -> 错误候选：用户喜欢咖啡
我叫林小舟以及我住在纽约吗     -> 错误候选：用户叫林小舟
我喜欢咖啡并不是真的           -> 错误候选：用户喜欢咖啡
```

根因有两个：

1. `_split_conj_spans()` 只让最后一个片段继承外层终止符，问号没有约束同一语法段的前半部分。
2. `_CONJ_SPLIT_RE = (?:并|而且|以及)` 把裸 `并` 当作通用分隔符；它会把 `并且`、`并不是真的` 甚至值中的 `并` 拆开，并在剩余片段无法解析时仍保留前面的候选。

## 必须建立的结构不变量

### 1. 标点段先验收，之后才允许拆并列事实

每个由逗号、分号、句号、换行等边界划出的 segment 是语境验收单元。对 segment 先执行完整的失败关闭检查：

- segment 的终止符是 `？` 或 `?` 时，整段零候选；
- segment 任意位置含疑问槽位/疑问语气、不确定语气或明确撤回/否认尾部时，整段零候选；
- 不得把这些标记只附着到最后一个拆分后的 child。

逗号等已经形成独立 segment 的兄弟可以分别处理。因此：

```text
我喜欢桂花乌龙，你呢？
```

仍应只保留第一段 `我喜欢桂花乌龙`。

### 2. 同一 segment 内采用 all-or-nothing 完整解析

只有以下两种情况可以产生候选：

1. 整个 segment 完整解析为一条支持的第一人称事实；
2. segment 由明确支持的并列连接词连接，并且每个 child 都完整解析为一条支持的第一人称事实。

只要同一 segment 中有一个 child 为空、无法解析、含残留前后缀或不是完整自我陈述，整个 segment 都拒绝；不得保留其中看似合法的前半段。跨逗号/分号的独立 segment 不受此规则影响。

### 3. 并列连接词必须按语法识别

- 不要再用裸 `并` 做通用正则切分。
- 至少正确支持完整连接词 `并且`、`而且`、`以及`。
- 连接词只有在左右两侧都能完整解析为受支持事实时才具有分隔作用。
- 值中的普通字符不得被误切，例如 `我喜欢并蒂莲` 必须仍是一条合法事实。

可以把“segment 完整解析 + span 生成”集中为一个本地函数，返回全部 child claim/span 或整体失败。不要在失败后回退到软收集、子串匹配或保留部分 child。

### 4. 保持现有可信边界

- 模型响应仍只能选择 `unit_id`，不能恢复任何自由事实字段。
- `ExtractedMemory.text/category` 仍只能由本地 renderer 生成。
- 无安全候选时仍不得发送 HTTP。
- `text == user_text[start:end]` 的 span 不变量继续成立。
- 不新增兼容旁路，不让旧 `validate_candidate_user_evidence` 回到正式写库授权路径。

## 必须新增的回归测试

候选枚举、无 HTTP、完整 ingestion + 临时 SQLite 三层都要覆盖零写入矩阵：

```text
我喜欢咖啡而且我住在纽约？
我喜欢咖啡并且我住在纽约？
我叫林小舟以及我住在纽约吗
我叫林小舟以及我住在纽约呢
我喜欢咖啡并不是真的
我喜欢咖啡而且这只是举例
```

同时覆盖合法/边界矩阵：

```text
我喜欢咖啡而且我住在纽约。       -> 两条事实
我喜欢咖啡并且我住在纽约。       -> 两条事实
我叫林小舟以及我喜欢乌龙茶。     -> 两条事实
我喜欢桂花乌龙，你呢？            -> 仅第一条事实
我喜欢并蒂莲。                    -> 一条事实，值为“并蒂莲”
```

每个正向 child 都要断言原文 span、规范 renderer 文本和 category。零写入用 fake transport 断言 `payloads == []`，并在临时 SQLite 中断言 `count_memories() == 0`。

## 验收命令与边界

完成后依次运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py diagnose_memory_ids.py
git diff --check
```

只更新测试、ADR/工程基线和 `docs/cursor-handoff-status.md` 的顶部状态。不要运行真实 Ollama、主程序、音频/设备或清理任务；live SQLite 只读；不要执行污染记录删除；不要提交、不要推送。完成本任务后停止，等待 Codex 最终复验，不进入存储 Round 2/3。
