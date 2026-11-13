# 下次额度恢复后的入口

**本文件已转为历史恢复记录。** 用户恢复额度后，TFM完整deadline、LLM内存准入、最终保行为整理与资源里程碑均已Root通过。当前入口改为docs/current-delivery-status-2026-11-04.md与docs/codex-review-performance-milestone-2026-11-04.md；不要重新实现下面已完成切片，也不要将下面的额度暂停当当前状态。真实语音短测受可用内存限制，真实长测按约后置。

**已按额度阈值暂停**：最新API确认primary used90%/剩10%。开发、CLI与测试均停止，无在途任务。只保留交接记录，未开启下一代码切片；等待用户额度刷新后继续。

用户要求Codex主5小时窗口剩余<=10%停止，周额度不影响，不自动启用reset credits。最新余额以get_usage_limits.primary实际读取为准。本文件创建时剩11%，当前所有官方Cursor会话/受监督测试都已结束，没有在途修改；不要自动启动后续代码。

先读AGENTS、四个相关repo skills、docs/codex-performance-progress-2026-11-04.md、docs/performance-followup-design-2026-11-04.md和dirty状态。六项本轮切片已最终PASS，具名review/ADR已更新；不重做已通过的测试/预取/协调器/退出协议，也不以旧handoff“待Root”推翻最新状态。

下一步是设计完整TFM deadline调度，尚未放行Cursor实现。先读temp_files.py所有pending/queue/claim/revoke/identity/shutdown调用及Root stale/claims/bounds探针。明确绝对due、同路径generation、队列满时更早任务公平性与有界存储，再交官方Cursor；不能只是换PriorityQueue而让满队列中的长delay挡住新due0，也不能加入无限陈旧堆项。保留明确delay、不重复累加delay、OS锁退避和耗尽显式重试；shutdown仍总deadline、actualquiet且失败保留owner。

其后设计LLM冷载RAM/commit保护，不能漏memory extractor独立post_chat或warm_up；sealed endpoint/禁redirect/短metadata总预算与response close保持，低资源明确拒绝或提示轻量模型，不默认改用户12B/音色，不卸载/停止他人Ollama。估算不是内存预留，不能承诺绝无卡顿。参考官方metadata文档已链接在performance-followup-design中。

最后整模块资源review，才考虑受控真实短测；本轮未运行真实模型/音频设备/长测，之前真实三模型共存失败仍有效。30–60分钟soak继续排除。正式data/memory未访问，未提交推送，巨大dirty全部保留，禁git回滚/清理。

实施仍由官方Cursor CLI，Root设计与严格独立review。Cursor先编写+局部compile/diffcheck STOP，由Root审查fixture安全后决定顺序scoped tests；禁止新监督器/覆盖报告/raw/discover/并发测试。所有新probe必须无共享临时文件残留，原826 WAV集合保持；测试不能通过直接撤销owner丢真实文件。音频构造前fake设备检测；所有gates/lease释放→实际join/aggregatecomplete→恢复patch→删除独占目录。

CLI路径tmp/cursor-cli-2026.10.22/dist-package/cursor-agent.ps1，模型composer-2.5，仍按已有用户外发代码文档与修改工作区授权执行；无需重复询问。新切片新会话，返修只resume对应会话；所有新gate使用唯一label保留失败证据。废弃tmp/cursor_rvc_cpu_budget_gate_2026_11_04.py不得运行，Root监督器tmp/codex_scoped_test_gate_2026_11_02.py是唯一测试入口。
