# 开发进展核对（2026-11-02）

## 结论与核对范围

当前是“已有可用助手 + 已落地的安全/记忆/基础陪伴改造 + 尚未完成验收的可选自然语音后端”，不是整体交付完成。主线卡点仍是语音资源生命周期、主程序预热接线与真实性能，而非长期记忆尚无实现。

本次读取实际实现、调用方、配置、Git 状态及具名评审；未启动主程序、Cursor、真实模型、麦克风、扬声器或自动清理，未读取/修改/删除正式 data/memory。未改生产代码，未提交/推送。历史的通过记录与本次实测分开列示，测试数量不跨轮相加。

## 当前版本状态

- 分支 yilin_dev，HEAD 7dff31b（2026-09-29，存储治理与资源生命周期 Round 2 检查点），本地比 origin/yilin_dev ahead 2；本次未 fetch，不能据此推断服务器最新状态。
- git status --porcelain：65 个已跟踪变更项、119 个未跟踪条目（目录条目不等于文件数）。本次保留全部原有修改。
- 本地交接及恢复记录最新阶段仍为 2026-10-18 的额度停止；未找到更新的最终交付记录。不能把日期经过或额度刷新解释成后台已经完成开发。
- 当前 YAML：Edge TTS；local_tts_worker=false；长期记忆与 retention=true；固定 warm_balanced 人格=true。CosyVoice 的存在不等于当前主程序已在使用它。

## 模块进展

| 模块 | 实际代码状态 | 验证与剩余边界 |
| --- | --- | --- |
| 文本/语音对话 | 有模式装配、按需语音加载、流式分句、有界播报队列、取消与关闭入口 | 已有基础能力；不能等同于语音端到端无停顿或声学回声消除 |
| 长期记忆 | SQLite 持久化；只从 turn.user_text 枚举本地事实单元；模型选择 unit_id；本地 render 生成入库文本；召回和维护已接装配 | 本次相关故障回归通过；未检查正式库内容，不能保证任意自然表达都能提取或回答正确 |
| 存储治理 | 历史轮数上限、日志轮转、临时文件所有权/延迟删除、有界扫描、空闲 SQLite prune | 有实际实现，但不涵盖全部嵌套评估产物或所有磁盘增长，详见下节 |
| 安全 Round 3 | 具名 RVC/本地 TTS 启动能力、固定路径/argv、远端模型/明文 HTTP/云语音分权、日志隐私边界 | 2026-09-30 里程碑记录通过；本次重跑其部分离线契约通过。不是操作系统沙箱，也未进行渗透测试 |
| 基础陪伴 | 固定人格、唯一 chat system 消息构建、仅对朗读副本做语音投影、Edge 句末韵律 | 既往里程碑通过，本次相关离线测试通过；不能证明真实听感已自然 |
| CosyVoice 可选后端 | 独立 worker/协议、TTSEngine 后端、共享临时文件管理、显式 prepare、CPU checkpoint staging、独立三模型预热协调器 | 已有离线及固定短句成功记录；当前默认未启用，主程序完整激活接线未完成 |
| Windows 本地 TTS 退出 | SafetyPolicy 本地 TTS 路径接入 owned Job；runtime 在重依赖导入前加入；异常保留 owner、关闭可重试 | 本次 fake 生命周期测试通过；历史真实无模型停止/自然退出/父退出三项通过；最终整片回归仍未收口 |
| RVC 退出 | SafetyPolicy.spawn_rvc_worker 仍直接调用 _popen_fn，client 终止/等待原始 Popen | 尚未接入同类 Job 进程树所有权；一次性 infer/probe 也仍是独立待办 |
| 表现层/Quest | character_interface 仍为扩展占位 | 不能描述为已接通 3D、VR 或已确定引擎 |

## 本次确认的关键缺口

### 1. RVC 实际解释器的退出尚未获得与本地 TTS 相同的证明

`src/core/safety.py:601` 的 spawn_rvc_worker 返回裸 Popen；`src/voice/rvc/worker.py:458` 只 terminate/kill/wait 该对象。Windows venv redirector 与实际解释器不同的历史复现仍适用这类启动方式；当前未看到 RVC 采用 src/core/owned_worker_job.py。这里是已确认的生命周期覆盖缺口，并非本次已观察到一个正在泄漏的 RVC 进程。

应先闭合本地 TTS 当前切片，再将已验证机制用于持久 RVC worker，并分别验证父退出、超时、异常启动和 stderr reader。单次 infer/probe 另行收口，不据一个入口修复声称整个语音链路无孤儿。

### 2. 当前 blocked-write 测试替身仍不足以给最终回归背书

`tests/support/cosyvoice_blocked_write.py` 的 BlockedWriteTeardown.run 吞掉 shutdown/stop 异常，join 后未确认合成线程已退出。`tests/test_cosyvoice_production_slice_b.py:427` 的专用 worker 只有 terminate/kill 关闭 stdout，尚无其自身 _run/finally EOF 和真实有界 wait 实现。`test_stop_phase_waits_for_blocked_write_rendezvous` 的后台 stop 线程直接执行断言，未把异常收回主测试线程，finally 也未统一回收它。

