# Cursor 交接状态

## 2026-11-12 输入链路v2本轮已独立验收 — 等待用户真实麦克风反馈

当前依据：[最终评审与证据](codex-review-input-audio-v2-final-2026-11-12.md)。官方Cursor编码、Codex逐阶段评审和独立验收。已接入CPU Silero + WebRTC降噪、固定启动线程预算、双工单采集线程/队列2/12秒上限/640ms静音收尾/3秒陈旧丢弃、可重试回收、迟到识别取消及采集时间窗文本回声过滤。最终327项相关fake回归及独立边界/真实CPU固定文件验收通过；100次采集生命周期内存稳定、控制器/session回收。无真实mic/播放/ASR/LLM/TTS/RVC整链路测试；实际播放参考/声学AEC/声纹/误打断恢复仍未完成。

用户下一步：[隔离人工测试](manual-input-audio-v2-test-2026-11-12.md)。正式data/memory未访问，Nahida音频hash保持，未提交/推送。当前无在途开发/测试；下方“开发中/未接入/无在途”的阶段记录均为历史，勿重复执行。

## 2026-11-12 输入链路v2 — 概率VAD/启动预算/本地NS已独立验收，连续流核心开发中

Codex设计评审、官方CursorAgentCLI编码。Phase2a见[验收](codex-review-input-audio-v2-phase2a-accepted-2026-11-12.md)，NS见[验收](codex-review-input-audio-v2-ns-accepted-2026-11-12.md)：最新252项独立fake回归通过，正式STT+真实CPU降噪/Silero+假ASR的固定文件25/25噪声拒绝、9/9语音对照通过；501个nativeDSP实例weakref回收、500次内存近似稳定。仅固定文件，用户真实房间/麦克风未测。未改ASR/RVC/Cosy环境/音色，正式data/memory未访问，未提交推送。

当前Cursor在执行[流式核心任务](cursor-task-input-audio-v2-streaming-core-2026-11-12.md)，只做连续DSP/VAD状态和有界segmenter，尚未接麦克风会话；未进入播放参考/AEC/误打断恢复。参考[连续输入设计](design-input-audio-continuous-2026-11-12.md)。下方2026-11-05“下一版未实施/无在途”是历史状态，勿当当前结论。

## 2026-11-05 防噪补充研究 — 研究完成，下一版方案未实施

依据：[市场策略对比与扩展实验](research-real-time-voice-input-strategies-2026-11-05.md)。当前 WebRTC mode2 放行了19个超过最低门限的合成非人声样本中的11个，mode3仍放行10个；这是VAD准入实验，不是ASR误转写率。上轮低幅负样本覆盖不足，Phase A代码通过不代表真实防噪问题已解决。下一版优先补输入降噪、概率型VAD、连续帧状态和目标房间样本验收，保留本地Paraformer；语义断句与本人识别分开规划。本轮没有下发Cursor/改生产代码/安装新模型，正式记忆未访问，无在途任务。

## 2026-11-05 安静环境误触发 Phase A — Codex 代码/离线 Accepted

当前依据：[最终审查](codex-review-voice-input-final-2026-11-05.md)。官方 Cursor 编码、Codex 独立评审；有界动态门限、本地轻量 WebRTC VAD、显式麦克风选择、PCM 硬边界和失败提示已接入默认 Paraformer 麦克风路径。最终 182 项相关回归 + 7 项独立检查通过；固定音频/合成噪声回放及 500 次调用资源观察通过。早期测试麦克风隔离缺口已修复并在最终门禁重验。真实麦克风效果待用户重启复测；双工回声 Phase B/本人验证未做。主 Python 新增固定 VAD wheel，未改 RVC/ASR 环境/模型，未访问正式记忆，未提交推送，无在途 Cursor/测试。下方 ASR 一期记录是本次优化之前的验收依据。

## 2026-11-05 本地 ASR 一期迁移 — Codex 最终 Accepted

当前依据：[最终验收](codex-review-local-asr-final-2026-11-05.md)。默认Paraformer/云STT关闭、独立环境与模型校验完成；232项相关离线检查（1既有skip）、18+8独立探针、实际配置轻量检查与最终真实CPU诊断通过。真实worker/I/O/PID回收，旧828个WAV及Nahida参考保持。冷约11.4秒、热p50约0.36秒；物理/commit可用门槛4/6GiB。当前无在途Cursor/模型/测试；未提交推送，正式memory未访问。剩余为用户麦克风/声卡、噪声/口音与新ASR全栈长测体验，不能声称已完成这些设备验收。以下Cursor STOP/待复核段落均为历史，不要重复执行旧任务。

## 2026-11-05 ASR final switch 返修 — Cursor STOP，待 Codex 复审

依据 [`docs/codex-review-asr-final-switch-2026-11-05.md`](codex-review-asr-final-switch-2026-11-05.md)：Paraformer `_ensure_worker_until` 在 pending I/O 时拒绝 spawn；装配 STT 回滚仅登记可调用 `shutdown`（遗留 LegacySTT 跳过）；Paraformer 回滚校验 complete；`_paraformer_runtime_ready` 在 `_shutdown_cancel` 置位时为 false；`test_diagnose_asr` 补 `redirect_stderr`。新增/扩展 `test_paraformer_races_round2.PendingIoOwnershipTests`、`test_assembly_rollback` 遗留/资源 STT 用例、`test_local_asr_final_switch` 关闭中就绪。**未执行**测试/模型；未改 `tmp/codex_*`。

## 2026-11-05 本地 ASR 默认切换 — Cursor 代码/测试 STOP，待 Codex 最终评审

任务 [`docs/cursor-task-local-asr-final-switch-2026-11-05.md`](cursor-task-local-asr-final-switch-2026-11-05.md)；前置 ACCEPTED [`docs/codex-review-local-asr-real-accepted-2026-11-05.md`](codex-review-local-asr-real-accepted-2026-11-05.md)。已改：`config/config.yaml` + `Config` 默认、`assembly`/`STTEngine` 缺省、`SafetyPolicy.from_app_config` 网络/ASR 缺省、`stt` 未就绪不开麦、`diagnose_asr` import 安全、README/VOICE/ADR/交付状态。新增 `tests/test_local_asr_final_switch.py`；`test_diagnose_asr` import 流测试；`test_security_round3b` app 缺省 cloud_stt 断言更新。**Cursor 未跑** pytest/模型/设备。Codex 待 scoped regression + final review。未提交/未推送；保留 dirty、828 WAV、Nahida、正式 memory、`tmp/codex_*` 探针。

## 2026-11-05 Root最终验收：本轮代码与无设备30分钟观察完成

唯一当前入口docs/current-delivery-status-2026-11-04.md；证据docs/codex-review-final-voice-soak-milestone-2026-11-05.md。下面更早“未长测/资源不足/待复核/暂停”等描述均为历史，不据此重做。

官方Cursor会话91265b8e-2f72-40be-a25f-5fe396bb9362最后窄幅修复Cosy纯标点计划及fake测试，Root逐项复核122项相关回归+18项独立根测试通过。最终real soak30_punctuation_nov5o：steady1800秒（28分钟按15秒轮次+2分钟空闲），112/112成功、114音频校验、全quiet；141运行文件hash不变、共享WAV828精确零差，原失败与取消保留。句柄阈值触发后补做320/256轮真实队列/预取fake合成验证，128条状态上限后平台稳定、对象释放回基线。

没有在途CLI/模型/测试；未提交推送、正式memory未访问，默认配置与Nahida参考hash保留。剩余为实际STT/声卡/主观听感/目标低配硬件验收。不得声称任意负载永久无泄漏或瞬时回复；本次首音频就绪中位约6.1秒。继续遵守主额度<=10%停止，不自动reset。

## 2026-11-04 Root最终里程碑：剩余代码与离线验收已完成

唯一当前入口：docs/current-delivery-status-2026-11-04.md。完整依据docs/codex-review-performance-milestone-2026-11-04.md；下面各项“待Root/Proposed/下一设计/未放行”均为历史，不得据此重做。

TFM完整deadline已Accepted；LLM冷热内存准入已Accepted，覆盖实际默认聊天/流/预热和memory transport/关闭。最后官方Cursor会话dea96dad-162d-48fb-b3ea-ca9cf6f0c3bd完成四文件窄幅整理并STOP，Root逐行复核与独立12/88通过。资源Root3矩阵：fake交替请求200次、默认实际类装配关闭25次后对象及线程均回收；真实低内存约146ms拒绝，零HTTP。各组共享WAV826零差，217Python AST和diffcheck通过，Nahida样本hash保持。

没有在途CLI/测试/模型任务。未提交推送、正式data/memory未访问。当前只剩资源满足后的真实语音短测，以及本轮明确后置的30–60分钟长测；当前约1.5GiB可用物理内存，不得冒险起真实模型或放宽阈值。继续前先只读采样并显式保护旧诊断裸客户端。主额度仍遵从primary<=10%停止，周额度忽略、不自动用reset credits。

## 2026-11-04 Root权威收口：本轮六切片最终PASS，全部CLI/测试已结束

以docs/codex-performance-progress-2026-11-04.md及各codex-review最终顶节为准。导出/预取精简、非流单consumer、RVC CPU预算、Cosy被动对账、TFM解pin唤醒均已Root独立验收；最后TFM5+78+9+1组均WAV826零差（组有重叠不相加）。下面RVC/Cosy等“待Root”是历史，ADR已由Root批准。

下一仅设计待办：完整TFM deadline调度解决HoL、LLM冷载RAM/commit保护及最终资源review/真实短测；尚未放行实现。用户要求primary剩10%停止直至刷新；周额度忽略，不自动用reset credits。保留巨大dirty，不碰正式memory/真实设备模型/提交推送。恢复先读上述权威文件与AGENTS/skills，不运行废弃tmp/cursor_rvc_cpu_budget_gate_2026_11_04.py，不覆盖任何已有gate报告。

## 2026-11-04 RVC CPU 预算 — **fixture 收口 STOP（Root 最终复验两模块）**

- **生产**：Root 已独立 PASS 源码差异 + 6/13/66/45 四组；**完全冻结**
- **本轮仅测试**：`tests/test_rvc_worker_lifecycle.py`（`test_queued_speak…` 全责任 finally：持锁时 release → patch 内 join → 重抛 worker 异常）；`tests/test_security_round3a.py`（`test_voice_builders…` patch `_initialize_engine` 禁真实 pyttsx3；patch 内 finally `shutdown(stop_rvc=False)` + aggregate.complete + mgr quiet）
- **ADR**：`adr-rvc-cpu-budget-2026-11-04.md` 恢复 **Proposed**（未 Root 批准）；不美化 CPU/延迟结论
- **验证**：Cursor **未跑**任何 test/gate；局部 compile/diffcheck 已做；Root 自行跑 `test_rvc_worker_lifecycle` + `test_security_round3a`（或等价小组）最终复验
- **历史 label `rvc_cpu_cursor_20261104b`**：保留为中断前未完成记录，禁止删/覆盖/复用

## 2026-11-04 RVC 子进程 CPU 预算 — ~~源码返修轮~~（见上节 fixture 收口）

## 2026-11-04 非流播报 structured_dispatch 测试迁移 — **STOP 待 Root 复审**

- **仅测试**：`tests/test_structured_dispatch.py` + 删除 `_immediate_thread_factory`；**生产冻结**
- **Gate**：`legacy_playback_structured_dispatch_20261104` — `structured_dispatch` + `natural_companion_round1` **33 OK**，WAV 826 零差
- **Review / 交接**：`codex-review-legacy-playback-tests-final-2026-11-04.md`、`cursor-handoff-legacy-playback-structured-dispatch-2026-11-04.md`
- **STOP**：Root 独立复验；未提交推送

## 2026-11-04 非流回复有界播报 — **生产已 Root5/5 + 162/162 PASS（源码冻结）**

- 窄修 c gate `legacy_playback_bound_cursor_20261104c`：162 OK
- ADR：`adr-legacy-playback-bound-2026-11-04.md`；主交接：`cursor-handoff-legacy-playback-bound-2026-11-04.md`

## 2026-11-04 预取控制流保行为精简 — **STOP 待 Root 独立 review**

- **范围**：仅 `src/voice/tts/turn_stream_prefetch.py`、`src/voice/tts/engine.py`（无意义 `except RuntimeError: raise`）
- **净精简**：`turn_stream_prefetch.py` **700→668 行（−32）**；删 `_owner_start_attempted`、`_cleanup_blocked`、未用 `_producer_cancelled`；producer 内层 fail 归并外层
- **Gate**：`prefetch_simplify_cursor_20261104a` — 7 模块 **146 OK / ~10s**，WAV 826 零差 → `tmp/codex_gate_prefetch_simplify_cursor_20261104a_2026_11_02.json`
- **静态**：`compileall` 两文件；`git diff --check` 退出 0；快照 `tmp/prefetch-simplify-root-before-20261104/` 未覆盖
- **ADR**：`docs/architecture/adr-prefetch-simplify-2026-11-04.md`（Proposed）
- **交接**：`docs/cursor-handoff-prefetch-simplify-2026-11-04.md`
- **任务**：`docs/cursor-task-prefetch-simplify-2026-11-04-draft.md`
- **STOP**：Root19 矩阵 + 源码复审；未提交推送；不进性能切片

## 2026-11-04 语音 PEP562 导出去重 — **STOP 待 Root 独立 review**

- **范围**：`src/_lazy_exports.py` + 五个 `src/voice/**/__init__.py` 调用 `install_lazy_exports`；映射 / `__all__` / TYPE_CHECKING / 缓存与异常语义不变
- **净精简**：五份相同 `__getattr__`+`__dir__` 块（各 ~12 行）合并为 1  helper（36 行）；包 `__init__` 合计约 **净减 ~14 行**，重复块 **~55 行 → 1 处**
- **Gate**：`voice_lazy_dedup_20261104` — `tests.test_voice_lazy_exports` + `tests.test_assembly_lazy_defaults` → **17 OK / 14.703s**，WAV 826 零差 → `tmp/codex_gate_voice_lazy_dedup_20261104_2026_11_02.json`
- **静态**：`py -3.12 -m compileall` 六文件；`git diff --check` 退出 0
- **ADR**：`docs/architecture/adr-voice-lazy-dedup-2026-11-04.md`（Proposed）
- **任务**：`docs/cursor-task-voice-lazy-dedup-2026-11-04-draft.md`
- **STOP**：未提交推送；不进其它精简或性能切片

## 2026-11-04 默认 stream 跨句预取 — **STOP 待 Root fixture 最终 review（返修 h / 仅 test_turn_audio_prefetch）**

- **生产**：Root19 + related 138/182 全 PASS，`src` 冻结
- **Review 顶节**：`docs/codex-review-turn-audio-prefetch-2026-11-04.md` 三处 harness 微修（patch 内 shutdown、aggregate.complete、Core CAS 取消 + 全 wait 断言）
- **Gate label**：`turn_prefetch_fixture_harness_20261104u`（68 tests：`test_turn_audio_prefetch` + `test_stream_playback` + `test_voice_session` → `tmp/codex_gate_turn_prefetch_fixture_harness_20261104u_2026_11_02.json`）
- **ADR**：仍为 Proposed，**Accepted 由 Root 统一裁定**
- **STOP**

## 2026-11-04 默认 stream 跨句预取 — **STOP 待 Root19 + 相关 A/B 复跑（返修 g / 入口与 fixture 收口，历史）**

- **Root review**：`docs/codex-review-turn-audio-prefetch-2026-11-04.md` 顶节 5 项；Root18 已过，Root19 入口 1 FAIL + 相关 A/B 各 FAIL
- **返修 g（engine 窄修）**：`_prepare_turn_stream_entry(..., external_cancel=)` 在 clear 前/epoch 锁内读外部取消；`save_to_file` interrupt 路径保留「已取消」公开文案；legacy `raise_on_busy=False` 覆盖 pending/retained guard
- **返修 g（tests）**：`_assistant_tts_shutdown_shell` 补 `_turn_stream_session*`；harness `shutdown_quiet` 顺序；rollback 模块 patch `_detect_backend`；stage 测第二段 cloud 屏障
- **Gate label**：`turn_prefetch_root19_fixture_20261104t`（29 tests → `tmp/codex_gate_turn_prefetch_root19_fixture_20261104t_2026_11_02.json`）
- **STOP**：Root19 + relatedA/B 独立验收后再进下一板块

## 2026-11-04 默认 stream 跨句预取 — **STOP 待 Root18 复跑（返修 f / prepare 阶段 cancel，历史）**

- **Root review**：顶节窄修 — `stream_cancel` 须含 session 永久 Event；Root17 OK，Root18 stages 1 FAIL
- **返修 f**：`TurnStreamPrefetchSession.prepare_cancel_view()` → Edge `stream_cancel`、Cosy `cancel_event`/`interrupted`；播放失败 `_play_item` 即时 `_session_cancel.set()`；`test_play_fail_aborts_late_edge_mp3_to_wav_stage`
- **Gate label**：`turn_audio_prefetch_cursor_20261104s`（59 tests → `tmp/codex_gate_turn_audio_prefetch_cursor_20261104s_2026_11_02.json`）
- **STOP**：Root18 + 相关回归后再进下一板块

## 2026-11-04 默认 stream 跨句预取 — **STOP 待 Root17 复跑（返修 e，历史）**

- **Gate label**：`turn_audio_prefetch_cursor_20261104o`

## 2026-11-04 默认 stream 跨句预取 — **STOP 待 Root（初稿 a，历史）**

## 2026-11-04 音频准备事务回滚 — **fixture STOP（生产已 Root7 PASS）**

- **生产冻结**；仅 tests fixture：`pin_io_paths` + fake retire 未 quiet + `enterContext`  teardown 顺序
- **Gate `audio_preparation_rollback_cursor_20261104e`**：12 OK → `tmp/codex_gate_audio_preparation_rollback_cursor_20261104e_2026_11_02.json`
- **data/tmp +3**：未删，Root 回收

## 2026-11-04 音频准备事务回滚 — **STOP 待 Root（返修 c，历史）**

## 2026-11-04 OwnedThreadTask — **STOP 待 Root 独立复审（返修 e）**

- **评审顶节**：单一外层 `except` 覆盖 factory/commit/start/ack；fixture 统一 `_wait`/`_run_on_thread`/`_require_quiet`；Root10 丢 error 已修
- **源码**：`start()` 去掉内层 except；`except` 记录原异常 + 已 commit → unknown；`finally` 仅收 creator；不覆盖已有 `.error`
- **测试**：17 项（含 post-start KI/settrace）；creator/racer 主线程捕获异常；`len(results)==4`；finally 断言 quiet join
- **监督器（唯一 label `owned_thread_prefetch_foundation_20261104e`）**：`tmp/codex_gate_owned_thread_prefetch_foundation_20261104e_2026_11_02.json`

```json
{
  "exit_code": 0,
  "timed_out": false,
  "elapsed_sec": 0.516,
  "modules": [
    "tests.test_owned_thread"
  ],
  "probe": null,
  "wav_before": 826,
  "wav_after": 826,
  "added": [],
  "removed": [],
  "summary": [
    "Ran 17 tests in 0.118s",
    "OK"
  ]
}
```

- **未改**：App/TTS/queue 草案/Root 脚本；**禁止接线**
- **Root**：自行复跑 Root10；禁止 Cursor raw 对照 probe

---

## 历史（返修 c/d）

- c：`tmp/codex_gate_owned_thread_prefetch_foundation_20261104c_2026_11_02.json`；Root10 仍 1 FAIL（error 丢失）
- d：gate 因 fixture 双 `start` 失败（标签已占用，不作交付）

---

## 2026-11-04 TFM 过期出队 queued 顺序 — **STOP 待 Root 最终验收**

- **续审**：`docs/codex-review-temp-file-bounds-2026-11-04.md` 顶节 — 过期项不得在身份验证前 `discard` 同路径 `_queued_paths`（否则旧任务 A 会清掉新身份 B 的 queued 标记）
- **生产**：`_queued_paths.discard` 仅移入身份匹配 **else**；过期分支只 `_clear_pending_for_obsolete_queue_item_locked` + `task_done`；`finally` 的 inflight/claim 释放仅 **`processing_item`**
- **仓库**：`StaleQueueWorkerTests` 在 adopt 后断言 `path_key in _queued_paths`；obsolete hook 观测处理旧项时 queued 标记仍保留
- **监督器（label `*_20261104o`，≤120s，仅两项）**：
  - `tmp/codex_gate_tfm_stale_queued_discard_20261104o_2026_11_02.json`（Root stale 1 项）
  - `tmp/codex_gate_tfm_stale_queued_discard_repo_20261104o_2026_11_02.json`（27）
- **边界**：未改 Root/监督器/其它组；无提交/预取

---

## 2026-11-04 TFM 过期队列出队 — 历史（`20261104n` 证据仍有效，queued 顺序由 `o` 轮闭合）

- **任务**：`docs/cursor-task-security3a-offline-memory-fixture-2026-11-04.md`
- **改动（仅测试）**：`tests/test_security_round3a.py` 增加 `setUpModule` → `install_module_sufficient_host_memory()`（`tests/support/host_memory_fixtures.py` 的 `unittest.enterModuleContext` 有界 fixture，模块结束自动恢复；非 import 级永久 patch）
- **根因**：TFM 回归组中 `tests.test_security_round3a.RvcCapabilityGateTests.test_infer_subprocess_uses_dedicated_not_generic` 等在绑定 fake runner 前被**真实主机** `host_memory_commit_insufficient` 拒绝；`test_security_round3a_review_fixes` 已迁移，主模块漏迁
- **监督器（唯一 label、≤120s 前台顺序）**：`review_fixes` + `test_security_round3a` → **`tmp/codex_gate_sec3a_offline_memory_fixture_20261104_2026_11_02.json`**
- **验证 JSON（监督器 stdout，与磁盘一致）**：

```json
{
  "exit_code": 0,
  "timed_out": false,
  "elapsed_sec": 7.031,
  "modules": [
    "tests.test_security_round3a_review_fixes",
    "tests.test_security_round3a"
  ],
  "probe": null,
  "wav_before": 826,
  "wav_after": 826,
  "added": [],
  "removed": [],
  "summary": [
    "Ran 53 tests in 5.120s",
    "OK"
  ]
}
```

- **未改**：生产准入/断言/权限/Root 探针与监督器脚本；`data/memory` 与 Root `tmp/codex_persistent_rvc_boundary_2026_11_03.py`（Root 自行补构造域 Enough，非本任务授权）；无提交/推送/模型设备下载
- **遗留**：TFM pending 可见性轮 Root 探针 `pio_b` 仍可能因本机 commit 未跑通；与本 fixture 无关，Root 资源充足环境复跑

---

## 2026-11-04 TFM 失败删除 pending 可见性 — **STOP 待 Root 最终验收**

