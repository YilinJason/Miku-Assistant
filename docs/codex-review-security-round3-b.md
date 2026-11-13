# Codex Review：安全硬化 Round 3B

> 结论：**通过 Round 3B 最终离线验收。** F1–F5 均已闭环，可进入 Round 3C。  
> 基线：同一未提交工作区中的 Round 3A（已通过）+ Cursor Round 3B + F1–F5 返修。  
> 边界：只修本文件发现项；不得进入 Round 3C 或自然陪伴；不得运行真实服务/设备；不得读取或删除 `data/memory`；不得提交或推送。

## Codex Final Review（F5 返修后）

- 独立反例确认：流式 302 在返回 HTTP failure 前 `close()` 恰好一次，零正文、零历史提交、单次 HTTP；GET 与 memory transport 同样先关闭再拒绝；`close()` 自身失败不覆盖 redirect 结果。
- F1–F4 复验继续通过：伪造端点不能绕过策略；TypeError 不二次请求；diagnostic 共享配置策略；空端口严格拒绝。
- Codex 独立运行 Round 3B 专项 **39 tests / OK**、全量 **510 tests / OK**、`compileall` OK、`git diff --check` OK（仅 Git 行尾转换提示）。
- 未运行真实 Ollama/HTTP、云 STT/TTS、RVC 或音频设备；真实网络和语音体验留待后续人工验收。

## Round 1 findings

### F1 [P0] `SealedModelEndpoint` 可伪造 `kind=loopback`，直接绕过远端许可

- 位置：`src/core/network_endpoint.py:34`、`src/llm/ollama_client.py:73`、`src/memory/ollama_extractor.py:210`
- 现状：`SealedModelEndpoint` 是公开可构造 dataclass。两个消费方只在 `kind != loopback` 时重新授权，因此以下对象被直接接受并向远端组装请求：

  ```python
  forged = SealedModelEndpoint("https://evil.example", ModelEndpointKind.LOOPBACK)
  OllamaClient(model="m", sealed_endpoint=forged)
  RequestsOllamaChatTransport("http://localhost", sealed_endpoint=forged)
  ```

- 要求：消费方不得信任调用者提供的 `kind`。必须由 `base_url` 重新得到规范化 URL/kind 并验证 capability 一致性，或把构造能力收窄到不能伪造且消费方能验证的模块内工厂。伪造的 base/kind 组合必须在任何 HTTP 前 fail closed。主聊天、memory transport/runtime 都补反例。

### F2 [P1] `TypeError` 兼容回退会重复请求并移除 `allow_redirects=False`

- 位置：`src/llm/ollama_client.py:126,140`
- 现状：`_request_post/_request_get` 捕获任意 `TypeError` 后删除 `allow_redirects` 并再次调用。`TypeError` 可能来自替身/适配器内部，而不是函数签名；第一次调用可能已产生副作用，第二次请求还恢复了默认跟随重定向。
- 要求：删除该回退。所有注入 fake/stub 必须接受并断言 `allow_redirects=False`。补测试：fake 在函数内部抛 `TypeError` 时只调用一次，不发生无安全参数的第二次调用；POST/GET 均覆盖。

### F3 [P1] 官方诊断 CLI 没有使用配置生成的 `SafetyPolicy`，远端许可无法按同一规则生效

- 位置：`diagnose_memory.py:95`、`src/memory/diagnostic.py:867,1327`
- 现状：CLI 调用 `run_diagnostic` 时不传策略，`run_diagnostic` 也不从 `config` 创建策略。因此即使配置显式允许远端模型，`resolve_diagnostic_endpoint` 仍以“缺少 SafetyPolicy”拒绝。即便调用方手工传入策略，默认 wrapper 的 `_default_llm_factory` 又构造了不带策略的 `OllamaClient`，远端执行会在更晚阶段失败。
- 要求：diagnostic 默认使用 `SafetyPolicy.from_app_config(config)`；显式注入策略仍优先用于测试。解析、memory transport 与默认 wrapper LLM 必须共享同一实例或由其授权的同一端点。补纯离线 fake：远端 HTTPS + permission=true 能贯通到 fake transport/LLM 且零真实 HTTP；permission=false 在临时目录/HTTP/线程前拒绝；远端 HTTP 仍要求双许可。

### F4 [P2] 显式空端口被静默规范化为无端口

- 位置：`src/core/network_endpoint.py:96`
- 复现：`classify_and_normalize_model_url("http://localhost:")` 返回 `http://localhost`。
- 要求：显式出现端口分隔符但端口为空必须作为非法端口拒绝，IPv4/hostname/IPv6 均补表驱动测试；不要误拒绝合法 IPv6 字面量中的冒号。

## 返修验证

1. 新增针对 F1–F4 的反例测试，不得只改实现。
2. 运行 Round 3B 专项、全量 unittest、compileall、`git diff --check`。
3. 更新 ADR、工程基线和 `docs/cursor-handoff-status.md` 顶部，明确返修内容与验证数量。
4. 完成后停止等待 Codex 复验，不得进入后续阶段。

## Round 2 finding

### F5 [P1] 拒绝 redirect 时流式 HTTP 响应未关闭，长期运行可能占用连接/句柄

- 位置：`src/llm/ollama_client.py:116-134,455-458`、`src/memory/ollama_extractor.py:236-265`
- Codex 独立反例：给 `OllamaClient._request_post` 注入 `status_code=302`、带 `close()` 计数的响应，并以 `stream=True` 调用。`_request_post()` 在返回前由 `reject_redirect_status()` 抛出，因此 `generate_stream()` 的局部 `response` 仍为 `None`，外层 `finally` 无法关闭该响应；实测 `closed=False`。GET 与 memory transport 的 3xx 拒绝路径也没有显式关闭。
- 风险：真实流式 `requests.Response` 可能仍持有 socket/连接池资源；恶意或错误配置的端点持续返回 3xx 时，会把安全拒绝路径变成资源泄漏路径，违背长期运行与失败回收要求。
- 要求：在任何 3xx 拒绝抛错前 best-effort 关闭响应，关闭失败不得覆盖原始 redirect 拒绝；不依赖垃圾回收。主聊天 POST、GET 和 memory transport 使用同一语义。不要重新开放 redirect，也不要恢复 TypeError 二次请求。
- 反例测试：
  1. `generate_stream()` 收到 302 时输出单个 HTTP failure、零正文、零历史提交，并且响应 `close()` 恰好一次；
  2. GET 302 在返回 `False` 前关闭响应且只请求一次；
  3. memory transport 302 在抛 `MemoryExtractionTransportError` 前关闭响应且只请求一次；
  4. `close()` 自身抛异常时仍保留原来的 redirect 拒绝类型/结果，不产生第二次 HTTP。

## Round 2 返修边界

1. 只修 F5，不进入 Round 3C 或自然陪伴。
2. 不运行真实服务/设备，不读取或删除 `data/memory`，不提交、不推送。
3. 运行 Round 3B 专项、全量 unittest、compileall、`git diff --check`；更新 ADR、工程基线和交接顶部后停止等待 Codex 复验。
