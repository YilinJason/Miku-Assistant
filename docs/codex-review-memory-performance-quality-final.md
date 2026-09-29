# 长期记忆资源治理、诊断遥测与误提取收口：Codex 最终离线验收

## 结论

本里程碑通过最终离线验收。资源上限、诊断短保活、Ollama 原生遥测、本地提取资格边界与前台准入闸已接入；Round 1 的自阻断、流式 lease 泄漏、遥测类型与 ADR 重复问题均已关闭。

本结论只覆盖离线契约。修复后的真实 `diagnose_memory.py --execute`、实际卡顿改善、真实模型提取质量与长时间资源稳定性仍未验证，`memory.enabled` 继续保持 `false`。

## 已验收行为

- 提取器请求使用独立 `num_ctx=2048`、`num_predict=512` 上限；严格拒绝非法类型和值，不改变主聊天客户端配置。
- diagnostic 使用专用 `keep_alive=1m`，支持 CLI 覆盖，不再默认继承主聊天 30 分钟保活；不会在 finally 无条件停止服务或卸载共享模型。
- 报告 schema v2 记录请求绑定的 load/prompt/eval duration 与 token counts；缺失或畸形字段为 null，不影响成功内容解析。duration 只接受非负整数纳秒。
- 临时算术、纯寒暄与明显注入在无稳定记忆信号时本地跳过 HTTP；提取结果仍经过窄过滤，稳定偏好与资料路径保留。该规则是本地资格边界，不声称是完整 prompt-injection 防护。
- `ForegroundAwareAdmissionGate` 在其他前台生成忙碌时拒绝新后台提取；已开始的 HTTP 不宣称可抢占。
- 非流式与流式成功轮均先释放本轮 foreground lease，再提交记忆，不会被自身 lease 丢弃。
- 流式 recall KI/SystemExit、底层同步构造异常、迭代异常、正常结束、提前 close 与底层 close 异常均 exactly-once 释放 lease；底层流仍只关闭一次。
- ADR 只保留一套有序 1～9 决策，首次真实 exit 2 与尚未复验的事实保留。

## Codex 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 284 tests in 6.821s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
→ OK

git diff --check
→ OK（仅既有 LF/CRLF 提示）
```

Codex 另行复现：

- 非流式成功提交时 `foreground_count=0`，返回后仍为 0。
- 流式 recall 抛 `KeyboardInterrupt` 后 `foreground_count=0`。
- `load_duration=1.5` 现映射为 null；合法非负整数按纳秒换算为毫秒。
- ADR 决策与替代方案标题唯一，1～9 编号各出现一次。

全部为纯离线验证；未启动 Ollama、主程序、麦克风、TTS/RVC 或系统操作，未运行真实 `--execute`，未提交、未推送。

## 下一步边界

下一步应由用户显式执行一次修复后的真实文本验收，并观察报告中的加载、提示处理与生成耗时。由于之前出现明显电脑卡顿，应只执行一次，不并行启动主程序或语音链路；若仍发生系统级卡顿，再基于 schema v2 报告决定是否采用独立小型提取模型或增加受控资源采样。
