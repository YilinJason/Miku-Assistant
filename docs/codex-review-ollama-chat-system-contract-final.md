# Codex 最终验收：Ollama `/api/chat` system 消息契约

## 结论

Round 1 通过纯离线最终验收，未发现需要退回 Cursor 的阻断项。动态 system 现在会作为 `/api/chat` 的首条 `role=system` 消息发送；流式与非流式路径共用同一消息构造边界，且不再发送无效的顶层 `system` 字段。

这解决了真实验收中“SQLite 与 recall 正确、模型却未使用记忆”的已确认传输缺陷。最终效果仍需使用保留的 `tmp/memory-live-test.sqlite3` 做一次真实 Ollama 文本复验。

## 已验收行为

- `OllamaClient._build_chat_messages()` 构造 `system → 最近历史 → 当前 user`，返回请求副本且不污染 `ConversationHistory`。
- `generate_result()`、聚合 `stream=True` 路径与 `generate_stream()` 真流式路径不再向 `/api/chat` 写顶层 `system`。
- `system=None` 或空字符串维持原来的历史与当前 user 序列。
- system 只属于本次请求；成功后短期历史仍只提交原 user/assistant 完整轮。
- 新增端到端 fake 穿过 `SqliteMemoryStore → MemoryQueryService → MemoryAwareLLMClient → OllamaClient → mocked requests.post`，最终 HTTP JSON 的首条 system message 同时包含 `银杏4729`、`桂花乌龙` 与 `subject=user`。
- 非流式失败和流式提前关闭继续保持不提交半轮或双提交。
- ADR、工程基线与 handoff 已同步最终 HTTP payload 契约。

## Codex 独立验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 343 tests in 10.874s ... OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest -q tests.test_ollama_chat_system_contract
→ Ran 8 tests in 0.030s ... OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
→ OK

git diff --check
→ OK（仅既有 LF/CRLF 提示）
```

全部检查为 fake HTTP、临时 SQLite 和纯离线测试。未启动真实 Ollama、主程序、麦克风、扬声器、TTS/RVC、浏览器、清理任务或系统操作；未修改 `tmp/memory-live-test.sqlite3`；未提交、未推送。

## 下一步真实复验

从仓库根目录启动纯文本模式：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 main.py --config tmp\memory-live-test.yaml --mode text
```

看到程序内的 `您:` 提示后输入：

```text
这是一个全新的会话。你知道我的测试代号和最喜欢的饮品吗？
```

通过条件：回答同时包含 `银杏4729` 与 `桂花乌龙`，不再出现 `Project Nightingale`、`抹茶拿铁` 或不知道既有记忆的否认语义；日志仍应显示 recall 命中且 `error=-`。完成后输入 `exit`，等待助手正常关闭。
