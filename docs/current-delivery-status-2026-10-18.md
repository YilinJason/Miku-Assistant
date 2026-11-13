> **已按额度规则停止（2026-10-18）**：Codex primary used90，剩10%；下次刷新 2026-10-18 05:01:31 America/New_York（UTC 2026-10-18 09:01:31）。已中断自有Cursor dbb7794f / exec44617（exit1）；只读CIM确认无匹配Cursor/门禁测试/本项目模型worker残留。没有新提交或推送。停止前的fixture修改仍未交付/未独立验收，不能标绿，不能继续RVC或真实模型。恢复先查实时额度，再读取当前测试helper与git diff；完整恢复步骤见本文件。用户真实data/memory未读改删。
> 最新状态：primary used88（剩12%），尚未触发10%停止。当前Cursor dbb7794f/exec44617仍工作，测试fixture未交付/未独立验收。53b96601四组root probes全通过，但root最终八模块290出现既有blocked-write前置entered.wait(1s)失败（不是关闭超时），保持未放行。本次先仅收集状态，禁止启动新模型/RVC任务。特别复审新_BlockedWriteWorkerProcess：需_run finally显式stdout.close、wait真实有界线程等待；support teardown不能吞异常/留下线程；controlled测试的stop线程异常必须主线程收集。当前未确认这些点已完成。当前文件下方历史仅恢复参考。
> 最新覆盖（2026-10-18）：Windows Job 首版f5b298e2→返修ba46cd75，root四项contract probe已通过；原六模块独立239 OK/62.545秒/824零差。**尚未放行**：root partial-spawn 行边界中断探针确认Popen已返回时，cleanup超时/Close失败会丢失owner。当前Cursor f25ca603-b221-4ca8-b28e-843c1e606928 / exec28829，按具名评审末尾补充实现保留owner→异常交接→client接收/关闭重试；完成后须root复审+真正八模块门禁，再无模型真实Windows父退出探针。未跑新的真实模型，资源采样/试听/长测与后续任务均未完成。最近额度短周期剩49%、周78%，实时接口优先。
# 当前交付状态（Codex，2026-10-18）

本文件为当日简明状态；历史在 delivery-follow-through-2026-10-17.md 与具名评审。未提交、未推送，真实 data/memory 未读改删。

## 已闭环

- CPU checkpoint staging 的初始化/别名边界修复及 EOF/启动进度测试替身修复。root探针、20次EOF通过；独立239项OK/46.496秒，miku WAV824→824。
- CUDA non-blocking加载切片曾独立261项OK，但实际未证明性能收益，已由Cursor24941a52定向撤回。Codex复审及独立239项OK/35.535秒；CPU staging及fixture修复均保留，WAV824不变。禁止把该切片记为现存优化。

## 当前真正阻塞

Windows venv redirector退出不代表实际解释器退出。root最小无模型睡眠进程复现（tmp/codex_venv_redirector_repro.py）：两PID不同，redirector退出后实际child仍活，stdout EOF甚至可为True；root通过事先持有的自有child句柄完整回收，无模型/设备。既有client.poll/terminate只看redirector可能漏掉资源占用，是优先修复项。

当前Cursor f5b298e2-aaa5-4aea-a690-0546b71f846c / exec73583；任务cursor-task-local-tts-windows-job-2026-10-18.md。方案：SafetyPolicy内父持有唯一命名Job；实际runtime在重依赖导入前self-join，只分配当前进程，立即关闭子job句柄；父生命周期只控制该自有Job+redirector。无任意PID/taskkill权限、无registry。等待严格Codex review与无模型真实venv/父退出探针，通过前不跑真实模型。后续RVC的持久worker/单次infer/probe同类边界也须里程碑检查，不假称已修复。

## 真实性能仍未通过

- CPU staging-only两模型diagnostic60 69fb748168cd成功：Ollama7.093/Cosy30.328秒（construct21.336），完整回收；生产30秒仍失败。
- 三模型297c4f1249d5失败：Ollama6.547/RVC38.172/Cosy32.062秒，零音频、完整回收。Ollama context8192/显存8360946564字节。
- 已撤回non-blocking版本：default30 2c218c419c48失败；diagnostic60 e127287e961f ready但Cosy56.672秒/construct42.157秒，cleanup incomplete(reader alive)。峰值测试树RSS3820MiB、Private6484MiB，系统RAM至少30198MiB可用、显存至少3198MiB可用；不是资源耗尽证据，也非长期无泄漏证明。
- root诊断监督器已增加每5秒资源采样（tmp/codex_windows_resource_sampler.py，Windows原生只读、无需安装psutil，最多40条）；180秒进程树超时不变。采样只覆盖自有测试树与GPU全局量，不采集其他程序命令或内容。

## 剩余交付

App owner接线C2b2b尚未开始（draft禁止提前执行）；C2c有界预取/长句预算/播放取消、D真实30–60分钟长测、跨模块里程碑审查、版本整理/Git检查点尚未完成。默认仍Edge/local_tts许可false，未启用麦克风或扬声器做审计。原始01_warm_companion_rvc.wav基准保留，新生产输出主观验收未完成。

额度最近短周期剩60%、周80%；以实时接口为准。任一Codex额度剩余<=10%时停止Cursor/模型测试，保存恢复状态等刷新。
