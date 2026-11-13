# RVC persistent worker — owned Windows process lifecycle

## 2026-11-02 恢复补充（本地 TTS 切片通过后才执行）

用户本轮明确要求完成退出/回收；持久 worker 先单独闭环，一次性 infer/probe 另一个切片，不能遗漏整体任务。没有授权本任务跨片直接开发预热/预取。

额外已核对：当前 _read_json_line 每次建一个 daemon stdout reader，client 不持有它；_release_exited_process 在 join stderr 前同步 close 管道、随后不检查线程是否退出就丢引用。必须同时覆盖现有 stdout/stderr reader 所有权，不只查询 Job 活跃数。可用窄内部 tracker/兼容可选参数等方式显式保存现有 reader，限制同时最多一个 stdout 读取；不得为每次超时再产生后台 reaper 或放任旧 reader 后重启。保持 free helper 的现有调用兼容。

进程树退出后先有界确认 reader；若管道读取仍活，保留 owner/reader，stop=False/retry，拒绝新分配。禁止在活跃 TextIO 读取线程仍持有锁时同步 close stream 造成无界等待；回收顺序及异常路径须有 Event fake 覆盖。process_alive 只表达进程状态时，重启准入还必须检查旧 reader 未结束。不要以 daemon=True 代替退出证明。

当前 root 门禁 tmp/codex_scoped_test_gate_2026_11_02.py 可指定明确离线模块，已通过 SafetyPolicy 执行监督器。新 gate 同样须使用 safe_subprocess_*，不能复制旧裸 subprocess/taskkill 路径。WAV 起点以实时集合为准（最近825），不要写死824。不要改 root 探针。

关闭路径不能再同步 write/flush shutdown JSON 后才开始有限等待：即使 idle，管道写本身也可阻塞。Windows owned worker 允许直接终止自有 Job（idle 无待提交工作），取消/关闭不等待管道写锁；保留非 Windows 兼容或明确用单个已有式有界 writer，不能每次 stop 新开线程。测试用阻塞 stdin 保证 stop 不调用其 write/flush 或仍在原有有界预算返回。

持久 client 的最小状态不变量：同一时间一个已发布/待回收 owner；start 尚在 spawn 时也算 busy/未退出，stop 必须使旧 generation 失效。若 stop 发生于 Popen 返回前，晚返回 owner 必须先接管再回收，绝不能在停止之后发布 ready 或丢引用。用短生命周期锁保护指针/启动状态，不能拿 operation 的长 IO 锁等待 stop。检查 _last_stop_exited 与真实 owner/readers/start_inflight 一致；任何未确认都 False。补 Event 控制的 start-vs-stop / 两个并发 stop / stopped-after-ready 反例，一批闭合，不在全量过后才补这些最核心路径。

主 Python312；RVC .venv-rvc/Py311；CLI 已恢复至工作区 tmp/cursor-cli-2026.10.22/dist-package/cursor-agent.ps1。使用当前任务期间的实际文件，不用不存在的旧 portable 路径。

2026-10-18. Execute only after Codex closes local-TTS Job review. Read AGENTS/architecture-contracts/collaboration-review/engineering-quality, docs/architecture/adr-local-tts-windows-owned-job.md, final local-TTS review and core owned_worker_job.py. Preserve dirty changes. User prioritizes responsiveness, robustness and strict code review. No models/main/devices/data/memory, defaults/voice/DSP/timeouts changes, commit/push. Only persistent RVC worker; single-shot infer/probe are an explicit later slice.

Confirmed: .venv-rvc uses the same Windows redirector arrangement as Cosy venv. spawn_rvc_worker currently returns bare Popen; RVCWorkerClient._terminate_process stops only redirector, allowing actual interpreter to survive. Existing parent watchdog is fallback, not proof of in-session shutdown. Reuse proven parent-owned Job + early self-join, not a second implementation/framework. Do not change Native Job logic that passed independent fake/native checks.

1. SafetyPolicy.spawn_rvc_worker keeps sealed interpreter/model/index/argv validation and Operation.RVC_WORKER before effects. On Windows require launch.parent_pid==os.getpid(), then use the existing owned helper. Introduce a neutral alias spawn_owned_voice_worker = existing function if useful, retain existing local-TTS entry/name/env contract for compatibility; do NOT widen name parsing or permissions. Non-Windows unchanged. No generic subprocess fallback. Ensure absent env preserves the old Windows/Python execution environment rather than replacing it with only two Job vars: copy the existing parent env as old RVC behavior when no env provided, then overwrite internal Job fields; explicit env handled unchanged except private fields. This slice does NOT claim RVC env/privacy hardening or thread cap improvement; no broad new env policy/config.
2. scripts/rvc_worker.py __main__ joins before src.voice/numpy/torch imports, using same stdlib-only core bootstrap. Existing module import/test injection stays effect-free. Defer the current top-level src.voice.rvc.vc_params import without unrelated restructuring. Missing/closed job or join failure exits2 before model side effects. No tone/config/device changes.
3. RVCWorkerClient._start_locked adopts pending owner from ANY spawn BaseException before handling/logging/propagating; use established take helper. Preserve ordinary public failure and KeyboardInterrupt/SystemExit identity. A partial/live/unknown owner remains _proc and blocks restart. stop retry may release only when raw+Job+existing stderr reader quiescent; no fake registry clear, no extra reaper threads. No holding a newly introduced lock over model/network IO.
4. Unit fakes explicitly inject Job backend via bind_process_runners. Never infer fake containment from callable identity. Keep caller capability rejection tests zero-spawn and existing dry-run. Verify normal spawn/ready, timeout, cancel during load/convert, redirector-exited/interpreter-active incomplete, pending PermissionError/control-flow owner, close failure + retry, no new worker while old alive, early bootstrap before heavy imports. Existing TTS/localJob tests must remain unaffected. Don't change acoustic parameters or loosen assertions to fit fake backend.

Save tmp/cursor_rvc_owned_job_gate.py with true120s tree timeout, UTF8, exact data/tmp miku WAV set (currently824) invariant. Include new RVC-owned tests, test_rvc_worker_lifecycle, security3a/3a_review fixes as relevant to capability runner changes, test_owned_worker_job, localTTS slice_a/b and mode assembly tests. Inspect actual module names first. Run once after targeted fakes; unexpected failure STOP, no repeated求绿. Compile/diff, brief ADR/handoff. Stop for Codex strict review. Root independently runs model-free .venv-rvc Job tree proof before real RVC audio. Real model coexists/long soak and single-shot RVC infer/probe containment remain uncompleted.
