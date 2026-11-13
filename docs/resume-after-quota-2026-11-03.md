# 额度停工恢复入口（2026-11-03 晚间更新）

用户最新明确“周额度无所谓，继续工作”：忽略周10%停工阈值，保留短周期remaining<=10%规则，不自动消费reset credits。已继续工作，以下停工信息为历史。

用户在短周期刷新后要求继续，本轮已执行至**周额度used90%=remaining10%**，短周期used49%。按用户规则停止。未消费reset credits、未增加付费spend。停止时所有自有Cursor CLI/测试/真实诊断均exit，无自有模型解释器残留（CIM确认）；不自动开新任务。恢复先查询实时get_usage_limits，不凭聊天说已刷新就绕阈值。

## 约束和目标

用户：退出回收/测试可靠性→真实等待及有界预取→保行为精简→性能/低配/泄漏治理。30–60分钟真实长测本轮排除。Codex设计与每轮严格review，官方Cursor CLI实施；每切片闭环后下一项。重大资源板块做里程碑评审。保留巨大dirty工作区，未提交推送，不reset。正式data/memory不读改删；不启动主程序/音频设备仅为审计。短真实固定公开文本模型诊断已授权，但必须有监督/资源预检/独立回收，不能静默改模型/声学/默认预算。系统副作用仍走SafetyPolicy；不碰用户Ollama服务、注册表、电源。不要另起子agent。

## 最新已完成（相对早间停工记录的新进展）

1. 前台有界request **Root分层PASS**：138之外是156项App/CLI回归8.219秒OK；最终独立interrupt4、foreground5、同步App9、shutdown6全过，WAV826零差。修复pre-start/真实OS bootstrap未知票据、creator中断/实际线程退出、sticky失败、pending文本过渡、候选interrupt和旧result兼容。review docs/codex-review-voice-foreground-2026-11-03.md 顶节。原同步ensure API保留。
2. 开发监督器 **Root分层PASS**：tmp/codex_real_voice_supervision.py import-safe、唯一主gate；循环不跑GPU子进程，默认GPU查询必须显式SafePolicy代理；BaseException/partialowner/独立清理、单10秒cleanup预算、真实quiet、坏metadata/日志cap/及时报告。Root16fake+6独立故障过；Root真实两层Native Job+实际第三代PID0.578秒全部结束。review docs/codex-review-real-diagnostic-supervision-2026-11-03.md。临时fake移回tmp，不污染正式tests。
3. 两模型--no-ollama路由 **Root8fake PASS**：不能用llmNone走强制Ollama协调器；现显式实际engine.prepare_rvc→prepare_local_tts，不伪造FakeLlm。仅诊断控制变量，非生产三模型已过。
4. **Cosy本地SDK导入优化已Root PASS**：src/voice/cosyvoice/local_modelscope_import.py + runtime worker接线；scope固定本地资产/仅snapshot_download/owner-thread/ModuleSpec/精确恢复/foreign冲突不覆盖且fail-closed/别名每次path_trust复核，禁止未知下载。无vendor/site-packages/声学/30秒改动。Root10独立边界0.082秒OK；Root六模块138测试13.698秒OK(skip1)，compileall/diffcheck通过。skip是主解释器无wetext额外项，vendor环境有依赖，核心fake导入链已验证。
5. 138回归发现旧Edge mock的None关闭期望与先前ADR严格声明API契约不符：只迁移tests.test_cosyvoice_production_slice_c2a（unknown先保留TFM，getter明确complete后重试关闭）及ADR。Cursor曾擅自加assistant.py engine_type条件，被Root中断执行75187并要求**只撤回该新增hunk**；后续源码已确认恢复，未夹带Core语义改变。TempFileShutdownStatus fixture补active_io_leases=0保留。

此前早间已通过blocked-write fixture、RVC单次owned-run/持久IO lease/聚合传播、App同步lane/候选等，仍有效；证据见具名review与docs/codex-review-resource-milestone-2026-11-03.md，不重做整套。

## 真实性能证据（不要混淆范围）

