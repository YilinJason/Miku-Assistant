# Codex 里程碑 Review：安全硬化 Round 3（A/B/C1/C2）

> 最终结论：**通过。** 3A、3B、3C1、3C2 及本里程碑 F1–F2 均已通过代码审查、确定性反例和完整离线测试；可以进入自然陪伴体验设计。

## Codex 最终复验（2026-09-30）

- 前台正式请求等待后台预热：默认 5 秒、严格硬上限 10 秒；不再继承默认 120 秒资源协调等待。
- GPU 串行契约：资源协调继续使用独立显式上限；Ollama 等待超时/失败时跳过 RVC 预热。
- 日志隐私：资源协调、系统控制和确认弹窗均只落稳定异常标签；注入 `SECRET_URL`、绝对路径和换行正文的反例未进入日志。
- 专项：**16 tests / OK**。
- 全量：**604 tests / OK（skipped=4）**。
- `compileall`：exit 0；`git diff --check`：exit 0（仅 Git LF/CRLF 工作区提示）。
- 边界：未运行真实 Ollama/RVC/音频/浏览器/系统操作/清理实扫；未触碰 `data/memory`；未提交、未推送。

## 已确认成立的边界

- RVC Worker / 一次性推理 / probe 使用固定具名能力；通用 subprocess 默认关闭；系统电源使用固定 argv 与确认双门。
- text/interactive 不在装配期构造语音栈；RVC/Ollama 预热串行协调，失败不默认反复冷启动 RVC。
- 远端模型须显式许可，明文 HTTP 需第二许可；模型 HTTP 禁 redirect；云 STT/TTS 分权。
- 持久日志有容量、路径与异常隐私边界；SQLite 绑定父目录/库身份并使用计划 v2；明文与标准库 TOCTOU 剩余风险已记录。
- 清理扫描、删除、延迟队列、会话历史、记忆队列与 SQLite busy wait 均有配置上限；关闭失败保持可重试状态。

## Finding 1 — P1：正式请求等待后台 Ollama 预热的上限沿用 120 秒资源超时

- 位置：`src/llm/ollama_client.py::_wait_for_warmup()`、`start_warmup()`，以及 `src/core/resource_settings.py` / `resource_coord.py`。
- 当前 `_warmup_wait_timeout = warmup_timeout + 5`；默认 `resource.warmup.timeout=120`。`generate_result()` 和流式入口在发正式请求前同步等待该 Event。因此预热 HTTP 卡住时，首轮文本/交互请求可无进度阻塞约 125 秒，之后正式请求仍有自己的 60 秒超时。
- `resource.warmup.timeout` 只要求有限正数，没有合理硬上限；极大有限值在产品上等同无界等待。

要求：

1. 把“资源协调等待 RVC 串行化”和“用户前台请求等待后台预热”拆成两个明确上限。新增严格校验的前台等待配置，默认建议 3–5 秒、硬上限不超过 10 秒；正式生成只能使用该前台上限。
2. `wait_for_warmup(timeout=...)` 继续供资源协调器使用显式超时；资源协调 timeout 本身设合理硬上限，拒绝 bool、NaN/Inf、非正数和超大值。不要在超时后启动 RVC 与仍活跃 Ollama 预热并行抢 GPU。
3. 前台等待超时后允许正式请求继续，并记录仅阶段/耗时/状态的日志；不得记录 URL 或原始异常。说明这可能短暂与尚未返回的空预热请求重叠，但不新建模型进程，由 Ollama 服务自行串行同模型请求。
4. 用未置位 Event / fake clock 或短毫秒级 Event 测试证明前台不使用 120 秒资源超时；配置边界与资源协调跳过 RVC 的旧契约不得回归。禁止真实 sleep 长测。

## Finding 2 — P1：跨模块仍有原始异常文本进入持久日志

- 位置至少包括：
  - `src/core/resource_coord.py` 的预热启动、RVC 预热、卸载失败与 `_wait_ollama_warmup()` detail；
  - `src/system/windows_control.py` 的系统命令错误；
  - `src/system/confirm_dialog.py` 的确认对话框错误。
- 这些调用使用 `%s, exc` 或把 `str(exc)` 放入随后会落日志的 detail；异常可包含绝对路径、完整 URL、命令输出或供应商错误正文，与 C1 的持久日志默认隐私契约冲突。

要求：

1. 持久日志统一使用 `exception_log_label()` 或稳定的异常类型/状态；`WarmupWaitResult.detail` 不得携带 `str(exc)`，也不得随后被原样落盘。
2. 面向当前本地用户的返回文案可保持必要信息，但不能借日志再次持久化正文。系统控制命令仅记录固定动作标签，禁止命令行/原始异常。
3. 全仓再扫描同类持久日志调用；只修确实会进入 logger 的原始异常，不改用户主动 CLI 输出契约。
4. 注入含 `SECRET_URL`、绝对路径和换行的异常，捕获日志并断言均不存在；保留异常类型或 HTTP status 标签。

## 返修验证门槛

- 新增安全里程碑专项测试；旧 3A/3B/C1/C2、资源协调、系统控制、全量 unittest 通过。
- `compileall`、`git diff --check` 通过。
- 不启动真实 Ollama/RVC/音频/浏览器/系统操作/清理实扫；不触碰 `data/memory`；不提交、不推送。
- 完成后停止等待 Codex 里程碑复验，不进入自然陪伴。
