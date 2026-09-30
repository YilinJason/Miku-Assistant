# Cursor 交接状态

## 2026-09-29 Codex 最终验收：存储硬化 Round 2 通过

- **结论**：Round 2 离线工程验收通过，无剩余阻断项；F1-A/B/C/D 独立反例全部转绿
- **独立验证**：专项 39 tests / OK；全量 418 tests / OK；无 `ResourceWarning`；compileall / `git diff --check` OK
- **soak**：`ok=true`、`bounds_ok=true`、`reclaim_ok=true`、`weakrefs_alive_after_shutdown=0`；本次 RSS 增长约 0.4 MiB，线程/直属子进程无增长
- **边界**：未运行真实 Ollama/RVC/音频设备及 30～60 分钟真实语音 soak；未读/未删 `data/memory`；未提交、未推送；Round 3 未开始

---

## 2026-09-29 Round 2 二次复验 F1 协议收口：完成，等待 Codex 复验

- **任务入口**：`docs/cursor-review-storage-performance-security-round2-final.md` 顶部「2026-09-29 二次复验」
- **状态**：一次性修复 F1-A/B/C/D；**保留已通过的 F2/F3/F4**；**停止等待 Codex 复验**；**未进入 Round 3**
- **保留**：`memory.enabled=true`、`retention.enabled=true`；未读/未删 `data/memory`；未提交、未推送

### 四条对照
1. **F1-A**：未发布且尚无正式 TTS 时 `_discard_unpublished_tts` → `stop_rvc=True`；shutdown-prewarm 后 `worker_alive=False`
2. **F1-B**：两者皆缺走 `_ensure_voice_stack_both` 事务；STT 失败回收局部 TTS；已有 TTS 仅补 STT 失败时不关正式 TTS
3. **F1-C**：shutdown 设置 closing 后 `_wake_voice_loaders()`；等待使用单一 `VOICE_LOAD_WAIT_SECONDS` deadline
4. **F1-D**：`_shutdown_engine` 用 `inspect.signature` 判断 `stop_rvc`；内部 TypeError 不重试，`calls==1`

### 确定性测试（新增）
- `FinalF1AShutdownPrewarmTests.test_shutdown_during_prewarm_stops_worker`
- `FinalF1BTransactionalVoiceTests.test_both_missing_stt_failure_rolls_back_tts` / `test_existing_tts_stt_failure_keeps_published_tts`
- `FinalF1CWaiterWakeTests.test_waiter_returns_promptly_after_shutdown`
- `FinalF1DShutdownExactlyOnceTests.test_internal_typeerror_shutdown_exactly_once`

### 离线验证
- `unittest discover -s tests -q` → **418 tests / OK**（此前 413；新增 FinalF1A–D）
- `compileall` OK；soak JSON：`ok=true` / `bounds_ok=true` / `reclaim_ok=true` / `weakrefs_alive_after_shutdown=0`
- `git diff --check` OK（仅 LF/CRLF 提示）
- **未运行**：真实 Ollama、RVC、麦克风/扬声器、浏览器、清理实扫

---

## 2026-09-29 Codex Round 2 二次最终复验：F1 生命周期仍需收口（历史）

- **任务入口**：`docs/cursor-review-storage-performance-security-round2-final.md` 顶部“二次复验”
- **已通过**：有界 SessionTimer、weakref/reclaim soak、cleaner 关闭、显式 TTS 重试、closing 优先检查；soak `ok=true / alive=0`
- **仍阻断**：shutdown-prewarm 遗留 Worker；双缺失时 STT 失败留下 TTS；loader 等待者关闭后不醒且无总 deadline；内部 `TypeError` 导致 shutdown 调用两次
- **状态**：已被上方 F1 协议收口条目承接

---

## 2026-09-29 存储硬化 Round 2 最终收口：完成，等待 Codex 最终复验

- **任务入口**：`docs/cursor-review-storage-performance-security-round2-final.md`
- **状态**：一次性修复全部 4 组 Round 2 收口 finding；**停止等待 Codex 最终复验**；**未进入 Round 3**
- **保留**：`memory.enabled=true`、`retention.enabled=true`、`.gitignore` 隐私规则与私人库；未读/未删 `data/memory`；未提交、未推送
- **离线验证**：`Python312 -X utf8 -m unittest discover -s tests -q` → **413 tests / OK**；`compileall` OK；`diagnose_resource_soak.py --turns 1000 --fails 100 --json` → `ok=true` 且 `weakrefs_alive_after_shutdown=0`；`git diff --check` OK
- **未运行**：真实 Ollama、主程序、RVC、麦克风/扬声器、浏览器、清理实扫

### 四组收口对照
1. **F1**：`ensure_tts`/`ensure_voice_stack` 共用 loader single-flight；已有 TTS 仅补 STT；未发布 TTS `shutdown(stop_rvc=False)`；顺序/并发/构造中 shutdown 回归
2. **F2**：`SessionTimer` 硬上限（默认 64）+ FIFO 淘汰；构造校验；10k 写入后记录数 ≤ 上限
3. **F3**：soak 删除强引用 + `gc.collect()`；`ok` 要求 `weakrefs_alive==0`；长目录 cleaner `try/finally stop_auto_cleanup`
4. **F4**：显式 `tts` 走 `allow_retry=True`；ensure 先查 closing；shutdown 后即使引擎仍绑定也失败

### 关键测试
- `tests/test_storage_round2_review_fixes.py`（F1 单飞/竞态、F2 容量、F3 真回收、F4 显式重试与关闭契约）
- `tests/test_mode_assembly_round2.py`（cleaner finally）

---

## 2026-09-29 Codex Round 2 最终复验：仍需一次收口（历史）

- **任务入口**：`docs/cursor-review-storage-performance-security-round2-final.md`
- **结论**：原 7 项中 F3/F6/F7 通过；Windows RSS/子进程 fallback 可观测。F1/F2/F4/F5 仍有 4 组确定性问题，Round 2 暂不通过
- **状态**：已被上方最终收口条目承接

---

## 2026-09-29 存储硬化 Round 2 评审修复：完成，等待 Codex 最终复验

- **状态**：已按 `docs/cursor-review-storage-performance-security-round2.md` **一次性修复全部 7 个 finding**；**停止等待 Codex 最终复验**；**未进入 Round 3**
- **保留**：`memory.enabled=true`、`retention.enabled=true`；未读/未删 `data/memory`；未提交、未推送
- **离线验证**：`Python312 -X utf8 -m unittest discover -s tests -q` → **407 tests / OK**（此前 392）；`compileall` OK；`git diff --check` OK
- **未运行**：真实 Ollama、主程序、麦克风/扬声器、RVC、浏览器、清理实扫

### Finding 对照
1. **F1**：`build_voice_engines` / `ensure_*` 事务式回滚；`_lifecycle_lock` 与 shutdown 协议；竞态丢弃未发布 TTS（`test_storage_round2_review_fixes.Finding1*`）
2. **F2**：`ensure_tts_for_playback` + `_apply_outcome_side_effects` / 流式路径按需加载；CLI 仅在资源可用时显示开启（`Finding2*` + `text_session`）
3. **F3**：wait 结果结构化；超时/失败跳过 RVC；KI/SystemExit 上抛（`Finding3*`）
4. **F4**：Windows RSS/子进程 WinAPI fallback；不可观测为 null；`diagnose_resource_soak.py` 真 soak；真实 `collect_assistant_queue_depths`（`Finding4*`）
5. **F5**：生产非流式/流式 `request`/`request_stream` 计时；`counts_as_request` 冷热（`Finding5*`）
6. **F6**：`math.isfinite` 拒绝 Inf/NaN（`Finding6*`）
7. **F7**：unload 须显式配置；须曾触达模型；文档改为 shared best-effort（`Finding7*` + ADR）

### 新增测试
- `tests/test_storage_round2_review_fixes.py`

---

## 2026-09-29 Codex Review：存储硬化 Round 2 暂不通过（历史）

- **修复单**：`docs/cursor-review-storage-performance-security-round2.md`
- **状态**：已被上方评审修复条目承接

---

## 2026-09-28 存储硬化 Round 2：按模式装配与资源协调 — 完成，等待 Codex Review

- **状态**：已按 `docs/cursor-next-storage-performance-security-hardening.md` **仅执行 Round 2**；**停止等待 Codex Review**；**未进入 Round 3**
- **保留**：`memory.enabled=true`、`retention.enabled=true` 与现有全部修改；**未删除/未读取** `data/memory` 私人数据
- **离线验证**：`Python312 -X utf8 -m unittest discover -s tests -q` → **392 tests / OK**（此前里程碑约 379）；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **未运行**：真实 Ollama、主程序、麦克风/扬声器、RVC 实机、浏览器、系统电源、清理实扫；未提交、未推送

### 实际修改
1. **模式装配**：`startup_mode` 在装配前可知；`text`/`interactive` 不构造 STT/TTS/AudioPlayer/RVC；`voice` 同步构造；`ensure_voice_stack()` 懒加载 + 结构化失败
2. **资源协调**：`resource.profile=balanced`；`serialize_gpu` + `ollama_then_rvc`；TTS `warm_rvc_on_init=False` + `prepare_rvc()`；退出 `unload_on_exit` 仅 `keep_alive=0`，不停 Ollama 服务
3. **观测**：`SessionTimer`（仅 ID/阶段耗时）；`resource_soak` / `diagnose_resource_soak.py`（RSS/线程/子进程/队列）
4. **文档**：`docs/architecture/adr-mode-assembly-resource-ownership.md`；`config`/`Config` 默认；工程基线；本交接

### 测试
- 新增 `tests/test_mode_assembly_round2.py`（模式装配、串行预热、fake unload HTTP、10k 文本 soak、清理器长目录有界）

### 剩余风险
- 未做 30～60 分钟真实语音 soak；Round 3（SafetyPolicy 子进程能力、远端 HTTP、云 STT/TTS 许可、日志隐私）未开始

---

## 2026-09-28 用户实机验收：RVC 与正式长期记忆通过