- 恢复后一次三模型FAIL：Ollama88.5秒，RVC87.297秒准备失败，未到Cosy；aggregate未静止。旧外层GPU同步采样/监督迟收尾被Root手工Ctrl+C停止，后来确认自有服务11445关闭/无自有模型进程；用户Ollama app/serve未动。原始tmp/codex_real_resumed_coexist_control.*日志，子报告data/test/voice-naturalness/cosyvoice-production-coexist/59204f7029a7/report.json。外层采样JSON未产生，不能声称峰值/完整采样。
- 监督修好后，两模型原版FAIL：RVC24.453秒成功，Cosy30.141超时；wetext_import14.391s，cosy_import4.419s，constructor未及完成。总56.78秒；第一次完整aggregate/TFM/Job退出PASS。tmp/codex_real_two_model_control_root.json，子目录b2f32668ee37。
- SDK优化后，同条件两模型prepare-only **PASS**：RVC24.172秒，Cosy18.265秒；wetext导入34.576ms，Cosy import5.737s，constructor8.267s。总44.8秒，采样RSS峰6805.57MB、最低空闲RAM12066MB；运行中GPU未采样。首轮六字段/TFM complete、所有quiet True，无cleanup错误。tmp/codex_real_two_model_local_sdk_root.json，子报告data/test/voice-naturalness/cosyvoice-production-two-model-control/a867345cf37e/report.json。无音频合成/播放。本次前后对照只有一组，未排除缓存/系统负载变化；不承诺普遍速度。
- 早间实际RVC单独prepare21.25s、首轮退出47ms完整聚合的证据仍保留。

性能/安全结论review：docs/codex-review-cosy-local-import-startup-2026-11-03.md；真实对照：docs/codex-review-coexist-control-2026-11-03.md。

## 恢复后顺序（仍未完成）

1. 当前SDK切片已闭环，先按资源预检用新版gate做**一次三模型prepare-only**对照：默认30秒不变，临时自有11445服务，唯一--label；确认实际耗时和所有聚合回收。不得说新SDK修好了先前RVC在共存下的87秒失败。若资源不足则跳过，不关用户应用/服务，不增预算。
2. 如共存仍失败，继续控制变量与明确资源预算/加载顺序设计；不要无依据添加GPU并行/隐藏降级。模型导入与推理/音频输出是不同验证，至少短固定公开文本无设备capture应在相应阶段补上。
3. **有界预取未开发**：任务docs/cursor-task-cosyvoice-bounded-lookahead-2026-11-03.md已写但待放行。最多future一段（正在准备+已排队合计1）+当前播放1，单producer/实际join，取消迟返有owner、资源不明不能下轮分配；Edge旧无界pipeline留性能阶段统一。
4. **独立保行为精简未执行**，前台控制器/新SDK功能修复不冒充精简。依据docs/delivery-roadmap-2026-11-03.md，保留安全检查/公开接口，避免重复状态机；适合先合并前台互补quiet谓词、移除真正无用重复代码，不能只按行数删。
5. **最终性能/低配未完成**：Edge Queue无界/producer正常join600秒、legacy每回复daemon线程积累；TFM IO pin后待删/owned总上限/拒删时准入；OllamaWarmup event与实际线程退出；RVC基础资产缺失时库requests.get无timeout自动下载（当前真实hubert/rmvpe文件齐全，不是本次已证慢原因；应本地前检快速失败，不删真实文件来测）。低配应显式保守配置/资源不足快失败，不静默改音色或下载模型。
6. 真实30–60分钟soak按用户要求留待办，不偷偷执行；未commit/push。

## 环境与CLI

cwd D:/Miku_Assistant_LLM；branch yilin_dev，HEAD7dff31b（本轮未fetch/commit）。Main Python C:/Users/uitra/AppData/Local/Programs/Python/Python312/python.exe 3.12.3；RVC .venv-rvc/Scripts/python.exe 3.11.9；Cosy vendor/CosyVoice/.venv/Scripts/python.exe 3.11.9，Torch2.3.1+cu121。

官方CLI tmp/cursor-cli-2026.10.22/dist-package/cursor-agent.ps1；resume **06f27f0e-7f51-4689-9e19-046fc05a34eb**，model composer-2.5；gpt-5.6-sol-high官方额度不足，未加付费。最后Cursor执行27942 exit0，最后真实95058 exit0。CLI -p --force --resume ID --model composer-2.5 --output-format stream-json，PowerShell按system/assistant/result过滤输出；用户已明确授权必要代码文档发官方服务及修改工作区，不重复请示。