- **续审**：`docs/codex-review-temp-file-bounds-2026-11-04.md` 顶节「失败删除仍未计入 pending」
- **生产**（`src/voice/audio/temp_files.py`）：`_record_failed_delete_intent_locked`；`_perform_os_remove_outside_lock` 非 `OSError` 释 claim + 记 failed intent 后原样上抛；前台永久 `OSError` / worker 耗尽同路径记 intent；显式 `remove` 遇 `_retry_exhausted` 先 discard 再 claim；成功仍清 pending
- **仓库**：`tests/test_tfm_bounds.py` — `DeleteErrorReleaseTests` 断言 `pending_cleanup_count()==1`；新增 `test_permanent_os_error_keeps_pending_without_worker`（EIO）
- **监督器（唯一 label、前台顺序）**：
  - delete-errors 2 项 → `tmp/codex_gate_tfm_pending_visibility_errors_20261104k_2026_11_02.json`
  - 仓库 26 → `tmp/codex_gate_tfm_pending_visibility_repo_20261104k_2026_11_02.json`
  - Root9 → `tmp/codex_gate_tfm_pending_visibility_root9_20261104k_2026_11_02.json`
  - claims5 → `tmp/codex_gate_tfm_pending_visibility_claims5_20261104k_2026_11_02.json`
- **必要回归**：storage 45 OK → `tmp/codex_gate_tfm_pending_vis_regress_storage_20261104k_2026_11_02.json`；persistent 仓库模块 14 OK → `tmp/codex_gate_tfm_pending_vis_regress_pio_mod_20261104k_2026_11_02.json`；Cosy c1+c2b2a 65 OK → `tmp/codex_gate_tfm_pending_vis_regress_cosy_20261104k_2026_11_02.json`
- **本机未过（主机 commit 余量，非 TFM）**：Root `codex_persistent_rvc_boundary_2026_11_03.py` → `tmp/codex_gate_tfm_pending_vis_regress_pio_b_20261104k_2026_11_02.json`；`tests.test_security_round3a*` → `tmp/codex_gate_tfm_pending_vis_regress_sec3a_20261104k_2026_11_02.json`
- **闭合前 Root 报告**：`tmp/codex_gate_tfm_pending_error_visibility_root_20261104k_2026_11_02.json`（3 FAIL）
- **ADR**：`docs/architecture/adr-temp-file-bounds-2026-11-04.md`（仍为 Proposed）
- **边界**：未改 Root 探针/监督器；无提交/预取/真模型/正式 memory

---

## 2026-11-04 TFM 幽灵 pending — 历史（supersede 见上 pending 轮仍保留生产不变量）

- **续审**：`docs/codex-review-temp-file-bounds-2026-11-04.md` 顶节「幽灵 pending」
- **生产**：`_delete_task_matches_owned_locked`；`_note`/`_requeue`/`pump`/`enqueue` 丢弃无 owned 同身份任务，stop 不再重造孤儿 pending
- **Root bounds 9**（未改 Root 文件）：`tmp/codex_gate_tfm_ghost_pending_root9_20261104h_2026_11_02.json`
- **仓库 25 项**：`tmp/codex_gate_tfm_ghost_pending_repo_20261104h2_2026_11_02.json`
- **回归 h**：storage/io/cosy/sec3a → `tmp/codex_gate_tfm_ghost_pending_regress_*_20261104h_2026_11_02.json`
- **历史 diagnostic**：`tmp/codex_gate_tfm_replacement_root_diagnostic_20261104g_2026_11_02.json`（pending_cleanup=1）
- **Root 已通过（勿动）**：claims5/errors `tfm_final_root_claims/errors_20261104g`

---

## 2026-11-04 TFM 删除异常出口 — 历史（supersede）

- **delete-errors e 轮**：`tmp/codex_gate_tfm_delete_errors_*_20261104e*_2026_11_02.json`

---

## 2026-11-04 RVC 显式重试 — **测试 fixture Root 续审收口 STOP 待 Root**

- **Root**：38 定点 + 85 所有权均已 PASS（`tmp/codex_gate_rvc_retry_root_repo_final_20261104o_2026_11_02.json` 等）；生产无新问题
- **本轮仅测试**（对照 `docs/cursor-task-rvc-retry-fixture-final-2026-11-04.md` 顶节续审 2 项）：
  - `test_cancelled_ctor…` quietFalse：恢复 quiet 的 `stop_rvc_worker` 嵌套在 **global patch 仍持有 client** 内，记录 `stop_hit`/`cleanup_observed` 证明命中同 client、fence 仍为 True，再断言 global/key 清空
  - `test_foreground_explicit_retry_short_budget`：`request` 返回后立即捕获 job/thread；`slow_retry` BaseException→`retry_error` 主线程核对；成功路径置 `engine._rvc_ready` 并断言 `job.result.ok` 且非 pending；finally 在 patch 内 release→join→not alive（身份不符不 join 他人线程）
- **Cursor gate（单模块 ≤120s）**：`--label rvc_retry_fixture_final_20261104p` → **`tmp/codex_gate_rvc_retry_fixture_final_20261104p_2026_11_02.json`**
- **compileall** + **git diff --check**（测试文件）；无生产改动、无提交推送

---

## 2026-11-04 RVC 显式重试 — 测试 fixture 最终收口（n 轮，已由 p 轮 supersede）

- **Cursor gate**：`--label rvc_retry_fixture_final_20261104n` → `tmp/codex_gate_rvc_retry_fixture_final_20261104n_2026_11_02.json`

---

## 2026-11-04 RVC 显式重试（失败 sticky + allow_retry）— 历史 STOP（k 轮）

- **Root 生产验收（i）**：独立 **8+13+2 全 PASS** → `tmp/codex_gate_rvc_retry_final_root8_20261104i_2026_11_02.json`、同前缀 `…root13…` / `…root2…`（Cursor 未改探针）
- **本轮仅测试**：`tests/test_rvc_explicit_retry.py` 增至 **26** 项；自含 `_StickyFailedTts` / `_MinimalVoiceConfig` / `_real_rvc_worker_client` 等 factory，覆盖 atomic13 关键场景（退休 cancel→sticky/inflight、policy 替换 fence 前置、ctor cancel+quietFalse、fresh 仅一次、Cosy App ready、want_stt 续装配、busy/foreign 零 stop 等），**不 import tmp Root**
- **Cursor gate（单模块）**：`--label rvc_retry_explicit_repo_20261104k` → **26 OK** → `tmp/codex_gate_rvc_retry_explicit_repo_20261104k_2026_11_02.json`（WAV 826；`…20261104j` 为同内容首次 gate）
- **h 轮历史**：completion/atomic8/13/75/67 见 `tmp/codex_gate_rvc_retry_*_20261104h_*` 与 `explicit2_20261104h`
- **compileall** + **git diff --check** OK；无生产行为改动、无提交推送

---

## 2026-11-04 Edge+RVC 前台有界 — **STOP 待 Root review**

- **任务**：`docs/cursor-task-edge-rvc-foreground-2026-11-04-draft.md`（host-memory Root 408 PASS 后放行）
- **实现**：`RvcPrepareState` + `get_rvc_prepare_state` / `prepare_rvc` inflight+attempt_completed（`src/voice/tts/engine.py`）；`tts_foreground_bound_path` 扩 Edge+RVC；`bound_tts_models_ready` / lane bound prepare / assembly voice defer RVC（仅 `start_ollama_warmup`）；warmup 三值返回与 Nahida fallback 警告
- **ADR**：`docs/architecture/adr-edge-rvc-foreground-2026-11-04.md`
- **Cursor scoped gate**：`tmp/codex_scoped_test_gate_2026_11_02.py` `--label edge_rvc_foreground_20261104` → **144 OK**（skip2），WAV 826 零差；报告 **`tmp/codex_gate_edge_rvc_foreground_20261104_2026_11_02.json`**
- **未动**：声学/模型预算/Root 探针/正式 memory/系统设置；无失败重试/预取；未跑真模型设备

---

## 2026-11-04 主机内存/commit 准入 — **Root 408 PASS 闭环**

- **任务**：`docs/cursor-task-host-memory-admission-2026-11-04-draft.md`
- **Root复审返修**：`docs/codex-review-host-memory-admission-2026-11-04.md` 顶节 — 旧 RVC/Cosy fake 模块 `setUpModule` + `install_module_sufficient_host_memory()`（`enterModuleContext` 有界生命周期）；`test_host_memory_admission` 不模块级 patch；sentinel/`safe_subprocess_run`；三入口 low phys/commit/unobservable 参数化；slice_b wetext 测试用 pinned 布局临时目录对齐 `repo_root` API（断言不变）
- **实现**：`src/core/host_memory.py`（Win64 104B ABI）；RVC 构造/start、infer、Cosy poll 前检
- **ADR**：`docs/architecture/adr-host-memory-admission-2026-11-04.md`
- **Cursor gate（前台顺序，失败即停）**：
  1. `test_host_memory_admission` → **16 OK**（skip1）
  2. + `test_rvc_owned_worker` → **26 OK**（skip1）
  3. `test_rvc_persistent_io_lease` + `test_rvc_worker_lifecycle` + `test_rvc_owned_run` → **75 OK**
  4. `test_cosyvoice_production_slice_c2b2a` + `slice_b` + `slice_c1` → **162 OK**
- **未动**：Root 探针/原断言/权限/声学/正式 memory/LLM/系统设置；未 detach、未跑真模型

---

## 2026-11-04 RVC 启动预检（省重复 IMPORT_RVC）— **Root 闭环**

- **任务**：`docs/cursor-task-rvc-startup-preflight-2026-11-04-draft.md`（已放行）
- **初审收口**：`docs/codex-review-rvc-startup-preflight-2026-11-04.md` — 严格 bool；warm 前 `_rvc_ready=False`；**最后契约**：去掉 warm 周围 try/except，原异常向外传播（Root 117 PASS 后源码审查）
- **任务 / ADR**：`docs/cursor-task-rvc-startup-preflight-2026-11-04-draft.md`；`docs/architecture/adr-rvc-startup-preflight-2026-11-04.md`
- **测试**：`tests/test_rvc_startup_preflight.py`（10 项）
- **Root**：完整 scoped gate **117 tests PASS**
- **Cursor 定点 gate**：`test_rvc_startup_preflight` + `test_rvc_health_probe_budget` + `test_cosyvoice_production_slice_c2b2a` → **39 OK**；`compileall`；`git diff --check`
- **未动**：CUDA/显式重试/Root 探针/声学/budget/正式 memory

---

## 2026-11-04 RVC 本地基础资产快失败 — **Root 91 PASS**

- **任务**：`docs/cursor-task-rvc-local-assets-2026-11-04.md`
- **初审返修**：`docs/codex-review-rvc-local-assets-2026-11-04.md` 三项已一次修齐（metadata→稳定码、stat+S_ISREG、worker main ready:false / infer 齐全 fake / 子进程 torch 守卫等）
- **实现**：`src/voice/rvc/local_assets.py`；`scripts/rvc_worker.py` / `scripts/rvc_infer.py`（无 download，资产前检在 torch/VC 之前）
- **ADR**：`docs/architecture/adr-rvc-local-assets-2026-11-04.md`
- **测试**：`tests/test_rvc_local_assets.py`（18 项 fake + 入口合同）
- **Cursor scoped gate（一次跑完）**：`tests.test_rvc_local_assets` + `test_rvc_health_probe_budget` + `test_rvc_owned_run` + `test_rvc_owned_worker` + `test_rvc_worker_lifecycle` → **Ran 90 tests OK**（1 skipped  symlink）；`compileall`；`git diff --check`
- **未动**：真实权重/vendor/health/prepare/early Job/Safety/声学/Root 探针；**未声称**三模型共存已达标
- **Root**：同上 scoped gate 独立复核

---

## 2026-11-04 装配默认工厂按需导入 — **Root 75 PASS**

- **任务**：`docs/cursor-task-assembly-lazy-defaults-2026-11-04-draft.md`（Root 已放行）
- **前置**：五包 lazy 已 Root 46 通过 — **未重做**
- **实现**：`src/core/assembly.py` — `default_tts_engine_cls` / `default_stt_engine_cls`；去掉顶栏 TTS/STT import；PEP562 `assembly.TTSEngine`/`STTEngine`
- **ADR**：`docs/architecture/adr-assembly-lazy-defaults-2026-11-04.md`
- **测试**：`tests/test_assembly_lazy_defaults.py`（7 项，子进程 sentinel + 注入/factory）
- **Cursor 已跑**：`tests.test_assembly_lazy_defaults`、`tests.test_voice_lazy_exports`、`tests.test_assembly_rollback`、`tests.test_mode_assembly_round2`、`tests.test_app_voice_activation`、`tests.test_voice_foreground_bound`；`compileall src/core/assembly.py`
- **未声称**：三模型共存、文本启动全路径已最优
- **Root**：上述 offline gate；**不改** Root 探针

---

## 2026-11-04 语音包按需导出（PEP 562）— **Root 46 已通过**

- **返修**：`docs/codex-review-voice-lazy-exports-2026-11-04.md` 四项 — `__dir__`=globals∪`__all__`；`globals()` 缓存；全 `__all__` identity 表；并发/KBI/SystemExit/重试均**独立解释器**（删同进程删 `sys.modules`）
- **任务**：`docs/cursor-task-voice-lazy-exports-2026-11-04.md`
- **实现**：五个 `__init__.py` 固定 lazy 映射；**未改** assembly/Core/mmap
- **ADR**：`docs/architecture/adr-voice-lazy-exports-2026-11-04.md`
- **测试**：`tests/test_voice_lazy_exports.py`（10 项子进程）
- **Cursor 已跑**：`tests.test_voice_lazy_exports`、`tests.test_cosyvoice_production_slice_a`、`tests.test_voice_foreground_bound`；`compileall src/voice`；`git diff --check`
- **Root**：scoped gate `--modules tests.test_voice_lazy_exports`；**不改** Root 探针

---

## 2026-11-04 Windows 提交余量 + isolated mmap 控制（仅 tmp）— **STOP 待 Root**

- **返修**：`docs/codex-review-commit-mmap-control-2026-11-04.md` 四项 — sampler **stdlib-only**（`GetPerformanceInfo` 物理余量 + commit，去掉 `src.voice`）；`read_bounded_log_tail` **seek 有界读**；mmap fake 补 **流式/既有 options/11445 端点**；API 异常 → **None**
- **任务**：`docs/cursor-task-commit-mmap-control-2026-11-04.md`
- **实现**：`tmp/codex_windows_resource_sampler.py`、`tmp/codex_real_voice_supervision.py`、`tmp/codex_diag_ollama_mmap_helper_2026_11_04.py`、smoke flag 不变
- **ADR**：`docs/architecture/adr-commit-mmap-diagnostic-2026-11-04.md`
- **Fake/Root（本回合齐跑 exit 0）**：sampler fake **3** + mmap fake **5** + 监督 fake **24** + launcher fake **4** + Root launcher **3** + Root6 + `codex_sampler_import_root_2026_11_04.py`
- **未改**：生产 `src/`（OllamaClient 默认）、Policy、配置、Root 探针断言
- **下一步**：Root 复跑同上顺序；通过后 **唯一新 label** `--isolated-ollama --ollama-mmap-control` 对照（仍不声称 mmap 已验证生效）

---

## 2026-11-04 隔离 Ollama launcher / 监督器 env（仅 tmp）— **STOP 待 Root**

- **任务**：`docs/cursor-task-isolated-launcher-policy-2026-11-04.md`
- **实现**：`tmp/codex_isolated_ollama_launcher_2026_11_03.py`（env 校验 → **`worker_entry_join_owned_job`** → serve 无 `env=`）；`tmp/codex_real_voice_supervision.py`（`run_error`、未 spawn 时 summary 非 0）
- **ADR**：`docs/architecture/adr-isolated-ollama-launcher-policy-2026-11-04.md`
- **Fake**：launcher fake **4** + 监督 fake **18** + Root launcher **3** + Root6
- **未改**：`src/core/safety.py`、生产 Core、SDK/关闭协议；**未跑**真实 Ollama/模型/用户服务
- **下一步**：Root 复跑 fake16 + Root6 +（可选）六模块 gate；通过后唯一 label 三模型复测

---

## 2026-11-03 Cosy worker 本地 Modelscope 导入边界 — **STOP 待 Root 短真实对照**

- **背景**：两模型 prepare-only 真实 FAIL（Cosy 30.141s 超时）；见 `docs/codex-review-coexist-control-2026-11-03.md` 顶节
- **返修**：按 `docs/codex-review-cosy-local-import-startup-2026-11-03.md` 六项 + Root **10** 项探针（path_trust 固定 wetext、foreign fail-closed/KBI 不掩盖、owner-thread、每调用复核 alias、ModuleSpec、删裸 subprocess gate）
- **实现**：`src/voice/cosyvoice/local_modelscope_import.py`；`scripts/cosyvoice_runtime_worker.py`（`repo_root` + 可选 `wetext_dir`）
- **ADR**：`docs/architecture/adr-cosyvoice-local-modelscope-import.md`
- **已验证（本切片）**：Root `tmp/codex_local_import_root_boundaries.py` **10/10**；`tests/test_cosyvoice_local_modelscope_import.py` **12 OK + 1 skip**
- **测试迁移（仅 fixture+ADR）**：C2a `test_edge_declared_status_unknown_defers_tfm_until_complete` 替换旧 Edge None/TFM 期望；对齐 ADR-rvc-retirement 决策 3；**未改 Core/SDK**
- **未在本切片修 Core**：138 回归其余项 Root 已绿；Edge 差异仅上述测试迁移
- **120s gate（显式 modules）**：`tests.test_cosyvoice_local_modelscope_import` + `c1` / `c2a` / `c2b1` / `c2b2a` / `cpu_checkpoint_staging` — **Root 短真实前由 Root 复跑**；本回合仅 fake+Root10
- **未改**：vendor/site-packages、默认 30s、声学、Native 核心；**未跑**真实模型/设备/main/正式 memory
- **下一步**：Root 复跑 120s 模块 gate + 短真实 `--coordinated --no-ollama --prepare-only` 对照

---

## 2026-11-03 两模型受控对照路由（--no-ollama）— **PASS（路由）；真实加载仍 FAIL**