- **RVC**：用户复现确认 Nahida 音色恢复正常。
- **正式记忆接线**：`config/config.yaml` 已显式启用 `memory.enabled`，正式库为 `data/memory/long_term.sqlite3`；`memory.retention.enabled=true`。
- **跨会话实测**：用户明确陈述抹茶偏好后产生记录；完整重启程序再次询问，能够正确回答，证明正式库写入、持久化与召回链路通过。
- **测试数据隔离**：未迁移 `tmp/memory-live-test.sqlite3` 中的银杏4729/桂花乌龙验收样本。
- **隐私收口**：`.gitignore` 已忽略 `data/memory/` 与仓库根 `tmp/`，防止真实记忆库和诊断报告误提交；未删除本地数据。
- **当前边界**：仅完成短时用户实测；30～60 分钟 RSS、线程、子进程、CPU/GPU 与延迟曲线仍未验证，不能据此宣称不存在长期泄漏或资源阶梯增长。
- **仓库状态**：Round 2 已在上方条目承接；Round 3 尚未开始。

---

## 2026-09-28 旧污染记录清理：已按用户授权完成

- **执行依据**：用户明确要求“执行删除计划”
- **计划**：`tmp/memory-pollution-delete-plan.json`，digest `b2d10198c816bbe8c8862e319f3cfd744c7497a1206d314626696054387792cd`
- **原子删除结果**：`deleted=3`
- **只读复核**：live SQLite `count=6`；三个目标 ID 剩余数量 `0`
- **审计**：计划文件保留但已消费/失效，不得再次执行；未删除其他记录

---

## 2026-09-28 Codex 最终验收：长期记忆写入可信边界通过

- **最终评审**：`docs/codex-review-memory-ingestion-trusted-boundary-final.md`
- **结论**：`unit_id` 可信边界、segment 先验收、并列 all-or-nothing、非法连接词禁止回退、本地 renderer、无候选不发 HTTP 全部通过；无阻断项
- **独立验证**：29 条语义矩阵 0 失败；33 项专项测试 OK；379 项全量测试 OK；`compileall`、`git diff --check` OK
- **live 库**：验收时只读 count=9；随后经用户明确授权删除 3 条已确认旧污染记录，只读复核 count=6
- **边界**：未运行真实 Ollama/主程序/设备/清理；未提交、未推送；存储 Round 2/3 尚未开始

---

## 2026-09-28 连接词回退旁路已修复：完成，等待 Codex 最终复验

- **状态**：**连接词回退旁路已修复**；已按 `docs/cursor-review-memory-trusted-boundary-conjunction-fallback.md` 区分「无连接词」与「连接词结构非法」；**停止等待 Codex 最终复验**
- **离线验证**：`unittest discover -s tests -q` → **379 tests / OK**（此前 376）；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **live 库**：未改动、未删除、未 `--execute`（只读边界维持）
- **未运行**：真实 Ollama、主程序、设备、清理任务；未提交、未推送

### 修复对照
1. **`try_parse_segment_units`**：先 `_SUPPORTED_CONJ_RE.search`；命中且 `_split_supported_conj_pieces` 为 `None` → 整段拒绝，**禁止**回退 `_parse_single_unit_user`
2. **零候选**：`我喜欢咖啡而且。` / `并且。` / `以及。` / `而且而且…` → 无候选、无 HTTP、临时 SQLite `count==0`
3. **合法负向 child**：`我喜欢咖啡而且我不喜欢红茶。` → 用户喜欢咖啡 + 用户不喜欢红茶

### 测试
- 新增 `test_conj_present_illegal_*`、`test_legal_conj_with_negated_like_child`；`_ZERO` 同步纳入四条非法连接词样例

---

## 2026-09-28 Codex 整段原子性复验：剩余一个连接词回退旁路（历史）

- **任务入口**：`docs/cursor-review-memory-trusted-boundary-conjunction-fallback.md`
- **已通过部分**：问号/疑问语气/已知否认已在拆分前约束整段；合法 `并且|而且|以及` 双事实、`并蒂莲`、span、unit_id renderer 均符合预期
- **最终阻断项**：连接词存在但 child 为空/结构非法时，切分返回 `None`，调用方错误回退单事实贪婪解析
- **状态**：已被上方「连接词回退旁路已修复」承接

---

## 2026-09-28 整段原子性修复：完成（历史）

- **状态**：已按 `docs/cursor-review-memory-trusted-boundary-segment-atomicity.md` 修复；已被连接词回退旁路修复承接
- **离线验证（当时）**：376 tests / OK

---

## 2026-09-28 Codex 最终可信边界复验：未通过，等待整段原子性修复（历史）

- **任务入口**：`docs/cursor-review-memory-trusted-boundary-segment-atomicity.md`
- **已确认正确**：模型响应只保留 `unit_id` 与排序元数据；事实正文/category 由本地 renderer 产生；无候选不发 HTTP
- **最终阻断项**：同一标点 segment 先按连词拆分且仅最后 child 继承终止符，导致疑问/否认语境丢失；裸 `并` 还会误拆 `并且`、`并不是真的` 和值中的普通字符
- **独立复现**：`我喜欢咖啡而且我住在纽约？`、`我喜欢咖啡并且我住在纽约？`、`我叫林小舟以及我住在纽约吗`、`我喜欢咖啡并不是真的` 都错误生成前半段候选
- **要求**：segment 先失败关闭；segment 内所有 child 完整解析才整体接受；连接词语法化；候选/no-HTTP/临时 SQLite 三层矩阵
- **状态**：已被上方整段原子性修复承接

---

## 2026-09-28 最终可信边界重构：完成，等待 Codex 最终复验

- **状态**：已按 `docs/cursor-next-memory-ingestion-trusted-boundary-final.md` 一次性完成可信边界重构与验收矩阵；**停止等待 Codex 最终复验**；未进入存储 Round 2/3；**未**执行旧局部 Round 6
- **离线验证**：`unittest discover -s tests -q` → **372 tests / OK**；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **live 库**：`tmp/memory-live-test.sqlite3` **只读 count=9**；未对 live 使用 `--execute`
- **未运行**：真实 Ollama、主程序、设备、清理任务；未提交、未推送

### 结构对照
1. **本地 `UserFactUnit`**：`src/memory/fact_units.py` 切分并保留终止符；仅完整可解析 claim 入选；`？/?` 与句尾否认/条件/引用等零候选
2. **模型只选 `unit_id`**：Schema `required: [unit_id]`，`additionalProperties=false`；禁止 text/category/evidence_quote
3. **本地 renderer**：`render_claim` / `category_for_claim` 生成 `ExtractedMemory`；恶意模型 text 永不入库存
4. **无候选不发 HTTP**；单项非法选择跳过；合法兄弟保留
5. **验收矩阵**：`tests/test_memory_trusted_boundary.py`（合法双事实写入、零写入矩阵、fake extractor、ingestion 临时 SQLite）

### 测试 / 文档
- 扩展/改写 `test_memory_trusted_boundary`、`test_memory_provenance`、diagnostic/integration/storage fixtures
- 更新 `adr-memory-service.md`、`adr-memory-product-integration.md`、`engineering-baseline.md`

---

## 2026-09-28 Codex 方案收敛：等待最终可信边界重构（历史）

- **最终任务入口**：`docs/cursor-next-memory-ingestion-trusted-boundary-final.md`
- **决策**：停止继续给模型自由 `text` 打验证补丁；改为本地生成完整候选单元与 claim，模型只能选择 `unit_id`，最终事实由本地 renderer 生成
- **必须消除**：模型正文/quote/value/category 的写库权、疑问终止符丢失、自由子串概括和兼容旁路
- **验收**：合法代号/茶等事实写入；历次所有疑问、条件、引用、否认、句尾元陈述和 assistant-only 样例零写入；覆盖完整 ingestion 临时 SQLite
- **状态**：已被上方最终可信边界重构承接

---

## 2026-09-28 Codex Round 5 Review：记忆出处契约暂未通过（历史）

- **评审单**：`docs/cursor-review-memory-ingestion-provenance-round5.md`
- **独立验证**：383 项全量测试、顺序独立 `compileall`、`git diff --check` 均通过；live 库只读 count=9
- **阻断项**：完整单元虽已严格解析，但 value 仍用自由子串对齐；“我喜欢抹茶拿铁是假的”“我住在纽约是谣言”等会错误保存前半段肯定事实
- **状态**：已被顶部“最终可信边界重构”承接，不再单独执行局部 Round 6；不得运行 live 删除、真实 Ollama/设备；未提交、未推送

---

## 2026-09-28 记忆出处契约 Round 5：完成，等待 Codex 最终复验

- **状态**：已按 `docs/cursor-review-memory-ingestion-provenance-round4.md` 完成 Round 5；**停止等待 Codex 最终复验**；未进入存储 Round 2/3
- **离线验证**：`unittest discover -s tests -q` → **383 tests / OK**；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **live 库**：`tmp/memory-live-test.sqlite3` **只读 count=9**；未对 live 使用 `--execute`
- **未运行**：真实 Ollama、主程序、设备、清理任务；未提交、未推送

### Round 5 修复对照
1. **严格包围单元**：`parse_user_predicates_strict` + `unit_strictly_supports_fact`；完整单元必须恰好一条用户谓词，并与候选 family/polarity/value 对齐
2. **授权路径**：`validate_candidate_user_evidence` 用完整单元 claim 授权写库；不再以 `is_clear_assertive_evidence_unit` 关键词黑名单作为主门禁
3. **截断防伪**：quote 自身仍须能支撑候选；条件/引用/否认等未解析前缀单元失败关闭
4. **安全前缀**：仅将 `其实`/`嗯+` 纳入用户谓词完整语法；`别以为/如果/假设/别人说` 等仍失败关闭
5. **准入**：`extract_assertive_clauses` 改为严格解析，而非自我标记+黑名单
6. **保留 Round 1–4**：assistant 隔离、极性、原子覆盖、语境定位、逐项容错、schema required、只读审计与原子删除

### 测试
- 扩展 `tests/test_memory_provenance.py`（`Round5EvidenceStrictUnitTests`）

