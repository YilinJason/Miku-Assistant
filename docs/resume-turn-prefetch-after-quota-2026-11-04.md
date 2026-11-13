# 额度刷新后恢复：默认语音预取返修

当前已暂停：Codex primary used90%/剩10%（300分钟窗口，resetsAt=1793808984）；只看primary，忽略周额度，不自动使用reset credits。用户明确刷新/额度恢复后重新读额度与工作区状态再继续。

最新唯一任务：读 `docs/codex-review-turn-audio-prefetch-2026-11-04.md`（Root CHANGES REQUIRED）与 `docs/cursor-task-turn-audio-prefetch-2026-11-04-draft.md`。Cursor初稿49绿不可靠，Root3两条实际首句路径FAIL；尚未交返修，不能当已验收版本。

官方CLI会话73a8e323-f207-4ee9-b30a-bec19f997a35，exec46880已exit0 STOP；Root gate exec18300已exit1结束，无在途CLI或测试。下一步通过官方Cursor CLI Composer2.5 resume该会话，先统一惰性唯一producer/credit/取消/路径回收设计，再严格源码与独立矩阵闭环。不要再新开当前切片并行开发，不另启subagent。生产与仓库测试仍只能Cursor写，Root写设计/文档/tmp独立probe。

Root探针 `tmp/codex_turn_prefetch_root_2026_11_04.py` 已在监督器allowlist，现3项只最小接线/串行/首段控制异常，不足全验收；返修后补真实producer异常、晚返/路径/pin/换轮取消及资源上限。用主Python312与 `tmp/codex_scoped_test_gate_2026_11_02.py` 唯一label前台顺序、硬截止<=120秒；禁raw unittest/discover/detach/并发。原报告 `tmp/codex_gate_turn_prefetch_root3_review_20261104a_2026_11_02.json`，WAV826零差，全部finally已结束。

前置已Root最终闭环：OwnedThread10+最终相关81；语音队列9+最终123；音频回滚7全异常矩阵+最终156；TFM17独立+6持久IO断言+最终175；RVC显式重试23独立及相关分组。各组有重叠，不能相加称唯一总量。具名review顶部/ADR为准，不重复修已过底层。

Cursor上轮违规raw测试产生的三个0B WAV已追溯并由Root SafetyPolicy+TFM精确回收，报告 `tmp/codex_audio_rollback_reclaim_result_20261104.json`：829→826，仅具名三项删除，complete=True。最终156 gate保持826零差，禁止再写共享tmp的fixture。正式data/memory全程未访问；无模型/设备/音色/默认配置改动、未提交推送，巨大dirty保留，禁git checkout/reset/restore/clean。

后续顺序：当前预取闭环→资源/等待里程碑评审→保行为精简→最终低配/等待/CPU线程/legacy播报线程/内存资源治理。三模型协调已通过voice_load_lane→voice_activation→prepare_local_voice生产接线；待解决的是实际12B+RVC+Cosy共存压力，不要重复列成未接线。30–60分钟真实soak仍排除；不能承诺真实等待/无泄漏/所有低配设备已通过。
