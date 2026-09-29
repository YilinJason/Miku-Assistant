# Cursor 最终修正：连接词存在时禁止回退单事实解析

## 结论

整段原子性主体修复有效，但还有一个确定的 all-or-nothing 旁路，当前不能通过最终验收。

`try_parse_segment_units()` 目前调用 `_split_supported_conj_pieces()`：

- 没有连接词时返回 `None`；
- 有连接词但一侧为空/结构非法时也返回 `None`。

调用方无法区分这两种状态，并在 `None` 时回退 `_parse_single_unit_user(seg_text)`。由于 `我喜欢(.+)` 等模式会吃掉尾部，以下输入被错误接受：

```text
我喜欢咖啡而且。                 -> 用户喜欢咖啡而且
我喜欢咖啡并且。                 -> 用户喜欢咖啡并且
我喜欢咖啡以及。                 -> 用户喜欢咖啡以及
我喜欢咖啡而且而且我住在纽约。   -> 用户喜欢咖啡而且而且我住在纽约
```

## 唯一代码修复

必须让“没有连接词”和“存在连接词但结构非法”成为不同状态。可采用任一清晰实现：

1. 在 `try_parse_segment_units()` 先检查 `_SUPPORTED_CONJ_RE.search(seg_text)`：
   - 未命中才允许走单事实完整解析；
   - 命中后 `_split_supported_conj_pieces()` 返回 `None`，立即整段拒绝；
   - 命中且得到 pieces，继续现有 child all-or-nothing 解析。
2. 或让切分函数返回显式三态结果，保持同一语义。

禁止在“已发现连接词但拆分失败”时回退软解析、单事实解析或保留部分 child。不要扩展模型权限、renderer 或正式写库路径。

## 回归测试

在 `SegmentAtomicityTests` 增加：

- 上述四条输入候选必须为空；
- fake transport 的 `payloads == []`；
- 临时 SQLite `count_memories() == 0`；
- 保留现有合法 `并且|而且|以及` 双事实、问句/否认、`并蒂莲`、span 和 renderer 测试。

可补一个合法负向 child 样例，使用至少两个字的值：

```text
我喜欢咖啡而且我不喜欢红茶。 -> 用户喜欢咖啡 + 用户不喜欢红茶
```

## 验收与边界

依次运行全量 unittest、compileall、`git diff --check`。不得运行真实 Ollama、主程序、设备或清理任务；live SQLite 只读；不得删除记录；不要提交、不要推送。更新 `docs/cursor-handoff-status.md` 顶部后停止等待 Codex 最终复验。
