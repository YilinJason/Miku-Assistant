# C1 Codex review：通过，可进入C2a

## 真实集成补充验收

`tmp/codex_c1_real_gate.py`180秒精确进程树监督器，实际exit0/76.73秒。经TTSEngine本地后端与真实RVC的3组固定文本全部成功，共6段40000Hz音频；AudioPlayer为仅保存捕获的fake，未用设备。报告`data/test/voice-naturalness/cosyvoice-production-c1/1da48e17c2f1/report.json`有本次完整RVC参数快照。

- RVC准备19.875秒；短句Cosy冷启动含合成17.875秒，加RVC后18.969秒。后续23–43字Cosy热合成4.546–7.156秒，RVC各1.109–1.282秒。47字两段总准备12.328秒、103字三段23.218秒，均不包含真实播放时间。
- 最终local worker/reader/writer退出、RVC worker退出、TFM complete/owned=pending=inflight=0；全局miku WAV644前后不变，GPU采样从运行中5571MiB回到1758MiB。不是连续峰值采样或长测。
- 原选定基准SHA256保持7C4A...19BA；新短句SHA256 D855...25E，相同长度不代表相同音频。新音频已提供用户试听，主观电流音/音色是否一致待反馈；不能替代设备验收。
- 最大实测单段43字；配置120上限的中文单句仍可能超30秒音频限制，C2后续须校准分句时长/字符预算。顺序合成有5–8秒句间准备，需单独设计有界预取与播放取消交接。

C2a只接装配与共享TFM关闭状态，仍不切默认/不预热本地模型；C2b再闭环资源预热/有界预取与真实共存。后续长测/最终评审/版本整理未完成。

## 当前有效结论（覆盖下方历史返修记录）

2026-10-17，Cursor最后返修01f07b64已交付。Codex独立审查两阶段gate、成功/失败的TFM owner保留、单段base/RVC边界、save epoch与generic cleanup隔离、shutdown预算与在途/registry、retire准入、fixture解锁清理，当前未见剩余阻断项。

- `tmp/codex_c1_owner_probe.py`独立实际exit0：未退役失败文件删除0；失败/成功延迟删除高水位各1；save陈旧cancel=false；停止通知delay0；100ms关闭预算探针wall0ms；retire期间新请求拒绝、重复retire不放开准入、退出后complete=true；continue不能注销开放client。
- Codex独立`tmp/codex_c1_validation.py`实际父监督器exit0：274 tests OK，46.25秒；miku WAV文件集合644→644，added/removed均空。不是仅引用Cursor的测试摘要。
- compileall通过，git diff --check通过（仅既有LF/CRLF提示）。未泛删历史无owner文件；644包含之前错误fixture遗留，不声称全部历史文件已回收。
- C1为可选本地基础WAV→现有RVC、固定warm_companion、顺序单段、无自动云/pyttsx3回退；未改assembly/config默认。真实TTS/RVC捕获验证下一步，不能用offline判定音质/设备/长测通过。C2仍未开始。

以下均为已被返修取代的过程记录。

## a159cf50交付后：仅剩一个阻断项，按两阶段顺序修

fixture与273相关门禁temp集合644→644已通过Cursor执行，Codex看代码确认fixture正常解锁回收。其它root探针均通过，但**`removed_outputs_before_failed_retirement=1`**，新_gate在failed_retention分支先_release文件，之后才retire。退役明确失败的worker仍可能写该文件，因此是文件生命周期安全回退，不得真实运行。Codex已把probe关键值转成assert，不能再只看其exit0而忽略错误字段。

不要再添加多余标志。精确设计只有两类待清理状态：

- `_retained_outputs`：本后端尚owned路径；其存在本身禁止下一次分配（不要仅len>=MAX才拒绝，否则待删1+新base/RVC2可突破单段预算）。
- `_pending_worker_retire`：这些失败路径可能仍有写者；只有client.retire_worker返回complete才可清除这个标志、尝试删除。**删除函数不能自己清退役标志**。

删除`_failed_retention`，将gate严格落实为以下设计（是设计伪码，不是让Codex直接改生产）：