Root监督器tmp/codex_scoped_test_gate_2026_11_02.py（--modules 显式unittest或固定--probe，最长180）；全部Python -X utf8。Edge asyncio在AppContainer可能socketpair挂，须normal Windows require_escalated，不能改生产绕过。真实gate tmp/codex_app_real_voice_gate_2026_11_03.py（--no-ollama/--isolated-ollama/--prepare-only/--label；旧标签存在拒覆盖），新监督core是tmp/codex_real_voice_supervision.py。不要运行旧裸subprocess诊断。

选定声音参考data/test/voice-naturalness/cosyvoice-instruct/01_warm_companion_rvc.wav及tmp/protected-voice-reference同字节保护副本，SHA256 7C4AEE8B69428E4BBE3BB773CFC47EFAE1618EFB46B7B80EBAF1465739FE19BA，382444字节，不覆盖/清理/调参。FileCleaner非递归，不把它会扫嵌套参考目录的旧警告当事实。
# 最近续作现场（2026-11-03 21:25 本地，覆盖下方旧暂停信息）

- 用户“周额度无所谓，继续工作”：忽略周阈值，仅短周期remaining<=10%停工；不消费reset credits。最终实时读取短周期used90%（剩10%）、周96%，**已触发短周期阈值停工**；无在途CLI/测试，以下为完整恢复现场。短周期resetAt=1793767366（2026-11-03 23:42:46 America/New_York），下次必须实时查询。
- Cursor官方CLI两轮实际执行且均结束：86721初轮、12347返修；会话06f27f0e-7f51-4689-9e19-046fc05a34eb，Composer2.5。无在途Cursor。Root scoped测试50017已exit0。
- 五个语音包lazy导出完成Root闭环：src/voice、tts/audio/rvc/cosyvoice的__init__。PEP562固定映射，globals属性缓存、dir=globals∪all；不创建组件或新线程。初审四项返修均已查源码。Root gate lazy_exports_root_final=46tests/13.150s OK(skip1)，总15.843s，WAV826零差，compileall/diffcheck退出0（CRLF提示）。具名task/review/ADR已保存，正式memory禁读改删，未提交推送。
- 本轮实际只读process/11445查询无输出，查无上轮自有模型/服务监听。未启动新的真实模型测试。11434只读tags查询URLError，未加载/停止用户Ollama，未得已安装模型清单。
- 上轮隔离mmap实验FAIL：Ollama120.016s超时、未到RVC/Cosy，总129.23s；日志证实mmap但未改善。树RSS7713.28MB、最低物理9610.56MB/commit7831.33MB。应用六字段与TFM完整（未开RVC）；外层quiet/cleanup True，2.03s；原taskkill timeout由NativeJob收尾。报告tmp/codex_real_three_model_mmap_commit_root.json。不推广生产mmap。不把负面实验转为提速成功。
- 隔离launcher不再safe_run env旁路，先earlyjoin NativeJob；commit监督stdlib-only、tail真有界。Root47相关fake/边界已通过，见commit-mmap review。
- 当前资源板块下一切片已写草案 **docs/cursor-task-assembly-lazy-defaults-2026-11-04-draft.md，未交Cursor**。Root新鲜解释器sentinel实证import assembly仍触发TTSEngine，文件顶端imports及四函数类默认值必须延迟；建议窄wrapper工厂、保留注入/kwargs/候选事务及兼容名称。勿假称整个text启动减重已完成。
- 然后仍需系统commit准入（物理RAM与commit分别预算、不盲增超时）、RVC重复heavy健康探针与失败sticky缓存显式重试；三模型等待板块未闭环，别提前开始预取。现Cosy lookahead草案必须先补真实stream_reply默认路线TurnSpeechPlayer按句调用tts.speak的跨句有界接口，否则仅单次speak内部预取不能改善主路径。
- 余下独立保行为精简/最终低配性能/Edge无界Queue和600s join/legacy每回复线程/TFM pin后待删与owned上限/缺RVC基础资产禁止运行时无timeout下载，保持以前roadmap；长真实soak用户排除，仍不做。