---

## 2026-09-28 Codex Round 4 Review：记忆出处契约暂未通过（历史）

- **评审单**：`docs/cursor-review-memory-ingestion-provenance-round4.md`
- **独立验证**：377 项全量测试、顺序独立 `compileall`、`git diff --check` 均通过；live 库只读 count=9
- **阻断项**：包围单元只做疑问/不确定关键词排除，未完整解析并与候选对齐；条件、引用、否认语境仍能把截短 quote 伪装成事实，fake extractor 已复现接受
- **状态**：已被上方 Round 5 修复承接

---

## 2026-09-28 记忆出处契约 Round 4：完成，等待 Codex 最终复验

- **状态**：已按 `docs/cursor-review-memory-ingestion-provenance-round3.md` 完成 Round 4；**停止等待 Codex 最终复验**；未进入存储 Round 2/3
- **离线验证**：`unittest discover -s tests -q` → **377 tests / OK**；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **live 库**：`tmp/memory-live-test.sqlite3` **只读 count=9**；未对 live 使用 `--execute`
- **未运行**：真实 Ollama、主程序、设备、清理任务；未提交、未推送

### Round 4 修复对照
1. **证据语境**：`validate_candidate_user_evidence` 定位 `evidence_quote` 在 `user_text` 中的每次出现，检查包围子句/原子单元；单元含疑问槽位、`吗/呢/是否/是不是/有没有`、或 `不确定/不清楚/可能/也许/大概/猜测` 时失败关闭
2. **截短 quote**：quote 自身仍须能支撑候选（保留截断伪造拒绝）；不得仅凭截掉 `吗/？` 后的子串过关
3. **多出现位置**：至少一处明确陈述包围单元即可；疑问位置不误配、不跨边界拼接
4. **混合句**：`我喜欢桂花乌龙，你呢？` / 名字陈述保留；`你觉得我喜欢…吗` 污染候选拒绝，合法兄弟项不受影响
5. **保留 Round 1–3**：assistant 隔离、极性、原子完整覆盖、逐项容错、schema required、只读审计与原子删除

### 测试
- 扩展 `tests/test_memory_provenance.py`（`Round4EvidenceContextTests`）

---

## 2026-09-28 Codex Round 3 Review：记忆出处契约暂未通过（历史）

- **评审单**：`docs/cursor-review-memory-ingestion-provenance-round3.md`
- **独立验证**：371 项全量测试、`compileall`、`git diff --check` 均通过；live 库只读 count=9
- **阻断项**：`evidence_quote` 可从疑问/不确定子句内部截去语气标记后伪装成肯定陈述；混合轮不会被准入跳过，fake extractor 已复现接受错误候选
- **状态**：已被上方 Round 4 修复承接

---

## 2026-09-27 记忆出处契约 Round 3：完成，等待 Codex 最终复验

- **状态**：已按 `docs/cursor-review-memory-ingestion-provenance-round2.md` 完成 Round 3；**停止等待 Codex 最终复验**；未进入存储 Round 2/3
- **离线验证**：`unittest discover -s tests -q` → **371 tests / OK**；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **live 库**：`tmp/memory-live-test.sqlite3` **只读 count=9**；未对 live 使用 `--execute`
- **未运行**：真实 Ollama、主程序、设备、清理任务；未提交、未推送

### Round 3 修复对照
1. **原子完整覆盖**：`provenance.fact_supported_by_evidence` 要求候选严格解析为恰好一条谓词；多子句/并列/`并而且以及`/未解析尾部拒绝；值兼容禁止候选比证据更具体
2. **Prompt**：`EXTRACTOR_SYSTEM_PROMPT` 明确每项单条原子事实，多条拆成多个数组项
3. **未知槽位**：`啥/哪天/几号` 进入准入与占位拒绝；`我的测试代号是啥？` / `我生日是哪天？` / `我对什么过敏？` 本地跳过
4. **保留 Round 2**：极性、混合问句陈述、逐项容错、schema required、只读审计与原子条件删除

### 测试
- 扩展 `tests/test_memory_provenance.py`（`Round3AtomicCoverageTests`）；同步 diagnostic fixture 为原子项

---

## 2026-09-27 Codex Round 2 Review：记忆出处契约暂未通过（历史）

- **评审单**：`docs/cursor-review-memory-ingestion-provenance-round2.md`
- **独立验证**：365 项全量测试、`compileall`、`git diff --check` 均通过；live 库只读 count=9
- **阻断项**：候选只验证已解析到的谓词片段，未验证的第二子句/并列追加/修饰语可随合法前缀入库；例如用户只说“我叫林小舟”，候选“用户叫林小舟，最喜欢抹茶拿铁”被错误接受
- **补充边界**：`啥/哪天/几号` 等常见未知槽位需在纯查询准入与候选验证双重拒绝
- **状态**：已被上方 Round 3 修复承接

---

## 2026-09-27 记忆出处契约 Round 2：完成，等待 Codex 最终复验

- **状态**：已按 `docs/cursor-review-memory-ingestion-provenance-round1.md` 修复 4 项；**停止等待 Codex 最终复验**；未进入存储 Round 2/3
- **离线验证**：`unittest discover -s tests -q` → **365 tests / OK**；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **live 库只读**：`tmp/memory-live-test.sqlite3` **count=9**；**未**对 live 库使用 `--execute`
- **未运行**：真实 Ollama、主程序、设备、清理任务；未提交、未推送

### Finding 修复对照
1. **极性/谓词**：`provenance` 按谓词族匹配肯定/否定；翻转拒绝，相符正负向可通过
2. **子句层查询**：疑问槽位不得作证据；四种纯查询本地跳过；混合句只保留陈述子句；标签占位拒绝
3. **ID 工具**：dry-run 用 SQLite `mode=ro`；确认计划含 id/text/source/updated_at+digest；`--execute --plan` 先全检再单事务条件删除
4. **单项字段错误**：跳过非法项保留合法兄弟；`evidence_quote` 进 Schema `required`

### 测试
- 扩展 `tests/test_memory_provenance.py`；新增 `tests/test_id_audit.py` / `src/memory/id_audit.py`

## 2026-09-27 Codex Round 1 Review：记忆出处契约暂未通过（历史）

- **评审单**：`docs/cursor-review-memory-ingestion-provenance-round1.md`
- **独立验证**：355 项全量测试、12 项专项测试、`compileall`、`git diff --check` 均通过
- **阻断项**：否定/谓词可被翻转接受；常见纯查询被当成陈述并允许占位垃圾；ID 工具 dry-run 非真只读且执行缺少陈旧确认/原子性；单个结构错误丢弃合法兄弟候选
- **状态**：已被上方 Round 2 修复承接

---

## 2026-09-27 记忆 ingestion 出处契约 Round 1：完成，等待 Codex Review

- **状态**：已按 `docs/cursor-next-memory-ingestion-provenance-fix.md` 完成 Round 1；**停止等待 Codex Review**；未进入存储任务 Round 2/3
- **离线验证**：`unittest discover -s tests -q` → **355 tests / OK**；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **live 库只读**：`tmp/memory-live-test.sqlite3` **count=9**；三污染 ID dry-run 可见且**未删除**
  - `42b6f68fa0044fbbad26f29abcb5ae44` / `a8f25792de3e49e39205bdc7c12f0e1b` / `4a97c2e8ed194442ac99007bd4415237`
- **未运行**：真实 Ollama、主程序、设备、清理任务、`diagnose_memory.py --execute`、对 live 库 `--execute` 删除；未提交、未推送

### 修改对照
1. **`src/memory/ollama_extractor.py`**：payload 仅 `user_text`；prompt/schema 增加 `evidence_quote`；本地证据验证后才产出候选（证据不入库）
2. **`src/memory/eligibility.py` + `provenance.py`**：准入只看用户侧；纯记忆询问跳过；自我陈述混合问句仍可提取
3. **`diagnose_memory_ids.py`**：默认 dry-run 按完整 ID 审计；删除须显式 `--execute`
4. **文档**：`adr-memory-service.md`、`adr-memory-product-integration.md`、`engineering-baseline.md`
5. **测试**：新增 `tests/test_memory_provenance.py`；同步既有 extractor fixture

### 未验证
- 真实 Ollama 上阻断助手幻觉再提取；live 库定向清理待 Codex Review 后由用户显式执行

## 2026-09-27 助手回答污染长期记忆：等待 Cursor Round 1（历史）

- **任务入口**：`docs/cursor-next-memory-ingestion-provenance-fix.md`
- **真实证据**：22:43 的错误回答被 ingestion 写成 2 条 `MODEL_INFERRED`；23:05 的复述又写入 3 条，live 测试库现有 9 条
- **状态**：已被上方 Round 1 修复承接

---

## 2026-09-27 Codex 最终验收：Ollama `/api/chat` system 契约通过

- **最终评审**：`docs/codex-review-ollama-chat-system-contract-final.md`
- **独立验证**：343 项全量测试、8 项专项测试、`compileall`、`git diff --check` 全部通过
- **结论**：动态 system 已作为 `messages[0] role=system` 进入最终 HTTP JSON；流式/非流式共用构造边界；未发现阻断项
- **下一步**：用户使用保留的 `tmp/memory-live-test.sqlite3` 运行一次真实纯文本问答，确认回复同时包含 `银杏4729` 与 `桂花乌龙`
- **边界**：未运行真实 Ollama/主程序/设备；未进入存储 Round 2/3；未提交、未推送

---

## 2026-09-27 Ollama `/api/chat` system 契约 Round 1：完成，等待 Codex Review

- **状态**：已按 `docs/cursor-next-ollama-chat-system-contract-fix.md` 完成 Round 1；**停止等待 Codex Review**；未进入存储任务 Round 2/3
- **离线验证**：`unittest discover -s tests -q` → **343 tests / OK**；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **未运行**：真实 Ollama、主程序、麦克风/扬声器/TTS·RVC 实机、浏览器、系统操作、`diagnose_memory.py --execute`；未改 `tmp/memory-live-test.sqlite3`；未提交、未推送

