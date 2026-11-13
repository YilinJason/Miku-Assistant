# Cursor 任务：安全硬化 Round 3B——网络出口、远端模型与云语音许可

> 状态：Cursor 已实现，停止等待 Codex Review。  
> 基线：同一未提交工作区中的 Round 3A（Codex Final Review 已通过）+ 本轮 3B。  
> 范围：只做本文件 3B。不得进入日志/数据库隐私 Round 3C 或自然陪伴体验。

## 先读取

- `AGENTS.md`
- `skills/architecture-contracts/SKILL.md`
- `skills/safe-refactoring/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `skills/collaboration-review/SKILL.md`
- `.cursor/rules/system-safety.mdc`（若存在）
- `docs/codex-review-security-round3-a.md` 顶部最终结论
- `src/core/{safety,assembly,config}.py`
- `src/llm/ollama_client.py`
- `src/memory/{runtime,ollama_extractor,diagnostic}.py`
- `src/voice/{stt.py,tts/engine.py}`
- `config/config.yaml`

## 威胁边界与目标

当前 Ollama / memory extractor 接受任意 `base_url`，本地端点可经 HTTP redirect 把 system、历史、用户正文和记忆提取内容转发到远端；默认 Google STT 会上传麦克风音频，edge-tts 会发送回复文本。配置与启动日志没有统一、不可绕过的许可边界。

本轮必须做到：

1. **模型端点严格验证且只验证一次语义一致**
   - 允许 `http` / `https`，拒绝其他 scheme、userinfo、query、fragment、非根 path、缺 hostname、非法 port。
   - `localhost`、IPv4/IPv6 loopback 可在 `remote_model_endpoint=false` 时使用；不得为判断本地而做 DNS 请求。
   - 非 loopback 必须显式开启 `security.permissions.remote_model_endpoint`；非 loopback 的明文 HTTP 还必须额外开启 `security.permissions.insecure_remote_model_http`。二者默认 false。
   - 规范化 URL 在构造时封存；请求路径只能由代码追加固定 `/api/chat`、`/api/tags`，正文/模型输出不能改变 host/path。
   - 所有 Ollama POST 禁止自动 redirect（`allow_redirects=False`）；3xx 按 HTTP 错误处理，绝不能带 payload 跟随到另一个 host。

2. **主聊天与 memory extractor 使用同一装配策略**
   - `OllamaClient`、`RequestsOllamaChatTransport`、`OllamaMemoryExtractor` 与 `MemoryRuntime` 都必须接收装配的同一个 `SafetyPolicy` 或由其生成的不可变网络 capability。
   - 下层省略策略时只允许 loopback，不能新建放行策略；远端不能靠直接构造、memory override、diagnostic 参数或注入另一个 URL 绕过。
   - `memory.extractor.base_url=null` 继承已验证的主 Ollama URL；显式 override 必须独立通过相同策略。
   - 配置错误在任何 HTTP、设备初始化、模型加载或后台线程启动前失败。

3. **云 STT/TTS 分权且不静默升级**
   - 新增独立 `security.permissions.cloud_stt` 与 `cloud_tts`。裸 `SafetyPolicy()` 两者默认拒绝；正式 `config/config.yaml` 为保持用户当前 Google STT + edge-tts 功能可显式设为 true，并在注释中写明分别会发送麦克风音频/回复文本。不得把这两项合并为一个笼统 network 权限。
   - Google `recognize_google` 必须在打开麦克风和调用 provider 前检查 `cloud_stt`；拒绝时返回稳定的 NOT_READY/SERVICE_ERROR 类状态，不录音、不调用网络。
   - Whisper 本地模型缺失时，只有 `cloud_stt=true` 才允许回退 Google；否则保持本地引擎不可用并明确说明，不能静默上传。
   - edge-tts 的每个出口（单段、流水线、`save_to_file`）在创建网络 client 前检查 `cloud_tts`。拒绝时若启用本地 pyttsx3 fallback 可回退，否则稳定失败；不得调用 edge provider。
   - 显式注入的测试/provider 边界须记录清楚，不允许生产装配借此绕过许可。

4. **可审计但不泄密的启动摘要**
   - 启动时仅记录：模型端点分类（`loopback` / `remote_https` / `remote_http`）、remote/insecure/cloud_stt/cloud_tts 布尔许可、实际 STT/TTS engine；不得打印完整 URL、用户目录、query、认证、prompt、reply 或音频内容。
   - text/interactive 懒加载模式不得因此构造 STT/TTS、打开设备或调用网络。

## 设计约束

- 网络 URL 解析/分类集中到窄模块或 `SafetyPolicy`，主聊天、memory 与 diagnostic 不得复制漂移规则。
- capability 必须是构造期不可变值；低层只能校验，不能事后扩大权限。
- 不用 DNS 解析、端口探测或真实 HTTP 来验证配置。
- 保持 public API 的合理兼容：可新增关键字参数；直接构造的远端 URL 若无显式 capability 必须 fail closed。现有 fake 测试可改用 loopback URL或显式拒绝/允许策略，不能加测试域名后门。
- 网络失败与业务返回契约保持：超时、连接、HTTP、响应错误不混淆；redirect 不得伪装为连接成功。
- `MIKU_DRY_RUN=1` 时 remote/cloud 三类许可全部视为拒绝；loopback Ollama 的 dry-run 行为保持原有诊断边界，不新增真实请求。

## 必须新增的纯离线行为测试

1. URL 表：localhost、127.0.0.1、IPv6 loopback、大小写/尾斜杠规范化；远端 HTTPS 许可/拒绝；远端 HTTP 双许可；拒绝 userinfo/query/fragment/path/非法端口/非 HTTP scheme。
2. redirect：主 chat、warmup、stream、release、tags/health 与 memory extractor 的 fake 302 均不得二次请求；每次 POST 明确 `allow_redirects=False`。
3. 共享策略：assemble 的主 Ollama、memory extractor、懒加载 STT/TTS 使用同一 policy；memory override 不能绕过 remote 权限；配置拒绝发生在 fake HTTP/设备/线程之前。
4. cloud STT：允许时才调用 fake Google；拒绝时不创建/打开 fake microphone、不调用 recognizer；Whisper import 失败在许可 false 时不回退，许可 true 时可回退且有明确日志。
5. cloud TTS：edge 成功、拒绝 + local fallback、拒绝 + 无 fallback、`save_to_file`、流水线；拒绝路径 `edge_tts.Communicate` 调用数为 0，临时 ownership 最终归零。
6. dry-run 与日志摘要：remote/cloud 均拒绝；摘要不含完整 URL、路径、正文和认证信息；text 模式零语音/设备副作用。

## 验证边界

只运行 Python 3.12 离线 fake/临时目录测试：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py diagnose_resource_soak.py
git diff --check
```

不得启动 `main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、清理实扫或电源操作；不得读取/删除 `data/memory`；不得安装依赖；不得提交或推送。

## 交付

- 新增网络隐私/端点许可 ADR，写清数据去向、默认值、redirect、兼容迁移与未覆盖项。
- 更新 `config/config.yaml`、`Config` 默认、`docs/engineering-baseline.md` 和 `docs/cursor-handoff-status.md` 顶部。
- 返回修改文件、测试数量、未运行项、剩余风险。
- 完成后停止，等待 Codex Review；不得自行进入 Round 3C。
