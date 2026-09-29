# Codex Review：长期记忆持久层 Round 2

## 结论

Round 1 的 finite 校验、时间与去重键不变量、SQLite 异常收口、真实事务、schema 原子性、来源不升级及交接历史恢复均通过代码复审。Codex 独立运行 209 项测试通过，`compileall` 与 `git diff --check` 通过。

剩余一个契约偏差，修复后即可最终验收。

## 必须修复：展示正文被静默裁剪

任务要求“正文规范化只用于精确去重键，不得悄悄改写用户可见正文”。当前 `src/memory/models.py::_validate_display_text` 返回 `text.strip()`，并由 `NewMemory` / `MemoryRecord` 写回 `text`，所以首尾空白会从展示正文中消失。

Codex 复现：

```text
NewMemory(text="  原样正文  ").text == "原样正文"
```

修复要求：

- 使用 `text.strip()` 只判断正文是否为空，合法时返回并保留原始字符串。
- `normalize_dedupe_key` 继续负责去首尾空白、压缩连续空白和 casefold；不要改变现有精确去重语义。
- `MemoryRecord.dedupe_key == normalize_dedupe_key(text)` 不变量继续成立。
- 增加测试证明：
  1. `NewMemory` 保留首尾和内部空白；
  2. SQLite 往返保留完整原始展示正文；
  3. 规范化等价的第二次写入仍返回第一条记录及第一条原始展示正文。

## 文档收口

- ADR 的故障契约补充已实现的 `MemoryDataCorruptError`。
- 工程基线明确“展示正文包括首尾/内部空白均原样保存，规范化仅作用于去重键”。
- 在 `docs/cursor-handoff-status.md` 顶部记录 Round 2 修复和实际全量结果，不删除已有历史。

## 验证与边界

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
git diff --check
```

不得接入对话、提取、检索或 UI；不得提交、推送或启动真实设备/模型。完成后停止并等待 Codex 最终验收。
