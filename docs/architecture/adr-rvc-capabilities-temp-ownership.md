# ADR：RVC 专用子进程能力与临时文件所有权（Round 3A）

- **状态**：F1–F7 评审修复已落地，等待 Codex Review
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步
- **任务**：`docs/cursor-task-security-round3-a.md`

## 问题

1. RVC Worker、一次性推理与环境/设备探测曾直接或经宽泛 `Operation.SUBPROCESS` 调用 `subprocess`；通用执行权限与 RVC 必需能力耦合，开启 RVC 等于放开任意 argv。
2. 临时音频删除仅靠目录白名单：未登记/预先存在文件、目录、symlink/junction 逃逸仍可能被删；STT Whisper 与 RVC 失败路径存在直接 `os.remove`/`unlink` 旁路。

## 决策

1. **具名能力**：新增 `rvc_worker` / `rvc_infer` / `rvc_probe`。裸 `SafetyPolicy()` 默认拒绝三项专用能力；仅 `from_app_config` / 显式配置开启。通用 `subprocess` **默认 false**；下层必须显式共享策略，禁止新建默认放行策略。策略实例变化时停止并重建 Worker；每次使用前再次 `require(RVC_WORKER)`。
2. **不可变请求 + 受信 profile**：`register_rvc_profile` 绑定解释器/模型/可选索引；一次性推理 I/O 须为共享 `TempFileManager` 当前拥有的精确普通文件；脚本固定为仓库内 `scripts/rvc_worker.py` / `rvc_infer.py`；探测仅允许固定 `-c` 片段。
3. **创建前校验**：拒绝 `shell=True`、字符串命令、未知/重复/缺值 flag、意外解释器；动态值作为独立 argv 元素。可注入 fake `run`/`popen`。`MIKU_DRY_RUN=1` 关闭全部子进程相关能力。拒绝日志不含完整命令、用户目录或正文。
4. **电源双门**：`safe_system_power_run` 仅允许固定 shutdown/restart/lock argv，门控为 `system_power`（+ 调用方确认），**不**依赖通用 `subprocess`。注册表写继续不可变拒绝。
5. **临时文件双条件**：仅 `create()` 授予所有权（无公开收养）；授权判定在 mkdir 之前；`directory=` 不得扩展白名单。助手关闭顺序：停播报消费者 → TTS/RVC → 共享 TempFileManager。
6. **配置**：`security.permissions` 严格 bool；未知键/非 bool 在设备与子进程副作用前失败。

## 替代方案

- 继续用通用 `subprocess=true` 覆盖 RVC：拒绝（权限面过大）。
- 各模块各自实现 argv 白名单：拒绝（易漂移；测试难共享）。
- 仅目录白名单删除：拒绝（不足以防止误删既有文件/逃逸）。

## 兼容迁移

- 旧配置未写 `rvc_*` 时默认开启专用能力；未写或仍写 `subprocess: true` 时通用入口行为不变，但 RVC 不再走该入口。
- `build_stt_engine` / `build_tts_engine` 新增关键字可选 `safety_policy` / `temp_file_manager`；正式装配显式传入。
- Chrome 自定义路径仍依赖通用 `subprocess`（需显式开启）；默认浏览器走 `browser_launch`。

## 失败语义

- 专用能力拒绝 → `PermissionError`，不创建进程。
- RVC 推理被拒或失败 → 上层回退默认音色（既有策略），不绕过到通用 subprocess。
- 临时文件清理失败不得掩盖原始业务异常（Whisper/RVC 路径已保持）。

## 明确未覆盖（Round 3B+）

- 远端 HTTP / loopback 策略、云 STT/TTS 许可文案、日志隐私强化。
- 未在本轮验证真实 RVC/麦克风/扬声器/电源。
- Chrome 自定义路径在 `subprocess=false` 时仍会拦截（需显式开启或走默认浏览器）。

## 回退

恢复 `subprocess` 默认 true、直接 `subprocess` 调用与仅白名单删除；删除本 ADR 与 `rvc_capabilities` / 所有权登记。
