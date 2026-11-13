# 长测协作停止：Root源码与离线门禁通过，真实取消待测

官方Cursor91265b8e-2f72-40be-a25f-5fe396bb9362已STOP，仅3个既有tmp与新signal模块。生产/config/vendor/正式memory未改。固定basename读信号，Event同步复核，无后台轮询线程；pure loop有cancelled状态与SoakStopped，部分row/elapsed保留。准备前、准备后、活动turn与idle均可取消；子进程写report并实际验证已构造资源quiet后退出130，parent operator_stop保持非零。

父最多6秒协作等待与hard fallback/server清理共用原10秒deadline；低资源/timeout不走协作延迟。cooperative_stop_child_quiet根据owned quiescence而非raw退出码；Native OwnedWorkerProcess.poll本来已检查Job，新增诊断检查主要防御其它owner适配器，不宣称发现生产RVC进程泄漏。

Root审查修复：零时钟首次信号被false cache漏掉、正常wait超时错误计数、准备期取消误判、child先退出导致parent误报成功（独立11项首次10过1败，原报告保留）；最后一项已实际复现并定点修复。

验证：

- cooperative_root11_final_nov5b：11项OK，信号路径/缓存/latch/metadata错误、Event wait、idle/active取消、旧state拒绝、协作child不kill、stubborn两owner共享<=10秒、先退出取消与raw已退Job仍需回收。
- cooperative_harness12_nov5c：12项OK，保留原RVC/首段/fatal路径，新增真实Timer的活动取消、实际TFM-owned私有WAV清理、prepare前取消不构造任何模型，report取消且exit130。
- cooperative_soak8_nov5d：原8项OK；cooperative_diag24_final_nov5f：原24项OK。
- 24项首次一次失败来自旧FakeChild.kill无条件把已完成的raw code0改成-9；对照真实OwnedWorkerProcess已returncode时kill无操作的契约，修正fake仅未退出时变码，保留code0断言并增加必须尝试回收unquiet Job的断言。不是为绿灯放宽生产契约，原失败记录cooperative_diag24_nov5e保留。

所有离线gate共享WAV828→828，无增删。重启本轮前发现828而历史已验收826，两个文件仅mtime与前次强制取消一致，归属未确定，保留原文件；精确name/dev/ino/size/mtime基线tmp/codex_wav_baseline_resume_20261105.json。真实取消必须对比此基线，不能只凭进程quiet说文件清理通过。

真实取消已通过：cooperative_cancel_nov5g，129.27秒，steady94.172秒已成功6turn/6音频，cancelled=True/complete=False，子130/父124(operator_stop)为预期取消状态。App/Timer/speech consumer/aggregate/TFM/warmup/guard均实际True，cooperative_stop_child_quiet=True、父回收0.14秒、无cleanup_errors。私有临时文件managed owned/pending/inflight/leases全0；与精确828基线相比无新增/删除/身份大小mtime变化。报告data/test/voice-naturalness/cosyvoice-production-soak-30/60837ef55a84/report.json。接受协作停止切片，不能将它算作30分钟PASS。
