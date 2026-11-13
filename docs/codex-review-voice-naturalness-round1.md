# Codex Review：语音自然度 Round 1

## 最终结论（2026-10-02）：PASS

F1–F3 阻断项均已关闭。Codex 独立复验：专项 **22 tests / OK**，相关回归 **215 tests / OK**，全量 **714 tests / OK (skipped=4)**；`compileall`、`git diff --check`、默认 dry-run 均 exit 0。真实 `test_rvc_conversion.py --execute` 在 Edge TTS、CUDA 与 Nahida RVC 上 **4/4 成功**，RVC Worker 正常停止且没有残留 Python 进程。

无 JIT 短帧分析显示四组 base→RVC 的基频跨度（半音）分别为：`8.64→8.30`、`8.07→8.14`、`11.60→11.92`、`5.47→5.83`；本轮没有观察到 RVC 将句级抑扬压平。RVC 后有效响度跨度仍为约 `10.36–15.77 dB`。音频保存在 `data/test/voice-naturalness/`，最终主观音色评价仍以人工试听为准。

本次未启动主程序、Ollama、麦克风或扬声器，未读改删 `data/memory`，未提交、未推送。

以下为历史评审记录；其中阻断项均已按上方最终结论关闭。

## F1（P1）：人工 A/B 的 `--execute` 路径无法通过安全硬化后的 RVC 边界

位置：`test_rvc_conversion.py::run_execute()`。

已确认触发链：

1. `check_rvc_health(rvc_config)` 没有传 `safety_policy`。
2. `check_rvc_health()` 的 `_can_import_rvc_python()` 在策略为空时明确返回 `False`，所以真实环境即使完整也会报告 RVC 未就绪。
3. 后续才调用 `SafetyPolicy.from_app_config(config)`，且没有传构造期 `TrustedRvcProfile`。
4. 即便绕过第一步，`TTSEngine.prepare_rvc()` / Worker / infer 会因策略没有封存解释器、模型、索引而拒绝；安全实现禁止下层运行期补登记。

因此当前脚本虽然 dry-run 和 unittest 全绿，却无法生成任何真实 A/B 样本。

### 必须修复

1. 在任何 RVC probe/Worker/infer 前，根据 `config.get_rvc_config()` 解析 RVC Python，并确认解释器、模型以及可选 index 的文件存在。
2. 使用这些已经解析的资产构造 `TrustedRvcProfile`，再通过：

   ```python
   SafetyPolicy.from_app_config(config, trusted_rvc_profile=profile)
   ```

   一次性封存。禁止调用废弃的运行期 `register_*` / `seal_rvc_profile()`，禁止复制或绕过 SafetyPolicy。
3. 将同一个 policy 传给 `check_rvc_health(..., safety_policy=policy)` 和 `TTSEngine(..., safety_policy=policy)`；不得创建第二个策略。
4. 缺少解释器/模型时，在 probe、网络、目录创建和 TTS 构造前返回非零。index 缺失按现有契约可作为无 index profile，但必须与传给 RVC 的有效配置一致；不要封存一个路径、执行另一个路径。
5. 保持无参数 dry-run 不解析/封存 profile、不探测、不创建目录。

### 必须补测试

使用 `TemporaryDirectory` 创建假的解释器、模型、index 文件，patch 探测/TTS，不启动真实进程或网络：

- `--execute` 路径传给 health 和 TTSEngine 的是**同一对象**；该策略已封存预期三项资产。
- health 拒绝时零 TTS、零输出目录创建。
- 缺解释器或模型时零 health probe、零 TTS、零目录创建。
- fake 成功路径能保存四组成对文件、调用 shutdown 一次并返回 0；任一 RVC 失败返回非零。
- 继续证明 dry-run 零 profile/health/TTS/目录副作用。

完成后运行新增专项、原相关集、全量、`compileall`、`git diff --check` 和无参数 dry-run。更新 ADR/交接说明真实 `--execute` 仍待 Codex 完成代码复验后运行。

## Codex 真实链路复验 F2（P1）：Edge 失败后把空/旧 base 当成成功输入，诊断可长时间卡住

状态：**CHANGES REQUESTED**。F1 的 SafetyPolicy 修复已通过 16 项专项、相关回归和 708 项全量离线测试；真实 `--execute` 首次运行暴露以下运行时问题。只修此项，完成后停止等待 Codex 复验。

### 已复现证据