```text
if closing or client.stop_failed: reject
snapshot pending_worker_retire
if pending_worker_retire:
    retire_result = client.retire_worker(bounded_timeout)
    if not complete: reject, zero_remove, zero_create
    set pending_worker_retire=False
# 到这里才确认没有潜在写者：
release_retained_outputs()   # remove后只有is_owned=False才能从集合去掉
if any retained_outputs: reject, zero_create
allow allocation
```

普通成功播放后仅OS占用的路径pending=false，允许先尝试删除，不杀健康idle worker；失败路径必须先退役。shutdown也必须client及在途调用确认完成后，清pending并release；若仍owned由TFM自身生命周期安全继续清理，不能丢引用。`_try_recover_retained_worker`可简化/合并，避免旧代码另一个分支再次删除/清标志。

测试：fake retire明确False且TFM.remove本来可以成功，第二次合成必须remove_calls=0/create_calls=1，不能用“TFM恰好锁住删不掉”掩盖顺序；retire成功才remove再下一次create；成功base+RVC删除延迟时后续零分配，解锁后恢复；保持既有全部断言不弱化。固定剩余范围backend+相关测试，无B/assembly/config改动。

修后先root probe（必须exit0且removed_outputs_before_failed_retirement=0）、C1专项，再一次120秒监督器273相关回归文件集合零增；交付后Codex独立复验。无需重新实现已修好的API guard/epoch/fixture。历史遗留644文件不泛删。

## c8dacdd2中止后的精确收口（先修fixture再跑回归）

Codex中止了重复回归：`tmp/codex_c1_validation.py`子测试272 OK但**整个监督器exit1**，temp_before/after连续640→641→642→643→644。不能看JSON中子测试exit_code0就声明门禁通过。

### 必修且仅这些剩余项

1. `test_delayed_remove_keeps_single_tfm_owner_high_water`的`_cleanup_locked_tfm`直接`tfm._owned.clear()`，从而剥夺删除授权、每轮遗留一个真实文件。改locked fake为可解锁flag，addCleanup先解除模拟占用再使用真正TempFileManager.shutdown（不清内部owner/registry、不直接unlink）；addCleanup即便断言失败也回收。测试最先单独运行并确认前后文件集合一致，**未做到不要继续跑272回归**。历史未归属miku文件不得泛删。当前644中包含前面错误fixture的遗留；只记录事实，不伪称旧文件都被回收。
2. 成功路径仍漏gate：独立探针`owned_high_water_on_success_with_deferred_delete=100`。`speak_sequential`finally和`prepare_playable`异常路径remove_many后仍owned的**本后端base/RVC输出**，必须进入同一待清理集合，后续分配前先尝试清理，未完成则拒绝新分配；不必杀正常idle worker（区分pending_worker_retire与仅OS占用）。上限按单段base+RVC可设2，临时pending不能每轮累加；成功save的调用方输出不纳入自动删除。shutdown仅在无写者后交TFM清理，仍owned引用保留可重试，明确状态。Probe中普通失败、锁定失败已通过，保留其逻辑。
3. 新公开`continue_shutdown`在未closing的正常client上调用也会注销registry，旧client仍可再合成；新的root probe字段`continue_shutdown_releases_open_client_registry=true`确认可构造第二client。最小化：只允许closing=True继续关闭，未closing抛RuntimeError且零stop/registry更动；或删除新公开方法而复用幂等shutdown(remaining)。补真实client不spawn测试。
4. retire_worker的开始锁内同时检查closing和_worker_retiring，拒绝已有退役，避免两个retire互相清标志/制造stop_failed。保持request→state锁序，busy立即返回incomplete不占用整个timeout。当前单退役新请求拒绝、确认退出返回complete的root probes已经通过，别回退。

`tmp/codex_c1_owner_probe.py`由Codex扩展到正常成功延迟删除/continue registry；Cursor可读跑不可修改。当前其它字段已通过：save stale=false、stop delay0、100ms关闭实际0ms、retirement_admits=false、retirement_complete=true。No-op重复退役的额外检查属鲁棒性收口，不必重构无关B代码。

