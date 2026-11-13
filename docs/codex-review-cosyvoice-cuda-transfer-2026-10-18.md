## 2026-10-18 撤回闭环：PASS

24941a52已仅移除non-blocking切片，保留CPU staging和既有fixture修复。Codex读取恢复入口和关键调用，root init/alias probes通过；独立239 tests OK/35.535秒，miku WAV824→824零差。真实收益未证实的代码不保留。下一步为单独Windows interpreter ownership切片；目前没有活动模型测试或Cursor。最小睡眠Python复现实际解释器在redirector退出后仍活；管道EOF可以为True，不能用EOF替代进程树退出证明。该探针自己的child句柄已完整回收。
## 2026-10-18 实测后撤回（`docs/cursor-task-withdraw-cuda-transfer-2026-10-18.md`）

受控 probe 后决定回滚 non-blocking 切片：两模型 diagnostic60（Cosy 56.672s、AutoModel 42.157s、清理不完整 reader alive；peak RSS / min free RAM/GPU 见任务文档）未显示相对 CPU staging-only（Cosy 30.328s、construct 21.336s）的保留理由；启动开销差异使严格归因困难。default30 失败、无真实 synth。Cursor 已移除 helper/测试并恢复 staging-only worker；**本节及以下历史复审条目保留**，不代表当前栈仍含该适配器。

## Codex 2026-10-18 最终离线复审：PASS（实际收益待测）

返修d6deacf4后读取最终 helper/worker/测试调用边界；三项root contract probe与委托probe均实际通过。独立七模块261 tests / OK（64.740秒），历史miku WAV824→824，零增删；一次门禁通过，没有改root探针。显式CPU加载、仅三位置参数/同线程/精确三路径、成功一次同步/异常不重复等待、作用域恢复/初始化异常/跨线程/旧alias均闭环。允许下一步固定文本真实验证；尚未证明速度或三模型共存，不接App/不改默认配置。GPU设备同步的墙钟上限仍由父进程deadline保证，不宣称Python同步函数可取消。
# Codex review — CUDA transfer follow-up: changes requested

2026-10-18, Cursor e1227107. Read helper, runtime call sites, pinned vendor load, added fake tests and updated import contract. No real performance run on this revision. Independent tmp/codex_cuda_transfer_contract_probe.py reproduced all three below; original root probe must remain unmodified.

1. model_loading._optimized_cosyvoice1_load uses map_location=device. Runtime currently nests CPU staging so composed path works, but local loader silently depends on a second global patch to uphold its CPU-checkpoint/resource contract. Use explicit map_location="cpu" for three optimized loads; delegated original untouched. Fake tests must assert CPU even without outer scope. This removes accidental GPU allocation if helper is reused/isolated, without changing default behavior.
2. Completion synchronize exception enters broad except and calls synchronize a second time. On failing/stuck CUDA, repeating same wait is unnecessary. Track attempted completion sync; once attempted, propagate failure without second sync. Cleanup sync only for failures before completion sync (e.g. partially queued copy/eval), preserve original BaseException even if cleanup sync itself throws BaseException. Parent process deadline remains true bound; never fake successful ready.
3. patched_load explicit llm_model/flow_model/hift_model parameters normalize keyword calls, so keyword-only/mixed calls are optimized contrary exact-three-positional delegation contract. Use (self,*args,**kwargs): only len(args)==3 and not kwargs may optimize; delegate captured original with unchanged args/kwargs in ALL other cases, including missing args. No broad retry.

Complete tests in one bounded batch: sync success is after last transfer/eval and before return; strict/copy/eval/sync failure propagates with no false success and <=1 sync; cleanup-sync secondary exception doesn't mask first. Current other-thread test may run only after scope exits: add Event done handshake inside active scope and finally join/assert not alive (same for concurrent holder). Add stale alias A while B active. Retain exact descriptor restoration and initialization release; nested finally release protects lock if restoration raises. Do not broaden model/path/permissions/budgets or weaken other tests.

Run targeted helper tests then existing 120s seven-module gate once, compile/diff; stop on unexpected failure, no actual models. Update ADR/handoff, stop for Codex. Root delegation/contract probes must not be edited. No data/memory, vendor, default/timeouts/voice, commit/push.
