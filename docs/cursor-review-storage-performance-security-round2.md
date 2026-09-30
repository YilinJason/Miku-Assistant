# Codex Review：存储硬化 Round 2 暂不通过

## 结论

Round 2 的方向正确：启动模式已经进入装配边界，纯文本/交互启动不再立即构造语音栈，Ollama 与 RVC 有了协调入口，392 项既有离线测试、`compileall` 与 `git diff --check` 均通过。

但独立复核发现下列资源生命周期、兼容行为与诊断真实性问题。它们会导致语音资源泄漏、默认自动播报失效，或让“串行预热/资源 soak”结论失真。本轮暂不验收。请一次性修复本单，完成后停止等待 Codex 最终复验；不要进入 Round 3。

## Finding 1（P1）：懒加载语音栈不是事务式所有权，失败或并发 shutdown 会泄漏 TTS/RVC

位置：`src/core/assembly.py:98-127`、`src/core/assistant.py:207-270`、`src/core/assistant.py:1059-1140`

`build_voice_engines()` 先创建 TTS，再创建 STT；若 STT 构造抛错，已经创建的 TTS 不会 `shutdown()`。`ensure_voice_stack()` 也可能在 shutdown 已开始或结束后才把局部 TTS/STT 绑定回助手，此时 shutdown 已错过它们。

Codex 确定性复现：

```text
STT 构造失败: tts_created=1 tts_shutdown_calls=0
ensure_voice_stack 与 shutdown 并发:
ensure_ok=True shutting_down=True assistant_tts_bound_after_shutdown=True tts_shutdown_calls=0
```

修复要求：

1. 懒加载在局部变量中构造，并建立明确的局部所有权回滚；STT 构造、预热协调或绑定前任一步失败，都 exactly-once 关闭本轮新建的 TTS/RVC。
2. 将语音懒加载与 shutdown 放入同一生命周期协议（锁、状态机或 generation 均可）。shutdown 开始后不得再发布新语音栈；已经完成构造但失去发布资格的资源必须由构造方关闭。
3. 不得关闭外部注入资源；只回收本轮懒加载实际拥有的对象。
4. 增加 Event 控制测试：STT 构造失败、预热等待异常、构造中 shutdown、绑定前 shutdown、重复 ensure/shutdown；断言无晚绑定、关闭次数恰好一次、失败可按明确契约重试。

## Finding 2（P1）：交互模式的 `auto_tts` / `tts` 开关静默失效

位置：`config/config.yaml:326-335`、`src/cli/text_session.py:100-157`、`src/core/assistant.py:780-805`

主配置当前 `assistant.auto_tts=true`，交互模式又按 Round 2 延迟构造 TTS。文本会话直接把 `use_tts=True` 传入助手，但 `_apply_outcome_side_effects()` 仅在 `self.tts_engine` 已存在时播报，不会触发懒加载。CLI 仍显示“自动语音输出：开启”，实际没有语音，也没有结构化失败提示。

Codex 确定性复现：

```text
interactive + process_text_input(use_tts=True)
tts_created=0 stt_created=0 assistant_tts=None
```

修复要求：

1. 保留按需加载，但在交互模式第一次真正需要 TTS 时显式加载所需资源；不能恢复启动时无条件全量语音初始化。
2. `tts` 开关只有在资源可用时才显示为开启；加载失败需给用户一次明确提示并继续文本对话，不能每轮重复重试/刷日志。
3. 覆盖主配置初始 `auto_tts=true`、运行时从 false 切 true、流式与非流式回复、加载失败后继续文本、关闭期间不得加载。
4. 若把“仅 TTS 输出”和“完整 STT+TTS 语音模式”拆成两个窄加载入口，必须保持所有权与 shutdown 契约一致。

## Finding 3（P1）：串行预热等待超时后仍启动 RVC，默认仍可能同时抢 GPU

位置：`src/core/resource_coord.py:12-112`

`_wait_ollama_warmup()` 丢弃 `wait_for_warmup()` 的布尔结果；`run_coordinated_warmup()` 无论等待是否超时都继续 `prepare_rvc()`。等待方法抛普通异常还会逃逸并中断装配。当前测试只覆盖等待成功。

Codex 确定性复现：

```text
wait_for_warmup() -> False
events=['ollama_start', 'ollama_wait', 'rvc_ok'] rvc_prepare_calls=1
```

修复要求：

1. 等待结果必须结构化表达完成/超时/失败。`serialize_gpu=true` 且 Ollama 未确认结束时，本轮不得启动 RVC 预热；记录不含正文的告警并延后到安全时机或跳过预热。
2. wait 的普通异常失败开放，不能使助手装配失败；`KeyboardInterrupt` / `SystemExit` 仍上抛。
3. 覆盖 wait=True、False、普通异常、KI/SystemExit、已有预热完成、两种 warmup order，断言默认不存在重叠。