### 修改对照
1. **`src/llm/ollama_client.py`**：共享 `_build_chat_messages`；非空 system → `messages[0]`（`role=system`），顺序 `system → 历史 → 当前 user`；流式/非流式均不再写顶层 `system`
2. **文档**：`docs/architecture/adr-memory-product-integration.md`、`docs/engineering-baseline.md` 明确 Chat API system 传输与 HTTP payload 测试要求
3. **测试**：新增 `tests/test_ollama_chat_system_contract.py`（非流式/流式/无 system/历史隔离/记忆端到端 HTTP JSON；失败与提前 close 不双提交）

### 未验证
- 真实 Ollama 上银杏4729 / 桂花乌龙 文本复验（需显式实机或 `--execute`）

## 2026-09-27 真实 Grounding 验收失败：等待 Cursor 修复 `/api/chat` system 契约（历史）

- **任务入口**：`docs/cursor-next-ollama-chat-system-contract-fix.md`
- **真实证据**：SQLite 与 recall 含正确的 `银杏4729`、`桂花乌龙`，日志为 `entries=4 chars=488 error=-`，模型却回答 `Project Nightingale`、`抹茶拿铁，少糖多奶`
- **根因**：`OllamaClient` 把动态 system 写到 `/api/chat` payload 顶层；Chat API 要求 system 位于 `messages` 的 `role=system` 消息。现有 fake 只验证包装器参数，未验证最终 HTTP JSON
- **状态**：已被上方 Round 1 修复承接

---

## 2026-09-27 Codex 最终验收：Grounding + 存储硬化 Round 1 通过

- **最终评审**：`docs/codex-review-memory-grounding-storage-final.md`
- **独立验证**：335 项纯离线测试通过；`compileall`、`git diff --check` 通过
- **结论**：Grounding 与存储硬化 Round 1 已完成；没有需要继续交回 Cursor 的缺陷
- **未验证**：真实 Ollama grounding、真实音频/设备、实际清理执行；主配置仍 `memory.enabled=false`、`retention.enabled=false`
- **后续**：先做一次保留测试库的真实记忆问答验收，再进入存储任务 Round 2

## 2026-09-27 Grounding+Storage Round 3 修复：完成，等待 Codex 最终复验

- **状态**：已按 `docs/cursor-review-memory-grounding-storage-round3.md` 修复 3 项；**停止等待 Codex 最终复验**；未进入存储任务 Round 2/3
- **离线验证**：`unittest discover -s tests -q` → **335 tests / OK**；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **未运行**：真实 Ollama、主程序、麦克风/扬声器/TTS·RVC 实机、浏览器、系统操作、`diagnose_memory.py --execute`；未提交、未推送

### Finding 修复对照
1. **活动 scandir sweep + 硬预算**：跨短批次复用同一真实 `scandir` iterator；每次 `next()` 计入 `max_scan_files`；状态键 `id(rule)|resolved_path|name`；exhausted / stop / 异常 / 规则移除时 exactly-once `close()`
2. **pending 挡 extract**：`try_acquire_extract` 在 `_foreground_pending > 0` 时失败；维护释放后前台优先于新 extract；KI 后 pending 归零
3. **并发 close 串行**：`_close_gate` 串行化整个 close 事务；成功等待者复用同一最终状态；失败后可重试且 store 最终只成功关一次

### 测试
- 新增 `tests/test_round3_resource_contracts.py`；同步更新 Round2 / storage_bounds 对活动 sweep 句柄语义的断言

## 2026-09-27 Codex 最终验收 Round 3：仍需三项收口（历史）

- **评审单**：`docs/cursor-review-memory-grounding-storage-round3.md`
- **独立验证**：325 项离线测试、`compileall`、`git diff --check` 均通过
- **仍需修复**：续扫 offset 的无预算前缀枚举、foreground pending 时 extract 抢先、并发 close 重复触碰 ingestion
- **状态**：已被上方 Round 3 修复承接

## 2026-09-27 Grounding+Storage Round 2 修复：完成，等待 Codex 最终验收

- **状态**：已按 `docs/cursor-review-memory-grounding-storage-round2.md` 修复 4 项；**停止等待 Codex 最终验收**；未进入存储任务 Round 2/3
- **离线验证**：`unittest discover -s tests -q` → **325 tests / OK**；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **未运行**：真实 Ollama、主程序、麦克风/扬声器/TTS·RVC 实机、浏览器、系统操作、`diagnose_memory.py --execute`；未提交、未推送

### Finding 修复对照
1. **FileCleaner 流式枚举 + 续扫**：改用可注入的 `os.scandir`；禁止依赖 `listdir`/`Path.iterdir` 全量物化；预算耗尽关闭句柄；跨周期游标使后续目标不被饿死
2. **admission 互斥**：extract 与 maintenance 互斥；maintenance 在 foreground/pending/extracting 时失败；`begin_foreground` 条件循环等待维护释放并登记 pending，防止维护插队
3. **维护 Timer 幂等**：`start()` 线程安全且同一时刻最多一个 Timer；generation token 使旧回调失效；close cancel + 有界 join
4. **close 可重试**：区分 closing/closed；仅 ingestion 停且自有 store 关成功后才 final closed；失败保留所有权；禁止无完成时伪造 `drained=True`

### 测试
- 新增 `tests/test_round2_resource_contracts.py`；更新 `tests/test_storage_bounds.py` 的 scandir 预算测试

## 2026-09-27 Codex Round 2：Grounding + 存储硬化仍未通过（历史）

- **评审单**：`docs/cursor-review-memory-grounding-storage-round2.md`
- **独立验证**：312 项离线测试、`compileall`、`git diff --check` 均通过
- **仍需修复**：真实流式目录枚举与跨周期续扫、maintenance/foreground/extract 完整互斥、重复 start 的 Timer 泄漏、runtime 关闭异常后的可重试所有权
- **状态**：已被上方 Round 2 修复承接

## 2026-09-27 Grounding+Storage Round 1 修复：完成，等待 Codex Round 2

- **状态**：已按 `docs/cursor-review-memory-grounding-storage-round1.md` 修复 6 项；**停止等待 Codex Round 2**；未进入存储 Round 2/3
- **离线验证**：`unittest discover -s tests -q` → **312 tests / OK**；`compileall` OK；`git diff --check` OK（仅 LF/CRLF 提示）
- **未运行**：真实 Ollama、主程序、麦克风/扬声器/TTS·RVC 实机、浏览器、系统操作、`diagnose_memory.py --execute`；未提交、未推送

### Finding 修复对照
1. **prune 受保护前缀**：SQL 排除 `USER_CONFIRMED`；淘汰序 importance↑→created_at↑→id；年龄/数量共享 `batch_limit`；`over_capacity_unresolvable` 表达仍超限但无可删
2. **FileCleaner 懒扫描**：逐项计入扫描预算后才取下一项；`from_config` 严格校验 bool/正数，拒绝 `"false"` 与负数夹逼
3. **TempFile 可中断 shutdown**：`stop_event.wait`；前台 `remove` 仅快速尝试后入队；`TTSEngine.shutdown` exactly-once 回收自有 TempFileManager
4. **历史上限配置**：`ollama.history.max_stored_turns` 严格校验，经装配传入 `OllamaClient`；显式注入 history 不覆盖
5. **第三人称边界**：解析跳过无 owner 第一人称（如「我喜欢…」）；合法「用户…」保留；旧库第一人称仍按 subject=user 注入
6. **维护 lease**：`try_acquire_maintenance`/`release_maintenance` 原子契约；无 API 的自定义 gate 失败关闭；close 先校验超时再取消定时器

### Retention 产品语义
- `memory.retention.enabled` 默认仍为 **false**；开启 `memory.enabled` 不会自动 prune
- 配置注释与启动日志明确：retention 关闭时警告；需显式 `enabled: true`（默认保护 USER_CONFIRMED）

### 主要改动文件
- `src/memory/sqlite_store.py`、`models.py`、`admission.py`、`runtime.py`、`ollama_extractor.py`
- `src/core/file_cleaner.py`、`assembly.py`、`config.py`
- `src/voice/audio/temp_files.py`、`src/voice/tts/engine.py`
- `src/llm/conversation_history.py`、`ollama_client.py`
- `config/config.yaml`、`tests/test_storage_bounds.py`、`docs/architecture/adr-storage-retention-cleanup.md`

## 2026-09-27 Codex 统一评审：Grounding + 存储硬化 Round 1 暂未通过（历史）

- **评审单**：`docs/cursor-review-memory-grounding-storage-round1.md`
- **独立验证**：301 项离线测试通过，`compileall` 与 `git diff --check` 通过
- **阻断项**：retention 受保护前缀、FileCleaner 无界枚举、临时删除 Worker 退出/所有权、history 配置未接线、第三人称提取未本地强制、维护准入竞态
- **状态**：已被上方 Round 1 修复承接

## 2026-09-27 Grounding + 存储硬化 Round 1：完成，等待 Codex 统一评审

- **状态**：阶段 A（记忆 Grounding）与阶段 B Round 1（有界存储/清理）均已离线实现；**停止等待 Codex 统一评审**；未进入 Round 2/3
- **任务入口**：`docs/cursor-next-memory-grounding-fix.md` → `docs/cursor-next-storage-performance-security-hardening.md`（仅 Round 1）
- **根因证据**：`docs/codex-review-memory-grounding-2026-09-27.md`
- **数据约束**：未清空/改写 `tmp/memory-live-test.sqlite3`；未把 `MODEL_INFERRED` 伪装成 `USER_CONFIRMED`

### 阶段 A（Grounding）修改
- `src/memory/context_format.py`：可信规则明确「当前用户事实 / 相关时必须使用 / 第一人称=用户 / 新会话不得否认」；JSON 行 `subject=user` + `fact`；正文仍不可执行
- `src/memory/aware_client.py`：system 注入与 grounding 契约对齐
- `src/memory/ollama_extractor.py`：新提取统一第三人称用户事实
- `src/memory/diagnostic.py`：schema/工具版本与 grounding 固定场景（银杏4729 / 桂花乌龙 + 全新会话问法）；假 LLM 路径可离线验证

