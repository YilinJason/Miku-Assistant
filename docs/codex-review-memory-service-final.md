# Codex 最终评审：长期记忆应用服务

## 结论

长期记忆应用服务、异步提取边界、确定性词法检索和上下文预算通过最终离线验收。Round 1 与 Round 2 发现的并发、生命周期和截断状态问题均已回归验证。本里程碑未接入真实 Ollama 提取器、对话提示词或产品装配。

## 已验证

- 只有成功且正文非空的对话轮可入队；提取与单条写入失败保持失败开放，不杀死 worker。
- 提取结果以 `MODEL_INFERRED` 写入，非法候选被逐条拒绝，日志不包含完整记忆正文。
- 满队列 `drain=True` 不删除已接受任务；`submit()` 与停止边界在线性化锁协议内闭合。
- worker 遇到 `KeyboardInterrupt` / `SystemExit` 后不把异常当普通失败；替代 worker 可继续排空，`pending` / `drained` 状态反映实际结果。
- owned store 在关闭超时且 worker 仍存活时保持开放和所有权；后续关闭在写入完成后只关库一次。
- 词法召回有界、排序确定、无匹配返回空；额外一个候选只用于探测 has-more，不参与计数或评分。
- 候选总数小于、等于和大于上限的 `truncated_candidates` 语义正确，最大候选上限仍保持有界。
- 上下文同时遵守条数与字符预算，以 JSON Lines 表达并标记为不可信历史资料。
- `ScoredMemory.score` 与停止超时拒绝 NaN、Infinity 和 bool 等非法值。
- 现有语音、STT、持续监听、会话历史及 SQLite 持久层回归继续通过。

## 独立边界复现

owned store 超时关闭：

```text
first  = timed_out=True, worker_alive=True, drained=False, pending=1
second = timed_out=False, worker_alive=False, drained=True, pending=0
saved=['saved']
closed=True
add_after_close=False
close_calls=1
```

候选数量恰好等于上限：

```text
candidate_count=2
truncated_candidates=False
ids=['a', 'b']
```

## 独立验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 232 tests in 6.002s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
→ OK

git diff --check
→ OK
```

全部为纯离线测试；未启动真实 Ollama、麦克风、扬声器、TTS、RVC、浏览器或系统操作。未提交、未推送。

## 未验证边界

- 真实 Ollama/HTTP 记忆提取器的延迟、失败与资源占用。
- 记忆上下文注入提示词后的召回质量、幻觉风险与隐私控制。
- 产品装配、启动和关闭路径中的实际线程与数据库生命周期。
- 语义或向量检索；当前交付仅为确定性词法检索。

下一里程碑可以实现真实提取适配器与产品接线，但应继续保持异步、失败开放、有界召回和可关闭生命周期，并先用 fake 固定装配与注入契约。
