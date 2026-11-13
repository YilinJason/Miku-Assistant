# Codex Review：安全硬化 Round 3A

## Codex Final Review（471 tests 返修后）

> 结论：**通过 Round 3A 离线验收**。F1–F22 均已闭环；Codex 复跑并独立复现确认关闭预算、late producer 清理、自有 manager 重试和 RVC converter 超时回收符合契约。可进入 Round 3B。

- 独立关闭预算：`timeout=0.05` 实测约 `0.065s`，返回 incomplete，`calls_at_return=2` 且 200ms 后仍为 2；第二次 shutdown 完成。
- 独立流水线晚返回：producer 超过 join deadline 后返回，最终 `owned=0/pending=0/worker=false`。
- 独立自有 manager：第一次 incomplete、第二次 complete，调用两次且仅第二次标记完成。
- 独立 RVC timeout：fake runner 抛 `TimeoutExpired`，Converter 返回 `None` 并只保留调用方输入 ownership；最终关闭 complete。
- 边界：未运行真实 Ollama/RVC/CUDA/麦克风/扬声器/浏览器、清理实扫或电源操作；未读取/删除 `data/memory`。

---

## Codex Review Round 4（针对 463 tests 返修）

> 结论：**仍未通过**。F14、F15 的主要依赖顺序与可观察状态已经改善，但本轮独立反例证明总关闭预算仍可被队列长度放大，流水线生产者晚返回时会泄漏新文件，自有 TempFileManager 的 incomplete 状态也被永久标记为完成。F17 声称的超时/取消矩阵仍没有实际覆盖。不得进入 Round 3B。

### F19 [P1] `TempFileManager.shutdown(timeout)` 会在 deadline 后继续同步抽干整条队列

- 位置：`src/voice/audio/temp_files.py` 的 `shutdown()` 同步 drain 循环。
- 触发：24 个延迟项，每次 fake `os.remove` 阻塞 30 ms，调用 `shutdown(timeout=0.05)`。
- 独立复现：`elapsed=0.785 complete=True owned=0 pending=0`。代码在超过 deadline 时仅执行 `pass`，随后仍删除当前项并继续取下一项，因此耗时按队列长度增长。
- 后果：退出、模式切换或异常回收时可长时间卡住调用线程；队列上限 1024 会放大停顿，违反有界关闭契约。
- 修复要求：deadline 到达后立即停止同步 drain，不再取下一项；未处理项必须保留为 pending/owned，返回 `complete=False` 供下一次 shutdown 重试。单次不可中断 OS 调用允许小幅越界，但不得在 deadline 后继续批量工作。不得丢弃队列项来伪造 pending=0。
- 回归测试：多项 + 慢删除，断言调用次数在 deadline 后不再增长、耗时接近总预算、状态 incomplete；第二次解除阻塞后可完成且全部归零。

### F20 [P1] 流水线 producer 在有界 join 后晚返回，可新增未被任何清理路径接管的文件

- 位置：`src/voice/tts/engine.py` 的 `_speak_pipeline()`。
- 触发：第一段进入 player 并抛错；第二段合成阻塞超过 2 秒。finally 在 producer 仍存活时只枚举当时的 `temp_registry`，producer 稍后返回并把第二段路径加入 set，但没有第二次清理或 handoff。
- 独立复现：`pipeline_late_file owned=1 pending=0 worker_alive=True`。
- 后果：网络 TTS/RVC 慢返回叠加播放异常或插话时会逐轮遗留音频，长期运行造成磁盘与 ownership 增长。
- 修复要求：建立 race-safe cleanup handoff。主线程异常/取消时先发布 cleanup-request；producer 每次生成一组路径后在同一锁/状态机下决定“交给播放队列”或“自行 remove/remove_delayed”，不得在最终 snapshot 后新增无人接管路径。producer finally 也要处理尚未 handoff 的本地路径。不能靠老化扫描兜底。
- 回归测试：producer 晚于 join deadline 返回、player 抛错、插话取消、producer 自身异常；最终有界重试后 `owned=0/pending=0/worker=false`，原始 player 异常保持。