### 执行顺序

先修fixture→**只跑该测试**核对miku文件集合零增→修成功路径/两项API guard→跑root probe检查所有高水位<=2且registry不能泄露→再运行`tmp/codex_c1_validation.py`一次（真实120秒监督器，返回0且前后文件集合零增）。新失败才针对修，不反复跑272。然后compile/diff-check，更新交接并停止。禁止真实模型/设备、内存库操作、提交推送、清理历史文件。

## 6653e231交付后独立复审：三项边界仍未闭环

Codex更新了自己的纯fake探针以覆盖新增接口，运行exit0得到：普通重复失败只retain1（通过）、save陈旧取消false（通过）、先停止delay0（通过）；但`owned_high_water_when_retired_but_deletion_delayed=100`、100ms shutdown预算实际250ms、`retirement_admits_new_request=true`。复现`tmp/codex_c1_owner_probe.py`，不要修改探针。当前CHANGES REQUESTED，不进入C2。

1. **retained集合有界不等于实际owner有界**。`_release_retained_outputs`在TFM.remove之前clear集合；真实TFM.remove是异步删除语义，锁住文件时返回None但仍is_owned=True。于是100次失败/退休会积累100文件。逐个remove后只有`not TFM.is_owned(path)`才能移出保留集合；仍owned则保留并拒绝下一次分配，不能靠delegate给有界延迟队列声称总量有界。清理待删成功后新显式请求应能恢复。正常播放/异常finally清理的本后端base+RVC文件也适用，不能绕过gate遗漏；可定义最多2个本后端路径（单段base+RVC），按“等待worker退休”与“安全可删但OS占用”两种状态处理，不用每次都杀已正常idle worker。不要把用户显式save成功后的调用方export文件算成后端泄漏，也不能删无owner替代物。
2. **shutdown仍超传入budget**。`shutdown_local`先调用无timeout参数的client.interrupt，它自己可花固定2秒，然后才算remaining调用shutdown。移除重复interrupt，client.shutdown本身标closing/epoch并终止。第一次client.shutdown在剩余budget内，等待backend在途退出后再用**剩余budget/0**重读最终client.shutdown状态（包含active/startup/registry释放），不能仅看三个alive字段假定client已解注册。预算覆盖所有wait，不新开线程。Probe的SlowInterrupt合法在client interrupt2秒契约内睡250ms，传backend100ms却耗250ms是确认缺陷。
3. **retire与新请求未互斥**。新增retire_worker未把自己纳入请求准入。实际retire线程在proc.wait中，另一线程`_begin_request(current_epoch)`仍True，可在旧回收期间启动/终止错误worker。用_request_lock保护新增retiring状态的开始/结束，与_begin_request使用相同的request→state锁序；退役开始后新请求零准入，已有active/startup时retire拒绝或有明确取消语义。closing并发时registry不能在retire仍进行就放开；shutdown完整状态应包含retiring，最后finally清状态可重试释放。保持B spawn/interrupt/close保证；不能拿一把长锁阻塞前台无界，也不能临时关掉单实例限制。测试实际client的FakeProcess.wait屏障，不只fake retire方法返回bool。

将已通过的三个点保留，补上述准确行为回归（含锁文件is_owned保留、清理后恢复、shutdown budget、retire期间新请求拒绝、retire/close不丢owner）。专项必须包含B（上一轮只A+日志+stream），以及旧voice/security回归；总超时真正120秒进程树监督器。Codex已备独立`tmp/codex_c1_validation.py`，Cursor可读不可改；交付后停止复审。

## 7719fe7d交付后独立复审：仍需下面三项收口

Codex实际运行`tmp/codex_c1_owner_probe.py`（纯fake/no文件创建/无设备模型）确认：10次普通失败创建并保留10个路径；新显式save收到cancel=true；shutdown预算200ms时204ms才调用client.shutdown，立即可停的在途仍报incomplete。真实B client sticky stop_failed后最终关闭的probe通过，**不**需要为该标志额外改B。