## Finding 4（P1）：资源诊断在当前 Windows 环境无法观测 RSS/子进程，也不是实际 soak

位置：`src/core/resource_soak.py`、`diagnose_resource_soak.py`、`tests/test_mode_assembly_round2.py:297-335`

当前 Python 3.12 环境没有 `psutil`。Windows RSS fallback 的 WinAPI 签名/句柄处理失败；子进程路径在依赖缺失时直接返回 `0`，把“不可观测”伪装成“没有子进程”。诊断脚本只采一次自身快照，不创建 fake workload、不采队列、不做基线/终点比较。现有测试还把队列深度硬编码为 0，`assert_stable_upper_bounds()` 不验证 RSS 或队列。

Codex 实际运行：

```json
{
  "rss_bytes": null,
  "thread_count": 1,
  "child_process_count": 0,
  "queue_depths": {}
}
```

修复要求：

1. 在无 `psutil` 的受支持 Windows 环境提供正确、可测试的 RSS 与直属子进程 fallback；无法观测时返回 `null`/明确 unavailable，绝不能谎报 0。
2. 区分单次 `snapshot` 与真正的离线 `soak`。soak 至少产生 baseline/workload/closed 多个样本，并验证线程、子进程、实际队列终点以及合理的 RSS 增长边界；输出每项是否可观测。
3. 测试必须调用 `collect_assistant_queue_depths()` 或等价真实采样，不得注入固定 0 冒充观测结果。
4. 重复创建/关闭要用弱引用或等价方式证明对象可回收；10k/1k 工作负载仍全部使用 fake，不接触设备/GPU/网络。

## Finding 5（P2）：`SessionTimer` 未接入生产请求路径，交接所称阶段观测实际上不存在

位置：`src/core/perf_timing.py`、`src/core/assistant.py:204,257-260`

生产代码只创建 `SessionTimer` 并在懒加载完成日志中读取 `session_id`；没有任何生产路径调用 `new_turn_id()`、`measure()` 或 `record_stage()`。因此实际文本/语音请求没有 Round 2 声称的 session/turn 阶段日志，也没有真实冷/热区分。

修复要求：

1. 在真实的非流式与流式请求编排边界生成稳定 turn ID，并记录少量有意义阶段；不得记录 prompt、回复或记忆正文。
2. 冷/热语义必须明确且可验证，不能只由“第一个任意 stage”翻转。
3. 用生产调用路径捕获日志，断言含 session/turn/stage/elapsed/cold-hot，且不含输入与输出正文；取消/失败也应闭合计时。

## Finding 6（P2）：资源配置接受正无穷，违反“有限超时”契约

位置：`src/core/resource_settings.py:35-45`

`_require_finite_positive()` 只拒绝 NaN 和非正数，没有使用 `math.isfinite()`。Codex 复现 `resource.warmup.timeout=float('inf')` 被接受并进入 `ResourceSettings`。

修复要求：使用有限数校验，并覆盖 NaN、正负 Infinity、bool、字符串、0、负数及合法 int/float。

## Finding 7（P2）：Ollama“仅释放本程序拥有的模型”是无法成立的所有权声明

位置：`src/llm/ollama_client.py:179-205`、`src/core/resource_coord.py:115-140`、`docs/architecture/adr-mode-assembly-resource-ownership.md`

启用 `unload_on_exit` 后，客户端无条件向共享 Ollama 模型发送 `keep_alive=0`。代码没有记录本客户端是否成功加载/使用过该模型；Ollama runner 也不是当前 Python 客户端的独占对象。同名模型可能正被其他本地客户端使用，因此文档“不卸载其他客户端”不可信。

修复要求：

1. 默认继续关闭此行为。不得由 profile 在缺少单独显式许可时暗中开启共享模型卸载。
2. 至少跟踪本客户端是否成功触达模型；从未成功加载/请求时 shutdown 不得发卸载 HTTP。
3. 将文档和日志改为真实语义：这是对共享 Ollama 中指定模型的 best-effort 卸载请求，可能影响同名模型的其他客户端，不能称作独占所有权。若要求绝不影响其他客户端，则本轮应取消主动卸载并改用有限 `keep_alive`。
4. 增加从未使用、预热失败、请求成功、包装器代理、重复 shutdown 与显式关闭配置的 fake HTTP 测试。

## 复验要求

仅修复 Round 2；保留 `memory.enabled=true`、`retention.enabled=true`、`.gitignore` 隐私规则与现有私人数据库。不得读取或删除 `data/memory`，不得进入 Round 3，不得启动真实 Ollama/RVC/麦克风/扬声器/浏览器/清理实扫，不得提交或推送。

完成后运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py diagnose_memory_ids.py diagnose_resource_soak.py
git diff --check
```

更新 ADR、工程基线和 `docs/cursor-handoff-status.md` 顶部，列出每个 finding 的对应修复与新增测试。完成后停止，等待 Codex Round 2 最终复验。