### 阶段 B Round 1 修改
- `src/llm/conversation_history.py`：`max_stored_turns`（默认 500），按完整轮裁剪
- `src/memory/store.py` / `sqlite_store.py` / `models.py`：`count_memories`、`prune_for_retention`、`vacuum_offline`、`MemoryPruneResult`
- `src/memory/runtime.py` + `ingestion.py`：`memory.retention.*` 配置；`maybe_run_maintenance` 仅 idle + 无前台 lease；禁止热路径 VACUUM
- `src/core/file_cleaner.py`：启动不同步无界扫描；扫描/删除上限；单轮互斥
- `src/voice/audio/temp_files.py`：单一有界延迟删除队列 + 单 Worker
- `config/config.yaml` / `src/core/config.py`：retention 与 file_cleanup 边界默认值
- ADR：`docs/architecture/adr-storage-retention-cleanup.md`；基线已同步有界历史与清理约束

### 离线验证
- `unittest discover -s tests -q`：**301 tests / OK**
- `compileall -q src tests diagnose_memory.py`：OK
- `git diff --check`：OK（仅 LF/CRLF 警告，无空白错误）
- **未运行**：真实 Ollama、`diagnose_memory.py --execute`、RVC/麦克风/扬声器/浏览器/系统操作；Round 2/3

### 剩余风险
- Grounding 真实模型回复仍需用户在保留的 live 测试库上复验
- text 模式仍初始化语音/RVC；RVC subprocess 与远端端点边界留 Round 2/3
- 仓库：未提交、未推送；`memory.enabled=false`；`retention.enabled=false`

---

## 2026-09-27 连续任务：等待 Cursor 实现 Grounding + 存储硬化 Round 1（历史入口）

- **执行顺序**：先 `docs/cursor-next-memory-grounding-fix.md`，完成阶段性记录后立即执行 `docs/cursor-next-storage-performance-security-hardening.md` 的 Round 1；两阶段结束后再停下等待 Codex 统一评审
- **根因证据**：`docs/codex-review-memory-grounding-2026-09-27.md`
- **现场状态**：SQLite 两条事实正确；recall `entries=2 chars=199 error=-`；注入块包含两事实；模型仍否认跨会话记忆
- **根因**：第一人称事实无 owner + system 仅强调“不可信”而未规定相关时使用 + “全新会话”触发模型无记忆先验
- **数据约束**：保留 `tmp/memory-live-test.sqlite3`，不得清库或伪装 `USER_CONFIRMED`
- **本轮连续范围**：Grounding 修复 + 有界会话历史 + SQLite retention/maintenance + 低干扰文件清理与线程上限
- **仍暂缓**：存储硬化 Round 2/3（按模式加载、显存释放、RVC 子进程白名单、端点与云服务隐私边界）
- **约束**：只做离线实现与 fake 测试；不提交、不推送；不启动真实 Ollama/设备/浏览器/系统操作

---

## 2026-09-27 存储、性能与安全硬化：等待 Cursor Round 1

- **任务入口**：`docs/cursor-next-storage-performance-security-hardening.md`
- **审计依据**：`docs/codex-audit-storage-performance-security-2026-09-27.md`
- **当前优先级**：先完成有界会话历史、SQLite retention/maintenance、低干扰文件清理；完成后停止并等待 Codex Review
- **即时安全修正**：系统控制/电源/浏览器自动启动/环境变量修改默认关闭；STT INFO 日志不再保存完整转写
- **Codex 验证**：290 tests / OK；`compileall` OK；`git diff --check` OK（纯离线）
- **已确认性能风险**：text 模式仍初始化语音/RVC；RVC 同步预热；Ollama `keep_alive=30m`；这些留到 Round 2
- **已确认安全债务**：RVC 仍有直接 subprocess 路径；宽泛 subprocess 权限尚未收窄；在线 STT/TTS 与远程端点边界留到 Round 3
- **约束**：不提交、不推送；不启动真实设备、Ollama、RVC、浏览器或系统操作；保留既有未提交修改

---

## 2026-09-27 真实 v3 验收：最终通过

- **状态**：长期记忆真实文本与资源验收通过；本里程碑关闭
- **报告**：`tmp/memory-text-report-v3.json`
- **结论**：`docs/codex-review-memory-context-stability-final.md`
- **契约**：四场景 pass；wrapper seed/final 双 idle pass；close drained、pending=0、incomplete=false；临时目录清理 completed；exit 0
- **runner**：本次只加载一次 `-c 8192` runner，未再发生 8192↔2048 context 重建
- **性能**：pref wall 46.9s→6.64s；pref load 28.6s→3.70s；wrapper 24.3s→14.1s
- **退出资源**：无 Python/`llama-server` 残留；GPU 回到约 1.68 GiB 系统基线；历史临时目录仍为 23，无新增
- **离线基线**：288 tests / OK；compileall OK；diff-check OK
- **边界**：一次样本不代表普遍质量；长时间 soak、显存峰值曲线、独立小模型与端到端陪伴未做
- **仓库**：未提交、未推送；`memory.enabled=false`

---

## 2026-09-27 关闭完整性与 context 稳定性：Codex 最终离线验收

- **状态**：最终离线验收通过；下一步由用户复跑真实 v3 验收
- **结论**：`docs/codex-review-memory-context-stability-final.md`
- **实际结果**：288 tests / OK；compileall OK；diff-check OK
- **目录卫生**：Codex 全量测试前后 `%TEMP%/miku-memory-diag-*` 均为 23，无新增；既有目录未删除
- **额外复现**：真实 ingestion worker Event 阻塞 → 首次 close incomplete → release → reclaim 关闭并删除本轮目录；报告仍保持 exit 1 / `close:incomplete`
- **契约**：wrapper seed+final 双 idle；默认 `num_ctx=None/inherit`；有界 reclaim，失败则 truthful retained
- **边界**：修复后真实 `--execute`、Ollama runner 稳定性、GPU 峰值与 soak 未验证
- **约束**：未启动/停止 Ollama/设备；未提交、未推送；`memory.enabled=false`

---

## 2026-09-27 关闭完整性 Round 1 修复（reclaim + 去 DEFAULT_NUM_CTX）

- **状态**：Round 1 两项已修复；等待 Codex Round 2
- **评审单**：`docs/cursor-review-memory-context-stability-round1.md`
- **约束**：未提交、未推送；**未运行**真实 `--execute`；未启动/停止 Ollama/设备；`memory.enabled=false`；**未删除**既有 23 个 `%TEMP%/miku-memory-diag-*` 目录

### 修复

1. **Finding 1**：incomplete close 后有界 reclaim（`wait_until_idle` → 重试 `runtime.close` → 再删本轮临时目录）；超时仍失败 → `temp_dir_cleanup=retained_incomplete_close`；契约 `close:incomplete` 不被后续清理改成 pass。测试注入 `temp_dir_factory`，禁止污染 `%TEMP%`。
2. **Finding 2**：删除失真常量 `DEFAULT_NUM_CTX=2048`；默认仍为 `None/inherit`。

### 实际验证

```text
%TEMP%/miku-memory-diag-* ：BEFORE=23 → AFTER=23；NEW_DIRS=none（未删既有目录）

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 288 tests in 7.524s … OK

compileall -q src tests diagnose_memory.py → OK
git diff --check → OK
```

### 剩余风险

- 真实 `--execute` 仍未复跑；既有 23 个历史泄漏目录需用户另行授权清理
- reclaim 宽限期外仍 incomplete 时目录会 retained（有意保留失败证据）

---

## 2026-09-27 关闭完整性与 context 稳定性 Round 1 Review（历史）

- **状态**：已被上方 Round 1 修复承接
- **评审单**：`docs/cursor-review-memory-context-stability-round1.md`
- **阻断项**：`deferred_incomplete_close` 没有实际延后清理执行者；incomplete-close 测试每次遗留一个 `%TEMP%/miku-memory-diag-*` SQLite 目录
- **现场证据**：Codex 复跑后共有 23 个该前缀目录，最新目录与本轮测试时间一致；本轮未删除任何工作区外目录
- **契约清理**：移除或改名已失真的 `DEFAULT_NUM_CTX=2048`；真实默认保持 `None/inherit`
- **Codex 验证**：287 tests / OK；compileall OK；diff-check OK
- **约束**：未运行真实 `--execute`；未启动/停止 Ollama/设备；不提交、不推送；`memory.enabled=false`

---

## 2026-09-27 关闭完整性与同模型 context 稳定性

- **状态**：离线实现完成；等待 Codex Review
- **任务入口**：`docs/cursor-next-memory-context-stability.md`
- **依据**：真实 v2 报告 `tmp/memory-text-report-v2.json`（内容通过，close incomplete；同模型 8192↔2048 runner 重建）
- **约束**：未提交、未推送；**未运行** `diagnose_memory.py --execute`；未启动/停止 Ollama/设备；`memory.enabled=false`

### 本轮完成

1. wrapper seed + **final** 双 `wait_until_idle`；最终超时 → `protocol_error=final_idle_timeout`（非内容 warning）；公开 `seed_idle_ok` / `final_idle_ok`
2. incomplete close 占位已由 Round 1 修复为有界 reclaim / retained（原 `deferred_incomplete_close` 表述失真）
3. 默认 `memory.extractor.num_ctx: null`（inherit）：options 不写 `num_ctx`；显式整数 / CLI 仍写入；报告 schema **v3** / tool **1.2.0**
4. ADR / acceptance / baseline / handoff 已同步

### 实际修改文件（本轮主要）

- `src/memory/ollama_extractor.py`、`runtime.py`、`diagnostic.py`
- `src/core/config.py`、`config/config.yaml`
- `tests/test_memory_diagnostic.py`
- `docs/architecture/adr-memory-product-integration.md`、`docs/memory-real-text-acceptance.md`、`docs/engineering-baseline.md`、本文件

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 287 tests in 7.386s … OK

