# 真实 Ollama 长期记忆文本验收入口：Codex 最终离线验收

## 结论

本里程碑通过最终离线验收。实现已提供无历史 JSON Schema 提取请求、线性化 idle 等待、默认零副作用 dry-run、显式真实文本验收入口、临时数据库与脱敏报告，并关闭两轮评审发现的故障边界。

本结论只覆盖离线契约与 fake 链路。`diagnose_memory.py --execute`、真实 Ollama 提取质量、真实延迟与 GPU 争用仍未运行和验证。

## 已验收行为

- `OllamaMemoryExtractor` 请求保持 `/api/chat`、`stream=false`、system + JSON 轮次两条无历史消息；每次生成独立 JSON Schema，包含动态 `maxItems`、`additionalProperties:false` 与 `temperature=0`。
- 本地解析继续拒绝错误根、未知条目字段、非法类别、非字符串正文、越界及 non-finite 数值；缺省字段规则与 ADR 一致。
- `keep_alive` 接受非空字符串与有限数值（含 0），拒绝 bool、空串、NaN、Infinity 和其他类型。
- `wait_until_idle()` 以 accepted-pending 计数覆盖 dequeue 到处理开始之间的空窗；停止、丢弃、异常替代 worker 与关闭状态继续由离线回归覆盖。
- diagnostic 默认只读取并校验配置，不发送 HTTP、不创建 SQLite、不启动 ingestion；只有显式 `--execute` 才可进入真实路径。
- execute fake 路径只使用合成文本与临时 SQLite；场景数据在 wrapper 前清空，本轮唯一事实必须真实进入 system 才能判定命中。
- 精确重复去重、lexical recall 与 context formatter 结果会参与 pass/warning/failure，不再只是展示指标。
- endpoint 缺 scheme 时规范化为 `http://` 并写入实际 endpoint；仅允许 http/https；query、fragment 与非法端口在资源创建前以 `MemoryValidationError` 拒绝。
- 报告不保存完整 prompt、回复或记忆正文，只暴露脱敏 host/port、模型、阶段指标、关闭状态和退出结论。

## Codex 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 276 tests in 6.704s … OK

compileall -q src tests diagnose_memory.py → OK
git diff --check → OK（仅既有 LF/CRLF 提示）
diagnose_memory.py（无 --execute）→ dry-run exit 0
```

补充边界复现：

- `localhost:11434/prefix/` 规范化为 `http://localhost:11434/prefix`。
- `ftp://...`、非法端口、query 和 fragment 均在 `SqliteMemoryStore` 创建前拒绝，store 调用次数为 0。
- CLI 可注入 `main(argv)` 测试确认无 scheme 成功规范化，ftp 与非法端口返回参数错误码 2。
- Round 1 的 gated dequeue、wrapper 空 seed、去重异常、recall/context 空和未知字段测试全部继续通过。

## 未验证边界

- 未运行 `diagnose_memory.py --execute`，因此不宣称当前本机 Ollama 模型已达到提取质量或延迟目标。
- 未验证前台生成与后台记忆提取的同机 GPU 调度。
- 未启动麦克风、扬声器、TTS、RVC、角色、主程序或系统操作。
- 未提交、未推送。
