# 真实 Ollama 长期记忆文本验收入口：Codex Round 2 Review

## 结论

Round 1 的四项阻断已修复：accepted-pending 关闭 dequeue 空窗，wrapper 使用清空后的 store 并校验本轮唯一事实，去重/recall/context 已参与结论，本地解析已拒绝未知字段。Codex 重新运行 273 项离线测试、`compileall`、`git diff --check` 与默认 dry-run，均通过。

本轮仍暂不最终验收，只需修复一个独立的参数校验缺口。不得扩大到真实服务、记忆业务、语音或设备；不得运行 `diagnose_memory.py --execute`；不得提交或推送。

## 必须修复

### P1：dry-run 会为不可用 endpoint 返回成功，非法端口被错误归类为契约失败

位置：`src/memory/diagnostic.py:223`～`src/memory/diagnostic.py:278`，`diagnose_memory.py:72`～`diagnose_memory.py:92`

dry-run 的职责是完成参数校验，但当前仅为 `urlparse` 临时补 `http://` 以提取 host，没有把补全后的 URL保存到 `DiagnosticEndpoint.base_url`，也没有限制 scheme；读取 `parsed.port` 的 `ValueError` 亦未转换为 `MemoryValidationError`。

Codex 的纯 dry-run 复现：

```text
diagnose_memory.py --base-url localhost:11434
→ exit 0（但 execute 会把无 scheme 的原字符串交给 requests）

diagnose_memory.py --base-url ftp://localhost:11434
→ exit 0（requests HTTP transport 不支持 ftp）

diagnose_memory.py --base-url http://localhost:notaport
→ exit 1，诊断失败: ValueError: Port could not be cast...
```

前两种会让 dry-run 对真实执行必然失败的参数给出错误绿灯；第三种应属于参数/配置错误，而不是 HTTP/JSON/关闭契约失败。

修复要求：

- 为诊断 endpoint 定义并测试明确规则：只允许 `http` / `https`。
- 缺少 scheme 时可选择拒绝，或统一规范化为 `http://...`；若选择规范化，必须把规范化后的 URL 写入 `DiagnosticEndpoint.base_url`，确保 execute 与 dry-run 校验的是同一个值。ADR/使用文档写明决定。
- 捕获非法端口等 `urlparse`/port 解析错误并转换为 `MemoryValidationError`，CLI 返回参数错误退出码 2。
- 拒绝会破坏 `base_url + /api/chat` 拼接的 query/fragment；路径前缀是否允许可自行决定并测试，不要静默构造畸形 URL。
- 新增纯函数或可注入 `main(argv)` 测试，证明上述无效值不会调用 HTTP/store/ingestion factory。
- 保持报告只暴露 host/port/model/timeout/keep_alive，不泄露 URL 密码、query token 或环境密钥。

## 回归要求

必须保留 Round 1 全部确定性故障测试，并仅运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 diagnose_memory.py
```

不得运行 `diagnose_memory.py --execute`。完成后更新 `docs/cursor-handoff-status.md`，记录 URL 规则、实际测试数量和真实 Ollama 仍未运行，然后停止等待 Codex 最终验收。

## Codex Round 2 实际验证

```text
unittest discover → Ran 273 tests in 6.720s … OK
compileall -q src tests diagnose_memory.py → OK
git diff --check → OK（仅既有 LF/CRLF 提示）
diagnose_memory.py（无 --execute）→ exit 0
```

未运行真实 Ollama、`--execute`、麦克风、扬声器、TTS、RVC、角色、主程序或系统操作；未提交、未推送。