1. **失败输出必须有硬上限且可恢复**。当前所有false都一直保留，新的显式请求继续创建，直到整体shutdown才清理，不能接受无限增长。建议：后端先拒绝新的创建，针对上一失败worker执行一次有界退休（可在B client增加窄公开`retire_worker`：不把client永久closing、不解单实例registry，只有确认proc/reader/writer/请求退出才返回complete；忙/stop失败保留owner拒绝新建）。失败worker确认退出后才把精确输出交TFM回收；未能回收时retain集合/owner计数有明确上限，不能继续分配。本轮失败仍中止当前播报，不重试已提交文本；仅后续新的显式请求可在确认旧worker退出后恢复。不要靠判断stop_failed=false就删（interrupt线程可能仍在停止窗口），不放宽文件身份，不强制清registry。也可选更小的等价安全实现，但必须既有界又能在故障消退后的新轮恢复。测试10/100次失败的owner高水位、stop失败不删且零新创建、后续新请求可成功；新增client接口必须fake验证零双模型、close/cancel竞态。
2. **新save沿用speak的epoch准入**：在方法入口记录epoch，获得speak_lock后再次确认；仅同代新请求允许清除上一轮interrupt_event。排队旧save被更新interrupt取消后不能清掉新信号。关闭后零新请求。补一个真实顺序“interrupt→save成功”的fake测试，及屏障排队save→interrupt不派发；与speak互斥断言，不只是空白目标测试。
3. **先通知停止，再等待在途退出**：shutdown_local现在先_wait_inflight_until，若工作只能靠client.shutdown中止，会耗尽全部budget后才终止。先closing/停止本地worker，再在同一绝对deadline剩余时间等待在途及finally清理；最后读取实际client+在途状态（不要沿用停止瞬间的旧active_request快照）。有界预算、incomplete owner保留继续维持；公开状态不能假完成。事件测试一个合成在收到shutdown后立刻退出的worker，给500ms预算应及时通知并完成，而不是用满500ms后失败。

还有测试完整性：构造import测试中shutdown在patch client_cls上下文内会拿MagicMock stop_failed，没覆盖真client停止语义；补明确状态fake/真实client不spawn情形。constructor tests也应addCleanup（声明全部addCleanup但几项仍最后一行shutdown）。之前评审要求的save/speak互斥与save/close/RVC in-flight屏障未见实现，补实际屏障且finally释放/join，防止失败断言遗留daemon线程。

当前Cosy save完全脱离generic cleanup、AudioPlayer fake、配置收敛、首段失败不继续、基础RVC次序、incomplete不关自有TFM均已看代码确认，保留。无需重做A/B既已通过的设计。

Cursor仅以上C1收口（必要窄client接口可改），专项含C1+相关B+旧voice/security/stream回归，用真正120秒进程树超时，不只给exec yield或环境变量；交付后停Codex复审。不要直接重跑900全量或真实模型。

Cursor session00287ff6-2f53-4a22-a3bb-0bf27a275e52已被Codex中止（不是交付完成），因为测试缺少AudioPlayer设备隔离。精确进程元数据复核未留该unittest子进程。不得进入C2，不得跑真实模型/设备。以下是当前最终文件的审查清单；一次逐项闭环，保留既有工作。

## C1-1 high：shutdown并不等待依赖使用者退出

`engine.shutdown`调用backend.shutdown_local后，无视complete直接TFM.shutdown。Codex `tmp/codex_c1_review_probe.py`确认temp_shutdown_while_worker_incomplete=true。即便client已退出，speak可能仍在RVC/播放/文件清理，save也不持speak锁，因此只看worker状态不代表引擎无人使用TFM。

修复：后端/引擎维护真正的在途调用生命周期（覆盖speak整段到finally、save、RVC/播放），closing先拒绝新操作，取消→client有界shutdown→在同一个明确截止内等待已进入调用退出；不能无限等speak_lock。只有worker/readers/writer、在途调用及本地待清理输出都完成才允许自有TFM.shutdown。否则保留owner/TFM，公开明确incomplete状态用于C2；shutdown重试可完成。外部TFM不归本类关闭。backend.shutdown_local不要先interrupt耗2秒再shutdown另2秒，client.shutdown本身会推进epoch/终止，遵守单一传入budget。