compileall -q src tests diagnose_memory.py → OK
git diff --check → OK
```

### 剩余风险 / 未验证

- **Cursor 未运行真实 `--execute`**；需 Codex 最终评审后由用户复跑真实验收
- inherit `num_ctx` 减轻同模型 runner 抖动属设计推断，真实卡顿改善未测
- 长时间 soak、独立小模型、端到端陪伴未做；退出后无残留 ≠ 已证无泄漏

---

## 2026-09-27 真实 v2 验收失败：等待 Cursor 修复关闭完整性与 context 抖动（历史）

- **状态**：已被上方 context-stability 里程碑承接
- **任务入口**：`docs/cursor-next-memory-context-stability.md`
- **真实报告**：`tmp/memory-text-report-v2.json`
- **实际结果**：四个 extractor 场景通过、wrapper 内容链路通过；close 为 `worker_alive=true / pending=1 / incomplete=true`，进程退出码 1
- **根因 1**：wrapper 只等待 seed 提取；query 成功后新提交的记忆事件尚未完成就进入 5 秒 close
- **根因 2**：同一模型在前台实际 `num_ctx=8192` 与 extractor `2048` 间切换，Ollama 日志显示 runner 重建，造成运行期负载抖动
- **资源复核**：运行后无 Python/`llama-server` 残留，GPU 回到约 1.7 GiB 系统基线；未证明退出后内存泄漏，仍未做长时间 soak
- **约束**：Cursor 不运行真实 `--execute`；不启动/停止 Ollama/设备；不提交、不推送；`memory.enabled=false`

---

## 2026-09-27 长期记忆资源治理：Codex 最终离线验收

- **状态**：最终离线验收通过
- **结论**：`docs/codex-review-memory-performance-quality-final.md`
- **实际结果**：284 tests / OK；compileall OK；diff-check OK
- **Round 1 关闭**：自身 lease 不再丢记忆；流式所有出口释放；duration 仅 int；ADR 唯一
- **边界**：修复后真实 `--execute` 未运行；实际卡顿改善、真实提取质量和长时间 soak 未验证
- **安全状态**：`memory.enabled=false`；未启动 Ollama/设备；未提交、未推送

---

## 2026-09-27 长期记忆资源治理 Round 1 修复

- **状态**：四项已修复；等待 Codex Round 2
- **评审单**：`docs/cursor-review-memory-performance-quality-round1.md`
- **约束**：未提交、未推送；**未运行** `diagnose_memory.py --execute`；未启动 Ollama/设备；`memory.enabled=false`

### 四项修复

1. **Finding 1**：`generate_result` / `generate_stream` 在 `_try_submit` 前 exactly-once 释放本轮 foreground lease；真实 `MemoryIngestionService` 不再因自身 busy 丢弃
2. **Finding 2**：流式自 `begin_foreground` 起，recall KI/SystemExit、同步构造异常、迭代异常、正常完成、提前 close、底层 close 异常均释放；`foreground_count==0`
3. **Finding 3**：遥测 duration 仅接受非负 `int` 纳秒；`1.5`/NaN/Inf/负数/bool → `None`
4. **Finding 4**：ADR「决策」重写为唯一有序 1～9；离线测试检查标题与编号不重复

### 本轮修改文件

- `src/memory/aware_client.py`
- `src/memory/ollama_extractor.py`
- `tests/test_memory_diagnostic.py`
- `docs/architecture/adr-memory-product-integration.md`
- 本文件

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 284 tests in 6.639s … OK

compileall -q src tests diagnose_memory.py → OK
git diff --check → OK
```

### 剩余风险

- 真实 `--execute` 仍未复跑
- 其他并发前台调用仍可使 gate 跳过提取；已发出 HTTP 不可抢占
- 冷加载/独立小模型/soak/端到端未做

---

## 2026-09-27 长期记忆资源治理 Round 1 Review（历史）

- **状态**：已被上方 Round 1 修复承接
- **评审单**：`docs/cursor-review-memory-performance-quality-round1.md`
- **阻断项**：本轮成功记忆可能被自身 foreground lease 丢弃；流式建立异常会泄漏 foreground 计数
- **次要项**：duration 遥测错误接受 float；ADR 决策段落重复
- **Codex 验证**：281 tests / OK；compileall OK；diff-check OK；三个定向复现均确认
- **约束**：未运行真实 `--execute`，未启动 Ollama/设备，未提交、未推送

---

## 2026-09-27 长期记忆资源治理 / 诊断遥测 / 误提取收口

- **状态**：离线实现完成；等待 Codex Review
- **任务入口**：`docs/cursor-next-memory-performance-quality.md`
- **依据**：`docs/codex-review-memory-real-run-2026-09-27.md`
- **约束**：未提交、未推送；**未运行** `diagnose_memory.py --execute`；未启动/停止 Ollama；未改 `memory.enabled=false`；未触设备/主程序

### 本轮完成

1. 提取器独立 `num_ctx`（默认 2048 / 上限 8192）与 `num_predict`（默认 512 / 上限 2048）；options 每次新对象且保留 temperature=0
2. 诊断 keep_alive 默认 `1m` + CLI `--keep-alive`/`--num-ctx`/`--num-predict`；不继承主聊天 30m；dry-run 打印上述字段
3. `extract_detailed` 遥测 envelope（load/prompt/eval ms + token counts）；报告 schema v2；畸形为 null
4. `eligibility`：寒暄/算术与注入在无稳定信号时跳过 HTTP；窄过滤误提取；保留偏好/资料路径
5. 小型 `ForegroundAwareAdmissionGate`（前台忙则跳过提取；不宣称可抢占）
6. ADR / acceptance / baseline / handoff 已更新

### 实际修改文件（本轮主要）

- `src/memory/ollama_extractor.py`、`eligibility.py`、`admission.py`、`ingestion.py`、`aware_client.py`、`runtime.py`、`diagnostic.py`
- `diagnose_memory.py`、`src/core/config.py`、`config/config.yaml`
- `tests/test_memory_diagnostic.py`
- `docs/architecture/adr-memory-product-integration.md`、`docs/memory-real-text-acceptance.md`、`docs/engineering-baseline.md`、本文件

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 281 tests in 6.620s … OK

compileall -q src tests diagnose_memory.py → OK
git diff --check → OK
```

### 剩余风险 / 未验证

- 修复后的真实 `--execute` 未复跑；真实模型质量仍不能仅凭离线资格规则宣称通过
- admission gate 为失败开放跳过，高峰时可能丢后台提取；已开始的 HTTP 仍不可抢占
- 仍使用同一大模型时，冷加载卡顿可能减轻（更小 ctx）但未实测
- 独立小型提取模型、长时间 soak、端到端陪伴未做

---

## 2026-09-27 真实 Ollama 长期记忆验收：资源与质量复核（历史）

- **状态**：已被上方资源治理里程碑承接
- **实测报告**：`tmp/memory-text-report.json`
- **Codex 复核**：`docs/codex-review-memory-real-run-2026-09-27.md`

---

## 2026-09-27 真实 Ollama 长期记忆文本验收入口：Codex 最终离线验收

- **状态**：最终离线验收通过
- **结论**：`docs/codex-review-memory-real-text-final.md`
- **实际结果**：276 tests / OK；compileall OK；diff-check OK；默认 dry-run exit 0
- **边界**：未运行 `diagnose_memory.py --execute` 或真实 Ollama/设备；真实提取质量、延迟和 GPU 争用仍未验证
- **仓库状态**：未提交、未推送

Round 1 的 idle、wrapper、去重/recall/context、未知字段故障，以及 Round 2 的 endpoint URL 参数故障均已关闭。

---

## 2026-09-27 真实 Ollama 长期记忆文本验收入口 Round 2 修复

- **状态**：Round 2 URL 校验已修复；等待 Codex 最终验收
- **评审单**：`docs/cursor-review-memory-real-text-round2.md`
- **约束**：未提交、未推送；**未运行** `diagnose_memory.py --execute`；未启动真实 Ollama/设备

### Round 2 修复

- `normalize_diagnostic_base_url`：缺 scheme → 规范化为 `http://...` 并写入 `DiagnosticEndpoint.base_url`；仅 `http`/`https`；拒绝 query/fragment/非法端口（`MemoryValidationError` → CLI exit 2）
- ADR / `docs/memory-real-text-acceptance.md` 已写明规则
- Round 1 全部确定性故障测试保留

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 276 tests in 6.804s … OK

compileall -q src tests diagnose_memory.py → OK
git diff --check → OK
diagnose_memory.py（无 --execute）→ dry-run exit 0
```

### 未验证

- 真实 `--execute` / 本机 Ollama 提取质量与延迟

---

## 2026-09-27 真实 Ollama 长期记忆文本验收入口 Round 2 Review（历史）

- **状态**：已被上方 Round 2 修复取代
- **当时阻断**：无 scheme / ftp dry-run 误放行；非法端口泄漏 ValueError 且 exit 1

---

## 2026-09-27 真实 Ollama 长期记忆文本验收入口 Round 1 修复

- **状态**：Round 1 四项已修复；等待 Codex Round 2
- **评审单**：`docs/cursor-review-memory-real-text-round1.md`
- **约束**：未提交、未推送；**未运行** `diagnose_memory.py --execute`；未启动真实 Ollama/设备

### Round 1 修复

1. `wait_until_idle`：以 `_accepted_pending` 线性化已接受未完成任务；gated dequeue 时不再误报 idle
2. wrapper：场景后 `store.clear()` 隔离；`memory_in_system`/`FakeDiagnosticLLM` 要求本轮事实「桂花乌龙」；seed 空提取不得 exit 0
3. 去重二次创建 → contract failure；稳定场景 recall/context 空 → quality warning；指标参与判定
4. `_parse_item` 拒绝未知键；ADR 已同步

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 273 tests in 6.533s … OK

compileall -q src tests diagnose_memory.py → OK
git diff --check → OK
diagnose_memory.py（无 --execute）→ dry-run exit 0
```

### 未验证

- 真实 `--execute` / 本机 Ollama 提取质量与延迟

---

## 2026-09-27 真实 Ollama 长期记忆文本验收入口 Round 1 Review（历史）

