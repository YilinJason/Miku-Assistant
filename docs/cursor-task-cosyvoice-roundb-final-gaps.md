# Cursor：B剩余缺口精确闭环

读取AGENTS与相关skills，保留所有未提交工作。上轮4d8fb18f已改部分，但仍不通过，不进入C。只修下面这些原有评审要求；无真实模型/设备/生产YAML/data-memory/提交推送。

## 1. 输出身份必须贯穿所有打开操作

`output_verify.py`当前仍capture/stat/info/read按路径打开多次。Codex新探针`tmp/codex_cosy_b_residual_probe.py`证实：在sf.info之后替换同尺寸正常WAV，父验证返回true。

设计：对原始output_path先词法固定TMP/reparse检查，之后TFM所有权；父打开一个只读句柄，fstat与最初identity一致，用同一个句柄创建SoundFile、检查格式/frames/rate/channels/duration/bytes，按小块验证finite（不要整文件解压无界分配），验证后再次确认路径仍指向该句柄身份。禁用sf.info(key)+sf.read(key)这类重开。父caller传已验证repo/tmp边界，独立helper测试可提供受信临时root，不能mock生产特例。

worker现有fd持有跨inference是进展，但run_inference_instruct_to_wav只拿Path，没拿inbound.output_dev/ino。validate返回至os.open之间替换时，新fd被当成合法owner。必须传expected_dev/ino到打开处并立即fstat比对；不匹配绝不truncate/write。保留生成期间/写前路径身份复查。累计字节/样本检查应在float32复制之前以shape/numel估算，PCM16头预留44。补两处替换race测试、原始链接/dotdot在父拒绝（不是TFM resolve后才验）、NaN/超上限/正常WAV成功。仍由同一个owned inode写出，不replace。

## 2. 生命周期必须真正使用同一deadline并防旧清理覆盖新owner

新探针实证：shutdown(timeout=.05)实际2.0秒；_release_process_if_clean直接丢掉仍活着的proc。当前terminate先join writer到deadline，再另wait1秒+1秒；没锁或proc identity保护。

建议：独立生命周期锁或等价stop-in-progress状态；publish/terminate/release都用同一归属。锁等待也有剩余时间，不能无界；默认停止给固定总预算。writer.close仅发停止信号，先terminate/kill解除pipe，再join；每次wait/join取min(阶段上限,deadline-now)，预算用尽保留live proc/thread引用、complete=false且禁重建。release先确认proc.poll已退出，清空前确认仍是同一proc/reader/writer。即使多个interrupt/shutdown并发也不能旧清理清空后来worker。ready发布前最后一次锁内检查closing/epoch；spawn阻塞期间保留startup owner。

必须补test：阻塞write下cancel/interrupt/shutdown三条真实client路径，50ms shutdown用合理调度余量验证总预算；停止失败保留并拒绝第二client；进程实际退出后shutdown重试成功；并发stop不丢owner；实际fakePopen屏障进入Event后才shutdown，shutdown返回后再让Popen返回，断言零ready/零成功并最终retry回收，不手动proc.stop替生产回收完成。所有测试有finally清理，不靠daemon。

## 3. 把原B5/B6证据缺口补齐

main获得模型锁后的所有普通异常/返回用单一try/finally释放；watchdog只os._exit，不提前unlock。validate_worker_lexical_layout必须错误时failclosed；resolve_trusted_worker_paths当前忽略再次validate返回值，须传播错误。所有cache子目录先词法检查再resolve/mkdir。锁existing普通1字节验证、exclusivecreate发生FileExistsError安全重试已有文件，不能跟随reparse或截断旧锁。

main fake行为矩阵：低RAM、低VRAM、CUDA不可用、pin失败、线程cap失败、CUDA fraction失败/资源不可测均零_load_model；正常只load一次并idle/正常退出释放锁；异常释放锁也有断言。patch watchdog/stdin线程副作用，fake admission/torch资源probe明确隔离，无真实torch加载/GPU。

## 4. 剩余协议边界

outbound synth_result.duration_ms仍接受bool，float(hugeint)会抛OverflowError；统一strict finite nonbool numeric验证转ValueError。error含rid/epoch时严格校验。生成完成后再次检查stdin_fatal，不能满队列致命状态下还emit成功；异常/错误返回也检查fatal。补生成期间置fatal的loop行为测试。此前截断/错rid阈值退出等行为保留。

## 验证与交付

先逐项实现与自查，再一次专项和一次全量；新失败修复可重跑相关，不要为了获取测试计数反复全量。可用临时Python supervisor subprocess.run(timeout=...)捕获完整stdout/stderr与真实returncode，超时终止该次测试进程树；专项90s、全量180s。不要把unittest stderr重定向null后再从stdout找Ran。后台Start-Process只能Hidden；工具yield不是总超时。

不要修改Codex探针以改变含义，接口迁移可最小适配导入。交付上述4项实际改动及测试证据、未跑项，停止待Codex评审。禁止宣称6项已闭环但省略这里任一高优先级事项。