### F21 [P1] `TTSEngine.shutdown()` 在自有临时管理器 incomplete 时仍永久标记完成

- 位置：`src/voice/tts/engine.py` 的 `shutdown()`。
- 触发：自有 manager 第一次 `shutdown()` 返回 `complete=False`，连续调用两次 TTS shutdown。
- 独立复现：`owned_manager_retry calls=1 done=True`。实现先设置 `_temp_files_shutdown_done=True`，再调用 manager，且忽略返回状态。
- 后果：直接构造或独立使用 TTS 时，文件占用导致的失败不会重试，残留被生命周期永久遗忘。
- 修复要求：只有 manager 返回 `complete=True` 才标记完成；incomplete 保持可重试并输出不含敏感路径的状态。异常同样不得标记完成。共享 manager 的所有权边界保持不变。
- 回归测试：第一次 incomplete、第二次 complete，断言调用两次且第二次才标记；永久 incomplete 每次有界且不声称完成。

### F22 [P1] F17 测试名称声称 timeout/cancel，实际只执行成功路径

- 位置：`tests/test_security_round3a_review_fixes.py` 的 `test_rvc_fake_runner_success_and_timeout_cleanup` 及相邻矩阵。
- 问题：该测试只有 fake runner returncode=0 的成功分支，没有抛 `subprocess.TimeoutExpired`；单段“interrupt”在合成前就置位，实际创建 0 个文件；没有流水线 player failure/取消/晚 producer 测试。名称与交接均误报覆盖完成。
- 修复要求：补真实失败注入，不得通过重命名规避。RVC one-shot timeout、Worker timeout/取消、流水线 player error、合成中取消、shutdown concurrent close 都必须经过真实 ownership 链；若底层应把 timeout 转为 False，则明确捕获并保留原始诊断，Converter/TTS 最终回收输出。
- 回归测试：每条分支断言最终状态以及 runner/player 调用次数，禁止 `or True`、空状态预置或 shutdown 前手工删除来绕过。

## Round 4 独立复现输出

```text
shutdown_budget elapsed=0.785 complete=True owned=0 pending=0
pipeline_late_file owned=1 pending=0 worker_alive=True
owned_manager_retry calls=1 done=True
```

复现只使用 fake player、fake `os.remove` 与 `TemporaryDirectory`；未启动真实服务/设备，未读取或删除 `data/memory`。

---

> 结论：**未通过**。432 项测试通过只能证明现有用例转绿；以下反例可在纯离线 fake/临时目录中稳定复现。修复完成前不得进入 Round 3B。

## F1 [P1] 下层可新建默认放行策略，绕过装配策略

- 位置：`src/core/safety.py:110-112`、`src/voice/rvc/infer.py:197`、`src/voice/rvc/worker.py:86`、`src/voice/rvc/converter.py:36`
- 触发：调用方未传 `safety_policy`；下层执行 `SafetyPolicy()`，而裸策略默认 `rvc_worker/rvc_infer/rvc_probe=true`。
- 后果：正式装配即使关闭专用能力，任一遗漏传参或直接 API 调用仍可重新获得 RVC 子进程权限，违反“整条链使用同一个装配策略、下层不得新建策略绕过”。
- 已复现：`SafetyPolicy().summary()` 返回三项 RVC 能力均为 `True`。
- 修复要求：裸 `SafetyPolicy()` 的三项专用能力默认拒绝；仅正式应用配置明确提供项目默认值。所有可能启动 RVC 进程的下层入口必须要求显式共享策略，或使用永久拒绝的安全策略，不能创建默认放行策略。保留直接依赖注入的无设备副作用兼容。
- 回归测试：关闭装配策略后，省略策略、直接 `RVCWorkerClient`、一次性推理和 Converter 路径都不得调用 fake runner。

## F2 [P1] 已启动 Worker 可跨策略复用，关闭权限后仍继续推理