- **状态**：已被上方 Round 1 修复取代
- **当时阻断**：idle 误报、wrapper 假通过、去重/召回未入判定、额外字段被接受

---

## 2026-09-27 真实 Ollama 长期记忆文本验收入口

- **状态**：离线实现与测试已完成；等待 Codex Review
- **任务入口**：`docs/cursor-next-memory-real-text-validation.md`
- **约束**：未提交、未推送；**未运行** `diagnose_memory.py --execute`；未启动真实 Ollama/设备/主程序；未改默认 `memory.enabled=false`

### 本轮完成

1. `OllamaMemoryExtractor`：独立 JSON Schema `format`、`stream=false`、`temperature=0`；`keep_alive` 支持非空字符串与有限数值；本地严格解析保留
2. `MemoryIngestionService.wait_until_idle` + `MemoryRuntime.wait_until_idle`（Condition，无 sleep 猜测）
3. `diagnose_memory.py` + `src/memory/diagnostic.py`：默认 dry-run；`--execute` 合成场景 + 临时库 + 报告契约
4. 离线测试 `tests/test_memory_diagnostic.py`；文档 `docs/memory-real-text-acceptance.md`；ADR/基线已更新

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 269 tests in 6.284s … OK

compileall -q src tests diagnose_memory.py → OK
git diff --check → OK
diagnose_memory.py（无 --execute）→ dry-run exit 0
```

### 未验证

- 真实 `--execute` 对本机 Ollama 的提取质量、延迟与 JSON 遵循率（需用户显式运行）
- GPU 并发调度；语音/设备链路

---

## 2026-09-27 长期记忆产品接线 Round 2 修复

- **状态**：Codex 最终离线验收通过
- **任务入口**：`docs/cursor-review-memory-integration-round2.md`
- **约束**：未提交、未推送；未启动真实 Ollama/设备/主程序；未扩大到调度或其他子系统

### Round 2 修复

1. `MemoryAwareLLMClient` 显式代理 `model`、`check_model_available()`、`list_models()`（保留既有 `check_connection`/`start_warmup`）；不用 `__getattr__`
2. `MemoryRuntime.close(timeout=...)` 覆盖参数先经 `_require_finite_number`；非法值不调用 ingestion/store、不改变所有权

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 257 tests in 6.102s … OK

compileall -q src tests → OK
git diff --check → OK
```

### Codex 最终验收

- 包装后 CLI/语音预检所需的 `model`、`check_model_available()`、`list_models()` 均正常代理；无 `AttributeError`。
- close 覆盖 timeout 对 bool/字符串/NaN/Inf/负数严格拒绝且无关闭副作用；0 与有限正数正常传递。
- Round 1 全部故障复现继续通过；完整结论见 `docs/codex-review-memory-integration-final.md`。

---

## 2026-09-27 长期记忆产品接线 Round 2 Review（历史）

- **状态**：已被上方 Round 2 修复取代
- **当时阻断**：CLI 预检成员缺失；close timeout 静默 `float(bool/str)`

---

## 2026-09-27 长期记忆产品接线 Round 1 修复

- **状态**：Round 1 已修复；等待 Codex Round 2
- **任务入口**：`docs/cursor-review-memory-integration-round1.md`
- **约束**：未提交、未推送；未启动真实 Ollama/设备/主程序；未扩大到调度或其他子系统

### Round 1 修复

1. turn_id 工厂/`submit` 失败开放，不破坏已成功前台结果（非流式+流式 COMPLETED 测试）
2. 流式外层提前 close 时显式关闭底层迭代器一次；不提交半轮
3. `create_from_settings` 中途失败局部关闭已创建 store
4. store close 失败/worker 存活保留所有权；助手 `shutdown` 可仅重试 memory close；TTS 等不重复关
5. 配置：真 int 校验；`enabled=false` 忽略损坏子配置；timeout 拒绝 NaN/Inf/bool

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 254 tests in 5.990s … OK

compileall -q src tests → OK
git diff --check → OK
```

### 需 Codex Round 2 复审

- turn_id 异常是否仍可能污染前台成功路径
- 流式 close 是否可移植关闭底层迭代器
- runtime 构造泄漏与关闭所有权/重试路径是否闭合
- 禁用路径是否仍因休眠坏配置启动失败

---

## 2026-09-27 长期记忆产品接线里程碑

- **状态**：Codex Round 1 未通过；等待 Cursor 按评审单修复
- **任务入口**：`docs/cursor-review-memory-integration-round1.md`
- **约束**：未提交、未推送；默认 `memory.enabled=false`；未启动真实 Ollama/设备/主程序；未改 STT/TTS/RVC/持续监听

### 本轮完成

- `OllamaMemoryExtractor`：无历史 `/api/chat`；transport 可注入；构造不联网
- `MemoryAwareLLMClient`：有界 recall 只扩 system；成功完整轮非阻塞 submit；流式半轮不提交
- `MemoryRuntime` + `memory` 配置；`assemble_default` 启用路径校验→创建→包装→start；回滚/shutdown 有界关闭
- ADR：`docs/architecture/adr-memory-product-integration.md`；基线 6.15

### 实际修改文件（本轮新增/接线）

- `src/memory/ollama_extractor.py`、`aware_client.py`、`runtime.py`、`errors.py`、`__init__.py`
- `src/core/assembly.py`、`assistant.py`、`config.py`
- `config/config.yaml`（`memory.enabled: false`）
- `tests/test_memory_integration.py`
- `docs/architecture/adr-memory-product-integration.md` 及基线/交接

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 245 tests in 6.070s … OK

compileall -q src tests → OK
git diff --check → OK
```

### 未验证

- 真实 Ollama 提取质量与延迟；同机 GPU 争用；端到端陪伴体验；语义/向量检索

### Codex Round 1 结论

- 全量 245 项离线测试通过，但补充复现确认 5 组未覆盖契约缺陷。
- 必须修复：辅助 turn_id 失败开放、底层流显式关闭、runtime 构造回滚、关闭失败/超时后的最终所有权、严格且禁用兼容的配置校验。
- 详细触发条件、复现结果和验收标准见 `docs/cursor-review-memory-integration-round1.md`。

---

## 2026-09-27 长期记忆应用服务 Round 2 修复

- **状态**：Codex 最终离线验收通过
- **任务入口**：`docs/cursor-review-memory-service-round2.md`
- **约束**：未提交、未推送；未接入真实 Ollama/对话注入/产品装配；未启动真实设备或模型

### Round 2 修复

1. **owned store 超时关闭**：`close()` 在 `worker_alive=True` 时不关闭 owned store，保留所有权；状态继续报告未完成；后续 `close` 完成写入后只关一次；断言无 `add_after_close`。
2. **候选截断**：`list_memories(limit=candidate_limit+1)` 探测 has-more；恰好等于上限为 `truncated_candidates=False`；`candidate_count` 不含探测项；存储允许有界探测上限 501。

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 232 tests in 6.002s … OK

compileall -q src tests → OK
git diff --check → OK
```

### Codex 最终验收

- owned store 超时关闭确定性复现通过：第一次返回 `timed_out=True, worker_alive=True, pending=1` 且不关库；释放 worker 后第二次关闭保存已接受任务、`pending=0`，store 只关闭一次且无 `add_after_close`。
- 候选数量恰好等于上限的确定性复现通过：`candidate_count=2` 且 `truncated_candidates=False`。
- 完整结论见 `docs/codex-review-memory-service-final.md`；本里程碑仍未接入真实 Ollama、提示词、产品装配或设备链路。

---

## 2026-09-27 长期记忆应用服务 Round 1 修复

- **状态**：Round 1 已修复；等待 Codex Round 2
- **任务入口**：`docs/cursor-review-memory-service-round1.md`
- **约束**：未提交、未推送；未接入真实 Ollama/对话注入/产品装配；未启动真实设备或模型

### Round 1 修复

1. **drain=True 满队列**：业务队列与 `_stop_event` 分离，不再用哨兵挤占容量偷删已接受任务；满载 + 阻塞 extract 时两个已接受 turn 均被处理。
2. **submit/stop 线性化**：可接受检查与入队在同一锁协议下；停止边界之后只能返回 False；测试钩子证明不存在「True 却落在退出信号之后」。
3. **BaseException 后 drained**：`KeyboardInterrupt`/`SystemExit` 仍不当普通失败；`drained` 由实际 `pending` 得出；必要时启动替代 drain worker 排空，不得谎报。
4. **finite 校验**：`ScoredMemory.score` 与 `stop(timeout=...)` 拒绝 NaN/Inf/bool。
5. **文档**：工程基线最近离线结果更新为 230；ADR 补充停止协议。

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 230 tests in 5.785s … OK

compileall -q src tests → OK
git diff --check → OK
```

### 需 Codex Round 2 复审

1. 满队列 drain 是否仍可能丢已接受任务。
2. submit 与 stop 竞态是否仍可能 True 落在边界之后。
3. BaseException 退出后 drained/pending 是否诚实，替代 worker 是否有界。
4. finite 与基线数量是否与实现一致。

---

## 2026-09-27 长期记忆应用服务里程碑

- **状态**：实现完成；等待 Codex 独立 Review
- **任务入口**：`docs/cursor-next-memory-service-milestone.md`
- **约束**：未提交、未推送；未接入真实 Ollama/HTTP 提取器；未注入提示词或用户输入；未修改助手/Handler/CLI/VoiceSession/`assemble_default`；未启动真实设备或模型

### 本轮完成

- 应用契约：`MemoryTurnEvent` / `ExtractedMemory` / `MemoryExtractor` / `MemoryRecallResult` / `MemoryContext`
- 有界异步提取：`MemoryIngestionService`（非阻塞入队、满载拒绝、失败开放、幂等 stop/close、默认不拥有 store）
- 确定性词法检索：`LexicalMemoryRecall` + `list_memories(newest_first=...)`；中文 bigram 为主，非语义检索
- 上下文预算：`format_memory_context` / `MemoryQueryService`（条数+字符、JSON Lines、不可信声明）
- ADR：`docs/architecture/adr-memory-service.md`；工程基线 6.14

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 225 tests in 5.365s … OK