- **任务**：`docs/cursor-task-two-model-control-route-2026-11-03.md`；监督器已 PASS（`docs/codex-review-real-diagnostic-supervision-2026-11-03.md` 顶节）
- **路由**：`coordinated && --no-ollama` → `prepare_two_model_control`（`prepare_rvc` → `prepare_local_tts`），**不**走 App/Ollama 协调器、不构造 `OllamaClient`
- **输出**：报告 `diagnostic_route=two-model-control`、`folder=cosyvoice-production-two-model-control`；三模型 `--coordinated` 原路径与 aggregate shutdown 不变
- **Helper**：`tmp/codex_real_voice_supervision.py`（`coordinated_smoke_route` / `prepare_two_model_control`）
- **Fake**：`tmp/codex_two_model_control_route_fake_2026_11_03.py`（8 项：含 KBI/SystemExit 传播）；Root6 + tmp16 保持
- **未改 src/**；本轮无真实模型/设备/用户服务；Root 验收后才跑短真实 two-model `--prepare-only`

---

## 2026-11-03 真实诊断监督器（入口一致性）— **PASS（监督切片关闭）**

- **顶节**：`docs/codex-review-real-diagnostic-supervision-2026-11-03.md` — 删 helper 重复 `run_real_voice_gate`/main；GPU 预检必须注入 SafePolicy `tree_sample_fn`；gate 显式传入；stderr tail 有界读
- **库**：`tmp/codex_real_voice_supervision.py` 仅 import-safe；唯一 CLI `tmp/codex_app_real_voice_gate_2026_11_03.py`
- **Nested Native Job 三 PID**：Root 已通过 `tmp/codex_nested_job_root_2026_11_03.py`（0.578s），本轮不重复 Native
- **验证**：Root6 + tmp fake15 由 Root 独立复跑；本轮补 fake `test_gpu_preflight_requires_safe_tree_sample_fn`
- **未改 src/**；真实模型/用户服务禁止；有界预取未放行

---

## 2026-11-03 真实诊断监督器（poll/句柄收口）— **已被上节 supersede**

## 2026-11-03 真实诊断监督器（tmp gate 返修）— **已被上节 supersede**

## 2026-11-03 真实诊断监督器（tmp gate 收口）— **已被上节 supersede**

## 2026-11-03 本地语音前台有界 pending（Cosy 单 Job 加载线程）— **STOP 待 Root 验收**

- **评审返修（四续）**：`join_foreground_loader_bounded` 与 job 静止谓词一致；unknown bootstrap 且 `is_alive=False` 时 **return False**（不长等未确认 start）
- **Root**：`tmp/codex_foreground_interrupt_2026_11_03.py` 四项 + 原 5 边界 + 七模块监督（各一次，`-X utf8`）
- **ADR**：`docs/architecture/adr-voice-foreground-bound.md`

---

## 2026-11-03 真实入口 RVC health probe 预算 — **STOP 待 Root 短真实验证**

- **评审**：`docs/codex-review-real-voice-probe-budget-2026-11-03.md`
- **定点**：`health_check._RVC_PROBE_TIMEOUT_SEC=30`（对齐 `run_rvc_probe` cap）；`tests/test_rvc_health_probe_budget.py`（真实 Policy + fake Popen/owned execute，不 patch probe 返回值）
- **监督（一次）**：`tmp/codex_rvc_health_probe_budget_supervisor_2026_11_03.py` 45s 三模块
- **未改**：Policy cap、App、core owned 协议、提交/真实模型

---

## 2026-11-03 持久 RVC IO lease（完整 public 静止谓词）——Root PASS

- **lease 失败仍 stop True**：`_proc is None` 分支误用窄 public；已扩展 **`_public_stop_succeeded`**（proc/start/pin/convert/readers/stdin/token/release_failed/`_convert_io_blocks_new_pin` fail-closed，仅观察不等待）
- **测试**：raw 已清 + release 失败 → stop False/global 保留 → retry True（client + infer 两例）
- **监督（各一次）**：`persistent_targeted` 35s **14 OK**；两 Root probe **exit 0**
- **ADR**：`docs/architecture/adr-rvc-persistent-io-lease.md`

---

## 2026-11-03 单次 RVC active 票据 + retire 独占（Root PASS，持久切片前置）

- **修复**：`execute` 仅 `reserved_gen` 持有者在 finally 调 `_end_execute(gen)`（被拒 reserve 不再清零他人 `_run_inflight`）；`public retire` pending 清理路径短锁设 `_retiring`+finally 清除
- **验证**（各一次，未跑旧 408 gate）：`codex_rvc_three_request_retire_gate_2026_11_03.py` 5/5 + reserve gate + final3/cancel2/contract7/status4/admission + `tests.test_rvc_owned_run` **16 OK**

---

## 2026-11-03 单次 RVC owned-run begin/reserve 空窗口收尾（已被上节 supersede）

- **修复**：`_ensure_previous_reclaimed` 原子发布 `_run_inflight`+starting record（移除独立 `_begin_execute`）；`retire` 在 `_run_inflight` 且无 record 时返回 False；移除不可达 `_retire_stale_outside_lock`（前次未静止须显式 `retire`）；取消路径禁止 `CompletedProcess` 成功；`owned.kill` 均在锁外
- **验证**（Cursor，各一次，**未重跑** 408 旧 gate）：`codex_rvc_reserve_retire_gate_2026_11_03.py` 4/4 + final3 + cancel2 + contract7 + status4 + admission + `tests.test_rvc_owned_run` **13 OK**
- **说明**：admission probe 仍 hook `_ensure_previous_reclaimed`（行为不变）；Root 随后统一扩展 gate

---

## 2026-11-03 单次 RVC owned-run cancel/start_failure 定点（已被上节 supersede）

- **Root 探针**（未改）：`tmp/codex_rvc_cancel_start_failure_2026_11_03.py` 曾 2/2 false → 定点修 `retire` 活跃取消+kill、晚返 owner 拒成功、capturer 仅 join 已 start（`ident`）
- **测试**：`tests.test_rvc_owned_run` **12 OK**（保留 busy/partial/lease/infer/非法 timeout；增 second-start、active retire kill、lease query fail-closed；**runpy/exec** bootstrap 非源码断言）；`tmp/cursor_rvc_owned_job_gate.py` 模块表含 `tests.test_rvc_owned_run`
- **验证**（Cursor，各一次）：cancel 2/2 + final boundaries 3/3 + contract 7/7 + admission 2/2 + status 4/4 + 扩展 gate **420 OK**（skip 1），~92s，826→826
- **未改**：Root 探针、`current-delivery-status`、提交/推送/下一切片

---

## 2026-11-03 单次 RVC owned-run 最终收口（已被上节 supersede）

- **任务**：统一 active/creator/pending/retire 状态协议（非叠补丁）；Root 曾报 partial handoff 丢 owner 窗口与 errors-only 语义错误，**不采信**旧 9 项单元测试结论
- **实现**：`_run_inflight` + 活跃 `PermissionError`；`_adopt_pending_owner` 先于 creator ack；`retire()` run/creator 在途 incomplete；capture 真实 `join`+`is_alive`；kwargs 与 Py3.12 text_mode（`errors` 隐含 text）；修复 `policy_rvc_runs_quiescent` 属性名
- **验证**（Cursor，Py3.12 win32，各 **一次**）：
  - `tmp/codex_rvc_final_boundaries_2026_11_03.py` → **3/3 true**
  - `tmp/codex_rvc_oneshot_contract_probe_2026_11_02.py` → **7/7 true**
  - `tmp/codex_rvc_oneshot_admission_probe_2026_11_02.py` → **2/2 true**（第二路可能 `_begin_execute` 拒配，spawn≤1）
  - `tmp/codex_shutdown_status_probe_2026_11_02.py` → **4/4 true**
  - `tests.test_rvc_owned_run` → **8 OK**（含 partial handoff、errors→str、runpy 顺序）
  - `tmp/cursor_rvc_owned_job_gate.py` → **408 OK**（skip 1），~97s，`MIKU_SET_CHANGED=0`（826→826）
- **未改**：Root 探针、`docs/current-delivery-status-*`、持久 reader/Native Job、data/memory/提交/推送/下一切片
- **ADR**：`docs/architecture/adr-rvc-oneshot-owned-run.md`

---

## 2026-11-02 单次 RVC owned-run 准入返修（评审第二次，已被上节 supersede）

- **修复**：`_ensure_previous_reclaimed` 改为短锁 compare-and-reserve（与 record 发布一体）；retire 用 `_retiring` 互斥；creator 阶段 `finally` ack（spawn/reader 失败可 quiescent）；`errors` 不再隐含 text、`lookup_error` 预检、retire timeout 有限、capture 任意读异常失败、lease 查询异常 fail-closed
- **验证**（Cursor，各一次）：`codex_rvc_oneshot_admission_probe` 2/2；旧 contract 7/7；`codex_shutdown_status_probe` 4/4；`tests.test_rvc_owned_run` 9 OK；`cursor_rvc_owned_job_gate` 408 OK
- **IO lease / 持久 convert 未知**：同前（infer 已覆盖，worker 未接入）

---

## 2026-11-02 单次 RVC owned-run 返修（评审第一次，已被上节 supersede）

- **范围**：恢复 `SafetyPolicy._require_sealed_local_tts_python`；重写 `owned_rvc_run` 生命周期（starting ack、无 finally unpin、partial spawn 接管、父环境+Job env、capture EOF 等待、encoding→text）；`TtsShutdownStatus.rvc_subprocess_complete` / `TempFileShutdownStatus.active_io_leases` 移末尾默认值；TFM `pin_io_paths` 单锁原子；Engine 仅 `stop_rvc=True` 时 policy retire；App `retire_rvc_runs` 经 `_run_step` 且 cleaner/character 独立继续。ADR：`docs/architecture/adr-rvc-oneshot-owned-run.md`。**未改** 持久 reader/Native Job、声学/默认/预热/candidate/data/memory/提交
- **IO lease**：单次 infer 输入/输出 **已覆盖**；持久 convert 取消未知 **未接入**
- **验证**（Cursor，Py3.12 win32，各一次）：
  - `tmp/codex_rvc_oneshot_contract_probe_2026_11_02.py` → **7/7 true**
  - `tests.test_rvc_owned_run` → **6 OK**
  - `tmp/cursor_rvc_owned_job_gate.py` → **408 OK**（skip 1），~92s，825→825
  - `tests.test_storage_bounds` + `tests.test_storage_round2_review_fixes` → **45 OK**
- **遗留**：Root 无模型 Py311 树、真实共存；Core 大板块/预热/预取仍禁止

---

## 2026-11-02 单次 RVC infer/probe owned run（首版，已被上节 supersede）

- **范围**：契约迁至 `src/core/shutdown_status.py`（stdlib only）；移除生产 `unittest.mock` 特判；`inspect.getattr_static(engine, name, sentinel)` 检测聚合/Cosy getter（含 instance 声明、noncallable 拒绝）；`verify` 两阶段 bool 校验。Engine re-export 兼容；App 从 core 取 helper。**未改** reader/Job/默认/预热/candidate/data/memory/提交
- **验证**（主 Python 3.12，各 **一次**）：
  - `TTSShutdownTests` + `TtsShutdownPropagationTests` + `ShutdownStatusProtocolTests` → **21 OK**
  - `tmp/codex_shutdown_status_probe_2026_11_02.py` → 四项全 `true`
  - `tmp/cursor_rvc_owned_job_gate.py` → **408 OK**（skip 1），~88s，`MIKU_SET_CHANGED=0`（825→825）
- **未做**：下一切片、重复求绿

---

## 2026-11-02 RVC 关闭结果传播返修（`docs/codex-review-rvc-retirement-propagation-2026-11-02.md` 中段）——**已被上节 supersede，保留记录**

- **范围**：Engine `stop_rvc_worker() is True` / Cosy·自有 TFM `complete is True` / `_publish_shutdown_status` 仅 `is True`；App `engine_declares_aggregate_shutdown_status` + `verify_aggregate_shutdown_status`（五字段 bool 且全 True），**不**绑定具体 `TTSEngine` 类型；`MagicMock` 动态属性不算 API；显式 Adapter 必须 respected。门禁 `tmp/cursor_rvc_owned_job_gate.py` 补 `tests.test_assistant_orchestration`。**未改** reader/Job/默认/预热/candidate/data/memory/提交
- **验证**（主 Python 3.12，各 **一次**）：
  - `TTSShutdownTests` + `TtsShutdownPropagationTests` → **15 OK**
  - `tmp/codex_shutdown_status_probe_2026_11_02.py` → 四项全 `true`
  - `tmp/cursor_rvc_owned_job_gate.py`（含 `test_assistant_orchestration`）→ **402 OK**（skip 1），~86s，`MIKU_SET_CHANGED=0`（825→825）
- **诚实边界**：`_voice_loading` 与 candidate 协议未变；单次 infer/probe 仍未覆盖
- **未做**：下一切片、重复求绿

---

## 2026-11-02 RVC 关闭结果传播（`docs/cursor-task-rvc-retirement-propagation-2026-11-02.md`）——**已被上节返修 supersede，保留记录**

- **范围**（**未改** 持久 worker reader/Job/默认/声学）：`TTSEngine` 新增冻结 `TtsShutdownStatus` + `get_shutdown_status()`；`shutdown()` 严格采集 `stop_rvc_worker()`、`speak_lock`/Cosy 在途、`stop_rvc=False` 共享边界、自有 TFM 全写入者就绪后才关闭；`MikuAssistant` 对真实 `TTSEngine` 验证聚合 `complete`，失败保留 `tts_engine`/共享 TFM 可重试；共享 TFM 前检查 `_voice_loading`。文档：`docs/architecture/adr-rvc-retirement-propagation.md`。**未做** 预热/预取、单次 infer/probe、C2b2b candidate 协议、data/memory/提交/推送
- **验证**（主 Python 3.12，各 **一次**）：
  - `tests.test_rvc_worker_lifecycle.TTSShutdownTests` 新增 5 项 + `tests.test_assistant_orchestration.TtsShutdownPropagationTests` 3 项 → **OK**
  - `tmp/cursor_rvc_owned_job_gate.py` → **375 OK**（skip 1），~88s，`MIKU_SET_CHANGED=0`（825→825）
- **诚实边界**：`_voice_loading` guard 仅覆盖已发布 loader 在途；未发布 candidate 持久所有权完整协议仍在 C2b2b draft，**未**接入，不能宣称候选预热已闭环
- **未做**：下一切片、重复门禁求绿、Root 沙箱外 gate 代跑

---

## 2026-11-02 RVC 能力测试 Windows 路径断言（`docs/codex-review-rvc-owned-worker-2026-11-02.md` 独立验证顶节）——**停止待 Codex 沙箱外 12 模块 gate**

- **范围**（**仅测试**，无生产改动）：`test_spawn_applies_job_env_and_preserves_parent_environ` 与 `test_legal_worker_infer_probe_reach_fake_runners` 对 `argv[0]` 使用 `Path(...).resolve()` 平台语义相等，`argv[1:]` 仍精确相等（worker/infer/probe）；后者 worker spawn 改为 `bind_fake_rvc_runners` + `FakeOwnedWorkerJobBackend` 显式 fake Job。**未改** asyncio/生产、identity 规则、reader/预算
- **验证**（主 Python 3.12，各 **一次**）：
  - `tests.test_rvc_owned_worker.RvcOwnedSpawnTests.test_spawn_applies_job_env_and_preserves_parent_environ` → **OK**
  - `tests.test_security_round3a.RvcCapabilityArgvTests.test_legal_worker_infer_probe_reach_fake_runners` → **OK**
- **说明**：Codex 核心 12 项 + 真实 Native 3 项已通过；AppContainer 下 `F16SpeakCleanupTests` asyncio socketpair 挂起为沙箱限制，**未**在本任务修补。全 **120s** 12 模块 union gate 由 **Root 在沙箱外**执行，Cursor **未**重跑全门禁
- **未做**：下一切片、data/memory、提交/推送

---

## 2026-11-02 RVC owned worker 安装在途完成状态（`docs/codex-review-rvc-owned-worker-2026-11-02.md` 第三版顶节）——**核心已 PASS，保留记录**

- **范围**（仅 `src/voice/rvc/worker.py` 三处最小差，**未改** reader/启动预算）：`_try_release_owner` 在 `_start_inflight` 时不关管道、不清 owner、不标完成；`_start_locked` 的 `finally` 先清 `start_inflight`，失败路径在锁外 `_retry_release_after_startup`（`KeyboardInterrupt`/`SystemExit` pending owner 跳过）；`_start_stderr_drain` 在 `Thread.start` 前登记 `_stderr_thread`。**未改** root 探针、Native Job、infer/probe、data/memory/默认、提交/推送
- **验证**（主 Python 3.12，`tmp/codex_scoped_test_gate_2026_11_02.py --timeout 20` 各 **一次**）：
  - `--probe codex_rvc_owner_probe_2026_11_02.py` → 五项全 `true`（~7.2s）
  - `--probe codex_rvc_startup_boundaries_probe_2026_11_02.py` → 三项全 `true`（~5.5s）
  - `--probe codex_rvc_install_stop_probe_2026_11_02.py` → 四项全 `true`（~5.5s）
  - `tests.test_rvc_owned_worker` + `WorkerPipeTests` → **26 OK**
  - `tmp/cursor_rvc_owned_job_gate.py` → **370 OK**（skip 1），825 WAV 零差
- **未做**：真实 `.venv-rvc` Job 树、模型/设备、下一切片、重复探针求绿、提交/推送

---

## 2026-11-02 RVC owned worker 启动边界返修（`docs/codex-review-rvc-owned-worker-2026-11-02.md` 第二版顶节）——**已被第三版 supersede，保留记录**

- **范围**：按顶节简化协议修 `src/voice/rvc/worker.py`——删除 `_force_put` / `bootstrap_timeout=1` / `_attach_stdout_reader` 首消息特判；Popen 返回后先在 `_spawn_lock` 接管 `_proc`，再登记 `_reader` 后 `Thread.start`；EOF 仅设 `_eof`，`poll_message` 50ms 循环先消费队列；`_stdin_writes_inflight` 与 write 准入同锁；`_retiring` 阻止 stop 期间新 write/start；busy `stop`（lifecycle 锁失败）返回 **False**；`_publish_ready` 检查 `stop_inflight`/`retiring`。**未改** root 探针脚本、Native Job 核心、infer/probe、data/memory/默认、提交/推送
- **验证**（主 Python 3.12，本轮各 **一次**，PowerShell `WaitForExit` + 超时 `taskkill /T`）：
  - `tmp/codex_rvc_owner_probe_2026_11_02.py` → 五项全 `true`（~9s）
  - `tmp/codex_rvc_startup_boundaries_probe_2026_11_02.py` → 三项全 `true`（~9s）
  - `tests.test_rvc_owned_worker` + `WorkerPipeTests` → **26 OK**（~14s）
  - `tmp/cursor_rvc_owned_job_gate.py` → **370 OK**（skip 1），~79s，`MIKU_SET_CHANGED=0`（825→825）
- **说明**：上述为 Cursor 离线 fake/门禁复验；**不等同** Codex 独立真实 `.venv-rvc` Job 树验收。第二版 370 报告已作废，本轮为新实现后的单次联合门禁。
- **未做**：真实模型/设备、infer/probe 切片、重复探针/门禁求绿、提交/推送

---

## 2026-11-02 RVC owned worker 生命周期协议闭合（`docs/codex-review-rvc-owned-worker-2026-11-02.md` 中段）——**已被顶节返修 supersede，保留记录**

- **范围**：持续 stdout reader（队列容量 1、多消息、EOF 唤醒）；`_OwnerBinding` 快照回收；`RLock` 修复 spawn 校验自死锁；`start_inflight` 时 `stop` 立即 `False`；idle owned 跳过 stdin shutdown；`_stdin_writes_inflight` 阻止 active write 时 close；warmup/convert 取消后拒绝迟到响应；reader/stderr **daemon** 兜底且仍以 join 证明退出。**未改** Native Job 核心、infer/probe、声学/默认超时、data/memory、提交/推送
- **验证**（主 Python 3.12，**第二版已被 Codex REJECTED**）：
  - `tmp/codex_rvc_owner_probe_2026_11_02.py` → 五项全 `true`（当时）
  - `tmp/cursor_rvc_owned_job_gate.py` 一次 → **370 OK**（已作废）
- **未做**：`.venv-rvc` 无模型真实进程树、真实 RVC 音频、infer/probe Job、重复门禁求绿

---

## 2026-11-02 RVC persistent worker owned Job 切片（`docs/cursor-task-rvc-owned-worker-2026-10-18.md` + 11/2 补充）——**已由上一节 supersede，保留记录**

- **范围**：Windows `spawn_rvc_worker` 复用 `spawn_owned_voice_worker`；`scripts/rvc_worker.py` 早期 Job join + 延迟 `vc_params`；`RVCWorkerClient` 单例 stdout reader、reader/stderr 有界 join 后关管道、idle owned stop 跳过阻塞 shutdown 写、`_owner_generation`/`_start_inflight` start-stop 竞态、`take_owned_worker_from_exception`。**未做** infer/probe Job 归属、预热/预取、真实模型/设备/data/memory、提交/推送
- **文档**：`docs/architecture/adr-rvc-windows-owned-job.md`；门禁 `tmp/cursor_rvc_owned_job_gate.py`
- **验证**（主 Python 3.12）：
  - 专项 fake：`tests.test_rvc_owned_worker` + `tests.test_rvc_worker_lifecycle.WorkerPipeTests` + `test_security_round3a.test_legal_worker_infer_probe_reach_fake_runners` → **24 OK**
  - `tmp/cursor_rvc_owned_job_gate.py` 一次 → **221 OK**（skip 1），~53s，`MIKU_SET_CHANGED=0`（825→825）
- **未做**：root `.venv-rvc` 无模型 Job 树探针、真实 RVC 音频、单次 infer/probe containment、重复门禁求绿

---

## 2026-11-02 slice_a Windows 路径断言可移植性（`docs/codex-review-blocked-write-fixture-2026-11-02.md` 顶节）——**停止待 Codex 复审**

- **范围**：仅 `tests/test_cosyvoice_production_slice_a.py` 中 `test_explicit_permission_reaches_fake_popen` 的 `argv[0]`/`cwd` 完整路径比较改为 `Path(...).resolve()` 平台语义相等；**未改**生产、`identities_match`、blocked-write 收口、未提交/推送
- **验证**（主 Python 3.12）：
  - `tests.test_cosyvoice_production_slice_a.LocalTtsWorkerCapabilityTests.test_explicit_permission_reaches_fake_popen` → **1 OK**
  - `tmp/cursor_owned_job_validation_gate.py` 一次 → **294 OK**（skip 1），`MIKU_SET_CHANGED=0`（825→825）
- **未做**：其它模块、重复门禁

---

## 2026-11-02 blocked-write `worker_exit_gate` 调度收口（`docs/codex-review-blocked-write-fixture-2026-11-02.md` 顶节）——**blocked-write 已 PASS；本节后接 slice_a 路径项**

- **范围**：专用 `_BlockedWriteWorkerProcess.worker_exit_gate` + `exit_waiting`；`BlockedWriteTeardown.worker_exit_gate` 在回收最先 set；仅 `test_shutdown_during_blocked_write` 接线。**未改**生产、两个 root 探针、data/memory、未提交/推送
- **行为**：shutdown 快照时 worker 在 `_run` finally 内被 gate 阻塞 → 断言 `complete=False`、`poll() is None`、`exit_waiting`；不固定 shutdown 时刻 `result==[]`；join 后 `result=[False]`，释放 stdin/worker gate 再有界 `continue_shutdown` 至 complete
- **验证**（主 Python 3.12）：
  - `tmp/codex_blocked_teardown_probe.py` → 三项全 `true`
  - `tmp/codex_blocked_write_schedule_probe.py` → `test_passed=true`（`request_ended_before_status` 可为调度差异，须 `complete=False` 路径稳定）
  - 专项 7 用例 → **7 OK**，~3.0s
  - 八模块 gate 一次 → **294 OK**（skip 1），`MIKU_SET_CHANGED=0`（825→825）
- **未做**：下一切片、重复门禁求绿

---

## 2026-11-02 blocked-write fixture Codex 返修（`docs/codex-review-blocked-write-fixture-2026-11-02.md`）——**已由 worker_exit_gate 节 supersede，保留记录**

- **范围**：闭合 root 探针三项 false-pass + 线程异常捕获 + shutdown 受控屏障；仅 `tests/support/cosyvoice_blocked_write.py`、slice_b 相邻 blocked-write/teardown 用例。**未改**生产、`tmp/codex_blocked_teardown_probe.py`、Job 其它探针、data/memory、未提交/推送
- **返修要点**：
  1. `thread_errors` 在 synth/stop 有界 join **之后** `_merge_thread_errors`（按 `id` 去重），不在 teardown 开头快照
  2. 各回收步骤独立 try/continue；**最终** `continue_shutdown` 仍 incomplete 则 `RuntimeError`，中间 incomplete 允许
  3. 多异常用 `BaseExceptionGroup` 聚合（非 `ExceptionGroup`）；新增 3 个 fault-injection 单测 + `thread_body_capture` 接入四个 blocked-write 线程
  4. `test_shutdown_during_blocked_write`：shutdown 前断言 `release`/worker/synth 仍存活屏障；shutdown 后 `complete=False` 且 synth 无结果，释放后再 `continue_shutdown` 至 complete
- **验证**（`C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe`）：
  - `tmp/codex_blocked_teardown_probe.py` → 三项全 `true`
  - 专项 7 用例（4 blocked-write + 3 teardown 可靠性）→ **7 OK**，~0.5s
  - `tmp/cursor_owned_job_validation_gate.py` 一次 → **294 OK**（skip 1），~39s，`MIKU_SET_CHANGED=0`（825→825）
- **未做**：下一功能切片、RVC/预热、重复门禁求绿

---

## 2026-11-02 blocked-write 测试 fixture 收口（`docs/cursor-task-fixture-resume-2026-11-02.md`）——**已由上一节 Codex 返修 supersede，保留记录**

- **范围**：仅 `tests/support/cosyvoice_blocked_write.py`、slice_b 中 `_BlockedWriteWorkerProcess` 与四个 blocked-write/ rendezvous 用例；门禁 `tmp/cursor_owned_job_validation_gate.py` 改为显式诊断 `SafetyPolicy` + `safe_subprocess_*`（超时后仅对门禁 root PID `taskkill /T /F`）。**未改**生产 `src/scripts/config`、Job root 探针、`tmp/codex_*`、data/memory、未提交/推送
- **线程所有权（fixture）**：
  - 主测试线程：断言、释放 `ready_gate`/`stdin.release`、在 `finally` 调用 `BlockedWriteTeardown.run()`
  - `synth_thread`：后台 `client.synthesize`；teardown 有界 join 并报告仍 alive
  - `stop_thread`（rendezvous 用例）：在 write 进入后 `shutdown`；异常收集到 `thread_errors` 回主线程断言，不在后台 traceback 代替失败
  - fake worker `_thread`：`_BlockedWriteWorkerProcess._run` 的 `finally` 显式 stdout EOF；`wait(timeout)` 先 join 线程再 `poll`，不沿用 base fake 的立即 `TimeoutExpired`
  - `BlockedWriteTeardown`：逐步释放 gate/stdin、shutdown/continue_shutdown、join synth/stop、`proc.stop`；任一步失败聚合为 `ExceptionGroup` 抛出，不静默吞异常
- **验证**（`C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe`）：
  - 专项四用例 `CosyVoiceClientStopBudgetTests.test_*_blocked_write*` + `test_stop_phase_waits_for_blocked_write_rendezvous` → **4 OK**，~0.6s
  - `tmp/cursor_owned_job_validation_gate.py` 一次 → **291 OK**（skip 1），~34s，`MIKU_SET_CHANGED=0`（825→825）
  - `compileall`（上述改动文件）→ OK；`git diff --check` → OK
- **未做**：RVC/预热/预取、真实模型/设备、提交/推送、重复门禁求绿

---


- **范围**：仅闭合 transition probe 已证的两处异常路径；**不改**正常 spawn/Native 路径、**未改** root 四组 probe；**未**跑真实模型/设备/data/memory、未提交/推送
- **实现**（`src/core/owned_worker_job.py`）：
  - `spawn` 清理：`orphan_handle` 重绑 `RuntimeError` 且 owner 已持有同句柄时只清局部标记，不 `CloseHandle` 仍由 owner 持有的句柄
  - `_bounded_spawn_cleanup`：最终 `_is_spawn_quiescent()` 抛错视为 unknown（返回 `False`），不替换原 spawn 异常；仍按既有语义附 `_miku_owned_worker_process`
- **验证**（`C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe`）：
  - `tmp/codex_owned_job_contract_probe.py`、`partial_spawn`、`remaining_paths`、`transition_probe` → 全通过
  - `tmp/cursor_owned_job_validation_gate.py` → **290 OK**（skip 1），~52s，`MIKU_SET_CHANGED=0`（824→824）
  - `python -m compileall -q src/core/owned_worker_job.py tests/test_owned_worker_job.py` → OK
- **说明**：Root 独立复审已确认真实 Native 正常路径（三项探针）；本交付仅 fake 回归 + 上述 diff
- **未做**：提交/推送、下一功能切片

---

## 2026-10-18 Windows 本地 TTS owned Job — f25ca603 剩余三条协议分支（`docs/codex-review-local-tts-windows-job-2026-10-18.md` 末尾补充）——**停止待 Codex 复审**

- **范围**：仅闭合评审已声明的三项（`PermissionError` 先接管 owner、无 redirector 的 `poll`/`shutdown`、pre-bind 孤儿 Job 句柄交接）；**未改** root probe；**未**跑真实模型/设备/data/memory、未提交/推送
- **实现**：
  - `CosyVoiceWorkerClient._start_worker_poll`：统一 `BaseException` 入口先 `take_owned_worker_from_exception`，`PermissionError` 仍 `return False`
  - `OwnedWorkerProcess.poll`：无 raw 时按 Job 活跃数与 `close` 结果终结（失败码 2）；`take_owned_worker_from_exception` 仅接受 `OwnedWorkerProcess`
  - `spawn_owned_local_tts_worker`：pre-bind 的 `orphan_handle` 先 `_bind_job_handle` 到既有 owner 再清理
- **验证**（`C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe`）：
  - `tmp/codex_owned_job_remaining_paths_probe.py` → 三项全 `true`
  - `tmp/cursor_owned_job_validation_gate.py` → **288 OK**（skip 1），~59s，`MIKU_SET_CHANGED=0`（824→824）
  - `python -m compileall -q src tests` → OK；`git diff --check` → OK
- **未做**：root 无模型 venv sleeper/parent-exit 探针、真实 CosyVoice 启动、提交/推送

---

## 2026-10-18 Windows 本地 TTS owned Job — ba46cd75 部分启动 owner 交接（`docs/codex-review-local-tts-windows-job-2026-10-18.md` 补充）——**停止待 Codex 复审**

- **范围**：仅评审补充第 4 项「Popen 返回后中断且清理未完成时不得丢活 owner」；**保留** root 四项 probe 与 prior 返修；**未**跑真实模型/设备/data/memory、未提交/推送
- **实现**：
  - `spawn_owned_local_tts_worker` 先分配 `OwnedWorkerProcess`（可无 redirector），再创建 Job / `popen` / 原子 `_bind_redirector`；有界清理失败时 `attach_owned_worker_to_exception`
  - `CosyVoiceWorkerClient._start_worker_poll` 经 `take_owned_worker_from_exception` 接管 pending owner → `_proc` + `stop_failed`，控制流异常继续上抛
  - 门禁脚本 `tmp/cursor_owned_job_validation_gate.py`（原六 + `test_owned_worker_job` + slice_a）
- **验证**（`C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe`）：
  - `tmp/codex_owned_job_partial_spawn_probe.py` → 三项全 `true`（含 `returned_redirector_still_owned`）
  - `tmp/cursor_owned_job_validation_gate.py` → **284 OK**（skip 1），~61s，`MIKU_SET_CHANGED=0`（824→824）
  - `python -m compileall -q src tests` → OK；`git diff --check` → OK
- **未做**：root 无模型 venv sleeper/parent-exit 探针、真实 CosyVoice 启动、提交/推送

---

## 2026-10-18 Windows 本地 TTS owned Job 生命周期（`docs/cursor-task-local-tts-windows-job-2026-10-18.md`）——**停止待 Codex 严格复审（返修 batch）**

- **范围**：仅 Codex `docs/codex-review-local-tts-windows-job-2026-10-18.md` 七项；**保留** CPU checkpoint staging、**未改** `tmp/codex_owned_job_contract_probe.py`；**未**跑真实模型/设备/data/memory、未提交/推送
- **返修要点**：
  - 移除 `bind_process_runners` 对自定义 popen 的隐式 `FakeOwnedWorkerJobBackend`；fake 后端迁至 `tests/support/`，CosyVoice 行为测试经 `bind_fake_local_tts_runners` 显式注入
  - `CreateJobObject` `ERROR_ALREADY_EXISTS`+有效句柄拒绝；`CloseHandle` 失败不标完成；`spawn` `BaseException` 清理；`OwnedWorkerProcess` `RLock`；`worker_entry_join_owned_job` 去掉 `exit_fn`
  - ADR 增补碰撞/关闭失败/锁与测试 DI 说明
- **验证**（`C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe`）：
  - `tmp/codex_owned_job_contract_probe.py` → 四项全 `true`
  - 八模块门禁（原六模块 + `tests.test_owned_worker_job` + `tests.test_cosyvoice_production_slice_a`）→ **277 OK**（skip 1），~56s，`MIKU_SET_CHANGED=0`（824→824）
  - `python -m compileall -q src tests` → OK；`git diff --check` → OK
- **未做**：root 无模型 venv sleeper/parent-exit 探针、真实 CosyVoice 启动、提交/推送

---

## 2026-10-18 撤回 CUDA non-blocking 切片（`docs/cursor-task-withdraw-cuda-transfer-2026-10-18.md`）——**停止待 Codex 关闭回滚**

- **依据**：Codex 实测后决定移除未证明收益的 `e1227107`/`d6deacf4` non-blocking 适配器；**保留** CPU checkpoint staging 与 239 门禁 fixture 契约修复
- **移除**：`src/voice/cosyvoice/model_loading.py`、`tests/test_cosyvoice_cuda_nonblocking_model_load.py`；门禁六模块列表去掉 non-blocking 模块
- **恢复**：`scripts/cosyvoice_runtime_worker.py` → `_import_cosyvoice_automodel_class` 仅 `AutoModel`；`_construct_automodel(automodel_cls, model_dir)` 仅 `cpu_checkpoint_staging_scope`
- **实测记录（Codex，非性能声称）**：两模型 diagnostic60 Cosy 56.672s / AutoModel 42.157s、清理不完整（reader alive）；与仅 CPU staging 的 Cosy 30.328s / construct 21.336s 对比未显示保留 non-blocking 的理由；default30 亦失败。无真实 synth
- **验证**（`C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe`）：
  - `tmp/cursor_cpu_checkpoint_staging_validation_gate.py`（120s/UTF-8）→ **239 OK**，~51s，`exit=0`，`MIKU_SET_CHANGED=0`（824→824）
  - `python -m compileall -q src tests` → OK；`git diff --check` → OK
- **未做**：真实模型/设备/data/memory、vendor/默认/超时变更、提交/推送

---

## 2026-10-18 CosyVoice CUDA non-blocking `CosyVoiceModel.load`（`docs/cursor-task-cosyvoice-cuda-transfer-2026-10-18.md`）——**已撤回，见上一节**

- **范围**：仅 runtime worker AutoModel 构造；与既有 CPU checkpoint staging 嵌套；**未**改 vendor/pin/超时/默认配置/Ollama context、未接 App、未跑真实模型/设备/data/memory
- **Codex 返修**（`docs/codex-review-cosyvoice-cuda-transfer-2026-10-18.md`）：优化路径 `map_location="cpu"`（不依赖外层 staging）；`patched_load(self,*args,**kwargs)` 仅 `len(args)==3` 且无 kwargs 可优化；完成同步失败不再二次 `synchronize`；清理同步不掩盖主异常；`finally` 嵌套保证恢复抛错仍释放锁
- **生产**：
  - `src/voice/cosyvoice/model_loading.py` → `cuda_nonblocking_cosyvoice1_model_load_scope`
  - `scripts/cosyvoice_runtime_worker.py` → `_construct_automodel` 双作用域；`_import_cosyvoice_automodel_class` 一次返回 `(AutoModel, CosyVoiceModel)`
- **测试**：`tests/test_cosyvoice_cuda_nonblocking_model_load.py`（含关键字委托、无 staging 的 CPU map、同步/失败边界、作用域内线程 done、stale alias）；`tests/test_cosyvoice_cpu_checkpoint_staging.py` / `tests/test_cosyvoice_production_slice_b.py`（import mock 返回二元组）微调
- **验证**（`C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe`）：
  - `tests.test_cosyvoice_cuda_nonblocking_model_load` → **22 OK**
  - `tmp/cursor_cpu_checkpoint_staging_validation_gate.py`（120s/UTF-8）→ **261 OK**，~42s，`exit=0`，`MIKU_SET_CHANGED=0`（824→824）
  - `python -m compileall -q src tests` → OK；`git diff --check` → OK（CRLF 警告のみ）
- **未测风险**：`non_blocking` 无 pinned host memory 时收益不确定；部分传输失败后的 `synchronize` 不保证取消已排队拷贝；CosyVoice2/3 仍走 vendor `load`
- **未做**：性能声称、真实两模型/三模型诊断重跑、提交/推送

---

## 2026-10-18 CosyVoice 239 门禁 fixture 契约（Codex `codex-review-cosyvoice-fixture-contracts-2026-10-18`）——**停止待 Codex 严格复审**

- **依据**：`docs/codex-review-cosyvoice-fixture-contracts-2026-10-18.md`；仅修 `tests/test_cosyvoice_production_slice_b.py` 专用 fake，**未改**生产 client / 超时 / 断言 / root init·alias 探针
- **修复**：
  1. `_IdleTeardownWorkerProcess.wait` → 有界 `thread.join` 模拟 `Popen.wait(timeout)`（Idle/EOF 场景）；`test_reader_eof_not_reused_as_ready` 先 `wait(_teardown_entered)` 再断言 `_stdin_consumer_stopped`
  2. `_StartupProgressWorkerProcess` → `_EofSignallingLineQueueStream` + `_run` `finally` `stdout.close()`（ready 延迟 1.25s 不误报 EOF）；未改全局 `_LineQueueStream`
- **验证**（`C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe`）：
  - 专项：`test_reader_eof_not_reused_as_ready` + `test_progress_does_not_extend_startup_deadline` → OK
  - `tmp/codex_eof_reproduce_gate.py`（20 次 EOF 压力）→ `iterations=20 ok=true`，`exit=0`
  - `tmp/cursor_cpu_checkpoint_staging_validation_gate.py`（120s/UTF-8）→ **239 OK**，~43s，`exit=0`，`MIKU_SET_CHANGED=0`（824→824）
  - `python.exe -m compileall -q src tests` → OK；`git diff --check`（本文件）→ OK
- **未做**：真实模型/设备/memory/vendor、生产改动、提交/推送、重复全套门禁求绿

---

## 2026-10-17 CosyVoice CPU checkpoint staging——窄适配，**停止待 Codex 严格复审**

- **任务**：`docs/cursor-task-cosyvoice-cpu-checkpoint-staging.md`（仅 runtime worker AutoModel 构造；**未接** App / 预取 / vendor pin 变更）
- **根因（代码确认）**：vendor 以 CUDA `map_location` 加载 `llm.pt`/`flow.pt`/`hift.pt` 再落 CPU module 后 `.to(CUDA)`；staging 改为 CPU 载入，保留 vendor 后续 `.to(device)`。**不得**提前声称性能或共存已通过。
- **生产**：`src/voice/cosyvoice/checkpoint_loading.py` + `scripts/cosyvoice_runtime_worker._construct_automodel` 作用域包装
- **测试**：`tests/test_cosyvoice_cpu_checkpoint_staging.py`（fake `torch` / 线程 / 恢复 / worker 接线）
- **验证**（主 Python 3.12，`tmp/cursor_cpu_checkpoint_staging_validation_gate.py` 120s 监督 + UTF-8 + `miku_*.wav` 集合）：
  - `py -3.12 -m compileall -q src tests` → OK
  - `py -3.12 tmp/cursor_cpu_checkpoint_staging_validation_gate.py` → **236 OK**，~61s，`exit=0`，`MIKU_SET_CHANGED=0`（644→644；首轮 `test_reader_eof_not_reused_as_ready` 偶发失败后单测通过，重跑门禁通过）
  - `git diff --check` → OK（仅 CRLF 警告）
- **未做**：真实模型/设备/memory、vendor/资产 pin/超时/音色/默认引擎/权限变更、性能数字、提交/推送、C2b2b

---

## 2026-10-17 RVC 共存准备契约——`warm_rvc_worker` 假成功收口，**停止待 Codex 严格复审**

- **任务**：`docs/cursor-task-rvc-coexist-readiness.md`（本轮唯一生产修复；**未接** App loader / C2b2b / 预取）
- **根因**：`worker_warmup_inference` 开启时 `warm_up` 返回 False 仍 `return True`；协调器会把 RVC 当就绪继续 Cosy，真实共存 `8c91b8a59344` 已失败（**不得**把 1007 离线门禁当共存通过）
- **生产**：`src/voice/rvc/infer.py` → `warm_rvc_worker`：`warm_up` 失败或 `worker.ready` 不真实则 `False`；关闭推理预热时 `start` 后仍校验 `ready`；无新配置/线程/超时改动
- **文档/注释**：`config/config.yaml`（RVC 预热失败语义注释）、`docs/engineering-baseline.md`（首段冷启动表）
- **测试**：
  - `tests/test_rvc_worker_lifecycle.py` → `WarmRvcWorkerPrepareContractTests`（start/warm/ready/disabled/policy 契约）
  - `tests/test_cosyvoice_production_slice_c2b2a.py` → 协调器穿透真实 `TTSEngine.prepare_rvc` + fake worker 预热失败零 Cosy
- **验证**（主 Python 3.12，`tmp/cursor_rvc_coexist_readiness_gate.py` 120s 监督 + UTF-8 + `miku_*.wav` 集合）：
  - `py -3.12 -m compileall -q src tests` → OK
  - `py -3.12 tmp/cursor_rvc_coexist_readiness_gate.py` → **86 OK**，~0.9s，`exit=0`，`MIKU_SET_CHANGED=0`（644→644）
  - `git diff --check` → OK（仅 CRLF 警告）
- **未做**：真实三模型共存/设备、memory 读写、音色参数/默认引擎/超时/线程数变更、提交/推送、C2b2b 接线
- **后续（Codex）**：一次 Cosy extended-cold 60s 上限诊断（生产默认 30s 不变），再评估实际启动预算

---

## 2026-10-17 CosyVoice C2b2a——第二次返修（快照/ Memory 原子边界），**停止待 Codex 严格复审**

- **依据**：`docs/codex-review-cosyvoice-production-roundc2b2a.md` 顶部「3d7718dd 第二次独立评审」——仅两项已复现边界 + `LocalVoiceWarmupResult` mapping 别名；**未改** `tmp/codex_c2b2a_review_probe.py`
- **返修**：
  1. `src/core/local_voice_warmup.py` → `_safe_warmup_snapshot` / `_safe_begin_generation`：snap 调用、属性读取与严格类型校验同一窄异常边界（generation 为正 `int` 非 bool；`finished`/`succeeded` 严格 `bool`；禁止 `bool('false')` 冒充成功）；KI/SystemExit 上抛
  2. `src/memory/aware_client.py` → `begin_fresh_warmup` 仅委托 inner 原子 API；缺失则 `0` 且零 `start_warmup`/snapshot 回退
  3. `LocalVoiceWarmupResult.__post_init__` → 始终 `dict()` 复制后再 `MappingProxyType`，隔离外部传入 proxy 与底层 dict 别名
- **测试**：`tests/test_cosyvoice_production_slice_c2b2a.py`（+6：属性异常固定失败、begin bool 拒绝、非严格 bool 字段、proxy 别名、legacy inner 零 start、wrapper 仅委托 begin）
- **探针**（未改 root）：
  - `py -3.12 tmp/codex_c2b2a_review_probe.py` → exit 0（六项 JSON 字段通过）
  - `py -3.12 tmp/codex_c2b2a_extra_probe.py` → `snapshot_property_escaped=False`，`non_atomic_wrapper_claims_fresh_success=False calls=0`
- **验证**（主 Python 3.12，`tmp/cursor_c2b2a_validation_gate.py` 120s 进程树监督 + UTF-8 + 精确 `miku_*.wav` 集合）：
  - `py -3.12 -m compileall -q src tests` → OK
  - `py -3.12 tmp/cursor_c2b2a_validation_gate.py` → **234 OK (skipped=1)**，~58s，`exit=0`
  - `MIKU_SET_CHANGED=0`（before=644 after=644）
- **未做**：真实三模型共存/首句、App loader/关闭 owner（C2b2b）、提交/推送

---

## 2026-10-17 CosyVoice C2b2a——独立三模型串行协调器初版（aced2b65），**已由第二次返修取代**

- **任务**：`docs/cursor-task-cosyvoice-production-roundc2b2a.md`
- **历史门禁**：223 OK（监督器曾挂在 unittest 模块，已迁至 `tmp/cursor_c2b2a_validation_gate.py`）

---

## 2026-10-17 CosyVoice C2b1——显式 prepare API（未接装配/自动预热），**停止待 Codex 严格复审**

- **任务**：`docs/cursor-task-cosyvoice-production-roundc2b1.md`（仅窄 effectful prepare；无 `assembly`/`resource_coord`/CLI 接线、无预取）
- **生产**：
  - `src/voice/cosyvoice/client.py` → `CosyVoiceWorkerClient.prepare(cancel_event=...)`
  - `src/voice/tts/cosyvoice_backend.py` → `CosyVoiceTtsBackend.prepare`
  - `src/voice/tts/engine.py` → `TTSEngine.prepare_local_tts` + `_MergedLocalTtsCancel`（外部 cancel 与 interrupt/shutdown 只读合并）
- **契约**：零 synth / 零 `TFM.create` / 零 Edge·RVC·播放；复用 B/C1 epoch、`_begin_request`、idle 退休、backend 待删 owner 两阶段 gate；`speak_lock` 忙即 False
- **文档**：`docs/architecture/adr-cosyvoice-production-backend.md`（C2b1 切片说明）
- **测试**：`tests/test_cosyvoice_production_slice_c2b1.py`（新建，17 项 fake 并发/取消/owner/互斥）
- **验证**（主 Python 3.12，120s 有界门禁）：
  - `py -3.12 -m compileall -q src tests` → OK
  - `py -3.12 -c "from tests.test_cosyvoice_production_slice_c2b1 import run_c2b1_validation_gate; raise SystemExit(run_c2b1_validation_gate())"` → **239 OK**，~53s，`exit=0`
  - `data/tmp` `miku_*.wav`：**644→644**，`MIKU_SET_CHANGED=0`
  - `git diff --check` → OK（仅 CRLF 警告）
- **未做**：真实模型/设备/主程序、memory、提交/推送、装配预热协调（C2b2+）

---

## 2026-10-17 CosyVoice C2a——装配接线 + 共享 TFM 关闭状态，**停止待 Codex 严格复审**

- **任务**：`docs/cursor-task-cosyvoice-production-roundc2a.md`（仅 C2a；无 C2b 预热/预取、无默认引擎切换）
- **生产**：`src/core/assembly.py`（`validate_assembly_voice_tts_config`、`build_assembly_safety_policy`、`build_tts_engine` cosy 内层传参）、`src/core/assistant.py`（`local_cosyvoice_shutdown_status` 未完成时不标记 TTS/共享 TFM 完成）
- **配置/文档**：`config/config.yaml`（`local_tts_worker: false` 与 `voice.tts.cosyvoice` 注释）、`docs/architecture/adr-cosyvoice-production-backend.md`、`docs/VOICE.md`
- **测试**：`tests/test_cosyvoice_production_slice_c2a.py`（新建）；`tests/test_assistant_orchestration.py`（+1 shutdown）
- **验证**（主 Python 3.12，120s 内进程树监督等价单次跑满）：
  - `py -3.12 -m compileall -q src tests` → OK
  - `py -3.12 -m unittest tests.test_cosyvoice_production_slice_c2a tests.test_assistant_orchestration.ShutdownLifecycleTests tests.test_assembly_rollback tests.test_mode_assembly_round2` → **45 OK**，~2.2s，`exit=0`
  - `data/tmp` `miku_*.wav`：**644→644**，`MIKU_SET_CHANGED=0`
  - `git diff --check` → OK（仅 CRLF 警告）
- **未做**：真实模型/设备/主程序、memory 读写、提交/推送、C2b

---

## 2026-10-17 CosyVoice C1——a159cf50 分配 gate 两阶段顺序，**停止待 Codex 复审**

- **依据**：`docs/codex-review-cosyvoice-production-roundc1.md` 顶部「a159cf50 交付后」唯一阻断项
- **修复**（仅 `src/voice/tts/cosyvoice_backend.py` + `tests/test_cosyvoice_production_slice_c1.py`）：
  - 删除 `_failed_retention`；`_release_retained_outputs` 不再清除 `_pending_worker_retire`
  - `_gate_synth_allocation`：`pending_worker_retire` 时先 `retire_worker`（未完成则零 remove / 零 create），再 `release_retained_outputs`；任意仍保留 owned 路径则拒绝分配
  - 成功路径延迟删除（仅 OS 占用）不置 `pending_worker_retire`；`shutdown_local` 在 complete 时同样先 retire 再 release
  - 新增 `test_incomplete_retire_blocks_remove_and_second_create`
- **探针**（未改 `tmp/codex_c1_owner_probe.py`）：`py -3.12 tmp/codex_c1_owner_probe.py` → exit 0，`removed_outputs_before_failed_retirement=0`，其余 assert 字段通过
- **专项**：`py -3.12 -m unittest tests.test_cosyvoice_production_slice_c1` → **38 OK**
- **门禁**（一次）：`py -3.12 tmp/codex_c1_validation.py` → `exit_code=0`，`temp_before=644` / `temp_after=644`，`added_files=[]`，**274 tests OK**（~46s）
- **未做**：真实模型/设备、memory、提交/推送；未改 B/assembly/config/探针

---

## 2026-10-17 CosyVoice C1——c8dacdd2 四项精确收口，**停止待 Codex 严格复审**

- **依据**：`docs/codex-review-cosyvoice-production-roundc1.md` 顶部「c8dacdd2 中止后的精确收口」四项（仅此项，不扩范围）
- **修复**：
  1. `test_delayed_remove_keeps_single_tfm_owner_high_water`：可解锁 `_LockedTempFileManager` + `addCleanup` 先解除模拟占用再 `tfm.shutdown()`（不再 `tfm._owned.clear()`/手工 unlink）
  2. `cosyvoice_backend`：成功/异常 `remove_many` 后仍 `is_owned` 的本后端路径进入有界待删集合（`MAX_RETAINED_OUTPUTS=2`）；失败保留与 `_failed_retention` 分离，避免 OS 占用与 worker 退休混淆
  3. `CosyVoiceWorkerClient.continue_shutdown`：未 `closing` 时 `RuntimeError`，零 registry 副作用；新增 `CosyVoiceClientShutdownGuardTests`
  4. `retire_worker`：锁内同时拒绝 `_worker_retiring` 已在进行，避免双退役清标志
- **探针**（未改 `tmp/codex_c1_owner_probe.py`）：`owned_high_water_on_success_with_deferred_delete=2`、`continue_shutdown_releases_open_client_registry=false`、退役相关字段保持通过
- **门禁**（Python 3.12，`tmp/codex_c1_validation.py` 一次）：`exit_code=0`，`temp_before=644` / `temp_after=644`，`added_files=[]`，273 tests OK（~49s）
- **单测先行**：`test_delayed_remove_keeps_single_tfm_owner_high_water` 前后 `miku_*.wav` 集合零增（644→644）
- **事实**：`data/tmp` 既有 644 个历史 `miku_*.wav` 含此前错误 fixture 遗留，本次未泛删
- **未做**：真实模型/设备、memory、提交/推送

---

## 2026-10-17 CosyVoice 切片 C1——7719fe7d 后三项收口，**停止待 Codex 复审**

- **任务**：`docs/cursor-task-cosyvoice-production-roundc1.md`；评审清单：`docs/codex-review-cosyvoice-production-roundc1.md`（顶部三项）
- **收口**：失败输出 `MAX_RETAINED_OUTPUTS=1` + 有界 `CosyVoiceWorkerClient.retire_worker` 后再分配；`save_to_file` 与 `speak` 同 epoch 准入并清除陈旧 `interrupt_event`；`shutdown_local` 先 `interrupt`/`client.shutdown` 再在同一 deadline 等 in-flight
- **范围**：`src/voice/cosyvoice/client.py`（窄 `retire_worker`）、`cosyvoice_backend.py`、`engine.py`、`tests/test_cosyvoice_production_slice_c1.py`
- **独立探针**（未改 `tmp/codex_c1_owner_probe.py`）：`py -3.12 tmp/codex_c1_owner_probe.py` → `failed_calls_created_outputs=1`、`next_explicit_save_receives_stale_cancel=[false]`、`stop_signal_delay_ms=0`
- **离线验证**（Python 3.12，`tmp/c1_roundc1_retest.log`）：`slice_c1` **34 OK** + `slice_a` + `security_round3c_logging` + `stream_playback` → **136 OK (skipped=3)** (~5.2s)；`compileall` + `git diff --check` OK
- **未做**：真实 worker/设备、主程序、memory、提交/推送；B 基线 900 tests 未重跑
- **Codex 下一步**：按 `docs/codex-review-cosyvoice-production-roundc1.md` 独立复审 C1 → 通过后再 C2

---

## 2026-10-17 CosyVoice 切片 B——idle 退休窗口竞态，**停止待 Codex 复审**

- **任务**：`docs/cursor-task-cosyvoice-idle-retirement.md`
- **根因**：`_ensure_worker_started` 仅以 `_ready` + `process_alive()` 复用 worker；idle 后主循环已退出、stdin 无消费者，但 teardown 期间 proc 仍存活，导致向“僵尸”worker 派发合成（真实 smoke：idle 2s + sleep 3s 后约 94ms 失败）
- **修复**（仅 `client.py` + fake 测试）：
  - 记录 worker 代际 `_worker_last_activity_mono`（ready / 合成成功时刷新）
  - 新请求派发前 `_worker_reuse_ok`：reader 存活、无协议致命错误、未达 idle 退休阈值（`idle_timeout_sec` 减固定 guard）；否则 `_reap_worker_for_reuse` 走既有 `_stop_worker_until` 有界回收，确认 proc/reader/writer 全退出后再 cold-start
  - 停止预算与 `_remaining` 统一经可注入 `_MONOTONIC`（默认 `time.monotonic`）
  - 无推理静默重试、无双模型、无加长 probe sleep
- **新增测**：`CosyVoiceIdleRetirementTests`（teardown proc 仍 alive、warm 不重启、stop 失败保留 owner、reader 非 alive 不复用、取消/关闭有界返回）
- **未改**：config、RVC、全局 safety、vendor、memory；未提交、未推送；**未**真实 cold/warm/idle smoke
- **离线验证**（`Python312 -X utf8`，**timeout=90**）：`cross_python_file_identity` + `slice_a` + `slice_b` + `model_lock` → **127 OK (skipped=2)**，`RC=0`，约 **49s**
- **Codex 下一步**：独立复审 → 通过后重跑默认 30s 真实 cold / warm / idle 三例 smoke

---

## 2026-10-17 CosyVoice 切片 B——portable 派发前 TFM 竞态返修，**停止待 Codex 复审**

- **依据**：`docs/codex-review-cosyvoice-cross-python-identity.md`（CHANGES REQUESTED → 仅派发前 ownership 竞态）
- **根因**：`synthesize` 在首次 TFM assert 后于 `capture_portable_file_identity` 窗口内路径可被替换；portable 指向未登记替代物后仍可能进入 `_begin_request`/spawn
- **返修**（仅 `client.py` + 测试）：
  - stdlib/portable 快照与 `validate_worker_output_path` 之后、`_begin_request` 之前再次 `assert_owned_regular_file`；portable 与复采 pair 不一致则拒绝
  - 不收养替代物、不更新原登记；child 仍用首次快照 pair（复验通过后与磁盘一致）
- **新增测**：`CosyVoiceClientLifecycleTests.test_synth_rejects_replacement_during_portable_capture_before_spawn`（真实 TFM、零 Popen/spawn）
- **未改**：`cross_file_identity` helper、全局 safety、RVC、config、vendor、memory；未提交、未推送；未真实模型 smoke
- **离线验证**（`Python312 -X utf8`，`subprocess` **timeout=90**）：
  - `cross_python_file_identity` + `slice_a` + `slice_b` + `model_lock` → **121 OK (skipped=2)**，`RC=0`，约 **44s**
  - `tmp/codex_portable_owner_race.py` → `entered_worker_start_with_unowned_replacement=0`，`owned=false`
- **Codex 下一步**：独立复验竞态修复 → 通过后继续 portable 身份里程碑与默认 30s smoke

---

## 2026-10-17 CosyVoice 切片 B——跨 Python portable 文件身份（B 真实缺陷），**停止待 Codex 复审**

- **任务**：`docs/cursor-task-cosyvoice-cross-python-file-identity.md`
- **根因**：主 Python 3.12 与 Cosy vendor 3.11 对同一文件 `os.stat().st_dev` 不一致（`st_ino` 可同），父子协议直接比 `st_dev` 导致模型已 ready 仍 `output_rejected`
- **范围**：
  - 新增 `src/voice/cosyvoice/cross_file_identity.py`：Windows `FILE_ID_INFO`（完整 64 位卷序列号 + 128 位 FileId），fd 经 `msvcrt.get_osfhandle`；非 Windows 仍为 `st_dev`/`st_ino`；API 不可用 fail closed
  - `worker_output.py` / `client.py`（协议 `output_dev`/`output_ino` portable）/ `output_verify.py`（词法校验用 portable；同进程 fd 仍 `capture_file_identity`）
  - `tests/test_cosyvoice_cross_python_file_identity.py`（3.12 vs 固定 3.11 真子进程 identity-only）；`slice_b` 探针改用 portable helper
- **未改**：全局 `capture_file_identity`、RVC、`config.yaml`、`data/memory`、vendor pin、assembly；未提交、未推送；**未**真实模型/GPU smoke
- **Codex 下一步**：独立复审 → 通过后由 Codex 跑默认 30s cold/warm/idle fixed-text smoke（不扩大默认 timeout）
- **离线验证**（`Python312 -X utf8`，bounded gate）：
  - `tests.test_cosyvoice_cross_python_file_identity` + `slice_a` + `slice_b` + `test_cosyvoice_model_lock` → **120 OK (skipped=2)**，`RC=0`，约 **44s**

---

## 2026-10-17 CosyVoice 切片 B——startup 诊断 Codex 返修三项，**停止待 Codex 复验**

- **依据**：`docs/codex-review-cosyvoice-startup-diagnosis.md`（CHANGES REQUESTED → 本轮仅三项）
- **返修**：
  1. `CosyVoiceStartupProgressTests`：`_client_with_fake` 绑定当前 `self`，TFM `addCleanup` 不再挂在未运行的临时 TestCase
  2. `scripts/cosyvoice_runtime_worker.py`：`wetext_import` 阶段 `_apply_local_wetext` 内第三方 `import` 亦包 `redirect_stdout_to_stderr`；`_emit` 仍走 `_PROTOCOL_STDOUT`
  3. `StartupProgressReporter`：注入 `clock` 同时用于 `t0` 与 `_elapsed_ms`（`self._clock`）
- **新增窄测**：`test_reporter_elapsed_ms_uses_injected_clock`、`test_wetext_apply_redirects_import_stdout_off_protocol`
- **未改**：默认 timeout、切片 C、`config.yaml`、RVC、vendor pin、`data/memory`、真实模型/设备；未提交、未推送
- **离线验证**（`C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8`，`subprocess.run(..., timeout=90)` 真 90s 守卫）：
  - `tests.test_cosyvoice_production_slice_a` + `slice_b` + `test_cosyvoice_model_lock` → **112 OK (skipped=2)**，`RC=0`，约 **36s**；stdout/stderr → `tmp/cosy_startup_progress_stdout.txt`、`tmp/cosy_startup_progress_stderr.txt`
  - `data/tmp` 精确集合 `miku_*.wav`：**BEFORE=540 → AFTER=540，Δ=0**（未删历史未归属文件）
- **Codex 下一步**：独立复验本返修 → 通过后由 Codex 跑 fixed-text cold smoke 读 stage 定位真实卡点

---

## 2026-10-17 CosyVoice 切片 B——有界 startup_progress 启动诊断（首轮实现，已被上条返修 supersede 验证计数）

- **任务**：`docs/cursor-task-cosyvoice-startup-diagnosis.md`
- **动机**：真实 cold 启动 30.094s 未 `ready`；需在**不**调高默认 `startup_timeout_sec`（仍 30）前提下获得阶段耗时证据
- **范围**：
  - `src/voice/cosyvoice/protocol.py`：`startup_progress` 消息、`StartupProgressAccumulator` / `StartupProgressReporter`、`StartupProgressSnapshot`
  - `src/voice/cosyvoice/client.py`：启动轮询消费 progress、有界快照 `startup_progress_snapshot()`、失败/超时保留最后快照
  - `scripts/cosyvoice_runtime_worker.py`：7 阶段 telemetry；`_PROTOCOL_STDOUT` 协议 sink；`cosyvoice_import` / `automodel_construct` 拆分且导入期 stdout→stderr
  - `tests/test_cosyvoice_production_slice_b.py`：`CosyVoiceStartupProgressTests` + main 探针适配
- **未改**：默认 timeout、切片 C、TTSEngine/assembly、`config.yaml`、RVC、vendor pin、`data/memory`；未提交、未推送
- **用户偏好（仅记录）**：主观试听选定 `data/test/voice-naturalness/cosyvoice-instruct/01_warm_companion_rvc.wav`；本轮**未**改 RVC/配置
- **自审（错误/取消/关闭/队列/资源）**：
  - 重复/乱序/超 14 事件 → 父 `accumulator` 拒收并走既有 `_terminate_worker_bounded`；reader 侧 schema 仍 fatal
  - progress **不**重置 `deadline`；超时仍 `startup_timeout_sec` 单调截止
  - 启动中 `cancel_event` / epoch 不匹配 → 原有终止路径；`shutdown`/`stop_failed` 语义未动
  - stdout 队列容量仍为 `STDOUT_QUEUE_CAPACITY=32`；progress 仅 +14 行
- **未运行**：真实 CosyVoice 模型/GPU/Ollama、主程序、全量 discover
- **首轮离线验证**（返修前）：**110 OK (skipped=2)**，`RC=0`，约 **35s**
- **Codex 下一步**：见上条「startup 诊断 Codex 返修三项」

---

## 2026-10-17 CosyVoice 切片 B——遗留模型锁迁移（model_lock_busy smoke 根因），待 Codex 复审

- **任务**：`docs/cursor-task-cosyvoice-lock-migration.md`
- **根因**：固定 `vendor/CosyVoice/runtime/cosyvoice_model.lock` 为早期版本留下的 7×NUL（非 1 字节）；旧逻辑在 **未** 持 OS 锁前要求 `size==1`，真实 worker smoke 固定 `model_lock_busy`
- **范围**：`src/voice/cosyvoice/model_lock.py`；`tests/test_cosyvoice_model_lock.py`（新建）；本 handoff 条目
- **行为**：
  - 先 `open`/`O_EXCL` 创建，再 `msvcrt` 锁字节 0；**持锁前**不 truncate/改写
  - 校验同 inode 普通文件；拒绝 link/reparse/非 REG、非 NUL 遗留、>64 字节
  - 0 字节或 ≤64 全 NUL 遗留：在已持锁句柄内迁移为 1×NUL（同 inode）；OS 锁忙则零改动
  - **未** shell 删改仓库内真实 7 字节锁文件（留给修复后 runtime 首次启动自迁移）
- **未改**：切片 C、`config.yaml`、`data/memory`、worker/client 其它逻辑；未提交、未推送
- **未运行**：全量 `unittest discover`、真实 CosyVoice 模型/GPU、主程序
- **离线验证**（`C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8`，90s 守卫）：
  - `tests.test_cosyvoice_production_slice_a` + `slice_b` + `test_cosyvoice_model_lock` → **97 OK (skipped=2)**，`exit=0`，约 **32s**
- **Codex 下一步**：独立 review 后由 Codex 重跑受控真实 fixed-text worker smoke（预期可过锁并自迁移 7 NUL）

---

## 2026-10-17 CosyVoice 切片 B——B6 回归测试探针修正（TEST-ONLY），待 Codex 复审

- **范围**：仅 `tests/test_cosyvoice_production_slice_b.py`（`test_synth_rejects_dotdot_alias_before_spawn`、`test_synth_rejects_reparse_alias_before_spawn`）；本 handoff 条目
- **问题**：旧探针 `parent/../name` 在 Windows 上 resolve 到 tmp **外**，TFM/所有权也会拒，无法区分「未对原始 path 词法校验」；reparse mock 打在 canonical `owned` 上，key 路径校验同样会拒
- **修正**：
  - dotdot：别名 `parent/../parent.name/name`，`assert Path(alias).resolve()==owned.resolve()`；主断言仍零 spawn
  - reparse：独立词法别名 + 窄 mock `assert_owned_regular_file(alias)→canonical`；`is_reparse_or_link` 仅对别名为真
  - 两段均在 `client.validate_worker_output_path` 边界临时 mock「旧 key 入参」并断言会 spawn（若生产改回 `key` 则主断言失败）
- **未改**：生产代码、config、`data/memory`；未提交、未推送
- **离线验证**（`C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8`）：
  - 上述 2 项 → **2 OK**，约 **1.2s**

---

## 2026-10-17 CosyVoice 切片 B——B6 父侧原始 output_path 词法（final narrow gap），待 Codex 复审

- **范围**：`src/voice/cosyvoice/client.py`（`synthesize` 一处）；`tests/test_cosyvoice_production_slice_b.py`（+2 client 测试）；本 handoff 条目
- **改动**：TFM 仍登记 canonical `key`；`validate_worker_output_path` 改为对**调用方原始** `output_path` 与已捕获 dev/ino 校验（在 `_begin_request`/spawn 之前），协议与 `verify_synth_output` 仍用 `key`
- **新增测试**：`test_synth_rejects_dotdot_alias_before_spawn`、`test_synth_rejects_reparse_alias_before_spawn`（mock reparse，零 popen）；保留 `test_synth_success_writes_owned_output`
- **未改**：`output_verify`、worker、config、`data/memory`；未提交、未推送
- **离线验证**（`Python312 -X utf8`，bounded B = slice_a + slice_b）：**86 OK (skipped=1)**，`exit=0`，约 **26s**（B6 探针修正见上条）

---

## 2026-10-17 CosyVoice 切片 B——测试卫生（TFM shutdown），Codex final B 前

- **范围**：仅 `tests/test_cosyvoice_production_slice_b.py`；未改生产代码、探针或断言
- **改动**：新增 `_register_tfm_shutdown`（`addCleanup`）；每个 `TempFileManager` 在 client / fake worker 退出后再 `shutdown()`，只清理本测试 owned 的 `miku_*.wav`
- **离线验证**（`C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8`，90s 守卫）：
  - `tests.test_cosyvoice_production_slice_a` + `slice_b` → **84 OK (skipped=1)**，`exit=0`，约 **26s**
  - `data/tmp` 精确集合 `miku_*.wav`：**BEFORE=528 → AFTER=528，Δ=0**（未广删历史文件）
- **未运行**：全量 `unittest discover`、真实 CosyVoice/GPU、主程序；未读改删 `data/memory`；未提交、未推送

---

## 2026-10-17 CosyVoice 切片 B——三项独立回归（roundb-three-regressions），等待 Codex 复审

- **任务**：`docs/cursor-task-cosyvoice-roundb-three-regressions.md`
- **状态**：仅修 fd 泄漏 / 关闭后孤儿 spawn / 过期 deadline 未发终止；**未**进入切片 C、**未**改生产 `config.yaml`
- **未运行**：真实 CosyVoice 模型/GPU、主程序；未读改删 `data/memory`；未提交、未推送

### 三项修复（自审）
1. **`output_verify.verify_synth_output`**：`os.open` 后全程 `try/finally` 关闭未移交 fd；身份/大小不符提前返回不再泄漏
2. **`_start_worker_poll` 关闭后 spawn**：在 `_spawn_lock` 下登记 `_proc`（禁止 ready），经 `_terminate_worker_bounded(deadline=now)` 非阻塞 terminate/kill；不在活进程上同步关管道；`stop_failed` + 保留 owner 时 shutdown `complete=false`、单实例 registry 不释放
3. **`_stop_worker_until` 预算用尽**：`remaining<=0` 仍非阻塞 terminate/kill（不 wait）；生命周期锁获取失败保留 owner；`_terminate_worker_locked` 在零预算时跳过 wait 但仍发 kill

### 独立探针（`tmp/codex_cosy_b_final_probe.py`）
- `fd_leaked_on_identity_mismatch=0`，`spawn_orphaned_worker_reported_complete=false`，`expired_deadline_sent_termination=true`
- 探针最小适配：`Proc.wait` → `subprocess.TimeoutExpired`；第二 client 前清 `_ACTIVE_CLIENT`（不改变判定语义）

### 新增回归测试（`tests/test_cosyvoice_production_slice_b.py`）
- `CosyVoiceOutputVerifyFdTests`：身份/大小/fstat 失败/正常 WAV 均 assert fd 已关闭
- `test_post_close_spawn_stubborn_proc_blocks_second_client`：terminate/kill 不退出的孤儿 proc，第二 client 拒绝、重试 shutdown 后 complete
- `test_synth_write_timeout_sends_termination`：1s synth 写超时后 worker 已 terminate
- `test_expired_deadline_stop_signals_without_wait`：过期 deadline 快速返回且信号已发、owner 保留

### 离线验证（Python 3.12，`PYTHONIOENCODING=utf-8`）
- `tmp/codex_cosy_b_final_probe.py` → **exit=0**（三项 JSON 如上）
- `tests.test_cosyvoice_production_slice_a` + `slice_b` → **84 OK (skipped=1)**，`RC=0`，约 **26s**（stderr → `tmp/cosy_ab_stderr.txt`）
- 全量 `unittest discover -s tests -q` → **857 OK (skipped=5)**，`RC=0`，约 **59s**（stderr → `tmp/cosy_full_stderr.txt`）
- `compileall src scripts` → **exit=0**；`git diff --check` → **exit=0**

---

## 2026-10-17 CosyVoice 生产后端 切片 B——session37dbffd6 六项闭环，等待 Codex 复审

- **任务**：`docs/cursor-task-cosyvoice-production-roundb.md`
- **评审**：`docs/codex-review-cosyvoice-production-roundb.md`（TOP 六项 consolidated findings，session37dbffd6）
- **ADR**：`docs/architecture/adr-cosyvoice-production-backend.md`
- **状态**：按六项评审闭环 B 范围（client/worker/协议/输出/生命周期）；**未**进入切片 C、**未**接 TTSEngine/装配、**未**改生产 `config.yaml`
- **未运行**：真实 CosyVoice 模型/GPU、主程序、Ollama/RVC 真实合成；未读改删 `data/memory`；未提交、未推送

### 对照六项评审（自审）
1. **B4 取消/写入**：`CosyVoiceStdinWriter.write_bounded` 单一绝对 deadline + `should_cancel` ≤50ms 轮询；`close()` 唤醒等待线程；client 传入 cancel/epoch/closing；专项 fake 阻塞写 + close/cancel 测试
2. **B1/B6 输出**：词法拒绝 `..` 与 TMP 外路径；推理全程持有已验证 fd、生成期 identity 检查；`output_verify.verify_synth_output` 拒绝 NaN/非单声道；WAV 头预留字节上限
3. **B2 协议**：inbound 拒绝 `schema=True`、负 epoch、超长 request_id；outbound 先判 bool 再比 schema；stdout/stdin EOF 无换行截断为 fatal
4. **B2 队列**：`StdinReaderFatal` 在 `run_worker_loop` 取队前后优先检查，满队列零 inference、固定 `ERR_PROTOCOL`（非 shutdown_ack）
5. **B3 生命周期**：`Popen` 可阻塞至 shutdown 返回后再 `start()` fake worker；冷启动 cancel 在 `Popen` 之后 set；fake 仅 `start()` 时登记 registry
6. **B5/B6 主流程**：`validate_worker_lexical_layout` 先于 admission/resolve；`worker_main` fake 低 RAM 零 `_load_model` 单测

### 独立探针（`tmp/codex_cosy_b_review_probe.py`）
- `dotdot_outside_tmp_accepted=false`，`nan_wav_reported_success=false`，`replacement_file_written=false`，`invalid_schema_epoch_rid_accepted=false`，`write_wait_ignores_close=false`
- `overflow_loop_result` 为 `protocol_error`（非 shutdown_ack）

### 离线验证（Python 3.12，进程总超时守卫）
- `tests.test_cosyvoice_production_slice_b` → **38 / OK**，约 **22s**
- `tests.test_cosyvoice_production_slice_a` + `slice_b`（`Start-Process -WindowStyle Hidden`，90s 上限）→ **exit=0**
- 全量 `unittest discover -s tests -q`（stderr 抑制计数）→ **exit=0**，约 **56s**（180s 守卫）

### 剩余风险
- 真实 GPU / 跨进程锁 / Windows 父进程 watchdog 仍须 Codex 后设备复测
- 切片 C（TTSEngine 接线）未开始

---

## 2026-10-17 CosyVoice 生产后端 切片 A——A1–A3 返修完成，等待 Codex 复验

- **任务**：`docs/cursor-task-cosyvoice-production-rounda.md`（仅切片 A）
- **评审**：`docs/codex-review-cosyvoice-production-rounda.md`（A1–A3 + 同轮小项）
- **ADR**：`docs/architecture/adr-cosyvoice-production-backend.md`
- **状态**：路径逐级 reparse/symlink 拒绝 + 封存后复验、caller Popen 全拒绝、UTF-8 管道协议、严格 `parent_pid`/settings/`assets.__all__` 已落地；**未**接 TTSEngine/切片 B runtime worker/真实模型
- **未运行**：真实 CosyVoice worker、GPU/设备、主程序、Ollama；未读改删 `data/memory`；未提交、未推送

### 返修要点（A1–A3）
1. `src/voice/cosyvoice/path_trust.py`：词法布局 + 祖先链 `lstat`/reparse 检测；`revalidate_fixed_layout_paths` 在封存与每次 spawn 前执行
2. `merge_local_tts_popen_kwargs`：任意 caller 键拒绝；`build_runtime_popen_options` 固定 `encoding=utf-8`、`errors=strict`
3. `settings` 大整数/非有限 → `ValueError`；`capabilities._require_strict_parent_pid`

### 离线验证（真实退出码）
- 专项 `tests.test_cosyvoice_production_slice_a` → **16 / OK (skipped=1)**，`exit=0`
- 相关（+ expressive + security_round3a + rvc_artifact）→ **74 / OK (skipped=1)**，`exit=0`
- `compileall`（`src`、`scripts`、`diagnose_expressive_tts.py`）→ **exit=0**
- `git diff --check` → **exit=0**
- `py test_rvc_conversion.py` dry-run → **exit=0**
- `py diagnose_expressive_tts.py` dry-run → **exit=0**
- **未运行**：全量 `unittest discover`（留待 B/C 整合后统一跑）

---

## 2026-10-17 RVC电流伪影矩阵——Codex最终工程复验通过，待人工选择

- F1-F4逐项返修并由Codex复审；相关111项、全量773项OK（skipped=4）；compile/diff/dryrun通过。
- 真实四组24/24，exit0，report.complete=true，90.4秒；输出`data/test/voice-naturalness/rvc-artifact-eval/`。
- 实际profile：idx025_p050、idx055_p033、idx025_p033、idx000_p050；RMVPE、filter_radius1。
- 无残留Python Worker/staging/publishing，显存1656→1659MiB；12个基线音频SHA256不变。
- 已交付A/B/C/D试听，等待用户电流音与Nahida音色选择。生产参数未切换，CosyVoice生产集成/30–60分钟长测/最终跨模块验收/版本整理仍待完成。
- 未触碰真实data/memory，未提交推送。恢复总清单：`docs/delivery-follow-through-2026-10-17.md`。

---

## 2026-10-17 RVC 伪影评估 Round 1 返修 F4——停止，等待复验

- **评审**：`docs/codex-review-rvc-artifact-evaluation-round1.md`（F4）
- **状态**：F1/F2/F3 主体保留；F4 infer 层并行加载防护已改；**停止等待 Codex 复验**
- **未运行**：真实 `--execute` / RVC Worker / GPU / 主程序 / Ollama / CosyVoice / 麦克风 / 扬声器；未读 `data/memory`；未提交、未推送

### F4（fallback / worker_disabled / 策略切换 fail-closed）
`get_rvc_worker()` 重建前停止失败抛 `RvcWorkerUnavailableError`，保留旧 client 与 config key，不返回不匹配实例。`warm_rvc_worker` / `run_rvc_infer` 仅捕获该窄异常并返回 `False`，不 fallback。`run_rvc_infer` 在显式 fallback、`worker_enabled=False` 一次性路径及策略实例切换前，经 `stop_rvc_worker()` 确认旧 Worker 已退出；仍存活且停止失败则拒绝子进程冷启动。`tests/test_rvc_worker_lifecycle.F4ParallelLoadGuardTests` 使用 `_FakeProcess` 显式 `process_alive` 行为，不为 Mock 改生产判定。

### 修改文件（F4）
- `src/voice/rvc/infer.py`
- `tests/test_rvc_worker_lifecycle.py`
- `docs/cursor-handoff-status.md`、`docs/codex-review-rvc-artifact-evaluation-round1.md`（F4 注记）

### 离线验证（F4）
- 专项 `test_rvc_artifact_evaluation` → **17 / OK**
- 相关（artifact + expressive + voice_naturalness + rvc_worker_lifecycle + stream_playback + voice_session）→ **153 / OK**（`PYTHONUTF8=1`）
- 全量 `unittest discover -s tests` → **773 / OK (skipped=4)**（`PYTHONUTF8=1`）
- `compileall`（`src`、`tests`、`diagnose_rvc_artifacts.py`）→ **exit=0**
- `git diff --check` → **exit=0**（仅 CRLF 提示）
- `py diagnose_rvc_artifacts.py` dry-run → **exit=0**

---

## 2026-10-17 RVC 伪影评估 Round 1 返修 F1/F2/F3——停止，等待复验

- **评审**：`docs/codex-review-rvc-artifact-evaluation-round1.md`
- **状态**：F1 矩阵、F2 Worker 生命周期、F3 publishing 清理均已改；**停止等待 Codex 复验**
- **未运行**：真实 `--execute` / RVC Worker / GPU / 主程序 / Ollama / CosyVoice / 麦克风 / 扬声器；未读 `data/memory`；未提交、未推送

### F1（矩阵 + 参数说明）
运行矩阵四组，`filter_radius` 固定为 1：`idx025_p050`、`idx055_p033`、`idx025_p033`、`idx000_p050`。基线只读 `idx055_fr1`（protect=0.5）。修正 `config/config.yaml`、`src/voice/rvc/vc_params.py`、`scripts/rvc_{worker,infer}.py`、`docs/VOICE.md` 中 protect（0.5 关闭、更小更保护）与 filter_radius（仅 Harvest）说明；**数值未改**。

### F2（Worker 停止失败 fail-closed）
`RVCWorkerClient.stop()` 仅在 `poll` 确认退出后清空 `_proc` 并关管道；仍存活则保留句柄、`last_stop_exited=False`、可重试 `stop`。`get_rvc_worker()` / `_start_locked()` 在旧进程未退出时拒绝重建或重复启动。`stop_rvc_worker()` 仅在确认退出后清全局单例。convert/warmup 超时终止同样不丢失仍存活的句柄。生命周期专项新增可观察/可重试/禁止重建/外部退出后释放句柄用例；`test_security_round3a_review_fixes.F17` interrupt mock 与成功终止对齐。

### F3
`_publish_profile_files()` 在 `copy2` 前把 `.publishing` 列入 scratch（上轮已交付）。

### 修改文件
- `src/voice/rvc/worker.py`、`src/voice/rvc/infer.py`
- `config/config.yaml`、`src/voice/rvc/vc_params.py`、`scripts/rvc_worker.py`、`scripts/rvc_infer.py`、`docs/VOICE.md`
- `tests/test_rvc_worker_lifecycle.py`、`tests/test_security_round3a_review_fixes.py`（F17 mock）
- `docs/cursor-handoff-status.md`

### 离线验证
- 专项 `test_rvc_artifact_evaluation` → **17 / OK**
- 相关（artifact + expressive + voice_naturalness + rvc_worker_lifecycle + stream_playback + voice_session）→ **148 / OK**
- 全量 `unittest discover -s tests` → **768 / OK (skipped=4)**
- `compileall`（`src`、`tests`、`diagnose_rvc_artifacts.py`）→ **exit=0**
- `git diff --check` → **exit=0**（仅 CRLF 提示）
- `py diagnose_rvc_artifacts.py` dry-run → **exit=0**

### 剩余风险
- **真实 `--execute` 矩阵仍禁止由 Cursor 执行**；需在 GPU 上确认 stop 失败边界与串行 profile 停机计时。

---

## 2026-10-16 RVC 伪影参数隔离评估 Round 1——完成，等待 Codex Review

- **任务**：`docs/cursor-task-rvc-artifact-evaluation-round1.md`
- **ADR**：`docs/architecture/adr-rvc-artifact-evaluation.md`
- **状态**：Round 1 已实现；**停止等待 Codex Review**；未进入其它功能
- **未运行**：真实 `--execute` / RVC Worker / GPU；未启动主程序、Ollama、CosyVoice、麦克风、扬声器、浏览器；未读改删 `data/memory`；未改 `config/config.yaml`；未提交、未推送

### 实现要点
1. `diagnose_rvc_artifacts.py` 默认 dry-run：零 mkdir、零写文件、零模型/子进程/设备。仅 `--execute` 跑固定六段 CosyVoice base。
2. 基线 `idx055_fr1` 只读引用上一轮 `*_rvc.wav`，不重跑、不删除。新增四组严格串行：`idx025_fr1`、`idx055_fr3`、`idx025_fr3`、`idx000_fr3`。
3. 其它参数锁死为 `pitch=0`、`protect=0.5`、`rms_mix_rate=0.68`、`f0_method=rmvpe`；后处理沿用已封存配置；`fallback_on_worker_failure` 强制关闭。bool/NaN/越界不能进入矩阵。
4. 持久 base 先复制到本次 staging 再 `adopt_existing`。RVC 走 `warm_rvc_worker` / `RVCConverter`，不直接启动 `scripts/rvc_worker.py` 或 `rvc_infer.py`。
5. 每组六段后 `shutdown(stop_rvc=True)`，`stop_rvc_worker()` 确认进程退出后才构建下一组。不构造 `TTSEngine`，避免 `AudioPlayer` 打开扬声器。
6. `complete=true` 仅在四组完成、staging 已清理、Worker 已停止后原子发布。失败报告只有固定阶段、label 和 profile id。

### 修改文件
- `diagnose_rvc_artifacts.py` — 固定 profile 诊断入口
- `src/voice/rvc/worker.py` — `stop()` 返回进程是否已退出
- `src/voice/rvc/infer.py` — `stop_rvc_worker()` 返回退出确认；`rvc_worker_running()`
- `tests/test_rvc_artifact_evaluation.py` — dry-run、矩阵、串行、所有权与失败路径
- `tests/test_rvc_worker_lifecycle.py` — 进程仍存活时 `stop()` 返回 False
- `docs/architecture/adr-rvc-artifact-evaluation.md` — 本轮决策
- `docs/cursor-handoff-status.md` — 本条交接

### 离线验证（真实退出码）
- 专项 `tests.test_rvc_artifact_evaluation` → **16 tests / OK**，**exit=0**（含在下方相关复跑内）
- 相关（artifact + expressive + voice_naturalness + rvc_worker_lifecycle + stream_playback + voice_session）→ **142 tests / OK**，**exit=0**
- 全量 `unittest discover -s tests -q` → **762 tests / OK (skipped=4)**，**exit=0**
- `compileall`（`src`、`tests`、`diagnose_rvc_artifacts.py`）→ **exit=0**
- `git diff --check` → **exit=0**（仅既有 CRLF 提示，无空白错误）
- `py diagnose_rvc_artifacts.py` dry-run → **exit=0**

### 剩余风险
- **真实 `--execute` 待 Codex 复验后运行**。离线 fake 不等于电流音听感，也不能代替 GPU 上的 Worker 退出计时。
- `filter_radius` 在 Worker 启动参数中，所以每组 profile 都要完整停机再启动；这是串行约束，不是生产默认值变更。

---

## 2026-10-05 本地表达型 TTS Round 1——Codex 最终工程复验通过

- **状态**：F1–F9 关闭；真实 CosyVoice → Nahida RVC 6/6、退出码 0；等待用户人工 A/B 听感决定是否进入生产集成
- **独立验证**：专项 26 tests / OK；compileall、diff-check、dry-run OK；真实报告 `complete=true`
- **真实性能**：总计 64.322 s；CosyVoice CUDA peak allocated/reserved 1935.1/3118.0 MB；RVC 逐段约 1.1–1.5 s
- **回收**：无 `_staging_*`、无残留 Python 进程、GPU 显存回到运行前约 1.48 GB
- **边界**：未启动主程序/麦克风/扬声器，未读改删 `data/memory`；未提交、未推送；生产默认 TTS 未切换
- **产物**：`data/test/voice-naturalness/cosyvoice-instruct/` 中 6 对 base/RVC WAV 与 `report.json`

---

## 2026-10-05 本地表达型 TTS Round 1——F9 返修完成，等待最终复验

- **任务**：`docs/cursor-task-expressive-local-tts-evaluation-round1.md`
- **评审**：`docs/codex-review-expressive-local-tts-round1.md`（F9 CHANGES REQUESTED → 已修复）
- **ADR**：`docs/architecture/adr-expressive-local-tts-evaluation.md`
- **状态**：F9 已修复；**停止等待 Codex 最终复验**；未进入其它功能
- **未运行**：真实 `--execute` / CosyVoice / RVC / 主程序 / 设备；未读改删 `data/memory`；未提交、未推送

### F9 对照
1. worker 报告校验成功后，对 staging `src_base` 调用 `temp_files.adopt_existing`，将返回的规范化路径直接交给 `apply_rvc_model`
2. RVC 成功后再分别复制 staging base 与 RVC 临时输出到最终 `dest_base` / `dest_rvc`；最终 A/B **不**长期登记给 TempFileManager
3. staging 清理仍删除已登记 `src_base`；`temp_files.shutdown` 回收 RVC 临时输出，不删最终 A/B
4. **未**削弱 `assert_rvc_io_paths` / SafetyPolicy；未登记输入仍 fail-closed
5. 回归：传给 RVC 的是已登记 staging 输入；未登记拒绝；成功后最终存在且 staging/RVC 临时回收；adopt/copy/RVC 失败 → 非零、非 complete、锁释放

### 修改文件
- `diagnose_expressive_tts.py` — F9：adopt staging → RVC → 再复制最终 A/B
- `tests/test_expressive_tts_diagnostic.py` — F9 所有权/失败路径回归；FakeTTS 模拟输入登记门禁
- `docs/cursor-handoff-status.md` — 本条交接

### 离线验证（真实退出码）
- 专项 `tests.test_expressive_tts_diagnostic` → **26 tests / OK**，**exit=0**
- 相关（expressive + voice_naturalness + rvc_worker_lifecycle + stream_playback + voice_session）→ **125 tests / OK**，**exit=0**
- 全量 → **745 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**
- `py diagnose_expressive_tts.py` dry-run → **exit=0**

### 剩余风险
- **真实 `--execute` 待 Codex 最终复验后运行**；离线 fake ≠ 听感/显存；此前真实链路已证明 CosyVoice 6/6 与 RVC 预热，本修针对首段输入未登记导致的 fail-closed

---

## 2026-10-05 本地表达型 TTS Round 1——F8 返修完成，等待最终复验

- **任务**：`docs/cursor-task-expressive-local-tts-evaluation-round1.md`
- **评审**：`docs/codex-review-expressive-local-tts-round1.md`（F8 CHANGES REQUESTED → 已修复）
- **ADR**：`docs/architecture/adr-expressive-local-tts-evaluation.md`
- **状态**：F8 已修复；由上方 F9 条目承接
- **未运行**：真实 `--execute` / CosyVoice / RVC / 主程序 / 设备；未读改删 `data/memory`；未提交、未推送

### F8 对照
1. `staging_entry_allowed` 精确允许 `worker_report.json.tmp`（不扩成任意 `*.json.tmp`）
2. 该半成品可经 TempFileManager 删除并移除 staging；未知 `.tmp` / `.json` 仍 fail-closed

### 离线验证（真实退出码）
- 专项 `tests.test_expressive_tts_diagnostic` → **22 tests / OK**，**exit=0**
- 相关 → **121 tests / OK**，**exit=0**
- 全量 → **741 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**
- `py diagnose_expressive_tts.py` dry-run → **exit=0**

### 剩余风险
- **真实 `--execute` 待 Codex 最终复验后运行**；离线 fake ≠ 听感/显存

---

## 2026-10-04 本地表达型 TTS Round 1——F7 返修完成，等待最终复验

- **任务**：`docs/cursor-task-expressive-local-tts-evaluation-round1.md`
- **评审**：`docs/codex-review-expressive-local-tts-round1.md`（F7 CHANGES REQUESTED → 已修复）
- **ADR**：`docs/architecture/adr-expressive-local-tts-evaluation.md`
- **状态**：F7 已修复；由上方 F8 条目承接
- **未运行**：真实 `--execute` / CosyVoice / RVC / 主程序 / 设备；未读改删 `data/memory`；未提交、未推送

### F7 对照
1. 六组 RVC 成功后、发布 `complete=true` 前，显式 `cleanup_owned_staging` 并确认完成
2. 清理失败 → `complete=false`、`failure_reason=staging_cleanup`、返回非零；不把成功路径清理异常吞成 debug 成功
3. `finally` 仅对未清理 staging 做失败路径尽力回收；正常成功只清理一次
4. 回归：cleanup 强制失败 → code≠0、非 complete、锁释放、RVC 恰 6 次；正常成功 cleanup 一次且 return 0

### 离线验证（真实退出码）
- 专项 → **21 tests / OK**；相关 **120**；全量 **740 (skipped=4)**；compileall / diff-check / dry-run → exit 0

---

## 2026-10-04 本地表达型 TTS Round 1——F1–F6 返修完成，等待最终复验

- **任务**：`docs/cursor-task-expressive-local-tts-evaluation-round1.md`
- **评审**：`docs/codex-review-expressive-local-tts-round1.md`（CHANGES REQUESTED → 已按 F1–F6 修复）
- **ADR**：`docs/architecture/adr-expressive-local-tts-evaluation.md`
- **状态**：F1–F6 已修复；由上方 F7 条目承接
- **未运行**：真实 `--execute` / CosyVoice / RVC / 主程序 / 设备；未读改删 `data/memory`；未提交、未推送

### F1–F6 对照
1. **F1**：`_atomic_write_wav` 显式 `format="WAV"` + `PCM_16`；写后 `sf.info` 校验再 replace；真实 soundfile 探针测试
2. **F2**：`build_worker_env` allowlist；注入哨兵密钥断言不传播
3. **F3**：worker `stderr=DEVNULL`（不再 PIPE 不读）
4. **F4**：跨进程单实例锁（忙则 fail-closed）；每次唯一 `_staging_<pid>_<hex>`；只清理本次目录；不允许 `ignore_errors` 吞失败
5. **F5**：加载前实测 CosyVoice/Matcha/WeText HEAD、模型 revision 与固定文件清单 bytes；报告 expected/actual
6. **F6**：有限非负数、bytes==stat、WAV 可读；坏值矩阵测试

### 离线验证（真实退出码）
- 专项 → **20 tests / OK**；相关 **119**；全量 **739 (skipped=4)**；compileall / diff-check / dry-run → exit 0

---

## 2026-10-04 本地表达型 TTS 评估 Round 1——完成，等待 Codex Review

- **任务**：`docs/cursor-task-expressive-local-tts-evaluation-round1.md`
- **ADR**：`docs/architecture/adr-expressive-local-tts-evaluation.md`
- **状态**：Round 1 已实现；由上方 F1–F6 返修条目承接
- **未运行**：真实 `--execute` / CosyVoice 加载 / RVC / 主程序 / 麦克风 / 扬声器；未读改删 `data/memory`；未提交、未推送

### 修改文件
- `diagnose_expressive_tts.py` — 父诊断入口（默认 dry-run；`--execute` 串行 worker→RVC）
- `scripts/cosyvoice_eval_suite.py` — 固定样本与版本常量（stdlib）
- `scripts/cosyvoice_eval_worker.py` — CosyVoice 窄 worker（本地 WeText、单次加载、原子报告）
- `src/voice/naturalness_helpers.py` — 抽取 purge / 非空 / TrustedRvc 封存
- `test_rvc_conversion.py` — 改为复用 helper（core/extended 兼容）
- `tests/test_expressive_tts_diagnostic.py` — 离线契约测试
- `docs/architecture/adr-expressive-local-tts-evaluation.md` — 更新为已固定依赖与串行边界

### 离线验证（真实退出码）
- 专项 → **13 tests / OK**；相关 **112**；全量 **732 (skipped=4)**；compileall / diff-check / dry-run → exit 0

---

## 2026-10-02 语音自然度扩展状态试听集——Codex 最终复验 PASS

- **评审**：`docs/codex-review-voice-naturalness-extended-suite.md`
- **状态**：`--suite extended` 已完成真实 Edge→Nahida RVC **10/10**，输出十组 base/RVC 共 20 个非空文件
- **独立验证**：专项 **27 / OK**；相关 **220 / OK**；全量 **719 / OK (skipped=4)**；compileall、diff-check、core/extended dry-run 均 exit 0
- **客观范围**：RVC 基频跨度 **5.87–11.49 半音**，有效响度跨度 **10.11–15.25 dB**；未观察到音高塌缩
- **资源**：RVC Worker 正常停止，无残留 Python 进程
- **边界**：未启动主程序/Ollama/麦克风/扬声器；未读改删 `data/memory`；未提交、未推送

---

## 2026-10-02 语音自然度扩展状态试听集——完成，等待 Codex Review

- **任务**：`docs/cursor-task-voice-naturalness-extended-suite.md`
- **ADR**：`docs/architecture/adr-edge-sentence-prosody.md`
- **状态**：扩展 suite 已实现；**停止等待 Codex Review**；未进入其它功能
- **未运行**：真实 `--execute` / Edge / RVC / 主程序 / 设备；未读改删 `data/memory`；未提交、未推送

### 对照
1. `--suite {core,extended}`，默认 `core`；core 四组与输出目录/文件名不变
2. `extended` 十组固定样本 → `data/test/voice-naturalness/extended/`；不覆盖 core
3. 复用同一 `run_execute` / purge fail-closed / SafetyPolicy；进度与成功判定按 suite 长度
4. dry-run 打印 suite、计划目录与有效 rate/pitch/volume；零网络/RVC/目录创建

### 离线验证（真实退出码）
- 专项 `tests.test_voice_naturalness_round1` → **27 tests / OK**，**exit=0**
- 相关 → **220 tests / OK**，**exit=0**
- 全量 → **719 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**
- `py test_rvc_conversion.py`（core dry-run）→ **exit=0**
- `py test_rvc_conversion.py --suite extended`（dry-run）→ **exit=0**

### 剩余风险
- **真实 `--execute --suite extended` 待 Codex 复验后再运行**；离线 fake ≠ 听感

---

## 2026-10-02 语音自然度 Round 1——Codex 最终复验 PASS

- **状态**：Round 1 与 F1–F3 返修全部关闭；真实 Edge→CUDA→Nahida RVC A/B **4/4 成功**
- **Codex 独立离线验证**：专项 **22 tests / OK**；相关 **215 / OK**；全量 **714 / OK (skipped=4)**；`compileall`、`git diff --check`、dry-run 均 exit 0
- **真实链路**：四组 base/RVC 文件已写入 `data/test/voice-naturalness/`；RVC 单段推理约 1.0–1.5 秒；Worker 正常停止，无残留 Python 进程
- **客观韵律**：四组 base→RVC 基频跨度（半音）`8.64→8.30`、`8.07→8.14`、`11.60→11.92`、`5.47→5.83`，未观察到 RVC 压平句级抑扬
- **边界**：未启动主程序/Ollama/麦克风/扬声器；未读改删 `data/memory`；未提交、未推送

---

## 2026-10-02 语音自然度 Round 1——Codex F3 purge fail-closed 完成，等待最终复验

- **任务**：`docs/cursor-task-voice-naturalness-round1.md`
- **评审**：`docs/codex-review-voice-naturalness-round1.md`（F3 CHANGES REQUESTED → 已修复）
- **ADR**：`docs/architecture/adr-edge-sentence-prosody.md`
- **状态**：F3 已修复；**停止等待 Codex 最终复验**；未进入其它功能
- **未运行**：真实 `--execute`/edge/RVC/主程序/设备；未读改删 `data/memory`；未提交、未推送

### F3 对照
1. `_purge_sample_artifacts` 不再吞异常；adopt/remove/删除后仍存在一律抛出
2. 含 symlink/异常路径；不直接 unlink；不削弱 TempFileManager/SafetyPolicy
3. 离线：旧非空产物 + fake adopt 拒绝 / remove 未删净 → 零 save_to_file/转码/RVC，返回非零，shutdown 一次

### 离线验证（真实退出码）
- 专项 `tests.test_voice_naturalness_round1` → **22 tests / OK**，**exit=0**
- 相关 → **215 tests / OK**，**exit=0**
- 全量 → **714 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**
- `py test_rvc_conversion.py` dry-run → **exit=0**（**未**执行真实 `--execute`）

### 剩余风险
- **真实 `--execute` 待 Codex 复验后再运行**；离线 fake ≠ 听感

## 2026-10-02 语音自然度 Round 1——Codex 真实链路复验 F2 完成，等待最终复验

- **任务**：`docs/cursor-task-voice-naturalness-round1.md`
- **评审**：`docs/codex-review-voice-naturalness-round1.md`（F2 CHANGES REQUESTED → 已修复）
- **ADR**：`docs/architecture/adr-edge-sentence-prosody.md`
- **状态**：F2 fail-closed 已实现；**停止等待 Codex 最终复验**；未进入其它功能
- **未运行**：`main.py`、真实 edge/RVC/`--execute`、Ollama、麦克风、扬声器；未读/未改/未删 `data/memory`；未提交、未推送

### F2 对照
1. 每样本合成前经 SafetyPolicy/TempFileManager 清理旧 `*_base`/`*_rvc`
2. `save_to_file` Edge 失败清理部分文件并重新抛出；保留 PermissionError→pyttsx3 fallback 语义
3. base 须存在且 size>0，否则不进入转码/RVC；任一样本失败立即返回非零并 `shutdown(stop_rvc=True)`
4. 离线矩阵覆盖空/缺失 base、旧产物、转码/RVC 失败与 save_to_file 契约

### 离线验证（真实退出码）
- 专项 `tests.test_voice_naturalness_round1` → **21 tests / OK**，**exit=0**
- 相关 → **214 tests / OK**，**exit=0**
- 全量 → **713 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**
- `py test_rvc_conversion.py` dry-run → **exit=0**（**未**执行真实 `--execute`）

### 剩余风险
- **真实 `--execute` 待 Codex 复验后再由人工/Codex 运行**；离线 fake ≠ 听感

---

## 2026-10-02 语音自然度 Round 1——Codex F1 TrustedRvcProfile 返修完成，等待最终复验

- **任务**：`docs/cursor-task-voice-naturalness-round1.md`
- **评审**：`docs/codex-review-voice-naturalness-round1.md`（CHANGES REQUESTED → 已按 F1 修复）
- **ADR**：`docs/architecture/adr-edge-sentence-prosody.md`
- **状态**：F1 已修复；**停止等待 Codex 最终复验**；未进入其它功能
- **未运行**：`main.py`、真实 edge/RVC/`--execute`、Ollama、麦克风、扬声器、浏览器、角色、清理实扫；未读/未改/未删 `data/memory`；未安装依赖；未提交、未推送

### F1 对照
1. `--execute` 在 health/网络/目录/TTS 前解析解释器、模型与可选 index，构造 `TrustedRvcProfile`，经 `SafetyPolicy.from_app_config(..., trusted_rvc_profile=)` 一次性封存
2. 同一 policy 传给 `check_rvc_health` 与 `TTSEngine`；有效 rvc_config 与封存资产一致（缺失 index 不封存也不下传）
3. 缺解释器/模型：零 probe、零 TTS、零目录创建；health 拒绝：零 TTS、零目录创建
4. fake execute 契约矩阵已覆盖；dry-run 仍不封存/探测/建目录

### 离线验证（真实退出码）
- 专项 `tests.test_voice_naturalness_round1` → **16 tests / OK**，**exit=0**
- 相关（naturalness + stream_playback + voice_session + rvc_worker_lifecycle + security_round3a_review_fixes + mode_assembly_round2 + companion R1/R2）→ **209 tests / OK**，**exit=0**
- 全量 → **708 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**
- `py test_rvc_conversion.py`（无参数 dry-run）→ **exit=0**（**未**执行真实 `--execute`）

### 剩余风险
- 离线 fake ≠ 真实听感；**真实 `--execute` 待 Codex 代码复验通过后再由人工运行**

---

## 2026-10-02 语音自然度 Round 1——完成，等待 Codex Review

- **任务**：`docs/cursor-task-voice-naturalness-round1.md`
- **ADR**：`docs/architecture/adr-edge-sentence-prosody.md`
- **状态**：F1–F5 已实现；**停止等待 Codex Review**；未进入其它功能
- **未运行**：`main.py`、真实 edge/RVC/`--execute`、Ollama、麦克风、扬声器、浏览器、角色、清理实扫；未读/未改/未删 `data/memory`；未安装依赖；未提交、未推送

### F1–F5 对照
1. **F1**：`src/voice/prosody.py` — 不可变 `EdgeProsody` / `EdgeProsodySettings`；严格带符号格式；clamp；`enabled=false` 中性；句末标点优先级
2. **F2**：`TTSEngine._make_edge_communicate` 供 `_edge_tts_to_mp3` / `save_to_file`；`build_tts_engine` 转发；无 YAML 兼容中性；仓库 YAML 启用温和基线
3. **F3**：`config.yaml` `filter_radius: 1`、`rms_mix_rate: 0.68`；`vc_params` 代码默认仍为 3 / 0.75
4. **F4**：`test_rvc_conversion.py` 默认 dry-run；`--execute` 写 `data/test/voice-naturalness/`；四类固定样本；不自动播放
5. **F5**：`tests/test_voice_naturalness_round1.py` 覆盖解析/句型/出口 kwargs/流水线/打断/assembly/dry-run/RVC 默认

### 离线验证（真实退出码）
- 专项 `tests.test_voice_naturalness_round1` → **12 tests / OK**，**exit=0**
- 相关（naturalness + stream_playback + voice_session + rvc_worker_lifecycle + security_round3a_review_fixes + mode_assembly_round2 + companion R1/R2）→ **205 tests / OK**，**exit=0**
- 全量 → **704 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**
- `py test_rvc_conversion.py`（无参数 dry-run）→ **exit=0**（未执行 `--execute`）

### 剩余风险
- 离线 fake ≠ 真实 edge/RVC 听感；edge-tts 外发朗读文本；人工 A/B 需用户本地 `--execute`

---

## 2026-09-30 真实语音记忆返修——Codex 最终复验通过

- **评审**：`docs/codex-review-natural-companion-memory-soak-fix.md`
- **状态**：F1–F3 全部关闭，离线验收 **PASS**；可进行受控的真实重启记忆验证
- **Codex 独立验证**：边界探针通过；专项 **18 tests / OK**；相关 **243 tests / OK**；全量 **692 tests / OK (skipped=4)**；`compileall` / `git diff --check` exit 0
- **边界**：未启动真实 Ollama/STT/TTS/RVC/设备；未读、改、删 `data/memory`；未提交、未推送

---

## 2026-09-30 真实语音 soak 返修——Codex F1–F3 返修完成，等待最终复验

- **任务**：`docs/cursor-task-natural-companion-memory-soak-fix.md`
- **评审**：`docs/codex-review-natural-companion-memory-soak-fix.md`（CHANGES REQUESTED → 已按 F1–F3 修复）
- **ADR**：`docs/architecture/adr-natural-companion-memory-soak-fix.md`
- **状态**：F1–F3 已修复；**停止等待 Codex 最终复验**；未进入新功能
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、角色、清理实扫；未读/未改/未删 `data/memory`；未安装依赖；未提交、未推送

### F1–F3 对照
1. **F1**：单字中文值（茶/猫）可经严格语法写入；`请记住我喜欢茶` 正例；什么/饮品/疑问/条件/引用等反例零候选；领域词可作 slot 不可作 value
2. **F2**：`generic` / `concrete` / `domain slot` 失败关闭；`我喜欢猫吗` 仅猫；`我喜欢什么饮品` 仅 slot 饮品；无 slot 抹茶不进领域问句；`我想喝抹茶拿铁` 仍可内容命中；混合临时库矩阵已覆盖
3. **F3**：`normalize_ingestion_skip_reason` allowlist；恶意字符串与带副作用 `__str__` 不进日志；固定码 `no_safe_fact_unit` / `eligibility_skip` 仍可见

### 离线验证（真实退出码）
- 专项 `tests.test_memory_soak_fix` → **18 tests / OK**，**exit=0**
- 相关（soak + trusted_boundary + memory_service + memory_integration + companion round1/2）→ **158 tests / OK**，**exit=0**
- 全量 → **692 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**

### 剩余风险
- 离线契约通过 ≠ 真实语音 soak 已复验；未触碰真实 SQLite

---

## 2026-09-30 真实语音 soak 返修——偏好写入与跨领域召回——完成，等待 Codex Review

- **任务**：`docs/cursor-task-natural-companion-memory-soak-fix.md`
- **ADR**：`docs/architecture/adr-natural-companion-memory-soak-fix.md`
- **状态**：首轮 F1–F3 已实现；由上方 Codex 返修条目承接
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、角色、清理实扫；未读/未改/未删 `data/memory`；未安装依赖；未提交、未推送

### F1–F3 对照
1. **F1**：白名单「记住」句首前缀 + 槽位句式；原文 span 不变；污染/歧义零候选
2. **F2**：内容词法特征门槛；冰激凌问句不命中抹茶；饮品/广义偏好与英文召回契约保留
3. **F3**：`processed` 日志含 `accepted` 与可选计数；`reason=no_safe_fact_unit`；无正文泄漏

### 离线验证（真实退出码）
- 专项 `tests.test_memory_soak_fix` → **12 tests / OK**，**exit=0**
- 相关（soak + trusted_boundary + memory_service + memory_integration + companion round1/2）→ **152 tests / OK**，**exit=0**
- 全量 → **686 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**

### 剩余风险
- 离线契约通过 ≠ 真实语音 soak 已复验；未触碰真实 SQLite

---

## 2026-09-30 自然陪伴里程碑——Codex 评审通过

- **评审**：`docs/codex-review-natural-companion-milestone.md`
- **人工验收**：`docs/natural-companion-live-voice-soak.md`
- **状态**：自然陪伴 Round 1/2 开发与离线评审闭环，里程碑 **PASS**；当前开发任务完成
- **最终验证**：`674 tests / OK（skipped=4）`；`compileall` / `git diff --check` exit 0
- **边界**：真实 Nahida/RVC、Ollama、麦克风与扬声器尚未启动，留待用户执行 30–60 分钟真实语音 soak；未读写 `data/memory`；未提交、未推送

---

## 2026-09-30 自然陪伴 Round 2——Codex 最终复验通过

- **评审**：`docs/codex-review-natural-companion-round2.md`
- **状态**：F1–F8 全部关闭；Round 2 最终离线验收 **PASS**；可进入自然陪伴里程碑评审
- **Codex 独立验证**：专项 **46 tests / OK**；相关 **123 tests / OK**；全量 **674 tests / OK（skipped=4）**；`compileall` / `git diff --check` exit 0
- **复杂度**：15k URL 逐字符 feed 约 **0.018s**，`scan_steps=15012`；1k→8k 扫描步数约 7.92 倍，保持线性
- **边界**：未运行真实 Ollama/TTS/RVC/麦克风/扬声器；真实自然度留待 30–60 分钟人工语音 soak；未读写 `data/memory`；未提交、未推送

---

## 2026-09-30 自然陪伴 Round 2 Codex Review 返修（F7–F8）——完成，等待最终复验

- **评审入口**：`docs/codex-review-natural-companion-round2.md` 末尾「Codex Review Round 3：F7–F8」
- **ADR**：`docs/architecture/adr-natural-companion-speech-projection.md`
- **状态**：F7–F8 已修复；**停止等待 Codex 最终复验**；未进入新功能
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、角色、清理实扫；未读/未改/未删 `data/memory`；未安装依赖；未提交、未推送

### 复杂度探针（修复后）
- 命令：`py -3.12 -X utf8 .cursor-agent-tmp/codex_probe_speech_f78.py`
- 逐字符裸 URL：`scan_steps` 1k=1021 / 8k=8021（ratio≈7.86）；15k 墙钟≈0.017s（修复前约 16s 平方）
- F8：`见 https://example.com。谢谢` → `见 链接已显示。谢谢`（无 `。。`）；隐私 sentinel 仍为零

### F7–F8 对照
1. **F7**：`token kind` + `progress` 增量续扫；裸 URL/MD/inline code 不从 hold 起点重扫；`scan_steps` 线性断言；1/7/500 字 chunk 等价与隐私保留
2. **F8**：固定提示与源句读 `。.!！?？,，;；:：` 边界合并；问号/叹号优先；跨 chunk 覆盖

### 离线验证（真实退出码）
- 探针 → **exit=0**
- 专项 → **46 tests / OK**，**exit=0**
- 相关（round2+stream_playback+voice_session+round1）→ **123 tests / OK**，**exit=0**
- 全量 → **674 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**

### 剩余风险
- 离线投影契约通过 ≠ 真实语音自然度已验收

---

## 2026-09-30 自然陪伴 Round 2 Codex Review 返修（F4–F6）——完成，等待 Codex 复验

- **评审入口**：`docs/codex-review-natural-companion-round2.md` 末尾「Codex Review Round 2：F4–F6」
- **ADR**：`docs/architecture/adr-natural-companion-speech-projection.md`
- **状态**：F4–F6 已修复；由上方 F7–F8 条目承接
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、角色、清理实扫；未读/未改/未删 `data/memory`；未安装依赖；未提交、未推送

### 探针复现（修复前 → 后）
- 命令：`py -3.12 -X utf8 .cursor-agent-tmp/codex_probe_speech_round2.py`
- 修复前：`SECRET_*` 出现于 upper/angle/overflow/incremental；`谢谢` 丢失
- 修复后：全部 `secret=False`；`cjk thanks=True`；overflow 仅 `前 链接已显示。后`

### F4–F6 对照
1. **F4**：hold 上限与输入上限对齐；先投影再判溢出；溢出 quarantine 整段丢弃至终止符，禁止截前缀重解析；覆盖单次/增量/恰好边界/边界+1/输入中途耗尽
2. **F5**：scheme 大小写不敏感；angle destination 消费 title + 外层 `)`；引号 title 内 `)` 不提前结束；逐字符/切分点反例
3. **F6**：裸 URL 遇 `，。！？；：、）】》」』` 终止并保留后文；MD 目标内 Unicode 路径仍私有

### 离线验证（真实退出码）
- 探针 → **exit=0**（sentinel 为零，`谢谢` 保留）
- 专项 → **38 tests / OK**，**exit=0**
- 相关（round2+stream_playback+voice_session+round1）→ **115 tests / OK**，**exit=0**
- 全量 → **666 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**

### 剩余风险
- 离线投影契约通过 ≠ 真实语音自然度已验收

---

## 2026-09-30 自然陪伴 Round 2 Codex Review 返修（F1–F3）——完成，等待 Codex 复验

- **评审入口**：`docs/codex-review-natural-companion-round2.md`
- **ADR**：`docs/architecture/adr-natural-companion-speech-projection.md`
- **状态**：F1–F3 已修复；由上方 F4–F6 条目承接
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、角色、清理实扫；未读/未改/未删 `data/memory`；未安装依赖；未提交、未推送

### F1–F3 对照
1. **F1**：Markdown/裸 URL 改为线性平衡括号扫描；长 URL 整段消费不回吐；未闭合目标 finish 时丢弃
2. **F2**：增量 hold URL/`![`/链接/inline code/围栏；逐字符与切分点与完整投影语义等价
3. **F3**：`_apply_outcome_side_effects` / `speak` 先投影；空则不建线程；tracked 空投影 accepted=0

### 离线验证（真实退出码）
- 专项 → **22 tests / OK**，**exit=0**
- 相关 → **99 tests / OK**，**exit=0**
- 全量 → **650 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**

### 剩余风险
- 离线投影契约通过 ≠ 真实语音自然度已验收

---

## 2026-09-30 自然陪伴 Round 2——朗读投影——完成，等待 Codex Review

- **任务**：`docs/cursor-task-natural-companion-round2.md`
- **ADR**：`docs/architecture/adr-natural-companion-speech-projection.md`
- **状态**：Round 2 实现已完成；由上方 F1–F3 返修条目承接

---

## 2026-09-30 自然陪伴 Round 1——Codex 最终复验通过

- **评审入口**：`docs/codex-review-natural-companion-round1.md`
- **状态**：统一人格提示边界与 F1–F2 全部通过；可进入自然陪伴 Round 2
- **Codex 独立验证**：专项 **24 tests / OK**；全量 **628 tests / OK（skipped=4）**；`compileall` / `git diff --check` exit 0
- **边界**：未运行真实模型/语音设备，真实自然度尚未验收；未触碰 `data/memory`；未提交、未推送

---

## 2026-09-30 自然陪伴 Round 1 Codex Review 返修（F1–F2）——完成，等待 Codex 复验

- **评审入口**：`docs/codex-review-natural-companion-round1.md`
- **ADR**：`docs/architecture/adr-natural-companion-persona-prompt.md`
- **状态**：F1–F2 已修复；**停止等待 Codex 复验**；**未进入 Round 2**
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、角色、清理实扫；未读/未改/未删 `data/memory`；未安装依赖；未提交、未推送

### F1–F2 对照
1. **F1**：`COMPANION_DYNAMIC_SEPARATOR` 改为附加 system 与基础人格共同生效；仅明确标为数据的正文不可当指令；不再声称整段动态 system 不可执行
2. **F2**：`normalize_base_system` 于 `OllamaClient` 构造期强制 `str` + ≤2400；`None`/`""` 关闭；拒绝 bool/bytes/容器/超长；错误不含提示正文；不改每请求动态 `system`

### 离线验证（真实退出码）
- 专项 → **24 tests / OK**，**exit=0**
- 相关 → **112 tests / OK**，**exit=0**
- 全量 → **628 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**

### 剩余风险
- 离线提示契约通过 ≠ 真实模型自然度已验收；未进入 Round 2

---

## 2026-09-30 自然陪伴 Round 1——统一人格提示边界——完成，等待 Codex Review

- **任务**：`docs/cursor-task-natural-companion-round1.md`
- **ADR**：`docs/architecture/adr-natural-companion-persona-prompt.md`
- **状态**：Round 1 实现已完成；由上方 F1–F2 返修条目承接

---

## 2026-09-30 安全 Round 3 里程碑——Codex 最终复验通过

- **评审入口**：`docs/codex-review-security-round3-milestone.md`
- **状态**：3A / 3B / 3C1 / 3C2 与里程碑 F1–F2 全部通过；下一阶段可进入自然陪伴体验
- **关键收口**：前台预热等待默认 5 秒/硬上限 10 秒；资源协调独立有界且不与 RVC 抢 GPU；原始异常正文不进入持久日志
- **Codex 独立验证**：里程碑专项 **16 tests / OK**；全量 **604 tests / OK（skipped=4）**；`compileall` / `git diff --check` exit 0
- **边界**：未运行真实服务/设备，未触碰 `data/memory`，未提交、未推送

---

## 2026-09-30 Round 3C2 Codex Review Round 2/3 返修（F6–F7）——完成，等待 Codex 复验

- **评审入口**：`docs/codex-review-security-round3-c2.md`（Round 2 新增 F6–F7）
- **ADR**：`docs/architecture/adr-sqlite-memory-privacy-boundary.md`
- **状态**：已修复 F6–F7；F1–F5 保持；**停止等待 Codex 复验**；**未进入自然陪伴**
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、清理实扫；未读/未改/未删 `data/memory`；未安装依赖；未提交、未推送

### F6–F7 对照
1. **F6**：新库 exclusive 预建于可信父目录；URI `mode=rw&cache=private`（非 rwc）；connect 前复验；父目录换 junction 时外部无 `race.sqlite3`/sidecar，既有外部同名 sentinel 不变
2. **F7**：`_transaction` 将 COMMIT 纳入同一 BaseException guard；失败先 rollback；`in_transaction=False`；不吞 KI/SystemExit

### 离线验证（真实退出码）
- 专项 → **70 tests / OK (skipped=2)**，**exit=0**
- 相关 → **239 tests / OK (skipped=4)**，**exit=0**
- 全量 → **588 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**

### 剩余风险
- 记忆库仍为本机明文；标准库 TOCTOU / 同用户攻击面未消除；真实 `data/memory` 未测；自然陪伴未做

---

## 2026-09-30 Round 3C2 Codex Review Round 1 返修（F1–F5）——完成，等待 Codex 复验

- **状态**：F1–F5 已修复；由上方 F6–F7 返修条目承接

### F1–F5 对照
1. **F1**：connect 后、任何 PRAGMA 前首次复验；硬化后再复验；硬链接替换时 sentinel 不变且无写性 PRAGMA
2. **F2**：逐段创建父目录；祖先 junction/symlink 在创建/连接前拒绝；外部零目录零文件
3. **F3**：绑定并持续比较数据库文件 `(st_dev, st_ino)`；同父普通文件身份替换拒绝
4. **F4**：计划删除 `BEGIN` 后任意 `BaseException` 统一 rollback；`in_transaction=False`；不吞 KI/SystemExit
5. **F5**：运行时路径/身份失效统一 `MemoryStoreUnavailableError`；CRUD/VACUUM 在 SQL 副作用前拒绝

### 离线验证（真实退出码）
- 专项（c2 + id_audit + memory_store + storage_bounds）→ **66 tests / OK (skipped=2)**，**exit=0**
- 相关（含 3A/3B/3C1 + memory）→ **235 tests / OK (skipped=4)**，**exit=0**
- 全量 → **584 tests / OK (skipped=4)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**
- 探针：`pragma-before-revalidate False`、`outside-unchanged True`、`ancestor-junction-outside-created False`、`transaction-still-open False`，**exit=0**

### 剩余风险
- 记忆库仍为本机明文；sidecar 新文件受 VFS 限制；真实 `data/memory` 未测；自然陪伴未做

---

## 2026-09-30 Round 3C2 SQLite 记忆库与诊断计划隐私边界——完成，等待 Codex Review

- **状态**：Round 1 实现已完成；由上方 F1–F5 返修条目承接

### Round 1 对照
1. **路径边界**：`src/memory/sqlite_security.py`；安全文件名与后缀；拒绝目录/symlink/junction/硬链接；父目录身份绑定；写前复验；sidecar 校验
2. **连接硬化**：foreign_keys / trusted_schema / temp_store / journal_mode=DELETE / secure_delete；禁扩展；ATTACH limit 0；只读 `mode=ro&cache=private` + query_only；busy ≤5s
3. **计划 v2**：无 text/路径；nonce 行指纹 + db_fingerprint；CLI `--db` 为准；O_EXCL 写计划；v1 拒绝

### 离线验证（真实退出码）
- 专项（c2 + id_audit + memory_store + storage_bounds）→ **59 tests / OK (skipped=2)**，**exit=0**
- 相关（含 3A/3B/3C1 + memory_integration）→ **228 tests / OK (skipped=4)**，**exit=0**
- 全量 `unittest discover -s tests -q` → **577 tests / OK (skipped=4)**，**exit=0**
- `compileall -q src tests diagnose_memory.py diagnose_memory_ids.py diagnose_resource_soak.py` → **exit=0**
- `git diff --check` → **exit=0**

### 剩余风险
- 记忆库仍为本机明文 SQLite；sidecar 新文件受 VFS 限制；真实 `data/memory` 未测；自然陪伴未做

---

## 2026-09-30 Round 3C1 Codex 最终复验通过；Round 5 F13 为最后一轮返修

- **评审入口**：`docs/codex-review-security-round3-c1.md`
- **状态**：Codex 最终复验通过；由 Round 3C2 条目承接后续安全板块
- **未进入**：自然陪伴

---

## 2026-09-30 Round 3C1 Codex Review Round 5 返修（F13）——完成，等待 Codex 复验

- **评审入口**：`docs/codex-review-security-round3-c1.md`（Round 5：Finding 13）
- **ADR**：`docs/architecture/adr-logging-privacy-file-boundary.md`
- **状态**：已修复 F13（普通截断 `OSError` 不再升为 `LogBoundaryError`）；F12 路径换入边界契约保持；**停止等待 Codex 复验**；**未进入 Round 3C2 / 自然陪伴**
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未安装依赖；未提交、未推送

### F13 对照
1. **F13**：`seek`/`truncate`/`flush`（及同路径截断中的 `fstat`）普通 `OSError` 关闭不确定 stream 后原样抛出；`emit` → `handleError` 吸收，不从 `logger.info` 冒泡；仅路径/根/reparse/硬链接/身份不一致抛 `LogBoundaryError`

### 离线验证（真实退出码）
- 专项 `tests.test_security_round3c_logging` → **51 tests / OK (skipped=2)**，**exit=0**
- 相关 → **313 tests / OK (skipped=2)**，**exit=0**
- 全量 → **561 tests / OK (skipped=2)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**
- 探针 `codex_probe_log_truncate_io_error.py`：`truncate-io-propagated None`、`stream-closed True`，**exit=0**

### 剩余风险
- 日志仍是本机明文；未覆盖全仓库历史日志语句；真实服务链路未测；Round 3C2 未做

---

## 2026-09-30 Round 3C1 Codex Review Round 4 返修（F12）——完成，等待 Codex 复验

- **状态**：F12 已修复；由后续 Round 5 F13 返修条目承接

### F12 对照
1. **F12**：禁止路径 `open(..., "w")`；对已通过 `_open()` 校验的 stream 做 `seek`/`truncate`；截断前后复验 fd/路径/可信根；路径换入外部硬链接时仅原句柄可被截断并 fail closed，外部 sentinel 不变；`_test_pre_truncate_hook` 仅同步

### 离线验证（真实退出码）
- 专项 `tests.test_security_round3c_logging` → **49 tests / OK (skipped=2)**，**exit=0**
- 相关（3a/3b/3c + rvc/voice/stt/generate/ollama/assembly/stream）→ **311 tests / OK (skipped=2)**，**exit=0**
- 全量 `unittest discover -s tests -q` → **559 tests / OK (skipped=2)**，**exit=0**
- `compileall -q src tests` / `git diff --check` → **exit=0**
- 探针 `codex_probe_log_zero_rollover_race.py`：`race-triggered False`（已无路径 `open w`）、`outside-unchanged True`、`outside-bytes 28`，**exit=0**

### 剩余风险
- 日志仍是本机明文；未覆盖全仓库历史日志语句；真实服务链路未测；Round 3C2 未做

---

## 2026-09-30 Round 3C1 Codex Review Round 3 返修（F8–F11）——完成，等待 Codex 复验

- **评审入口**：`docs/codex-review-security-round3-c1.md`（Round 3：F8–F11）
- **ADR**：`docs/architecture/adr-logging-privacy-file-boundary.md`
- **状态**：已修复 F8–F11 并补确定性回归；由后续 Round 4 F12 返修条目承接
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未安装依赖；未提交、未推送

### F8–F11 对照
1. **F8**：`LogBoundaryError`；业务 `ValueError` 不冒泡且 handler 可续写；根替换仍抛边界异常
2. **F9**：`backup_count=0` 同路径截断有界；`max_size_mb_to_bytes` 至少 1（`1e-12`→1）
3. **F10**：`_miku_app_owned`；true→false 关句柄；a→b 原子切换；创建失败保留旧；同路径更新持锁
4. **F11**：打开后比对当前路径 / 打开前 / fd；可控 hook 竞态测试

### 离线验证（真实退出码）
- 专项 → **47 tests / OK (skipped=2)**，**exit=0**
- 相关 → **261 tests / OK (skipped=2)**，**exit=0**
- 全量 → **557 tests / OK (skipped=2)**，**exit=0**
- `compileall` / `git diff --check` → **exit=0**
- 探针：emit 契约 / bounds 重配置 / root swap 均符合预期（exit=0）

### 剩余风险
- 日志仍是本机明文；未覆盖全仓库历史日志语句；真实服务链路未测；Round 3C2 未做

---

## 2026-09-30 Round 3C1 Codex Review Round 2 返修（F6–F7）——完成，等待 Codex 复验

- **评审入口**：`docs/codex-review-security-round3-c1.md`（Round 2：F6–F7）
- **状态**：F6–F7 已修复；由后续 Round 3 F8–F11 返修条目承接

---

## 2026-09-30 Round 3C1 Codex Review 返修（F1–F5）——完成，等待 Codex 复验

- **评审入口**：`docs/codex-review-security-round3-c1.md`（F1–F5）
- **ADR**：`docs/architecture/adr-logging-privacy-file-boundary.md`
- **状态**：F1–F5 已修复；由后续 Round 2 F6–F7 返修条目承接
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未安装依赖；未提交、未推送

---

## 2026-09-30 Round 3C1 持久日志隐私与日志文件边界——完成，等待 Codex Review

- **任务入口**：`docs/cursor-task-security-round3-c1.md`
- **ADR**：`docs/architecture/adr-logging-privacy-file-boundary.md`
- **状态**：已实现路径边界、容量上界、默认无正文日志、`include_tracebacks`；由后续 Codex F1–F5 返修条目承接
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未安装依赖；未提交、未推送

### 交付要点
1. `src/core/log_privacy.py`：安全 `file_name`、路径解析 fail-closed、`exception_log_label`、`PrivacyRotatingFileHandler`
2. `logging_setup`：先验证后创建；重复 setup 不重复 handler；启用日志仅记相对文件名与容量元数据
3. 调用点收口：搜索/角色/Ollama/VoiceSession/RVC worker·health·converter（及相邻 STT/TTS/infer/postprocess）
4. 配置：`include_tracebacks` 默认 false；`max_size_mb`≤50、`backup_count` 0..10

### 离线验证
- `py -3.12 -X utf8 -m unittest tests.test_security_round3c_logging -q` → **23 tests / OK**（skipped=1，本机无法创建 junction 时跳过专属用例）
- 相关：`tests.test_security_round3{a,b}* / test_rvc_worker_lifecycle / test_voice_session / test_stt_contract / test_generate_* / test_ollama_chat_system_contract` → **206 tests / OK**
- `py -3.12 -X utf8 -m unittest discover -s tests -q` → **533 tests / OK**（skipped=1）
- `compileall` OK
- `git diff --check` OK（仅 LF/CRLF 提示）

### 剩余风险
- 日志仍是本机明文；未覆盖全仓库历史日志语句；真实服务链路未测；Round 3C2（SQLite 残留）未做

---

## 2026-09-30 Round 3B Codex Review Round 2 返修（F5）——完成，等待 Codex 复验

- **评审入口**：`docs/codex-review-security-round3-b.md`（Round 2：F5）
- **ADR**：`docs/architecture/adr-network-egress-model-voice-permissions.md`
- **状态**：已修复 F5（3xx 拒绝前 best-effort close）并补确定性反例；**停止等待 Codex 复验**；**未进入 Round 3C / 自然陪伴**
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未安装依赖；未提交、未推送

### F5 对照
1. `enforce_no_redirect`：3xx 先 `best_effort_close_response`，再抛 redirect 拒绝；`close` 失败不得覆盖原拒绝
2. Ollama `_request_post`/`_request_get`（含 `generate_stream`）与 memory transport 共用语义
3. 反例：stream 302 单次 FAILED/零正文/零历史/`close`×1；GET 302；memory 302；`close` 抛错仍保留 redirect 且无第二次 HTTP

### 离线验证
- `Python312 -X utf8 -m unittest tests.test_security_round3b_review_fixes tests.test_security_round3b -q` → **39 tests / OK**
- `Python312 -X utf8 -m unittest discover -s tests -q` → **510 tests / OK**
- `compileall` OK
- `git diff --check` OK（仅 LF/CRLF 提示）

### 剩余风险
- 真实远端/云语音未测；日志隐私属 Round 3C

---

## 2026-09-30 Round 3B Codex Review Round 1 返修（F1–F4）——完成，等待 Codex 复验

- **评审入口**：`docs/codex-review-security-round3-b.md`（Round 1：F1–F4）
- **ADR**：`docs/architecture/adr-network-egress-model-voice-permissions.md`
- **状态**：已逐项修复 F1–F4 并补反例；**停止等待 Codex 复验**；**未进入 Round 3C / 自然陪伴**
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未安装依赖；未提交、未推送

### F1–F4 对照
1. **F1**：`reseal_model_endpoint`；OllamaClient / RequestsOllamaChatTransport / MemoryRuntime 不信任伪造 `kind=loopback`
2. **F2**：删除 TypeError→去掉 `allow_redirects` 二次请求；内部 TypeError 仅调用一次且参数仍含 `allow_redirects=False`
3. **F3**：diagnostic 默认 `SafetyPolicy.from_app_config`；transport 与 `_default_llm_factory` 共享策略；远端允许/拒绝/双许可离线覆盖
4. **F4**：显式空端口（hostname / IPv4 / `[::1]:`）拒绝；合法 IPv6 冒号不误拒

### 离线验证
- `Python312 -X utf8 -m unittest tests.test_security_round3b_review_fixes tests.test_security_round3b -q` → **35 tests / OK**
- `Python312 -X utf8 -m unittest discover -s tests -q` → **506 tests / OK**
- `compileall` OK
- `git diff --check` OK（仅 LF/CRLF 提示）

### 剩余风险
- 真实远端/云语音未测；日志隐私属 Round 3C

---

## 2026-09-30 Round 3B 网络出口 / 远端模型 / 云 STT·TTS——完成，等待 Codex Review

- **任务入口**：`docs/cursor-task-security-round3-b.md`
- **ADR**：`docs/architecture/adr-network-egress-model-voice-permissions.md`
- **状态**：已实现威胁边界与离线矩阵；**停止等待 Codex Review**；**未进入 Round 3C / 自然陪伴**
- **未运行**：`main.py`、真实 Ollama/HTTP、edge/Google、RVC、麦克风、扬声器、浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未安装依赖；未提交、未推送

### 交付要点
1. `SealedModelEndpoint` + `authorize_model_endpoint`（无 DNS）；loopback 默许；远端 HTTPS/HTTP 双许可
2. Ollama / memory transport / diagnostic 共用策略；全部 POST/GET `allow_redirects=False`；3xx 不跟随
3. 独立 `cloud_stt` / `cloud_tts`；裸策略拒绝；正式 yaml/Config 默认 true（注释标明数据去向）
4. 启动摘要仅分类与布尔许可 + engine 名；text 模式零语音构造

### 离线验证
- `Python312 -X utf8 -m unittest tests.test_security_round3b -q` → **24 tests / OK**
- `Python312 -X utf8 -m unittest discover -s tests -q` → **495 tests / OK**
- `compileall` OK
- `git diff --check` OK（仅 LF/CRLF 提示）

### 剩余风险
- 真实远端/云语音未测；日志隐私属 Round 3C
- 既有注入 `_http_post` 若不接受 `allow_redirects` 会 TypeError 回退（仅 stub；生产 `requests` 始终禁 redirect）

---

## 2026-09-30 Round 3A Codex Review Round 4 返修（F19–F22）——完成，等待 Codex Review

- **评审入口**：`docs/codex-review-security-round3-a.md`（Round 4：F19–F22）
- **状态**：已逐项修复 F19–F22 并补真实生命周期反例；**停止等待 Codex Review**；**未进入 Round 3B**
- **未运行**：`main.py`、真实 Ollama/RVC/麦克风/扬声器/浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未提交、未推送

### F19–F22 对照
1. **F19**：`TempFileManager.shutdown` 在 deadline 后停止继续取队列；worker stop 后放回 in-flight；pending/owned 可重试
2. **F20**：流水线 cleanup-request + 同锁 handoff/自清理；late producer 不得新增无人接管路径
3. **F21**：自有 TempFileManager 仅 `complete=True` 才标记 `_temp_files_shutdown_done`；incomplete 可重试
4. **F22**：真实 oneshot `TimeoutExpired`、Worker timeout/cancel、pipeline player error/合成中取消/late producer、concurrent shutdown

### 离线验证
- `Python312 -X utf8 -m unittest tests.test_security_round3a_review_fixes tests.test_security_round3a -q` → **53 tests / OK**
- `Python312 -X utf8 -m unittest discover -s tests -q` → **471 tests / OK**
- `compileall` OK
- `git diff --check` OK（仅 LF/CRLF 提示）

### 剩余风险
- 真实 RVC/CUDA/音频未测
- Round 3B 未开始

---

## 2026-09-30 Round 3A Codex Review Round 3 返修（F14–F18）——完成，等待 Codex Review

- **评审入口**：`docs/codex-review-security-round3-a.md`（Round 3：F14–F18）
- **状态**：已逐项修复 F14–F18 并补充真实生命周期反例；**停止等待 Codex Review**；**未进入 Round 3B**
- **未运行**：`main.py`、真实 Ollama/RVC/麦克风/扬声器/浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未提交、未推送

### F14–F18 对照
1. **F14**：speech 未完成时本轮不关闭 TTS/TempFileManager；成功后才 exactly-once `speech → tts → temp`
2. **F15**：`TempFileManager.shutdown` 返回 `TempFileShutdownStatus`；有界 reclaim 队列/in-flight/owned；仅 complete 才标记 `_temp_shutdown_done`
3. **F16**：`_speak_single` / 流水线以 `try/finally` 回收临时文件；清理失败不掩盖播放异常
4. **F17**：清理矩阵改为真实 ownership 链路（edge 成功/播放失败、RVC fake runner 成功、长 delay shutdown）
5. **F18**：`TrustedRvcProfile` 仅构造期传入；公开 `seal_rvc_profile` 永久拒绝；封存资产用严格 size/mtime 检测原地改写

### 离线验证
- `Python312 -X utf8 -m unittest tests.test_security_round3a_review_fixes tests.test_security_round3a -q` → **45 tests / OK**
- `Python312 -X utf8 -m unittest discover -s tests -q` → **463 tests / OK**
- `compileall` OK
- `git diff --check` OK（仅 LF/CRLF 提示）

### 剩余风险
- 真实 RVC/CUDA/音频未测
- Round 3B 未开始

---

## 2026-09-30 Round 3A Codex Review Round 2 返修（F8–F13）——完成，等待 Codex Review

- **评审入口**：`docs/codex-review-security-round3-a.md`（Round 2：F8–F13）
- **状态**：已逐项修复 F8–F13 并补强反例测试；**停止等待 Codex Review**；**未进入 Round 3B**
- **ADR**：`docs/architecture/adr-rvc-capabilities-temp-ownership.md`（封存/文件身份契约已在实现中落地）
- **未运行**：`main.py`、真实 Ollama/RVC/麦克风/扬声器/浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未提交、未推送

### F8–F13 对照
1. **F8**：装配期 `seal_rvc_profile` 一次性封存（含 `st_dev/st_ino` 身份）；下层 `register_*` 拒绝；同名替换在 fake runner 前拒绝
2. **F9**：Worker `convert()` 强制绑定共享 `TempFileManager`；缺失即 fail closed，不 start/不写 stdin
3. **F10**：`TempFileManager` 按文件身份登记；缺失/同名替换/hardlink 只撤销所有权，不删替代物；队列项携带身份
4. **F11**：播报关闭超时保留可重试引用；仅完成组件标记终态；TTS/temp exactly-once；删除 `or True` 假断言
5. **F12**：清理矩阵覆盖 edge 异常回收、MP3→WAV 异常、RVC 拒绝/成功、shutdown 后 owned/pending/worker 归零
6. **F13**：移除 `docs/engineering-baseline.md:111,114,117` 尾随空格；`git diff --check` clean

### 主要改动
- `src/core/safety.py`：`FileIdentity` / `seal_rvc_profile` / 启动前身份复核
- `src/core/assembly.py`：仅装配密封存；无模型保持未封存
- `src/voice/rvc/{infer,worker}.py`：下层只校验；convert 强制 manager
- `src/voice/audio/temp_files.py`：身份所有权；`owned_count` / `delayed_worker_alive`
- `src/core/assistant.py`：可重试 incomplete shutdown
- `src/voice/tts/engine.py`：edge/WAV 异常路径回收临时文件
- `tests/test_security_round3a_review_fixes.py` 等

### 离线验证
- `Python312 -X utf8 -m unittest tests.test_security_round3a_review_fixes tests.test_security_round3a -q` → **36 tests / OK**
- `Python312 -X utf8 -m unittest discover -s tests -q` → **454 tests / OK**
- `compileall` OK
- `git diff --check` OK（仅 LF/CRLF 提示，无 trailing whitespace）

### 剩余风险
- 真实 RVC/CUDA/音频未测
- `subprocess=false` 时 Chrome 自定义路径仍依赖通用入口
- Round 3B 未开始

---

## 2026-09-30 Round 3A Codex 评审修复（F1–F7）——完成，等待 Codex Review

- **评审入口**：`docs/codex-review-security-round3-a.md`
- **状态**：已逐项修复 F1–F7 并新增反例测试；**停止等待 Codex Review**；**未进入 Round 3B**
- **ADR**：`docs/architecture/adr-rvc-capabilities-temp-ownership.md`
- **未运行**：`main.py`、真实 Ollama/RVC/麦克风/扬声器/浏览器、清理实扫、系统电源；未读/未删 `data/memory`；未提交、未推送

### F1–F7 对照
1. **F1**：裸 `SafetyPolicy()` 默认拒绝三项 RVC 能力；`from_app_config` 合并应用缺省；下层要求显式策略
2. **F2**：策略实例变化先 `stop_rvc_worker`；每次 Worker 使用前 `require(RVC_WORKER)`
3. **F3**：`register_rvc_profile` + 一次性推理 I/O 须 TempFileManager 所有权
4. **F4**：移除公开 `register_owned`
5. **F5**：`create` 先授权再 mkdir
6. **F6**：关闭顺序 speech→TTS→TempFileManager；补齐清理矩阵反例测试
7. **F7**：自 `7dff31b` 恢复 baseline 行尾双空格，仅保留 3A 语义行

### 新增/关键测试
- `tests/test_security_round3a_review_fixes.py`（F1–F6 反例）
- `tests/test_security_round3a.py` / `tests/test_rvc_worker_lifecycle.py` 同步适配

### 离线验证
- `Python312 -X utf8 -m unittest tests.test_security_round3a_review_fixes tests.test_security_round3a -q` → **26 tests / OK**
- `Python312 -X utf8 -m unittest discover -s tests -q` → **444 tests / OK**
- `compileall` OK
- `git diff --check`：仅 LF/CRLF 提示；`engineering-baseline.md` 行尾双空格为 Markdown 硬换行（F7 自 `7dff31b` 恢复的既有风格）

### 剩余风险
- 真实 RVC/CUDA/音频未测
- `subprocess=false` 时 Chrome 自定义路径仍依赖通用入口
- Round 3B 未开始

---

## 2026-09-30 Round 3A：专用子进程能力与临时文件所有权——完成，等待 Codex Review

- **任务入口**：`docs/cursor-task-security-round3-a.md`
- **状态**：仅实现 Round 3A；**停止等待 Codex Review**；**未进入 Round 3B**
- **ADR**：`docs/architecture/adr-rvc-capabilities-temp-ownership.md`
- **基线提交**：`7dff31b`；本轮未提交、未推送
- **未运行**：`main.py`、真实 Ollama/RVC/麦克风/扬声器/浏览器、清理实扫、系统电源实操；未读/未删 `data/memory`；未安装依赖

### 实现摘要
1. RVC Worker / 一次性推理 / probe 经 `SafetyPolicy` 具名能力 + `rvc_capabilities` 固定契约；通用 `subprocess` 默认 false，不是 RVC 回退旁路
2. `TempFileManager`：白名单 + `create` 所有权双条件；STT Whisper / edge-tts / RVC 输出走统一清理
3. 装配共享同一 `SafetyPolicy` 与 `TempFileManager`；text 模式不构造语音栈；电源走 `safe_system_power_run`；注册表写仍永久拒绝

### 实际修改文件（主要）
- `src/core/safety.py`、`src/core/rvc_capabilities.py`（新）
- `src/voice/rvc/{worker,infer,health_check,device,converter}.py`
- `src/voice/audio/temp_files.py`、`src/voice/{stt.py,tts/engine.py}`
- `src/core/{assembly,assistant,config}.py`、`src/system/windows_control.py`
- `config/config.yaml`、`tests/test_security_round3a.py`（新）、相关既有测试微调
- `docs/architecture/adr-rvc-capabilities-temp-ownership.md`、`docs/engineering-baseline.md`、本交接文档

### 离线验证
- `Python312 -X utf8 -m unittest discover -s tests -q` → **432 tests / OK**（此前 Round 2 为 418；本轮含 `test_security_round3a` 等）
- `compileall` OK；`git diff --check` 仅余文档行尾空白提示（已清理）/ LF/CRLF 提示

### 剩余风险
- 真实 RVC/CUDA/音频未测；仅 fake runner 契约
- `subprocess=false` 时 Chrome 自定义路径仍依赖通用入口
- Round 3B（HTTP/云许可/日志隐私）未开始

---

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
