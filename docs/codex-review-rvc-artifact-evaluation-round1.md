# Codex Review：RVC 伪影评估 Round 1

- 日期：2026-10-17
- 状态：FINAL ENGINEERING PASS；真实矩阵24/24通过，等待人工听感选择
- F1-F4闭环；生产集成仍待真实听感选择。

## 2026-10-17 切片复验

F1/F2/F3 代码返修已提交交接（见 `docs/cursor-handoff-status.md` 顶部）；离线全量 768 OK。待 Codex 代码复验与真实 `--execute` 矩阵；Cursor 仍不得跑真实矩阵。

## F1：原始矩阵依据错误，RMVPE 不受 filter_radius 影响

本机 `.venv-rvc/Lib/site-packages/rvc_python/modules/vc/pipeline.py` 的 `get_f0()` 仅在 Harvest 分支使用 `filter_radius>2` 执行 median filter。RMVPE 分支未使用该参数。因此 idx025_fr1/idx025_fr3 是等价配置，不能验证 F0 平滑。`protect<0.5` 才保护无声/清辅音特征，0.5 为关闭，现有注释“越高越保护”相反。

替换为最小有效矩阵（所有 profile 固定 `filter_radius=1`、RMVPE、pitch=0、rms=0.68；不改变生产值）：

| id | index_rate | protect | 目的 |
| --- | --- | --- | --- |
| idx025_p050 | 0.25 | 0.5 | 单独降低检索 |
| idx055_p033 | 0.55 | 0.33 | 单独开启清辅音保护 |
| idx025_p033 | 0.25 | 0.33 | 组合候选 |
| idx000_p050 | 0.0 | 0.5 | 无索引控制 |

基线可继续引用现有 idx055_fr1，但报告明确其 protect=0.5。profile dataclass 显式携带 protect，精确白名单校验并报告实际参数。修正当前 YAML、vc_params、scripts CLI、相关 docs 中 protect/filter_radius 的错误说明，数值默认保持不动。不要只为文案改变行为。

## F2：停止失败时丢失进程持有者，后续可重复加载模型

`RVCWorkerClient.stop()` 开头就 `_proc=None`；即使 wait/terminate/kill 后仍存活，也无法再 process_alive 或重试。`stop_rvc_worker()` 又无条件清空全局 client。Codex fake 复现：stop=False、client_retained=False、running_after_failed_stop=False。

要求：保留存活进程与 client 引用，直到 poll 确认退出；失败可有界重试。`get_rvc_worker()` 重建前若旧进程未确认退出应拒绝创建新对象；同 client `_start_locked()` 同样 fail closed。取消、超时、初始化失败的终止路径均审查，不得遗失活进程引用。不要用 mock 特例决定生产语义。补 stop失败后仍可观察/可重试、禁止重建、稍后退出后允许重建的行为测试。

## F3：copy2 写出部分 .publishing 后抛错，清理列表遗漏该文件

`_publish_profile_files()` 在 copy2 成功后才 `scratch.append(publishing)`。磁盘写满等场景会留下部分文件，而 except 无法清理它。必须在开始写入前纳入本次所有权/清理计划。追加测试：fake copy2 先创建部分目标再抛 OSError，断言非零、非 complete、无 publishing 残留，原输入哈希不变。

## 验证与交接

Codex最终复验：111项相关专项OK、全量773项OK（skipped=4），compileall/diff-check/dry-run通过。F2停止失败保留进程，F4拒绝重建与并行fallback，F3半成品清理均已代码复审。沙箱文件重命名受限，测试在沙箱外以相同命令复跑通过。尚未据此宣称真实音频或长时间资源通过。

## 真实矩阵结果

Codex执行`diagnose_rvc_artifacts.py --execute`退出0，4组各6/6，24个WAV可读且bytes与报告一致；报告complete=true、worker_stopped=true，总计90.4s。

| profile | index_rate | protect | 单段中位数(s) |
| --- | ---: | ---: | ---: |
| idx025_p050 | 0.25 | 0.5 | 1.055 |
| idx055_p033 | 0.55 | 0.33 | 1.066 |
| idx025_p033 | 0.25 | 0.33 | 1.060 |
| idx000_p050 | 0.0 | 0.5 | 0.204 |

filter_radius均1，RMVPE；基线12文件SHA256均未变。结束后无staging/publishing/miku临时音频或Python进程，显存1659MiB，与启动前1656MiB基本一致。此短时矩阵不能证明30–60分钟无泄漏，长测尚未执行。

结论：本切片工程/资源回收门禁通过。已交付同句基线与四候选供人工试听；未将任何候选写入生产参数，未把“电流音已消除”记为完成。

### F4：Worker仍存活时fallback/worker_disabled可并行冷启动

Codex fake确认：worker.convert=False且process_alive=True、fallback_on_worker_failure=True时，run_rvc_infer_subprocess仍被调用，返回True。即便生产默认fallback关闭，也必须确保显式fallback不会在停止失败后加载第二套模型。worker_disabled与策略更换路径也应在任何一次性启动前确认旧Worker退出，未确认时返回False。

get_rvc_worker重建失败应抛明确的RvcWorkerUnavailableError（或等价窄异常），保留旧client，不能返回不匹配的配置/策略实例；warm_rvc_worker/run_rvc_infer捕获后返回False且不fallback。原始底层异常不得在这里统一吞掉。

补行为测试：旧Worker活着且停止失败时，fallback=True、worker_disabled、策略变化均零一次性启动/零新client；旧进程退出并清理后允许正常启动。测试替身必须显式定义process_alive，不为Mock修改生产判定。

F2主体代码已读：保留活进程与管道、超时同步状态修复可接受；F4已交付 infer 层 fail-closed（窄异常 + fallback/worker_disabled/策略切换前确认退出），待统一专项/相关/全量复验。当前F1旧文案已修，参数数值未改。

先修复并专项/相关/全量、compileall、diff-check、dry-run。保持生产参数、真实 memory 和既有未提交修改。Cursor 不跑真实 --execute，不提交/推送。更新交接顶部停下等待复验；本轮不实现 CosyVoice 生产集成。