- 位置：`src/voice/rvc/infer.py:98-110`、`src/voice/rvc/infer.py:271-286`
- 触发：相同 RVC 配置先由允许策略取得全局 Worker，再以新的拒绝策略调用；`get_rvc_worker` 只替换 `worker.safety_policy`，不停止进程，`convert()` 也不重新检查能力。
- 后果：撤销 `rvc_worker`/`rvc_infer` 后，旧进程仍可处理新请求。
- 已复现：允许策略的假 Worker 缓存后，以两项专用能力均为 false 的新策略调用 `run_rvc_infer()`，结果仍为 `True`，`convert_called=True`、`stop_called=False`。
- 修复要求：策略实例变化时停止并重建 Worker；每次使用已存在 Worker 前再次要求 `RVC_WORKER`。未显式提供策略时不得复用已授权 Worker。
- 回归测试：allow→deny、deny→allow、同策略复用、并发切换四种路径；拒绝路径不得调用 `convert` 或一次性 fallback。

## F3 [P1] 专用能力接受任意模型/输入/输出路径

- 位置：`src/core/rvc_capabilities.py:145-184`、`src/core/safety.py:305-316`
- 触发：构造 `RvcInferLaunch`，将 `output_path` 指向预先存在的用户文件或任意位置；当前只验证固定脚本与 flag 形状，路径不受所有权或注册 profile 约束。
- 后果：固定脚本仍可被授权写入/覆盖任意文件，也可读取任意输入或加载未登记模型；任务要求的“路径越界在创建进程前拒绝”未实现。
- 已复现：`output_path=<临时目录>/preexisting-user-file.txt` 可到达 fake runner。
- 修复要求：能力注册时绑定受信任 RVC profile；解释器、模型和可选索引须与配置登记值完全一致。一次性推理输出必须是共享 `TempFileManager` 当前拥有的精确普通文件；输入须满足明确的只读受信任契约（共享所有权或由上层显式登记的单次输入），并在运行前重新校验普通文件、白名单与 resolve 后边界。不得允许 `data/memory`、模型之外的任意用户文件成为输出。
- 回归测试：预存输出、未登记输入、越界、父目录穿越、symlink/junction、模型/索引替换均在 fake runner 前拒绝；合法共享临时输入/输出可到达 runner。

## F4 [P1] `register_owned()` 可收养并删除任意预存文件

- 位置：`src/voice/audio/temp_files.py:134-155`
- 触发：在白名单目录创建任意预存文件，调用公开 `register_owned()` 后再 `remove()`。
- 后果：所谓“本进程创建”退化为调用方声明；任意现有文件保护可被绕过。
- 已复现：预存 `preexisting.txt` 经 `register_owned` 后被删除，`exists=False`。
- 修复要求：删除公开收养能力，或改为无法伪造的原子创建/所有权凭据；当前实际 TTS/STT/RVC 路径均应只使用 `TempFileManager.create()`。若确有外部生产器写入需求，先由 manager 原子预创建空文件再交给生产器写入，不得事后收养已有文件。
- 回归测试：预存普通文件、硬链接、symlink/junction、重命名替换均不能登记或删除；manager 原子创建的精确文件可幂等删除。

## F5 [P2] 未授权目录在拒绝前已经被创建

- 位置：`src/voice/audio/temp_files.py:115-123`
- 触发：`create(directory=<未登记目录>)`。
- 后果：函数最终抛 `PermissionError`，但已执行 `mkdir`，违反“配置/授权失败在副作用前失败”。
- 已复现：异常为 `PermissionError`，但 `outside_directory_created_before_reject=True`。
- 修复要求：先对 resolve 后目标父路径做授权判定，再创建目录/文件。默认目录若需要创建，应在构造期只登记固定受信根，不允许调用参数扩展白名单。
- 回归测试：拒绝后目录和文件均不存在，runner/线程均未启动。

## F6 [P1] 强制测试矩阵未覆盖，关闭顺序可能让共享管理器先于播报消费者关闭