这说明额度停止时的测试修改仍未完成验收。本次未重复执行已知未收口的 290 项门禁，不将旧失败冒充新失败，也不将 139 项专项通过冒充该门禁通过。下一轮应由 Cursor 仅修 fixture，Codex 复审后运行一次完整相关门禁；不能通过放宽生产等待或重复运行至绿解决。

### 3. “对象已构造”与“本地模型已准备好”尚未在主程序统一

`src/core/assistant.py:280` 在已有 tts_engine 时直接返回成功；新建路径调用的仍是旧 run_coordinated_warmup。装配同样使用该旧协调器。新的 prepare_local_voice 尚未被这些主入口调用，TTSEngine.prepare_local_tts 目前只是显式 API。因此不能声称用户进入语音模式时三模型已经成功预热。

App 接线必须同时考虑未发布引擎、在途 loader、取消、关闭重试和共享 TempFileManager 所有权，不能简单插入一次同步 prepare。

### 4. 顺序合成仍会形成句间等待，真实共存性能尚未过关

`src/voice/tts/cosyvoice_backend.py:374` 明确采用合成、转换、播放逐段串行，没有有界预取。历史 C1 的热短句含 RVC 约 5.8–8.3 秒，不含设备播放；最大实测单段 43 字，不代表配置上限 120 字已验收。

历史 C2b2a/恢复记录仍写明三模型共存失败（一次 Ollama 6.547s / RVC 38.172s / Cosy 32.062s），不能改写为性能通过。CPU staging 已保留；未证明收益的 CUDA non-blocking 适配器已撤回。新一轮真实测量尚未运行。

### 5. 存储维护有范围，不是整个工作区的容量硬上限

- 当前日志 max_size_mb=10、backup_count=5，主日志及轮转文件约 60 MB 的配置量级；不代表整个 logs 或工作区的绝对上限。
- FileCleaner 每轮扫描最多 500 项、删除最多 100 项，每 10 分钟调度，启动不立即扫描。清理匹配的直接目录文件：临时音频 1 小时、日志 1 天、测试输出 72 小时。
- `_cleanup_rule` 忽略子目录，不递归；测试文件仅匹配 test_*.wav、test_*.mp3、miku_test_*。嵌套语音评估目录、模型缓存与一般根 tmp 产物不受这套 TTL 全覆盖。
- **校正旧记录的推断**：选定参考音频位于 data/test 的嵌套目录，名字为 01_warm_companion_rvc.wav；不能仅因超过 72 小时就推断当前 FileCleaner 会删除它。此前保护副本仍有保留价值，但旧“超 TTL 必误删”解释不准确。
- SQLite prune 默认每 24 小时、仅空闲且取得维护 lease 后运行，每批最多 64 条。max_records=2000 与 365 天是维护目标；preserve_user_confirmed=true 可使受保护记录超过目标。删除记录复用库页，不等同于数据库文件立即变小；热路径不做 VACUUM。
- 磁盘占用与 RAM/显存泄漏是不同问题。要判断运行时持续增长，仍需进程树 RSS/Private、线程、GPU、在途任务和临时文件的长测数据。

### 6. 默认语音仍有云端数据流

cloud_stt=true、cloud_tts=true，分别允许 Google 识别上传录音及 Edge 合成发送朗读文本；远端模型、通用 subprocess、shell、注册表与电源默认关闭。权限已经分离，但不能把当前默认称为纯离线或零外传。此为当前显式配置，不是本次发现绕过权限。

## 本次实际验证

主解释器已确认 Python 3.12.3。一次运行：

```powershell
& 'C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -m unittest tests.test_security_round3_milestone tests.test_natural_companion_round1 tests.test_natural_companion_round2 tests.test_memory_soak_fix tests.test_owned_worker_job
```

结果：139 tests / OK，unittest 计时 2.517 秒。使用 fake、mock 和隔离临时目录；未启用实际模型或设备。另对 src 118、scripts 5、tests 53 个 Python 文件做 ast.parse，共 176 个，语法错误 0。没有运行全量回归、当前 blocked-write 组、真实音频或 30–60 分钟长测。

既往 604/674/957/1007 等测试记录属于不同日期、不同范围；不能相加或当作今天的全量通过结果。

## 建议后续顺序

1. Cursor 收口 blocked-write fixture → Codex 最终审查本地 TTS Job 切片 → 一次有总时限的相关回归。
2. RVC 持久 worker 进程树回收及异常 owner 保留 → 单次 infer/probe 生命周期边界 → 无模型真实退出验证。
3. 重新测三模型共存的冷/热加载、前台等待及资源峰值；找到瓶颈后再调整，避免盲目加大超时。
4. App 预热/关闭接线闭环，再做有界预取、长句预算和取消交接；保留用户选定 Nahida 基准，不默认换音色。
5. 隔离记忆库的 30–60 分钟真实资源长测及人工音频验收，覆盖连续对话、插话、错误、空闲和重启。
6. 最终跨模块里程碑审查、评估产物保留策略、文档整理与明确范围的可恢复 Git 检查点；不推送，不提交模型、私人数据或测试音频。

以上是本次审查建议，未派发新开发任务。当前实现已具备较多工程保护，但在真实共存、完整退出及长测闭环前，不能承诺运行永不卡顿或已经无内存泄漏。
