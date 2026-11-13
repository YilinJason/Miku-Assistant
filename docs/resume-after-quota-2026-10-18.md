> **已按额度规则停止（2026-10-18）**：Codex primary used90，剩10%；下次刷新 2026-10-18 05:01:31 America/New_York（UTC 2026-10-18 09:01:31）。已中断自有Cursor dbb7794f / exec44617（exit1）；只读CIM确认无匹配Cursor/门禁测试/本项目模型worker残留。没有新提交或推送。停止前的fixture修改仍未交付/未独立验收，不能标绿，不能继续RVC或真实模型。恢复先查实时额度，再读取当前测试helper与git diff；完整恢复步骤见本文件。用户真实data/memory未读改删。
> 最新状态：primary used88（剩12%），尚未触发10%停止。当前Cursor dbb7794f/exec44617仍工作，测试fixture未交付/未独立验收。53b96601四组root probes全通过，但root最终八模块290出现既有blocked-write前置entered.wait(1s)失败（不是关闭超时），保持未放行。本次先仅收集状态，禁止启动新模型/RVC任务。特别复审新_BlockedWriteWorkerProcess：需_run finally显式stdout.close、wait真实有界线程等待；support teardown不能吞异常/留下线程；controlled测试的stop线程异常必须主线程收集。当前未确认这些点已完成。当前文件下方历史仅恢复参考。
# 2026-10-18 恢复入口（持续更新，非完成声明）

用户要求Codex剩余额度<=10%停止工作/停止Cursor，等刷新。当前最新只读查询primary used82（剩18）、weekly used27（剩73），reset1792314091；继续前实时复查，不能引用旧剩余量。当前尚未因额度停止。

用户持续授权官方Cursor CLI发送所需代码/文档、修改D:/Miku_Assistant_LLM；Codex设计和严格review，Cursor写生产代码。不要直接写生产代码、不要subagents、不要push/重置或误删现有dirty；真实data/memory禁读改删。主Py312/RVC及Cosy独立Py311。默认Edge/local_tts许可false，选定原始Nahida声音不变。工作区yilin_dev/HEAD7dff31b，已有巨大dirty保留，未提交/未推送。

本轮完成：CPU staging初始化/别名修复、EOF/启动进度fake修复；未证明收益的nonblocking CUDA adapter已定向撤回。发现并修复Windows venv redirector/实际解释器资源归属，生产src/core/owned_worker_job.py + SafetyPolicy localTTS封装 + runtime早期Job加入，经历严格异常/原生API评审与原始probe。全部root contract4/partial3/remaining3/transition3目前通过；实际无模型Windows stop-tree/natural-exit/parent-death三项全过（47/156/31ms），见tmp/codex_owned_job_real_probe.json。Job生命周期不是OS沙箱，未证明音频加速或长期无泄漏。

**当前卡在最终离线门禁的一项旧fixture**：53b96601修正后root八模块290项出现1失败 test_shutdown_during_blocked_write 的 entered.wait(1.0)前置阶段（不是shutdown预算），临时目录解构时线程仍在随后报告脚本不存在。root已定位阶段耦合与缺finally；不重跑求绿。当前Cursor dbb7794f-f192-4458-a753-25a7e8e65f21 / exec44617，任务 docs/cursor-task-blocked-write-fixture-2026-10-18.md，仅测试fixture/三个blockedwrite用例，生产不改、预算不放宽。完成后root必须阅读最终fixture及线程清理，独立tmp/cursor_owned_job_validation_gate.py一次（120秒/精确824 WAV零差），通过后才能将本地TTS Job标最终PASS。再有具体失败则诊断再修，不能直接重复至绿。

根目录tmp中的root probes：codex_owned_job_contract_probe.py（构造仅改为具名参数，断言未弱化）；codex_owned_job_partial_spawn_probe.py；codex_owned_job_remaining_paths_probe.py；codex_owned_job_transition_probe.py。真实无模型测试codex_owned_job_real_gate.py有45秒自有进程树监督器，用generic SafetyPolicy SUBPROCESS仅运行已知测试fixture；不借密封LOCAL_TTS能力执行替代脚本。只fixture解释器sleep，不加载模型/设备。

声音基准mtime10/5已超过data/test 72h TTL，原件仍在；root已哈希验证保护副本tmp/protected-voice-reference/01_warm_companion_rvc.wav，hash7C4AEE8B69428E4BBE3BB773CFC47EFAE1618EFB46B7B80EBAF1465739FE19BA，382444 bytes，根tmp忽略且FileCleaner不扫。正式参考目录迁移/ignore待版本整理。

真实性能仍失败：CPU-only staging两模型diagnostic60 69fb748168cd ready/完整回收（Cosy30.328、construct21.336秒），默认30失败；三模型297c4f1249d5失败（Ollama6.547/RVC38.172/Cosy32.062秒），零音频；已撤回nonblocking版本e127287e961f模型ready却56.672秒且回收不全，不作为性能通过。当前没有活动真实模型。最近GPU退出基线1308MiB占用/14754MiB空闲，RAM约30GiB以上空闲仅短测；资源观察root gate每5秒记录测试树RSS/Private/threads和全局GPU，最多40条。

**下一项必须优先补RVC同类归属**：已静态确认SafetyPolicy.spawn_rvc_worker仍裸Popen，RVC也独立venv。任务 docs/cursor-task-rvc-owned-worker-2026-10-18.md已写但尚未下发/实现，必须本地TTS闭环后执行。只先persistent worker；单次run_rvc_infer/run_rvc_probe仍需要后续单独收口，不能假称全音频链路无孤儿。root无模型探针可参数化解释器后验证.venv-rvc。

其后：实际三模型共存/热短句，C2b2b App loader owner/关闭（仅draft，未实现），C2c有界预取/长句时长/播放取消；D隔离库真实30–60分钟soak（未开始，capture-only不等同麦克风/扬声器人工验收），跨模块里程碑review，版本整理/Git检查点（当前未新增提交）。四项总体交付均未全部完成，不能报全部完成。

Cursor入口：C:/Users/uitra/AppData/Local/cursor-agent-portable/2026.09.28-64d2043/dist-package/cursor-agent.ps1 -p --force --model composer-2.5 --output-format stream-json。过滤思考与工具大结果，只显示system/assistant/result。主Python C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe。10%时中断自有CLI、停止自有测试树、保存该文件与current-delivery状态；不可启动新开发/模型任务。无自动恢复/goal状态，刷新并获得继续指令后查实时额度。
