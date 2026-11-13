# Codex Review：CosyVoice生产切片B

2026-10-17 当前状态：**B里程碑通过，可以进入C**。下文未通过条目保留为历史返修依据，已被本结论替代。

## 当前里程碑验收

- startup阶段诊断、Windows跨Python完整文件身份、派发前owner复验、idle退休窗口均已完成Cursor返修与Codex独立复审。专项评审见startup-diagnosis和cross-python-identity文档。
- 固定非敏感文本真实runtime smoke：cold 19.094秒、warm 2.578秒、after_idle 18.922秒，3/3成功；idle诊断值2秒，原3秒等待未放宽。旧进程退出已确认，新进程实例已确认。
- 最终worker/reader/writer均退出，TempFileManager owned/pending/inflight=0。未播放音频、未读改真实记忆。GPU回到约1.7GiB桌面基线。
- Codex独立有180秒真实进程树超时的全量验证：900 tests OK(skipped=6)，81.76秒；final probe exit0，fd泄漏0、post-close spawn不假报完成、过期deadline仍发送终止。
- 默认startup30秒保持；一次系统冷缓存首次加载曾超过30秒，会按预算安全失败，不能宣称所有冷启动都在30秒内。热合成约2.6秒不包含RVC/LLM/播放。C须处理显式预热状态与资源协调。
- 此验收仅覆盖B worker边界；TTSEngine/装配集成、模型共存长测及人工语音验收仍未完成。Edge默认与现有Nahida参数未切换，无提交/推送。

## Codex最终离线复验

- 复查worker generator API、inbound expected dev/ino贯穿打开、父单句柄分块有限性校验、所有提前return的fd finally、原始output_path词法校验；预期行为已落地。
- 复查writer取消/epoch/closing轮询、统一停止预算、deadline耗尽仍terminate/kill、live worker/reader/writer保留与registry、post-close stubborn spawn owner；原缺陷已修复。
- Codex独立`tmp/codex_cosy_b_final_probe.py`：fd泄漏0、孤儿进程假完成false、过期deadline发送终止true，exit0；probe通过实际shutdown重试清理，不再强制清registry。
- 独立`tmp/codex_b_validation.py`使用Python312 -X utf8、180秒总超时及超时按精确PID终止测试树：859 tests OK(skipped5)，exit0，59.34秒。日志`tmp/codex_b_full_tests.stderr.txt`；compile/diff-check通过。
- Cursor测试清理补丁经审查：所有本测试创建的TFM登记shutdown，先回收client/fake worker；专项前后miku WAV528→528，独立全量时快照仍528。无历史未归属文件删除。
- B6回归修正：dotdot真实解析到同一owned文件；别名reparse与canonical分开，旧key行为会触发spawn。正常owned成功继续覆盖。
- 未据此声称真实音频/GPU/听感或长测通过。生产默认未切换；无真实data/memory、无提交推送。

## 返修交付后独立复审（session37dbffd6，仍不通过）

Cursor报告818项OK。Codex隔离探针`tmp/codex_cosy_b_review_probe.py`实际exit0，确认以下仍是原B1–B7未完成项，不得把测试数当闭环：

