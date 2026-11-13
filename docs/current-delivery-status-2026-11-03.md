# 当前交付状态（2026-11-03）

**用户最新覆盖：周额度阈值不再停工，已授权继续。** 保留短周期remaining<=10%停工；不购买额度或自动消费reset credits。后续继续三模型共存→有界预取→精简→性能，原周额度停工段落为历史。

**最新：短周期阈值停工（覆盖下方历史叙述）**：周额度不再停工。最终读取短周期used90%（剩10%），按用户规则已停止；Cursor与Root测试均结束，恢复现场见resume-after-quota同日文件顶部。三模型默认及mmap隔离对照均FAIL；mmap对照Ollama120.016秒超时，未到RVC/Cosy，外层回收全部quiet，总129.23秒。禁止把mmap推广生产。报告tmp/codex_real_three_model_mmap_commit_root.json。隔离launcher及commit轻量监督47个Root边界通过；实际CIM/11445复查无自有模型残留。

语音五个包lazy导出已Root最终PASS：46tests/13.150秒OK(skip1)，WAV826零差，compileall/diffcheck通过；初审四项已修复。任务docs/cursor-task-voice-lazy-exports-2026-11-04.md、review同名codex-review文件。assembly显式TTS导入尚未解决，不宣称整个文本启动减重。三模型资源预算、预取、精简、最终性能仍未完成。

本地SDK导入优化已经Root138测试OK(skip1)+10边界通过，且一次两模型真实prepare-only成功：RVC24.172秒、Cosy18.265秒（原30.141秒超时），wetext导入34.576ms（原14.391秒）；默认预算/声学不变。总44.8秒，首轮完整aggregate/TFM/Job quiet，CIM无自有模型残留。报告tmp/codex_real_two_model_local_sdk_root.json。三模型共存复测、真实音频输出、有界预取、独立精简、最终性能/低配仍未完成，下方早前在途叙述以本段覆盖。

用户额度刷新后已恢复。Codex设计及独立review，官方Cursor CLI实施；每切片闭环后下一项。未提交推送，正式data/memory未读改删，未开音频设备或主程序，保留既有dirty工作区与Nahida音色。

## 已验收

- 原blocked-write fixture、RVC单次owned-run/持久IO lease/传播聚合，以及App同步lane/未发布候选所有权：此前分层通过，证据见对应具名review。
- 前台有界request：Root156回归OK；最终interrupt4/foreground5/同步9/shutdown6独立通过，WAV826零差。冷加载期间文本可用，未知线程/候选不会提前关闭TFM。review：docs/codex-review-voice-foreground-2026-11-03.md。
- 开发监督器：Root16 fake+6独立故障边界、真实嵌套Native Job三PID0.578秒回收；仅tmp工具，控制流分层PASS。循环不跑GPU外部查询，单cleanup预算10秒、错误不伪成功、Policy/metadata读取有界。review：docs/codex-review-real-diagnostic-supervision-2026-11-03.md。

## 真实结果与当前在途

三模型仍FAIL。恢复后一次自有11445对照：Ollama88.5秒，RVC87.297秒准备失败，未到Cosy；aggregate未完成。旧监督收尾慢，Root手工中断，随后确认自有Python/模型/11445均结束，用户Ollama服务未动。详见docs/codex-review-coexist-control-2026-11-03.md；不以源码/单测绿灯冒充真实性能达标。

两模型路由已Root8fake通过，并完成一次不加载/连接Ollama的prepare-only真实对照：RVC24.453秒成功，Cosy30.141秒超时（wetext导入14.391秒、Cosy导入4.419秒，constructor未及30秒）。总56.78秒，首轮完整六字段shutdown/TFM/NativeJob quiet全部True，最低空闲RAM14285MB、树RSS峰5481.8MB；无音频合成。报告tmp/codex_real_two_model_control_root.json，CIM后查无自有模型残留。

当前Cursor执行docs/cursor-task-cosy-local-import-startup-2026-11-03.md：专用worker本地导入适配，避免为已固定本地资产导入整个Modelscope SDK；先fake验证精确恢复/未知请求拒绝等，不改vendor/site-packages/声学/默认30秒。真实提速尚未验证，预取不提前放行。

## 仍未完成

1. 真实共存慢/退出失败的控制变量诊断与修复。
2. 一段有界预取：规格docs/cursor-task-cosyvoice-bounded-lookahead-2026-11-03.md已写，尚未放行。
3. 独立保行为代码精简，以及最终等待/低配/内存文件线程上限优化。Edge无界Queue/600秒join、legacy每回复新线程、TFM pin后删除重试/owned上限仍须处理；详见delivery-roadmap。
4. RVC缺基础资产时第三方自动下载无timeout：当前真实文件齐全，不是本次失败的已证原因，后续安全/性能切片需本地缺失快失败。

用户本轮排除30–60分钟真实长测，仍保留待办。仅短周期remaining<=10%停止Cursor/测试并保存，实时值优先，不消费reset credits。官方CLI会话06f27f0e-7f51-4689-9e19-046fc05a34eb，模型Composer2.5；高阶Cursor模型额度不足未加付费限额。