compileall -q src tests → OK
git diff --check → OK
```

### 未验证 / 明确未做

- 真实 Ollama 提取适配器与模型资源调度
- 记忆上下文注入系统提示词或用户 prompt
- 产品装配与启动时自动 `start()`
- 向量/语义检索；真实麦克风、TTS、RVC、浏览器

### 需 Codex Review

1. SUCCESS 入队与非成功/空正文拒绝是否闭合。
2. 提取写库是否始终 `MODEL_INFERRED`，非法候选/异常是否失败开放且不杀死 worker。
3. 词法检索是否有界、确定排序、无匹配为空，且未宣称语义检索。
4. 上下文预算是否永不越界；日志是否不含正文。
5. 关闭语义、drain/超时、`owns_store` 默认 False 是否正确。

---

## 2026-09-27 长期记忆持久层 Round 2 修复

- **状态**：Codex 最终离线验收通过
- **任务入口**：`docs/cursor-review-memory-round2.md`
- **约束**：未提交、未推送；未接入对话/提取/检索/UI；未启动真实设备或模型

### Round 2 修复

- 展示正文：`_validate_display_text` 仅用 `strip()` 判空，合法时**原样返回**（保留首尾与内部空白）；去重仍只经 `normalize_dedupe_key`。
- 测试：NewMemory 保留空白、SQLite 往返原样、规范化等价第二次写入返回第一条原始展示正文。
- ADR / 工程基线已写明展示正文原样保存与 `MemoryDataCorruptError`。

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 211 tests in 5.056s … OK（Codex 独立复验）

compileall -q src tests → OK
git diff --check（本轮相关路径）→ OK
```

### Codex 最终验收

- 展示正文仅用 `strip()` 判空，合法时原样保留。
- 去重仍只经 `normalize_dedupe_key`，第二次写入返回第一条原始展示正文。
- ADR、基线、交接与实现一致；`compileall` 与 `git diff --check` 通过。

---

## 2026-09-27 长期记忆持久层 Round 1 修复

- **状态**：Round 1 已修复；等待 Codex Round 2
- **任务入口**：`docs/cursor-review-memory-round1.md` / `docs/cursor-next-memory-milestone.md`
- **约束**：未提交、未推送；未启动真实设备或模型；未接入对话/提取/检索/UI；保留既有未提交修改

### Round 1 修复

1. 领域：`math.isfinite` 拒绝 NaN/Inf；`updated_at >= created_at`；`dedupe_key` 必须等于 `normalize_dedupe_key(text)`。
2. 异常：公开操作将 `sqlite3.Error` 映射为 `MemoryStoreUnavailableError`；破损行 → `MemoryDataCorruptError`；`mkdir` 纳入转换；`_open` 失败关闭局部连接。
3. 事务：取消 autocommit；写操作与 schema 初始化使用 `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK`；初始化失败不提升 `user_version`。
4. 删除未使用的 `MemoryDuplicateError`；精确重复返回 `created=False` 且不升级来源。
5. 恢复本文件既有语音/STT/性能交接历史（以下保留）。

### 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 209 tests in 5.074s … OK

compileall -q src tests → OK
git diff --check（本轮相关路径）→ OK
```

### 需 Codex Round 2 复审

1. NaN/Inf 与 dedupe_key 不变量是否闭合。
2. 关闭底层连接 / 破损行 / mkdir 失败是否均收口为具名记忆异常。
3. 真实事务与 schema 失败不 bump `user_version`。
4. `MODEL_INFERRED` 后同正文 `USER_CONFIRMED` 是否仍不升级来源。

---

## 2026-09-27 音频侧首句延迟

- **状态**：第一阶段 Ollama 优化真机确认有效；第二阶段音频优化离线通过，等待真机复验
- **最新实测**：Ollama 首增量 2.877 秒、服务端总计 3.909 秒；首个 MP3→WAV 1.303 秒、首个 RVC 推理 5.320 秒；插话后的 Worker 冷重启使一次 RVC 总等待达到 12.1 秒
- **修改**：启动时执行一秒不播放的 RVC 推理预热；librosa 重采样预热作为默认关闭的可选项；空闲 Worker 插话时保留，活跃工作立即有界终止；取消代际与互斥重建避免旧任务/新 Worker 竞争
- **结果**：191 项 unittest、compileall、`git diff --check` 通过
- **边界**：第二阶段未启动真实麦克风、扬声器、edge-tts 或 RVC；真实首句改善待用户复验；未提交、未推送

## 2026-09-26 首轮响应延迟

- **状态**：Codex 已按真实日志定位并完成离线优化，等待用户真机复验
- **实测根因**：冷态 Ollama 首增量约 46.2 秒；热态约 7.4 秒；首段 RVC 约 10.3 秒，且原实现每段重复约 1.8 秒 CUDA 探测
- **修改**：Ollama 后台空 chat 预热、30 分钟可配置保活、官方阶段指标日志；RVC Worker 缓存键不再重复设备探测
- **决策**：`docs/architecture/adr-voice-latency-prewarm.md`
- **结果**：183 项 unittest、compileall、`git diff --check` 通过
- **边界**：本轮未发起新的 Ollama 生成、麦克风、扬声器、edge-tts 或 RVC 推理；真实改善尚待复验；未提交、未推送

## 2026-09-26 持续监听与插话

- **状态**：Codex 离线实现与评审通过，等待用户真机调参
- **决策**：默认持续监听；转写后过滤助手回声、重复语气音和极短内容；有效插话取消生成、待播队列、播放器与 RVC Worker
- **兼容**：串行 `safe_to_resume_listen` 路径和旧 STT provider 保留
- **架构**：`docs/architecture/adr-duplex-listening.md`
- **真机步骤**：`docs/manual-voice-runtime-check.md`
- **结果**：174 项 unittest、compileall、`git diff --check` 通过
- **边界**：不是声学 AEC/语义 VAD；未启动真实麦克风、扬声器、Ollama、edge-tts、RVC；未提交、未推送

## 2026-09-26 语音运行时反馈修复

- **状态**：Codex 离线评审通过，等待用户真机联合复验
- **评审记录**：`docs/codex-review-voice-runtime.md`
- **真机步骤**：`docs/manual-voice-runtime-check.md`
- **结果**：168 项 unittest、compileall、`git diff --check` 通过
- **边界**：未启动真实麦克风、扬声器、Ollama、edge-tts、RVC；未提交、未推送

---

- **状态**：已通过 Codex 最终离线验收
- **任务入口**：`docs/cursor-next-stt-milestone.md` / `docs/codex-review-stt-final.md`
- **约束**：未提交、未推送；未启动 main.py / 真实麦克风 / Google / Whisper / RVC / Ollama / 浏览器 / 系统操作；保留全部既有未提交修改（含 STT、RVC Worker、语音流式契约）

## 独立评审结论

对照仓库代码与 `tests/test_stt_contract.py` 自行核验（不信任既有摘要）：

| 区域 | 结论 |
|------|------|
| `recognition.py` | 不变量成立；导入无设备副作用。原先仅靠 `__post_init__`，对绕过构造的破损对象缺少边界收口。 |
| `stt.py` | Provider 注入跳过默认初始化；WaitTimeout/UnknownValue/RequestError/OSError/未就绪/短音频/Whisper 空结果与异常映射正确；`listen()` 字符串兼容；`recognize()` 不打印；取消在开麦前与录音后检查；KI/SystemExit 上抛；校准/短音频/临时文件/回退保留。 |
| `assistant.py` | 旧 `listen` 兼容与预取消正确；`process_voice_input` 契约保持。原先对结构化结果只做 `isinstance`，破损 SUCCESS/带正文失败可能漏过。 |
| `voice_session.py` | SILENCE 静默重试、UNRECOGNIZED 节流、CANCELLED 退出、致命错误单次 STT → `stt_error`；不与 `unsafe_pause` 混用；旧「STT 模块未初始化」不进 LLM。流式播报既有行为保留。 |
| 测试 | 既有覆盖大体到位；缺破损对象收口、SystemExit、Whisper 临时文件删除与缺模型早退的行为断言。 |

## 实际修改

1. `RecognitionResult.coerce()`：非法类型 / 绕过 `__post_init__` 的破损对象 → `INTERNAL_ERROR`，正文清空。
2. `STTEngine.recognize`、`MikuAssistant.recognize_voice_input`、`VoiceSession._recognize_once` 经 `coerce` 收口。
3. Whisper 缺模型时在打开麦克风前返回 `NOT_READY`。
4. 补充测试：coerce 不变量、SystemExit 上抛、Whisper 临时文件清理与早退、`process_voice_input` 字符串契约、破损 SUCCESS 不得进 dispatch 且结果为 `stt_error`（非 `unsafe_pause`）。
5. ADR 补充 `coerce` 说明。

## 实际验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 158 tests in 4.411s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
→ OK (exit 0)

git diff --check
→ OK（仅 Git LF/CRLF 提示，无 trailing whitespace / conflict marker）
```

全部 fake/离线；日志中的构造失败、网络错误、播报失败与识别失败来自预期故障用例。

## 明确未验证

- 真实麦克风、Google 识别、Whisper 模型与真实连续语音体验
- 阻塞 `Recognizer.listen()` 的即时硬取消
- VAD、唤醒词、后台录音、回声消除、barge-in
- 未更换或安装 STT 模型/依赖

## 需 Codex 复审

Codex 已完成上述五项复审。发现 `coerce()` 首次实现未完整复验所有字段：破损成功结果的 `error_detail` 会被忽略，缺字段实例会逃逸普通异常。现已改为通过数据类构造器完整重验 `status/text/error_detail`，并补充对应测试；Whisper 缺模型测试也改为设备构造一旦发生便失败。未发现其他阻塞缺陷。

Codex 最终实际验证：

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -v
→ Ran 158 tests in 4.435s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests
→ OK (exit 0)

git diff --check
→ OK（仅 Git LF/CRLF 提示）
```

---

**长期记忆持久层里程碑已通过 Codex 最终离线验收；未提交、未推送。**
