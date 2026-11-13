# Codex Review：本地表达型 TTS 评估 Round 1

- **日期**：2026-10-04
- **任务**：`docs/cursor-task-expressive-local-tts-evaluation-round1.md`
- **状态**：FINAL PASS；真实六状态链路已完成，等待人工 A/B 听感验收

## 独立复验

- `Python 3.12 -m unittest tests.test_expressive_tts_diagnostic`：13 tests / OK。
- `Python 3.12 diagnose_expressive_tts.py`：dry-run exit 0，未创建文件、未启动模型。
- CosyVoice 隔离环境最小真实 SoundFile 探针：`sf.write("runtime/soundfile_probe.wav.tmp", ...)` 稳定抛出 `TypeError: No format specified and unable to get format from file extension`。
- 未运行真实 CosyVoice/RVC 合成，未启动主程序/麦克风/扬声器，未接触 `data/memory`。

## Findings

### F1 — blocker：原子 WAV 临时文件没有显式格式，真实 worker 必然失败

- 位置：`scripts/cosyvoice_eval_worker.py::_atomic_write_wav`。
- 触发：真实 `soundfile.write()` 写入 `*.wav.tmp`；扩展名是 `.tmp`，libsndfile 无法推断 WAV。
- 后果：第一条固定样本在写文件阶段抛 `TypeError`，worker 返回非零；fake `FakeSF.write` 未暴露该问题。
- 修复：保留同目录原子写，向 `sf.write` 显式传 `format="WAV"`（以及必要的稳定 subtype），写后用 `sf.info` 验证帧数/时长/采样率为正再 `os.replace`。新增使用真实 `soundfile` 的小型测试，不加载模型，只写短零/正弦张量到临时目录并验证 WAV 可读。

### F2 — high：第三方 worker 继承父进程全部环境变量

- 位置：`diagnose_expressive_tts.py::build_worker_env` 的 `os.environ.copy()`。
- 触发：父终端存在 API key、云凭证、代理认证、Cursor/模型令牌或其它敏感变量。
- 后果：CosyVoice 第三方进程获得与推理无关的凭证，超出最小权限边界。
- 修复：构造明确 allowlist 环境，只保留 Windows/Python/CUDA 运行确需的非敏感系统项（例如 `SystemRoot/WINDIR/COMSPEC/PATH/PATHEXT/TEMP/TMP` 及必要架构项），再加入固定离线/缓存变量；显式不传播 `PYTHONPATH/PYTHONHOME`、代理和 token/key/secret/credential 类变量。离线测试注入哨兵密钥并断言子环境不存在。

### F3 — high：`stderr=PIPE` 从不读取，真实生成可能管道塞满后假死

- 位置：`diagnose_expressive_tts.py::run_execute` 启动 worker。
- 触发：CosyVoice/tqdm/ONNX/PyTorch 在六次生成中持续写 stderr，父进程只 `wait()`，没有 `communicate()` 或 drain 线程。
- 后果：管道缓冲区满后 worker 阻塞，父进程最终误判为 900 秒超时。
- 修复：既然契约只需要退出码和原子 JSON，直接用 `stderr=DEVNULL`；或者实现有界、脱敏的持续 drain。不得保留未消费 PIPE。新增 fake/参数断言。

### F4 — high：固定 `_staging` 与无策略 `rmtree(ignore_errors=True)` 存在并发竞争和清理失真

- 位置：`diagnose_expressive_tts.py` 启动前与 `finally` 的两处 `shutil.rmtree`。
- 触发：两个诊断进程并发运行，或旧目录为占用/异常项。
- 后果：两个进程可互删 staging，同时加载两套 CosyVoice；这既破坏报告，也违反 GPU 串行边界。`ignore_errors=True` 还可能让旧文件残留但流程继续。
- 修复：增加跨进程单实例锁（Windows 锁在崩溃后由 OS 自动释放，等待必须有界/忙则 fail closed），在锁内使用每次运行唯一且位于固定根下的 staging。只清理本次拥有的目录；启动前不能吞清理失败。对子项采用固定 allowlist/TempFileManager 所有权，清理后验证不存在。测试覆盖第二实例被拒绝、staging 唯一、清理失败返回非零且不启动 worker/RVC。

### F5 — medium：报告写的是期望版本常量，没有验证实际资产

- 位置：`scripts/cosyvoice_eval_worker.py` 报告与父级 `validate_worker_report`。
- 触发：vendor checkout、子模块或模型文件被替换/缺损但目录仍存在。
- 后果：报告仍声称固定 commit/model bytes，失去可追溯性；父级只是在验证常量回声。
- 修复：在模型加载前实测并 fail closed：CosyVoice HEAD、Matcha HEAD、WeText HEAD 与固定值一致；模型顶层固定文件清单和实际总 bytes 与 `2295746007` 一致。报告区分 `*_expected` 与 `*_actual`，父级验证 actual。Git 检查使用固定参数列表、`shell=False`、短超时和绝对/可信 git 可执行文件，或使用经过测试的只读 Git 元数据解析；不能把失败当匹配。测试覆盖 SHA/bytes/文件清单任一不符时零模型加载。

### F6 — medium：worker 报告数值与实际 WAV 未形成完整契约

- 位置：`diagnose_expressive_tts.py::validate_worker_report`。
- 触发：`synth_seconds/audio_seconds/peak` 为字符串、NaN、负数，或报告 bytes 与文件实际大小不一致。
- 后果：最终报告可标记完成但指标无效，无法用于性能/资源判断。
- 修复：验证必需数值类型、`math.isfinite`、非负/正数边界；逐条核对 `bytes == staging WAV stat().st_size`，并校验实际 WAV 可读、采样率/帧数/时长为正。补齐坏值矩阵测试。