- 位置：`tests/test_security_round3a.py:432-460`、`src/core/assistant.py:1524-1538`
- 问题：新增测试只覆盖 Whisper 异常清理；任务要求的 edge-tts 与 RVC 成功、异常、取消、超时、shutdown 统一所有权清理没有行为断言。助手关闭顺序为 TTS → TempFileManager → `TurnSpeechPlayer`，共享临时管理器可能在仍在退出的播报消费者之前关闭。
- 后果：网络合成/播报并发关闭时可能出现新临时文件登记失败、残留或消费者超时，离线测试无法证明资源闭环。
- 修复要求：先停止接收/取消播报生产，再停止 TTS/RVC 并有界等待消费者，最后关闭共享 `TempFileManager`；超时必须报告未完全关闭且保持可重试语义，不能声称成功。补齐任务文件第 5 项全矩阵，全部使用 fake 引擎/临时目录，不运行真实设备或服务。
- 回归测试：至少覆盖 edge 保存成功/抛错、MP3→WAV 抛错、RVC 成功/拒绝/超时、插话取消、shutdown 与并发 close；每个分支断言 owned/pending/worker 数量、原始错误不被清理错误覆盖、关闭后无新登记。

## F7 [P2] 基线文档出现无关格式改写

- 位置：`docs/engineering-baseline.md`
- 问题：PowerShell `Set-Content` 清除了多处既有 Markdown 行尾双空格，产生与 3A 无关的格式噪声。
- 修复要求：恢复无关行，只保留 Round 3A 的新增段落与确有语义变化的行；不要全文件格式化。

## 本轮复审边界

- 未启动 `main.py`、Ollama、RVC、麦克风、扬声器、浏览器、文件清理实扫或电源操作。
- 未读取或删除 `data/memory`。
- 反例只使用 fake runner 与 `TemporaryDirectory`。
- 修完上述 finding 后运行专项用例、全量 unittest、compileall、`git diff --check`，更新交接文档并停止等待下一轮 Codex Review。

---

## Codex Review Round 3（针对 454 tests 返修）

> 结论：**仍未通过**。本轮专项与全量测试虽通过，但关闭依赖顺序、最终临时文件回收和播放异常仍有确定性资源泄漏；RVC profile 仍保留公开的事后提权入口。不得进入 Round 3B。

### F14 [P1] 播报消费者未退出时仍关闭 TTS 与共享 TempFileManager

- 位置：`src/core/assistant.py:1528-1556`。
- 触发：`speech_player.shutdown(timeout=2.0)` 返回 `False`。实现虽保留 player 供下次重试，却继续关闭 `tts_engine` 与共享 `TempFileManager`，并将两者标记为完成。
- 后果：仍存活的消费者可能继续调用已经 shutdown 的 TTS/临时文件管理器，造成登记失败、播放异常或残留；第二次 shutdown 只有 player 可重试，依赖已不可恢复。原 F6 要求的依赖关闭顺序没有闭合。
- 修复要求：先停止接收并有界等待 speech consumer。只要 speech 未完成，本轮不得关闭其 TTS/TempFileManager 依赖，并将整体保持 incomplete；下一次 shutdown 先重试 speech，成功后才 exactly-once 关闭 TTS，再关闭 TempFileManager。memory 等互不依赖组件可按既有规则处理。
- 回归测试：player 第一次 False、第二次 True；第一次断言 TTS/temp 调用数均为 0 且 manager 仍可供存活消费者清理，第二次断言顺序 `speech -> tts -> temp` 且各成功组件只调用一次。永久 False 时 TTS/temp 始终不关闭且每次有界。

### F15 [P1] TempFileManager.shutdown 会遗留 owned 文件，却被上层标记为成功

