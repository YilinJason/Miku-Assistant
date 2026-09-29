# ADR：长期记忆产品接线与无历史 Ollama 提取适配器

- **状态**：已采纳（关闭完整性 / 同模型 context 继承已实现；真实 v3 验收通过）
- **日期**：与 `docs/engineering-baseline.md` / `docs/cursor-handoff-status.md` 同步
- **依赖**：`adr-long-term-memory-store.md`、`adr-memory-service.md`

## 问题

应用服务已可独立验证，但仍未进入对话路径。若直接复用 `OllamaClient.generate*` 做后台提取，会污染 `ConversationHistory` 并与前台回复争抢模型；若默认启用记忆，会改变既有资源与测试基线。真实 v2 验收还证明：wrapper 查询轮后未等后台 idle 即 close，以及同模型 `num_ctx` 8192↔2048 切换导致 runner 重建。

## 决策

1. **默认关闭**：`memory.enabled: false`。未启用时不创建目录/数据库、不启动 ingestion 线程、不包装 LLM；既有行为与开销不变。
2. **装饰器边界**：`MemoryAwareLLMClient` 包装现有客户端。仅扩展 `system`（保留调用方原文 + 固定分隔 + 不可信声明）；不改写用户正文；不把记忆写入 `ConversationHistory`。`OllamaClient` 经共享 `_build_chat_messages` 把非空动态 system 作为 `/api/chat` 的 **`messages[0]`（`role=system`）** 发送，顺序固定为 `system → 最近历史 → 当前 user`；**禁止**再写顶层 `system`（那是 `/api/generate` 契约，Chat API 会静默忽略）。`system is None`/空串不插入空 system。离线回归必须断言最终 HTTP `json` payload，不能只检查包装器方法参数。`generate()` 必须委托本装饰器的 `generate_result()`。turn_id 工厂/`submit` 失败开放，不得破坏已成功的前台结果；流式外层提前 close 时显式关闭底层迭代器一次。前台 admission lease 为 exactly-once：自 `begin_foreground` 成功起，recall KI/SystemExit、底层流同步构造异常、迭代异常、正常完成、调用方提前 close、底层 close 异常均须释放；成功提交前必须先结束**本轮** lease，避免自阻断提取。显式代理 CLI/预检所需的 `model`、`check_connection()`、`check_model_available()`、`list_models()`、`start_warmup()`，不用宽泛 `__getattr__`。
3. **提交时点**：仅 `GenerateStatus.SUCCESS` 且非空正文，或流式合法 `COMPLETED`（SUCCESS 非空）时非阻塞 `submit` **一次**；失败、空回复、半轮、提前 close、取消均不提交。不得因 lease 修复造成双提交或漏关底层流。
4. **无历史提取器**：`OllamaMemoryExtractor` 经可注入 transport 直接 `POST /api/chat`（`stream=false`）。**先本地枚举安全 `UserFactUnit`**（标点 segment 先语境验收；同段并列连接词 all-or-nothing；禁止裸「并」切分），请求 messages 为 system + **仅含 candidates 的 JSON**（不发送 assistant_text / 自由 user_text 写库权）；无候选则不发 HTTP。Schema **required: unit_id**（`additionalProperties=false`；禁止模型 text/category/evidence_quote）。本地 `render_claim` 生成 `ExtractedMemory.text`，`category_for_claim` 映射类别；importance/confidence 只影响排序。单项非法选择跳过；全无效结构错误仍抛错。落库 `MODEL_INFERRED`。
5. **装配与所有权**：启用时先严格校验全部 memory 配置（禁用时只校验开关并返回安全默认快照），再创建 `MemoryRuntime`（拥有 store，ingestion `owns_store=False`），包装 LLM 并 `start()`。默认装配注入小型 `ForegroundAwareAdmissionGate`：其他并发前台忙碌时拒绝新的后台提取（失败开放跳过，不排队等待以免关闭死锁）；**已发出的 Ollama HTTP 不可可靠抢占**。当前调用不得被自身 lease 阻断。`wait_until_idle` / close 契约不变。
6. **性能与 GPU**：同步 recall 仅有界 SQLite + 词法评分。同机争用由 admission gate 做准入，不引入调度框架/进程管理/CUDA 控制。默认 inherit `num_ctx` 降低同模型 runner 抖动；下一阶段可扩展有界等待或独立小模型，但不宣称可抢占。
7. **系统/搜索意图**：不经过 LLM 成功路径时不得写入长期记忆。
8. **真实验收入口**：`diagnose_memory.py` 默认 dry-run；`--execute` 合成数据 + 临时库。诊断 **keep_alive 默认 `1m`**（CLI `--keep-alive` 可覆盖），**不继承**主聊天 `ollama.keep_alive=30m`。wrapper **seed 与 query 各一次**有界 `wait_until_idle`；最终 idle 超时为协议失败 `final_idle_timeout`，不得记为内容 warning。报告 schema **v3** / tool **1.2.0**：`endpoint.num_ctx` 可为 null（控制台展示 `inherit`）；`wrapper.seed_idle_ok` / `final_idle_ok`；`temp_dir_cleanup` 为 `completed` | `retained_incomplete_close` | `failed`。incomplete 契约 close 后有界 reclaim（idle → 重试 close → 再删目录）；超时仍无法回收则 **retained**（失败证据，不是虚假 deferred）。独立小型文本模型是以后可选项，本轮不自动选型/下载。
9. **真实结果**：2026-09-27 首次验收 exit 2（算术/注入误提取）；v2 四场景与 wrapper 内容通过，但 close incomplete + 同模型 context 抖动。修复后的 v3 由用户显式复跑并 exit 0：四场景、wrapper 双 idle、完整 close 与临时目录清理均通过；日志只加载一次 `-c 8192` runner，退出后无 Python/runner 残留。一次样本通过不等于普遍质量或 soak 保证。

## 替代方案

- 复用带历史的 generate 做提取：污染会话，拒绝。
- 默认启用记忆：改变兼容基线与资源，拒绝。
- 引入第二模型/调度框架：扩大范围，拒绝。
- 仅调大 `close_timeout` 掩盖最终 idle 缺失：拒绝。

## 回退

关闭 `memory.enabled`；或删除接线文件并恢复装配/助手绑定。不回滚无关修改。显式配置 `num_ctx: 2048` 可恢复旧提取器 context 行为。

## 明确未做 / 未验证

资格、lease、关闭完整性与 inherit `num_ctx` 已实现，真实 v3 合成样本通过。长时间 soak、独立小模型选型、可抢占调度、端到端陪伴与设备真机仍未做。其他并发前台调用仍可令 gate 跳过提取。不得把「本次退出后无残留」写成「已证明绝无内存泄漏」。

## 验证

纯离线 `tests/test_memory_integration.py` + `tests/test_memory_diagnostic.py` + 全量 discover；见 handoff。ADR「决策」段仅保留有序 1～9 一份，由离线测试做轻量重复检查。旧 schema v2 报告可读作历史样本；新公开契约以 v3 为准，不做历史数据迁移。