## C1-2 high：失败/超时后删除可能仍被worker写的文件；继续32次重试

`synthesize_owned_wav`对client返回false或异常立即remove。B明确停止失败时仍保留live proc并stop_failed，不能删其仍写的WAV。保留该精确owned输出至确认所有使用者退出后再清理；引用不能丢失，也不能每个新请求再创建路径无限积累。stop_failed/closing时零创建新输出。相应try/finally覆盖prepare_playable意外异常。

`speak_sequential`遇play_path=None就continue，模型缺失/资源不足/启动超时可能对剩余32句各冷启一次。首段失败（不是正常空投影）应中止本次播报并返回可观察失败/抛固定错误，不静默重试其余段、不fallback。补第一段失败时synth_calls=1、长文本不消耗32个启动预算。

## C1-3 high：Cosy save未隔离通用失败清理/未纳入生命周期

PermissionError分支已经有Cosy guard（独立probe确认未进pyttsx3），但普通Exception仍调用`_cleanup_partial_save_target`，可adopt+删除未owned调用者.wav（例如空文本、错误扩展名等ValueError）。Cosy分支完整独立于Edge generic cleanup/fallback；无效目标/文本不能改删目标，owned但尚未派发的目标也保持不变。只对本次真正创建/写入且已安全退出的输出按owner规则处理。

save与speak/close/interrupt需同一请求准入、epoch和在途生命周期，不并发client乱用、不在关闭后发起请求；新显式save在前一次interrupt后可以恢复，但不能清掉较新的取消信号。把原始filename传到已有client词法校验，不能先canonicalize隐藏dotdot/reparse。固定TMP内普通owned WAV验证在worker派发前完成；禁止宽泛export/adopt。

## C1-4 medium：初始化失败与旧替身兼容

构造AudioPlayer/自有TFM后client注册可能因单实例限制失败，需只回收本次自己构建的资源；不能停仍由正式实例持有的RVC或共享TFM。给旧object.__new__测试替身调用interrupt/shutdown等路径使用安全getattr访问新增可选字段（现直接self._cosy_backend会AttributeError）。不要改弱已有断言。

## C1-5 medium：配置解析多形态绕过strict契约

`if not cosyvoice_config`把[]/0/False当默认；支持whole voice.tts wrapper又会丢弃wrapper未知键。当前尚无外部调用此新参数，收敛为唯一契约：cosyvoice_config=None或`voice.tts.cosyvoice`的dict字段映射。全部走现有parse严格字段，不接受其它类型或wrapper，异常在音频/TFM/client副作用前ValueError。更正文档与测试，C2会传inner dict。

## C1-6 high：测试并非完全fake，且遗漏关键竞争

_make_engine与construct测试调用真正AudioPlayer.__init__，会pygame.mixer.init。所有相关测试通过fake AudioPlayer/patch _detect_backend，真实play/系统命令/RVC/pyttsx3/torch/子进程必须可断言零调用。TFM与client都addCleanup，不依赖最后一行shutdown（断言失败也不能泄漏）。

按事件屏障补：synth正在写且停止失败的输出保留；shutdown等待在途RVC/播放/清理且超时incomplete；重试成功；save与speak互斥、save/close/取消的晚到结果；无效unowned文件在白名单内也原字节不变；失败中止后续段；Edge不创建Cosy client；极小max chars/边界3200/32；所有临时文件净增长0。构造不导入torch的测试要拦截真实import而非只按名字断言engine_type。保留现有正常WAV→RVC顺序/基音fallback测试。

## 交付

Cursor按上面一次修复后，先用Python312完全离线专项（C1及相关旧voice/security/stream tests），真实进程树总超时120秒并保存UTF8日志；不反复全量、不运行真实链路。compile/diff-check，更新交接并停止待Codex复审。当前B900项是真实已验证基线，不应回写为C1通过。