- 位置：`src/voice/audio/temp_files.py:318-390`、`src/core/assistant.py:1550-1555`、`tests/test_security_round3a_review_fixes.py:837-849`。
- 确定性复现：manager 创建 WAV，`remove_delayed(delay=30)` 后立即 `shutdown(timeout=.5)`。实测 `pending_delayed=0`、`worker_alive=False`，但 `owned_count=1` 且文件仍存在。队列项已被 worker 取走，故 `qsize()==0` 掩盖了未完成清理。
- 后果：应用关闭可记录成功并永久遗留音频；当前“并发 shutdown”测试先同步删完所有文件再 shutdown，只证明空队列关闭。
- 修复要求：为 manager 提供可观察的 complete/incomplete 关闭契约。停止新登记后，取消延迟等待并在总超时内尽力清理队列中、in-flight 和所有 owned 文件；删除失败须保留 ownership 以便再次 shutdown 重试，不能声称完成。上层必须检查返回状态/结果，只有 `owned=0 && pending/inflight=0 && worker_alive=false` 才标记 `_temp_shutdown_done`。不得无界等待或一文件一线程。
- 回归测试：长 delay 队列立即 shutdown、多个 queued + in-flight、模拟 Windows 文件占用第一次失败第二次成功、永久失败、重复 shutdown；分别断言状态、owned/pending/inflight/worker、文件存在性和有界耗时。

### F16 [P1] 单段播放异常绕过 finally，临时音频泄漏

- 位置：`src/voice/tts/engine.py:280-293`。
- 确定性复现：`_prepare_playable_audio` 返回 manager-owned WAV，fake player 的 `play()` 抛 `RuntimeError`。实测 `owned_count=1` 且文件仍存在。
- 后果：扬声器/解码器异常时每轮可残留一组 MP3/WAV/RVC 输出，长期运行会持续占用磁盘与 ownership。
- 修复要求：单段播放的合成成功后，以 `try/finally` 回收全部 temp paths；清理失败不得掩盖原始播放异常。流水线也必须在 player 异常、取消和 prep 线程退出的每条路径最终回收；如果生产者仍存活，状态必须显式 incomplete，并由其完成后的受管控回收闭环，不能只依赖老化扫描。
- 回归测试：fake player 正常、抛错、插话取消；每条路径最终 `owned=0/pending=0/worker=false`，播放异常类型/消息保持。

### F17 [P1] 强制清理矩阵仍以 mock 成功与空状态代替生命周期验证

- 位置：`tests/test_security_round3a_review_fixes.py:370-478,837-849`。
- 问题：RVC “成功”只 mock `run_rvc_infer=True`，没有写输出或触发超时/取消；shutdown 前手工同步删除文件；未覆盖 edge 成功、RVC 超时、player 抛错、流水线取消、长 delay/in-flight 与并发 close。测试注释声称矩阵完成，但原任务第 5 项仍缺分支。
- 修复要求：使用 fake edge saver、fake player、fake RVC worker/runner 和临时目录走真实 ownership 调用链，补齐成功、异常、取消、超时、shutdown/concurrent close。不得启动真实服务或设备。每个最终边界必须断言 manager 的完整关闭状态；允许 incomplete 时必须断言可重试，不能用空队列预置绕过。

### F18 [P1] `seal_rvc_profile` 仍是公开可变入口，下层仍能首次封存任意 profile

- 位置：`src/core/safety.py:260-293`、`src/core/assembly.py:271-286`。
- 触发：任何持有尚未封存共享 policy 的下层调用者都可直接调用公开 `seal_rvc_profile()`，把其提供的解释器/模型提升为受信任。当前测试只证明旧 `register_*` 被拒绝，却直接把公开 `seal_*` 当作测试辅助调用。
- 后果：F8 的“只有正式装配边界能建立信任，下层只能校验”仍是命名约定，而非对象生命周期契约。
- 修复要求：RVC profile 必须随正式策略构造一次性传入（例如 `from_app_config(..., trusted_rvc_profile=不可变 spec)`），策略发布后不再暴露任何 public mutator；或使用等价的 assembly 私有 builder/capability，使已发布 policy 的下层 API 无法封存。测试辅助也应通过正式构造 helper 建 policy，而不是调用生产 public seal。无模型/text 模式保持未封存并 fail closed。
- 额外硬化：临时文件允许同一 inode 内容写入，因此 temp identity 可只比稳定对象身份；解释器/模型/索引是封存资产，应另用 size/mtime 等低成本元数据检测常见原地改写。不要对大模型每次全量 hash，也不要把 temp 写入误判为替换。

