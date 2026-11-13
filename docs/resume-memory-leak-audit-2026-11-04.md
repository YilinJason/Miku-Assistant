> 历史恢复记录：2026-11-05额度恢复后已继续。HTTP切片Root最终通过，RVC3×10、双模型六句、三模型准备后六句及退出已实际验证。当前进度以docs/current-delivery-status-2026-11-04.md及docs/codex-real-memory-short-test-2026-11-05.md为准；以下旧“暂停/唯一未验收”不再有效。

# 内存泄漏专项恢复入口

按用户要求，2026-11-04在Codex主额度primary used90%/剩10%停止；周额度忽略，不自动reset。所有官方Cursor/监督器已结束，没有在途模型设备任务。Root仅写此交接，不继续开发或测试。

当前请求是用户严重怀疑Miku内存泄漏，要求检查。Root系统只读诊断+源码审查已实际复现两项应用层缺口：Ollama预热线程未纳入默认Core关闭、unknown Thread.start确认失败后丢owner可重复起线程。已由官方Cursor修复，Root独立8项llm_warmup_root8_final_20261104c与8模块152项llm_warmup_repo_final_20261104f通过；100次实际线程/fake请求循环client weakrefs0、线程1→1，WAV826零差。完整证据docs/codex-review-llm-warmup-leak-2026-11-04.md，勿重做已通过切片。

当前唯一未验收切片：HTTP响应显式回收。官方Cursor会话4183f10b-471d-4193-b7cf-0a068d278ae6，最新exec33799已STOP/exit0；src/llm/ollama_client.py、src/memory/ollama_extractor.py与新tests/test_http_response_lifecycle.py已改。初稿Root发现memory redirect重复close和无close fake删除类方法错误，返修已修；Root看过最终redirect分支，response先保存status再置None，finally不双close。需要继续审新tests（整文件约425行）、fatal原对象覆盖：Cursor摘要误说KBI，实际新增KeyError/SystemExit/GeneratorExit/custom；应确认/补KeyboardInterrupt，不能把KeyError当fatal。普通client/内存业务错误分类保持。

尚未运行HTTP新仓库模块或最终Root3。下一步：读AGENTS、四repo skills、安全规则及HTTP任务文档；保留巨大dirty，不回滚/提交推送；正式data/memory禁读写删。比对tmp/http-release-root-before-20261104两个源快照确认只有finally与redirect窄修，审所有fake（无设备/HTTP/正式DB）。只用tmp/codex_scoped_test_gate_2026_11_02.py唯一监督器、唯一新label，顺序运行：Root旧3矩阵tmp/codex_llm_warmup_leak_root_2026_11_04.py（含HTTP成功close检查）及tests.test_http_response_lifecycle，随后适当相关client/stream/记忆/安全模块。不能raw/discover/并行或覆盖既有报告；新fixture修复仍由官方CursorCLI，Root独立评审。HTTP通过后更新审计与当前状态，再考虑资源安全的真实模型短测试。

系统归因还未知：旧物理内存约63GiB、可用1.4GiB，内核池10.5GiB，工作集总14GiB且没有Python/RVC/Cosy进程，未归属大块不能全算Miku。用户这轮之前已重启（OS boot 11/04 19:16:41纽约），可用恢复约47.6GiB；20秒三个只读采样available48432→48380→48372MiB、内核池稳定约2.5GiB，无模型进程。没有重启/清内存/停止用户服务操作。报告docs/codex-memory-leak-audit-2026-11-04.md。

真实模型长期泄漏尚未验证，旧三模型共存失败仍有效。当前ollama.keep_alive=30m、unload_on_exit=false为现有缓存配置，未擅自变更。真实诊断必须使用已有资源监督与显式注入/回收新LLM guard、close_warmup，不运行旧裸smoke旁路。30–60分钟语音长测按既有约定后置。本轮未启动真实模型/音频设备或正式数据库。