## 返修门禁

完成 F1–F6 后运行专项、现有语音自然度/RVC 相关回归、全量 unittest、`compileall`、`git diff --check` 与 dry-run。Cursor 仍不得运行真实 `--execute`。更新交接顶部并停止等待 Codex 复验。

## 最终复验追加 Finding

### F7 — high：成功报告在 staging 清理前发布，清理失败仍返回成功

- 位置：`diagnose_expressive_tts.py::run_execute` 的成功返回与 `finally`。
- 已确认触发：在六组 fake 全成功时强制 `cleanup_owned_staging` 抛错，实际得到 `code=0`、`report.complete=true`，且 `_staging_*` 目录残留。
- 后果：存储清理/所有权契约失败却被报告为完整成功，后续运行可能持续积累临时音频；这与 F4 的 fail-closed 要求冲突。
- 修复：成功报告和 `return 0` 必须发生在本次 staging 已确认清理完成之后。建议在六组 RVC 完成后显式执行一次 cleanup，成功后将 staging 标记为已清理，再原子发布 `complete=true`；清理失败则发布/覆盖 `complete=false`、明确 `failure_reason=staging_cleanup` 并返回 1。`finally` 仍负责所有失败/取消路径的尽力回收，但不得把成功路径的清理异常吞成 debug。
- 回归：新增集成式 fake 测试，强制成功路径 cleanup 抛错，断言退出非零、报告非 complete、锁释放、无第二次 RVC；正常成功仍只清理一次且返回 0。

### F8 — medium：worker 原子 JSON 半成品不在 staging 清理白名单

- 位置：`scripts/cosyvoice_eval_suite.py::staging_entry_allowed`。
- 触发：worker `_atomic_write_json` 已创建 `worker_report.json.tmp`，但在写入或 `os.replace` 时失败。
- 后果：父进程失败路径清理把这个固定、由本次 worker 拥有的文件判为 unexpected，staging 永久残留；每次失败使用唯一目录会持续占用磁盘。
- 修复：精确允许 `worker_report.json.tmp`，不要扩成任意 `.json.tmp`；补测试证明该文件可经 TempFileManager 删除并移除 staging，而其它未知 `.tmp/.json` 仍 fail closed。仍不得运行真实 `--execute`。

### F9 — blocker（真实链路）：CosyVoice 基础 WAV 未登记所有权，RVC fail closed

- 位置：`diagnose_expressive_tts.py::run_execute` 的 `src_base -> dest_base -> apply_rvc_model(dest_base)`。
- 真实复现：CosyVoice worker 6/6 生成成功，固定资产 pin 全部匹配；RVC 健康检查、启动和预热成功。首段 `warm_companion` 调用 `RvcWorker.convert()` 时，`TempFileManager.assert_rvc_io_paths()` 因输入 `dest_base` 未登记所有权而拒绝，最终 `report.json` 为 `complete=false`、`failure_reason=rvc_stage_rvc`、退出码 1。
- 后果：所有离线门禁通过，但真实 CosyVoice → RVC 永远在第一段失败；不能生成可试听 A/B 样本。
- 修复边界：不要削弱 `assert_rvc_io_paths`，也不要把最终 A/B 文件长期登记给 TempFileManager。worker 报告校验成功后，将 staging 内本轮拥有的 `src_base` 用 `temp_files.adopt_existing(src_base)` 精确登记，并把返回的已规范化路径直接交给 `apply_rvc_model`；RVC 成功后再分别复制 `src_base` 和 RVC 临时输出到 `dest_base/dest_rvc`。staging 清理仍删除已登记的 `src_base`，最终 A/B 文件保持非临时资产，不应被 `temp_files.shutdown()` 删除。
- 回归：真实路径语义的 fake 测试必须证明：传给 RVC 的是已登记 staging 输入；未登记输入仍被拒绝；成功返回后最终 base/RVC 均存在，而 staging 与 RVC 临时输出均回收；任何 adopt/copy/RVC 失败均返回非零、报告不 complete、释放锁，且不得放宽安全策略。

## 最终复验（F9 后）

- Codex 独立专项：`tests.test_expressive_tts_diagnostic` **26 tests / OK**；`compileall`、`git diff --check`、dry-run 均 exit 0。
- 真实 `diagnose_expressive_tts.py --execute`：exit 0；CosyVoice 6/6、Nahida RVC 6/6；`report.json` 为 `complete=true`、`success_count=6`。
- 真实资源：CosyVoice 冷加载 6.987 s，六段生成 18.393 s，峰值 CUDA allocated/reserved 1935.1/3118.0 MB；RVC 预热约 4.7 s，逐段约 1.1–1.5 s；整次 64.322 s。
- 产物：6 对 base/RVC WAV 全部可读；base 22.05 kHz、RVC 40 kHz；成对时长误差约 0.01–0.02 s；无 `_staging_*` 残留。
- 回收：结束后无残留 Python 进程；GPU 显存回到运行前约 1.48 GB。未启动主程序/麦克风/扬声器，未读取或修改 `data/memory`。
- 客观韵律：RVC 输出的统计音高跨度约 18.7–26.4 半音、RMS 动态约 11.7–13.6 dB，未被统一压平；鼓励/严肃样本的音高跨度较 base 收窄，须由用户重点试听。

结论：工程与安全门禁通过，Round 1 原型具备可评审的真实 A/B 产物。自然度属于主观产品门禁，在用户确认明显优于现有 Edge 路径之前，不切换生产默认 TTS。