## Round 3 复现输出

```text
shutdown_pending 0
shutdown_owned 1
shutdown_worker_alive False
shutdown_file_exists True
play_failure_owned 1
play_failure_file_exists True
in_place_model_mutation_accepted True
```

复现仅使用 fake player 与 `TemporaryDirectory`；未启动真实服务/设备，未读取或删除 `data/memory`。

---

## Codex Review Round 2（针对 444 tests 返修）

> 结论：**仍未通过**。本轮没有进入 Round 3B。现有测试虽为绿色，但仍有下层扩大信任边界、Worker I/O 绕过、同名替换删除和不可重试关闭等确定性反例。

### F8 [P1] RVC profile 仍可由下层自行登记/改写，装配信任边界没有封闭

- 位置：`src/voice/rvc/infer.py:185-200,237-246`、`src/voice/rvc/worker.py:137-144`、`src/core/safety.py:197-238`。
- 触发：装配策略未登记 profile，或登记后下层再次调用公开 `register_rvc_python/register_rvc_profile`。`ensure_policy_rvc_python()`、one-shot infer 和 Worker 都会从各自收到的 `rvc_config` 写入策略；注册方法本身也允许覆盖已有值。
- 后果：任何持有共享策略的低层调用方都能把自己提供的解释器、模型和索引提升为“受信任”，使 `assemble_default()` 的固定配置边界失效。
- 同一路径替换同样未被识别：当前 profile 只存 resolve 后路径，登记后替换该路径上的 Python/模型/索引仍会通过路径比较；现有“模型替换”测试仅换了路径，没有覆盖原路径文件身份被替换。
- 修复要求：profile 只能在正式装配边界一次性封存；下层只校验，不能登记。可使用 assembly 持有的私有 builder/seal API，或构造期不可变 profile 值；同一策略不得被后续覆盖。登记时记录并在启动前复核普通文件身份（至少 path + `st_dev/st_ino` 或 Windows 等价稳定标识）；缺失、同名替换或身份变化均在 fake runner 前拒绝。若 text 模式没有模型，可以保持未登记并安全拒绝，不要由调用路径补登记。
- 回归测试：未登记 profile 的 worker/infer/probe 不得调用 runner；低层无法登记；重复相同登记幂等或明确拒绝，冲突登记必须拒绝；Python/模型/索引在同一路径删除重建后必须拒绝。

### F9 [P1] Worker 未绑定 TempFileManager 时跳过 I/O 所有权验证

- 位置：`src/voice/rvc/worker.py:264-278`。
- 触发：用允许策略直接构造 `RVCWorkerClient(..., temp_file_manager=None)`，或装配遗漏绑定，然后调用 `convert(arbitrary_input, arbitrary_output)`。
- 后果：`convert()` 只在 manager 非空时校验；任意路径仍会被写入 Worker stdin。固定脚本并不能限制它读取/覆盖的目标。
- 修复要求：预热可以没有音频路径；真实 `convert()` 必须要求绑定共享 `TempFileManager`，缺失即 fail closed，且不得启动/复用 Worker、不得写 stdin。不要把 manager 设为可选安全降级。
- 回归测试：无 manager、外部 manager、未登记/同名替换/硬链接/越界输入输出均在 start/write 前拒绝；同一共享 manager 的合法文件才可发送。

### F10 [P1] 临时文件“所有权”只记录路径字符串，会删除或授权同名替换文件

