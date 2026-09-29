# Codex Review：长期记忆产品接线 Round 2

## 结论

Round 1 的五组问题均已按原触发条件修复：turn_id 与提交失败开放、底层流显式关闭、runtime 构造回滚、关闭所有权与助手重试、严格且禁用兼容的配置校验均通过确定性复现。全量 254 项离线测试、`compileall` 和 `git diff --check` 通过。

暂不做最终验收。产品接线仍缺少三个现有 CLI 调用需要的兼容成员；启用 memory 后会在进入对话前触发 `AttributeError`。另有一个 Round 1 明确要求的 timeout 入口仍会把 bool/字符串预先转换为浮点数。只需完成以下最小修复，不扩大范围。

## 必须修复

### 1. P1：`MemoryAwareLLMClient` 未保留 CLI 和语音预检实际使用的客户端接口

位置：`src/memory/aware_client.py`；实际调用方：`src/cli/app.py:77`～`src/cli/app.py:82`、`src/cli/voice_preflight.py:65`～`src/cli/voice_preflight.py:69`

启用 memory 后，`assemble_default()` 把 `assistant.llm_client` 替换成 `MemoryAwareLLMClient`。装饰器只代理了 `check_connection()`，没有代理：

- `model`
- `check_model_available()`
- `list_models()`

Codex 确定性复现：底层 fake 同时实现上述三项，包装后访问三项均为 `AttributeError`。因此文本 CLI 的启动检查和语音 preflight 都可能在真实对话前失败。

修复要求：

- 按现有 `OllamaClient` 签名显式代理 `model`、`check_model_available()`、`list_models()`；保持底层返回值，不复制模型状态；
- 继续保留已有 `check_connection()`、`start_warmup()`、生成、历史与清空入口；不要用 `__getattr__` 宽泛透传掩盖接口边界；
- 用具有上述成员的 fake 包装后，直接验证代理值与调用次数；
- 增加一条产品调用方行为测试：至少通过 `check_voice_preflight()` 或等价 CLI 检查路径证明启用包装后不再 `AttributeError`。必须 fake STT/Ollama，不启动设备或服务。

### 2. P2：`MemoryRuntime.close(timeout=...)` 仍在校验前执行 `float()`

位置：`src/memory/runtime.py` 的 `MemoryRuntime.close()`

当前覆盖参数使用：

```python
timeout_f = self.settings.close_timeout if timeout is None else float(timeout)
```

这会把 `True` 转成 `1.0`、数字字符串转成浮点数，再传给 ingestion；与 Round 1 “所有 timeout 入口拒绝 bool/非数值/非 finite”要求不一致。

Codex 复现：

```text
runtime.close(timeout=True)
ingestion 实际收到 timeout=1.0 (float)
调用被接受并返回 drained=True
```

修复要求：

- 覆盖 timeout 在转换前使用严格 finite 数值校验，拒绝 bool、字符串、NaN、Infinity 和负数；允许 0 与有限正数，语义与 ingestion 一致；
- 非法参数不得调用 `ingestion.close()` 或 store `close()`，不得改变 runtime 所有权/关闭状态；
- 添加上述非法集合与 0/正数的行为测试。

## Round 2 已验证通过的部分

- turn_id 工厂异常时，非流式仍返回原 `SUCCESS`，流式仍交付原 `COMPLETED`。
- 外层流读取 delta 后提前关闭，底层 close 恰好一次且未提交记忆。
- extractor 构造失败后已创建 store 被关闭。
- store 首次关闭失败保留所有权，第二次关闭成功且最终只完成一次。
- ingestion worker 超时后，助手第二次 shutdown 只重试 memory；TTS 不重复关闭，延迟写入保留。
- `enabled=false` 忽略损坏的休眠子配置；启用时 bool/float/数字字符串整数均被拒绝。
- transport 拒绝 NaN/Infinity/bool timeout。

## 独立验证证据

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 254 tests in 6.047s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
→ OK

git diff --check
→ OK
```

另运行了 Round 1 全部纯 fake 复现及本轮两个缺口复现；未启动真实 Ollama、主程序、麦克风、扬声器、TTS、RVC、浏览器或系统操作。

## 修复后验证

完成最小修复后重跑全量离线测试、`compileall` 和 `git diff --check`，更新 ADR、工程基线和 handoff，停止修改等待 Codex 最终验收。不得提交、推送或修改无关模块。