1. B4取消：`stdin_writer.write_bounded`仍仅长时间wait(done)，未轮询cancel/epoch/closing，close也不唤醒等待者。探针`write_wait_ignores_close=true`。给writer单一绝对deadline + cancelled callback/Event，入队及等待用<=50ms轮询；client传cancel/epoch/closing；停止先终止进程解开pipe，所有join/wait共用剩余deadline，shutdown不能writer先耗完预算再额外2秒wait。补真实阻塞write，分别cancel/interrupt/shutdown，界内返回、writer退出或保留owner拒绝重建。
2. B1/B6输出：`validate_worker_output_path(tmp/../outside)`接受仓库内TMP之外文件（探针true）。词法拒绝`..`，逐级拒绝reparse后再校验resolved在固定TMP。parent必须校验原始output_path，不能TFM先resolve隐藏链接。worker仍sf.write(path)，探针在generator期间替换输出后，新inode被写入（true）。改校验并打开不截断句柄，fstat身份对齐，持有该句柄跨生成/写入，不重开路径；写前再核对路径仍指向该句柄，失败不改替代文件。父通过同样可信句柄验证完整WAV有限样本、单声道、<=30秒/4MiB；探针当前NaN WAV返回true。资源上限须在复制/拼接之前检查，并预留WAV头字节。补替换发生在inference期间、FLOAT NaN、dotdot、链接、超长/超字节测试。
3. B2协议：inbound `schema=True/epoch=-1/rid129`合并仍接受（探针true）。严格非bool整数/非负/长度<=128；outbound metric拒绝bool和hugeint导致OverflowError，所有坏schema转固定错误。EOF非空无换行必须作为截断协议失败，不接受partial JSON。错rid/epoch到阈值或不可能状态消息必须回收该worker，不能留队列污染下一轮。补行为测试。
4. B2队列：StdinReaderFatal设true后，run_worker_loop只在收到None时检查；满队列时None无法入队，loop仍处理旧请求。探针overflow_fatal_set=true但结果shutdown_ack。loop在取队列前及处理前优先检查fatal并固定ERR_PROTOCOL退出（包括生成刚完成时）；idle时间从请求完成起算。补实际满队列->loop零inference，不用手动塞None伪造覆盖。
5. B3生命周期/测试：现有shutdown竞态gate在fake worker thread内，fake Popen仍立即返回，不是要求的spawn阻塞屏障。补Popen callable等待barrier，shutdown返回后再让Popen返回，禁止ready/成功，保留owner直到回收后shutdown retry完成。terminate/release须串行或按proc身份校验，禁止并发shutdown/interrupt旧清理清掉后来进程；release须确认proc退出才能清句柄。冷启动取消必须在Popen之后再set，不是调用前已set。
6. B5/B6主流程：cache/model/vendor等仍在worker main先resolve再检查，须用词法固定路径先校验（cache全部祖先/子目录），再资源准入/pin/模型；使用try/finally保证普通返回释放锁，watchdog只OS退出释放。当前仅admission函数单测，补fake main入口低RAM/低VRAM/CUDA缺失/pin失败/capAPI失败时零_load_model，以及正常一次加载/idle退出；无需真实torch。锁已exclusive create，但创建竞争FileExistsError须安全尝试已有文件且确认普通固定1字节。

请一次完成此列表并自行逐项对照，不删弱化既有断言，不通过mock特例绕过路径。可将客户端输出验证抽小模块，避免client继续膨胀。全部限定B及共用A路径边界。随后专项+全量总超时验证，Cursor停止待Codex复审。

## B1 blocker：实际CosyVoice API调用错误，空WAV也可被父进程报成功

`scripts/cosyvoice_runtime_worker.py`把inference_instruct解包为speech/sample_rate；实际固定vendor/cosyvoice/cli/cosyvoice.py:116返回迭代器，yield字典含tts_speech，采样率是model.sample_rate。现有eval worker才是正确消费方式。真实runtime必失败。

要求：消费真实契约的generator，在整个迭代期间重定向第三方stdout；累计样本数/字节/时长在追加或cat前检查，拒绝空块、NaN/inf、超30秒/4MiB、非法采样率。沿用同一owned输出inode写入，不用replace。父在成功响应后重新确认原dev/ino身份、输出普通WAV可读/有限/正时长且bytes与响应一致，未成功验证不能返回True。Codex fake实证：把fake写WAV置空，仍得到success=True、bytes=0。

## B2 high：pipe读取先分配后限长，坏协议被静默跳过

stdout_reader._run与worker._stdin_reader用无size的readline，完整读取后才len检查，不能限制内存。改为readline(MAX+1)或等价有界帧读取，超长/不完整/坏schema触发固定协议错误并终止/拒绝本进程，不反复跳过任意日志。队列满策略必须使请求可观察失败，不得drop关键结果后继续假定正常。

协议严格校验schema为int非bool、epoch非负int、有限非负耗时、1..MAX_AUDIO_BYTES字节、有限长度request_id、固定字段。客户端不把其它rid/epoch结果当当前成功，也不能在本次收到未知结果后无限continue。补真正带size的fake reader、超长无换行、NaN指标、schema=True、错id与队列满测试。

## B3 high：关闭与reader持有者提前丢失

shutdown无论worker是否仍存活都_unregister_client，使第二个client可构造，违反单实例。_release_process在reader.join失败时仍清reader引用，下次可假报完成。complete状态在回收前计算也过时。要求只有进程/reader/活跃请求全部确认退出才解注册；否则保留可重试owner和引用。关闭返回状态按回收后实际值计算。

startup期间_lock一直持有，第二次synthesize会阻塞到startup_timeout，不是忙则立即拒绝。将状态互斥与启动等待分开；短锁保护状态而非整段I/O。shutdown与spawn并发必须以屏障测试：fakePopen阻塞，shutdown返回后才允许spawn返回，必须立即回收该proc且永不ready，不允许提前解注册或创建第二模型。现有ready_delay在fake构造前sleep，不是该竞态测试。