- 位置：`src/voice/audio/temp_files.py:62,129-172,179-239,343-356`。
- 确定性离线复现：manager 创建文件后删除它，再在同一路径写入新文件，`remove()` 会删除新文件；`assert_owned_regular_file()` 也接受该替换。删除后不创建替代文件时，断言仍会接受一个不存在的“owned”路径。Windows 支持硬链接时，把该路径替换为指向另一文件的 hardlink 也会被接受。
- 实测输出：`missing_owned_path_accepted=True`、`same_name_replacement_survives_remove=False`、`hardlink_replacement_accepted=True`。
- 后果：外部同名替换会被清理器误删；hardlink 可让 RVC 输出间接改写非临时文件。原 F4 要求的 hardlink/重命名替换并未覆盖。
- 修复要求：`create()` 原子创建后记录文件身份，不只记录字符串；每次 assert/remove/延迟删除前验证文件仍存在、为普通非链接文件、路径在白名单且身份与登记值一致。不存在或身份变化时撤销该 ownership 记录并拒绝操作，绝不能删除替代物。队列项应携带或复核原身份。对无法可靠取得身份的平台应 fail closed。
- 回归测试：missing、unlink+同名新建、rename-over、hardlink 替换、symlink/junction、队列等待期间替换；替代物/链接目标都必须保持原样，合法原文件仍可 exactly-once 删除。

### F11 [P1] 播报关闭超时被标记为终态，后续 shutdown 无法重试

- 位置：`src/core/assistant.py:1525-1532,1561-1564`、`tests/test_security_round3a_review_fixes.py:489-523`。
- 触发：`speech_player.shutdown()` 返回 `False`。实现把 `_speech_player` 先清空，最后无条件把 `_non_memory_shutdown_done=True`；当 memory 正常时又把 `_shutdown_flow_done=True`。第二次 `shutdown()` 直接跳过，无法再次 join/停止仍活着的播放器。
- 测试缺陷：该测试最终条件含 `or True`，因此没有任何有效断言，所有实现都会通过。
- 后果：日志虽称“部分资源释放失败”，资源生命周期却被永久遗忘；这正是长期运行时线程/音频占用泄漏风险。
- 修复要求：超时对象必须保留可重试引用/状态；只对实际完成的组件标记终态。再次 shutdown 需有界重试未完成项，且已成功组件保持 exactly-once。返回值或可观察 shutdown 状态应能区分 complete/incomplete；日志不能代替状态契约。
- 回归测试：player 第一次 False、第二次 True，断言第二次确实再次调用且最终完成；永久 False 时每次均有界、保持 incomplete；TTS/temp 不重复关闭；删除 `or True`。

### F12 [P1] F6 强制清理矩阵仍没有验证资源闭环

- 位置：`tests/test_security_round3a_review_fixes.py:370-435`。
- 问题：edge 保存异常后只断言 `_owned` 数量“没有减少”，实际上接受泄漏；MP3→WAV 异常没有断言文件回收；RVC 只测拒绝，没有成功/超时/取消；没有并发 shutdown 后 `owned/pending/worker` 归零断言。直接读取私有 set 也没有验证最终 observable cleanup。
- 修复要求：按原任务第 5 项补齐成功、异常、取消、超时、shutdown 分支。每个测试都必须在最终边界断言：owned 数量为 0（或明确由仍存活消费者持有且状态为 incomplete）、pending 为 0、清理 worker 不存活、原始业务异常/结果未被 cleanup 异常覆盖。必须用 fake edge/RVC/player 和临时目录，不启动真实服务或设备。

### F13 [P2] 必需验证命令尚未通过

- `git diff --check` 当前在 `docs/engineering-baseline.md:111,114,117` 报 trailing whitespace。
- 修复要求：只移除这三处新增语义行的尾随空格，不要再次全文件格式化；最终必须提供 clean 的 `git diff --check`。

## Round 2 返修与验证边界

- 只修复 F8-F13，不进入 Round 3B，不扩大到网络、云服务、日志隐私或自然陪伴。
- 不启动 `main.py`、真实 Ollama/RVC、麦克风、扬声器、浏览器、清理实扫或电源操作；不读取/删除 `data/memory`；不安装依赖。
- 运行专项测试、全量 unittest、compileall、`git diff --check`。完成后更新交接文档并停止等待 Codex Review；不提交、不推送。