1. CUDA、Nahida Worker 加载和预热成功。
2. 首个 Edge 请求因受限网络返回 `ClientConnectorError`，`TTSEngine.save_to_file()` 只记录错误并吞掉异常。
3. 脚本随即继续把 `01_neutral_base.mp3` 当作成功；本次留下的是 0 字节文件。若目录中已有旧文件，还可能继续转换上次结果。
4. 随后的 MP3/WAV/RVC 路径超过两分钟没有返回；人工中断后 Python 3.12、RVC Python 3.11 和 `.venv-rvc` 子进程才全部退出。

### 必须修复

1. `test_rvc_conversion.py --execute` 的每条样本必须 fail closed：在合成前通过现有 SafetyPolicy/TempFileManager 边界清理该样本的旧 `*_base`、`*_rvc` 和中间文件，禁止把上次产物计入本次结果。
2. `save_to_file()` 返回后必须验证本次 base 文件存在且大小大于 0；失败立即抛出/返回非零，不得进入 `_mp3_to_wav()` 或 RVC。若能以窄、兼容方式让 `save_to_file()` 在 Edge 失败时清理部分文件并重新抛出，优先修复 API；先审查现有调用契约和安全测试，不能破坏 fallback 语义。
3. 任意样本在 Edge、转码或 RVC 阶段失败时，脚本必须在有界时间内进入 `finally` 并关闭 Worker/temp 资源。失败后不得保留本轮 0 字节、半成品或把旧成品呈现给用户；已经完整成功的前序成对样本可保留。
4. 不改变生产朗读请求数、RVC 生命周期与 SafetyPolicy 边界，不直接 `unlink` 绕过删除策略，不触碰 `data/memory`。
5. 日志只报告阶段和异常类型，不打印外部响应正文或用户文本。

### 必须补充的离线测试

- Edge fake 创建 0 字节文件或不创建文件时：返回非零，零转码、零 RVC，`shutdown(stop_rvc=True)` 恰好一次。
- 输出目录预置旧 base/RVC 后 Edge fake 失败：旧产物不会被复用或报告成功，本轮残留按契约清理。
- Edge 成功但转码失败、RVC 失败：都返回非零并有界清理；四组全部成功仍返回 0。
- 如果修改 `TTSEngine.save_to_file()` 的异常契约，覆盖 Edge 异常、PermissionError fallback 成功/失败和部分文件清理；保持已有安全测试通过。

运行新增专项、相关回归、全量、`compileall`、`git diff --check` 与 dry-run。禁止 Cursor 自行运行真实 `--execute`；该步骤由 Codex 在复验后执行。不得提交或推送。

## Codex F2 代码复验 F3（P1）：旧产物清理失败仍继续，fail-closed 尚未闭合

状态：**CHANGES REQUESTED**。F2 主路径方向正确，但 `_purge_sample_artifacts()` 当前捕获 `PermissionError/RuntimeError/OSError` 后只写 debug 并继续；`TempFileManager.remove()` 遇文件占用也可能只入延迟队列后立即返回。因此存在：

1. 旧非空 base 被占用、身份变化或清理被拒绝；
2. purge 返回，Edge 请求随后失败或没有覆盖旧文件；
3. `_require_nonempty_audio()` 看到旧文件仍非空，把它误当作本轮产物并进入转码/RVC。

现有路径对 symlink/junction 也会由 `adopt_existing()` 拒绝，但 helper 吞掉拒绝后继续对同一路径写入，违背安全边界。

### 必须修复

- purge 对每个已存在路径（包含 symlink/异常路径）必须同步确认已不存在后才能返回；任何采用现有 SafetyPolicy/TempFileManager 的登记、删除或删除后确认失败，都立即抛出并在网络/TTS/转码/RVC 前返回非零。
- 不允许直接 `unlink`、不允许扩大通用删除 API、不降低身份检查。可以在 `remove()` 后检查 `exists()/is_symlink()`；若删除被排队或拒绝则本次 A/B 安全退出，由后台清理线程继续既有回收。
- 补充离线测试：预置非空旧 base/RVC，fake `remove` 不删除或 `adopt_existing` 拒绝时，零 `save_to_file`、零转码、零 RVC，脚本返回非零且 shutdown 恰好一次。测试不得依赖 Windows 创建 symlink 权限。

只处理本项，重跑专项、相关、全量、compileall、diff-check、dry-run；禁止真实 `--execute`，不提交不推送，不接触 `data/memory`。完成后停止等待复验。
