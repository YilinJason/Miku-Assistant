# Codex 最终验收：Memory Grounding + Storage Hardening Round 1

## 结论

本里程碑通过最终纯离线验收。Grounding 修复与存储硬化 Round 1 的已知阻断项已经闭合；没有发现需要继续交回 Cursor 的缺陷。不得把本结论扩写成真实 Ollama、真实音频链路或存储任务 Round 2/3 已验收。

## 已验收行为

- 长期记忆上下文明确 `subject=user`，旧库第一人称按用户视角解释；新提取的无 owner 第一人称在本地边界被过滤。
- `ConversationHistory` 按完整轮有界裁剪，严格配置已接入默认产品装配，注入 history 保持兼容。
- SQLite retention 在 SQL 候选阶段保护 `USER_CONFIRMED`，按 `importance → created_at → id` 稳定淘汰，年龄与数量共享批次预算，并能表达全受保护导致的不可收敛超限。
- maintenance 只在 ingestion idle 且获得原子 lease 后运行；foreground、pending、extract、maintenance 的准入与释放契约已覆盖确定性竞态。
- `MemoryRuntime.start()` 不重复创建 Timer；旧 generation 不会在 close 后重排；并发 close 串行化，异常后保留所有权并可重试。
- FileCleaner 使用真实流式 `os.scandir`，每个底层 `next()` 都计入硬预算；跨短批次复用活动 sweep，多个规则可继续推进，停止/异常/移除时关闭句柄。
- 临时音频延迟删除使用单一有界 Worker；等待可被 shutdown 中断；TTS 只回收自己拥有的 TempFileManager。
- 默认清理目录限于运行时白名单，不扫描仓库根 `tmp/`，没有执行真实文件清理。

## Codex 独立验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 335 tests in 10.929s ... OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
→ OK

git diff --check
→ OK（仅既有 LF/CRLF 提示）
```

额外复验覆盖：

- 单批扫描预算 1 时底层枚举不超过 1，连续批次最终到达后续目标。
- 多条清理规则在共享预算下均能继续推进，不因前序规则反复重开而永久饥饿。
- maintenance 持有与 foreground pending 时新的 extract 均被拒绝；维护释放后前台先进入。
- 重复 start 只有一个维护 Timer；close 后旧回调不重排。
- 两个及多个并发 close 对 ingestion 的最大并发为 1，成功路径只关闭一次，失败路径可以重试。
- ingestion/store 关闭异常、KI/SystemExit、worker_alive 与重复 close 保持真实状态和所有权。

全部为 pure fake、临时目录和线程契约测试。未启动 Ollama、主程序、麦克风、扬声器、TTS/RVC、浏览器或系统操作；未运行 `diagnose_memory.py --execute`；未删除用户文件；未提交、未推送。

## 仍需真实验证与后续范围

1. 使用保留的 live 测试库真实运行 grounding 问法，确认模型回答同时包含“银杏4729”和“桂花乌龙”，且不出现“不知道/无法记住”等否认语义。
2. 主配置目前 `memory.enabled=false`，`memory.retention.enabled=false`。数据库定期 prune 是显式 opt-in；启用 memory 后仍需单独开启 retention，默认继续保护 `USER_CONFIRMED`。
3. 存储任务 Round 2 尚未开始：按模式装配、纯文本不加载音频/RVC、模型与 GPU 资源协调、无设备 soak。
4. 存储任务 Round 3 尚未开始：RVC 子进程能力白名单、本地端点与云服务隐私边界、日志隐私回归。

本里程碑完成后可先做一次真实长期记忆问答验收，再进入 `docs/cursor-next-storage-performance-security-hardening.md` 的 Round 2。