## B4 high：阻塞写管道不在deadline内

client.synthesize和shutdown同步stdin.write/flush，deadline在write后才建立。worker不读stdin时可能无限阻塞；停止路径也不能被同一I/O锁卡住。实现每worker至多一个可回收写入者/有界写队列或等价非阻塞机制，deadline覆盖写入到响应；shutdown可直接有界terminate，不必先发阻塞shutdown行。测试fake write阻塞，取消/timeout/close仍在契约内返回且停止后writer退出，不积累每请求daemon线程。

## B5 high：资源限制失败被吞且线程env设置太晚

set_per_process_memory_fraction异常pass后继续AutoModel，实际无显存上限；_apply_thread_limits异常pass且环境在importtorch后才设置，BLAS可已初始化。必须在spawn env固定OMP/MKL/OpenBLAS/NUMEXPR<=4，torch线程/显存限制设置失败返回固定拒绝并零模型加载。不要silent CPU fallback。加入真实worker主流程fake admission测试：低RAM、低VRAM、CUDA不可用、pin失败、限制API失败均零AutoModel；正常只加载一次。

## B6 high：worker路径与Windows句柄遗漏

_validate_output_file先resolve隐藏symlink/junction，再验dev/ino，可借链接到owned输出绕过路径拒绝；cache与lock同样过早resolve。使用A的词法路径逐级校验，覆盖data/tmp自身、祖先、输出、cache子目录与lock路径；固定TMP输出在父客户端也验证，不接受任何其它owned目录。模型锁a+b在获取锁前append一个字节，会逐次增长且未经锁写；改固定1字节、锁后写、不新增不可信目录。

Windows _parent_is_alive未声明OpenProcess/WaitForSingleObject/CloseHandle的argtypes/restype，64位HANDLE被当int。复用现有RVC正确实现或提取小共用helper，设完整ctypes签名。父watchdog不要先释放模型锁再os._exit（此时模型仍在显存里）；由OS在进程退出释放锁。资源probe和真实Windows当前PID可以只读测试，不启动模型。

## 证据缺口与验收

### 返修中断后的补充：B7 与尚未闭环项

2026-10-17：上一 Cursor 会话已中断，4个滞留的纯离线 unittest 子进程随会话退出，已核对无残留。部分代码返修保留，尚未通过复审，不得进入C。

- B7：`_ProtocolWorkerProcess`在构造时即启动非daemon线程，`_run`永久循环，而terminate/kill只改布尔值、没有唤醒或终止线程。提前拒绝的用例连Popen都没调用，也已创建未归属线程。fixture须登记全部实例，独立清理停止信号、解除阻塞并join/assert无存活；不得改daemon或清空registry掩盖泄漏。
- writer等待须以单一绝对deadline覆盖入队/完成，短轮询取消/epoch/closing；当前一次write_bounded可能用两倍timeout且取消等待整个synth timeout。shutdown状态与注销必须包含writer_alive，不只reader/进程。
- 输出固定TMP限制须父子都校验，拒绝词法`..`及reparse；父成功验证还需音频字节/时长上限、有限样本。worker写入时通过已核实身份的打开句柄写同inode，防校验后替换路径；累计30秒上限不允许额外加1秒。
- 锁文件不存在时用exclusive create，不能exists后write_bytes竞争截断另一进程锁文件；禁止创建未经信任检查的任意父目录。
- stdin队列溢出用独立fatal状态传给主循环，不能把fatal再塞入已满队列后丢失；未终止的错误结果必须拒绝/回收而非污染下一轮。

后续测试每次必须有独立总超时（专项90秒、全量180秒；超时回收该次进程树），前一次未退出不得再次启动测试。不得只靠exec的yield时间作为超时。修复后交付实际专项/全量退出码与耗时；所有fake线程均退出。

当前B测试主要是fake客户端，未实际覆盖worker低RAM/VRAM/模型generator/idle释放，冷启动取消只是预先set事件。补契约真实的fake worker loop测试，测试线程在tearDown必须退出，不能靠daemon逃逸；所有wait/join测试有上限。修复后必须运行任务要求的全量unittest，不能擅自推迟到C；保留原测试行为并修正fake成功bytes为真实大小。

范围仅B及必要A共享边界，无TTSEngine/装配/生产YAML，无真实模型/设备，无data/memory，无提交推送。完成交接停止待Codex逐行复验。
