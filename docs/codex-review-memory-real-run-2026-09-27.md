# 真实 Ollama 长期记忆文本验收：资源与质量复核

## 结论

本次 `diagnose_memory.py --execute --report tmp/memory-text-report.json` 已真实运行，但**未通过质量验收**（exit 2）。当前证据没有显示 Python、memory worker 或 Ollama 在命令结束后持续泄漏资源；用户感受到的卡顿更符合模型冷加载、同一张显示 GPU 上的高强度推理和 Ollama 提示缓存增长造成的瞬时资源竞争。

在完成资源治理和误提取修复前，继续保持 `memory.enabled: false`，不要重复运行真实验收。

## 实际结果

- `pref_drink`：pass，`52069 ms`，其中首个请求包含模型冷加载。
- `profile_city`：pass，`4029 ms`。
- `ephemeral_math`：warning，错误提取 1 条 `other`。
- `prompt_injection`：warning，错误提取 4 条 `other`。
- wrapper：pass，`13731 ms`；关闭状态为 `drained=true`、`worker_alive=false`、`pending=0`、`incomplete=false`。
- 报告结论：`passed=false`、`exit_code=2`；正式记忆库未触碰。

## 资源证据

Ollama `server.log` 对应本次运行记录：

- GPU discovery watchdog 曾超时一次，随后继续使用旧的可用显存值。
- `llama-server started in 43.40 seconds`；首个 `/api/chat` 总耗时约 `49.998 s`，与报告首场景 `52.069 s` 对齐。
- 模型大小约 `7766.86 MiB`，49/49 层全部 offload 到 GPU。
- CUDA 模型缓冲 `6952.32 MiB`，KV 缓冲 `512 + 640 MiB`，主要计算缓冲 `246.04 MiB`；仅这些明确项已约 `8.35 GiB` 显存。
- 运行使用 `n_ctx=8192`；Ollama 提示缓存上限为 `8192 MiB`，本轮日志中缓存曾增长到约 `1747.903 MiB`。
- 模型加载前系统内存总量 `63.1 GiB`、空闲 `32.4 GiB`、空闲交换空间 `24.0 GiB`，GPU 空闲约 `14.8 GiB`。这不支持“运行开始前已接近内存耗尽”的判断。
- 本工具顺序产生 8 个 `/api/chat` 请求；首个之后的请求约为 `1.00–4.65 s`，说明 52 秒主要是冷加载，不是每次提取都持续变慢。

命令完成后的只读快照：

- 没有 `ollama`、`python`、`miku` 或 `rvc` 相关进程。
- RTX 4070 Ti SUPER 显存占用约 `1498 MiB`、利用率 `17%`、温度 `33°C`、功耗约 `13.33 W`。

因此目前只能排除**已观察到的退出后持续泄漏**，不能仅凭一次运行证明长时间进程绝无缓慢泄漏。若要验证后者，应先增加进程级采样，再执行受控的多轮 soak test；不应在当前会造成明显卡顿的版本上直接重复真机压测。

## 已确认的代码因素

1. `memory.extractor.*` 为空时回退主 `ollama.*`，所以提取器使用同一个 `Miku_Assistant-emotion:latest` 大模型。
2. 该模型当前按 8192 context 加载；简单结构化提取没有独立的小上下文限制。
3. 提取器只设置 `temperature=0`，未限制 `num_ctx` 与 `num_predict`。
4. 诊断继承 `keep_alive: 30m`。这会请求 Ollama 在请求结束后继续驻留模型；它是有意保活，不等同泄漏，但会延长显存占用。
5. 报告只记录墙钟时间，没有保存 Ollama 响应已有的 `load_duration`、prompt/eval token 与阶段耗时，因此无法单靠报告区分加载、提示处理和生成。
6. 当前后台 ingestion 失败开放且前台不等待，但没有同机 Ollama 前台优先级协调；正式启用后仍可能让后台提取阻塞下一轮前台请求。

## 处置决定

- 默认长期记忆继续关闭。
- 下一轮先补低成本、可验证的资源边界与报告遥测，再由用户决定是否复跑真实文本验收。
- 不自动下载或切换模型，不改 Ollama 服务端环境，不运行麦克风、TTS/RVC 或主程序。
- 不把本次质量失败归因于资源问题：临时算术和 prompt injection 的误提取必须独立修复并复验。

下一里程碑见 `docs/cursor-next-memory-performance-quality.md`。
