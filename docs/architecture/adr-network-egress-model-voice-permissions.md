# ADR：网络出口、远端模型端点与云 STT/TTS 分权（Round 3B）

- **状态**：Round 1 F1–F4 与 Round 2 F5 返修已落地，等待 Codex 复验
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步
- **任务**：`docs/cursor-task-security-round3-b.md`；评审 `docs/codex-review-security-round3-b.md`

## 问题

1. Ollama / memory extractor 接受任意 `base_url`；本地端点可经 HTTP redirect 把 system、历史、用户正文与记忆提取内容转发到远端。
2. 默认 Google STT 上传麦克风音频、edge-tts 发送回复文本，却无独立、不可绕过的许可边界。
3. 主聊天、memory、diagnostic 各自解析 URL，规则易漂移；配置错误可能拖到 HTTP/设备初始化之后才暴露。

## 决策

1. **集中封存**：`src/core/network_endpoint.py` 负责规范化与分类（`loopback` / `remote_https` / `remote_http`）。允许 `http`/`https`；拒绝 userinfo、query、fragment、非根 path、缺 host、非法 port、**显式空端口**（`host:` / `[::1]:`）。**不做 DNS**。构造期产出不可变 `SealedModelEndpoint`；请求只能追加固定 `/api/chat`、`/api/tags`。
2. **不信任调用方 kind**：消费方经 `reseal_model_endpoint` 仅用 `base_url` 重算分类并授权；声称 kind/URL 与重算不一致一律拒绝（防伪造 `loopback`）。
3. **许可**：`remote_model_endpoint`（非 loopback）与 `insecure_remote_model_http`（非 loopback 明文 HTTP）默认 false。省略 `SafetyPolicy` 时仅允许 loopback。主聊天、memory transport/runtime、diagnostic 共用装配策略；memory override 独立过同一策略，null 继承已封存主 Ollama URL。
4. **禁止跟随 redirect**：所有 Ollama POST/GET 使用 `allow_redirects=False`；3xx 经 `enforce_no_redirect` **先 best-effort close 再抛拒绝**；`close` 失败不得覆盖原 PermissionError。**不得**用 `TypeError` 回退去掉该参数并二次请求。
5. **诊断 CLI**：`run_diagnostic` / `run_dry_run` / `run_execute` 默认 `SafetyPolicy.from_app_config(config)`；显式注入优先。解析、默认 transport 与 `_default_llm_factory` 共享同一策略实例。
6. **云语音分权**：独立 `cloud_stt` / `cloud_tts`。裸策略拒绝；正式配置默认 true。Google 在打开麦克风前检查；Whisper 缺失仅 `cloud_stt=true` 可回退。edge-tts 各出口在创建网络 client 前检查。
7. **启动摘要**：仅分类、remote/insecure/cloud 布尔、STT/TTS engine 名。`text`/`interactive` 不构造语音栈。
8. **`MIKU_DRY_RUN=1`**：remote/cloud 全部视为拒绝。

## Round 1–2 返修（F1–F5）

| ID | 修复 |
|----|------|
| F1 | `reseal_model_endpoint`；Ollama/memory transport 不信任伪造 kind |
| F2 | 删除 TypeError→去 `allow_redirects` 重试；stub 须接受该参数 |
| F3 | diagnostic 默认 from_app_config；wrapper LLM 共享策略 |
| F4 | 显式空端口拒绝（含 IPv6 方括号形式） |
| F5 | 3xx 拒绝前 best-effort close（POST/GET/stream/memory）；close 失败保留 redirect |

## 数据去向

| 出口 | 默认（正式配置） | 发送内容 |
|------|------------------|----------|
| loopback Ollama | 允许 | 本机模型 API |
| 远端 HTTPS/HTTP 模型 | 拒绝 | system/历史/用户正文/记忆提取 |
| Google STT | 允许（可关） | 麦克风音频 |
| edge-tts | 允许（可关） | 待朗读文本 |

## 替代方案

- 笼统 `network` 权限：拒绝。
- DNS 判断本地：拒绝。
- TypeError 兼容去 `allow_redirects`：拒绝（可能重复请求并恢复跟随）。

## 兼容迁移

- 注入 `_http_post`/`_http_get` 必须接受 `allow_redirects=False`。
- diagnostic 未显式传策略时自动 from_app_config。

## 明确未覆盖（Round 3C+）

- 日志/数据库隐私；真实远端/云语音未测。

## 回退

删除 `network_endpoint` 门控与 reseal；恢复任意 `base_url` 与默认 redirect。
