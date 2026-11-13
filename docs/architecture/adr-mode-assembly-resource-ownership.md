# ADR：按模式装配与资源所有权（Round 2）

- **状态**：已采纳（Round 2 评审修复后；最小权限与子进程能力见 Round 3）
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步

## 问题

此前 CLI 在解析 `--mode` 之前完成全量装配：即使 `--mode text` 也会构造 TTS/AudioPlayer/STT，并在 TTS 构造期同步做 RVC 健康检查与 Worker 预热，同时后台启动 Ollama 预热，默认可能并行争用 GPU。退出路径也不区分会话保活与主动卸载请求。

## 决策

1. **启动模式在装配前可知**：`MikuAssistant(..., startup_mode=)` / `assemble_default(..., startup_mode=)`；CLI 将 `--mode` 传入。合法值：`text` / `voice` / `interactive`。
2. **按模式构造语音栈**：
   - `text` / `interactive`：装配期不创建 STT、TTS、AudioPlayer、RVC Worker；`voice_autoload_enabled=True`。
   - `voice`：装配期构造语音栈，但 `warm_rvc_on_init=False`。
   - 首次进入语音：`ensure_voice_stack()`；首次真正播报：`ensure_tts_for_playback()`。失败返回结构化结果。
   - **事务式懒加载**：本地构造 + 局部回滚；STT/预热/绑定失败 exactly-once 关闭本轮新建 TTS；与 shutdown 共用 `_lifecycle_lock`，关闭开始后不得发布新栈。
3. **资源协调**：`resource.profile` 默认 `balanced`。`serialize_gpu=true` 时等待 Ollama 预热必须确认完成，否则**本轮跳过 RVC**；wait 普通异常不使装配失败。
4. **保活与卸载请求**：
   - `ollama.keep_alive` 控制会话驻留。
   - `resource.ollama.unload_on_exit` **默认 false**，且 **不会** 因 `low_vram` 等 profile 暗中开启。
   - 显式开启时：仅当本客户端曾成功触达模型，才对共享 Ollama 指定模型发出 **best-effort** `keep_alive=0`；**可能影响同名模型的其他本地客户端**；不停止 Ollama 服务。不得称为独占所有权。
5. **性能观测**：生产非流式/流式请求边界记录 session/turn/stage/elapsed/cold；仅 `counts_as_request` 翻转冷/热；不记录正文。
6. **Soak**：`diagnose_resource_soak.py` 产生 baseline/workload/closed；Windows 无 psutil 时用正确 WinAPI fallback；不可观测返回 `null`，不谎报子进程 0。
7. **兼容**：`from_dependencies` 默认禁止自动创建真实引擎。

## 替代方案

- 继续全量装配再按模式忽略语音：拒绝。
- 退出时 `ollama stop`：拒绝。
- profile 自动开启共享模型卸载：拒绝（Round 2 评审）。

## 回退

恢复装配期无条件构造 TTS/STT；去掉 `resource.*` 与串行预热；删除本 ADR。

## 明确未做（Round 3B+）

远端 HTTP 策略、云 STT/TTS 许可文案、日志隐私强化。Round 3A 已完成 RVC 专用能力与临时文件所有权（见 `adr-rvc-capabilities-temp-ownership.md`）。
