# 30分钟真实模型流式语音长测（前置首段切片闭环后实施）

首段前置切片已Root125/9/24及真实三case闭环，首可播放10000/4422/9812ms。现正式放行本任务。用户已要求完成剩余优化及长测。只做固定合成文本驱动的真实Ollama→App流式派发/队列→Cosy→RVC→capture链，不录麦克风、不打开扬声器、不访问正式memory，不冒充人工听感/设备验收。生产源、config、vendor、tests冻结；不运行任何test/model。只允许改以下tmp：codex_real_voice_supervision.py、codex_app_real_voice_gate_2026_11_03.py、codex_app_real_voice_smoke_2026_11_03.py，新增import-safe codex_real_voice_soak_loop_2026_11_05.py。只AST后STOP，Root独立审/测。

## 显式有界入口

- 固定flag --soak-30 / GateFlags.soak_30=False；必须isolated_ollama，拒绝no_ollama/rvc/torch/prepare_only/first_clause_control/ollama_mmap_control混用。无需extended-cold，不更改任何生产默认。只此flag时gate给既有Supervisor.run显式total_deadline_sec=2010；其它仍180。ready15/EXIT10/资源阈值与OwnedJob finally不变。
- parent每5秒资源检查持续全程。只long时最多420样本，其它36；绝不能因达到旧36便停止采样/中止检查。long中RAM/commit不可观测或sample异常均中止；保持原RAM<6144/commit<4096中止与预检线。日志原2MiB上限保持，失败不伪造成功。
- 只long时检查固定prefix.with_suffix('.stop')存在则abort_reason=operator_stop，走原finally。Root必要时写自有label stop文件可在5秒内要求收尾；不接受外部任意路径，不清理用户进程。

## 纯循环helper与有限状态

- 新helper公开名固定SoakState与run_fixed_soak(run_turn, state, *, monotonic_fn=time.monotonic, sleep_fn=time.sleep, emit_fn=print)，run_turn(index)返回含ok is True的标量metrics dict。新helper无模型/线程/设备/文件副作用；固定steady1800秒、cadence15秒、MAX_TURNS120、最后120秒不启动新turn、成功至少60turn才complete。实际模型运行慢时不补并发、不跳过等待虚报完成。
- 注入monotonic/sleep/run_turn/emit，caller持有SoakState（rows<=120，elapsed、completed、complete）；每turn先登记partial row，callback失败保存error type后原异常传播，finally更新elapsed；只有真实elapsed>=1800且达到成功门槛才complete。空转sleep<=1秒，禁止busy loop。每10turn发固定统计，纯helper可fake clock立即走完1800秒测试；CLI没有任意duration通道。

## 实际App链与有限产物

- 保留既有一次三模型协调准备与准备后驻留检查。已有MikuAssistant.from_dependencies的app在long复用；确认memory_runtime is None，并仅内存Config关闭memory/retention（不保存配置、不构造DB），固定输入“请温柔地提醒我喝水，用一句简短的话。”，无工具动作。
- 只long在prepare后将自有llm.timeout=15；给该client的既有_apply_runtime_options临时包一层固定options.num_predict=32，不绕_request_post/stream/准入，不改正式模型配置或keep_alive。warmup仍原预算。每turn调用app.process_text_input_streaming(use_tts=True, wait_for_playback=True, playback_timeout=60)，要求ReplyStage.OK、safe_to_resume_listen与实际音频verified计数增加。记录字数/耗时/首可播放时刻/状态，不保存回复正文。
- 每turn一个90秒Timer设外部cancel_event，Timer在start前发布为active owner；try/finally cancel+实际join<=2秒（未start用ident/is_alive处理），线程未quiet不得进入下一turn；异常原对象优先，收尾错误独立记录。共享finally再次回收残余timer。
- CaptureOnlyPlayer仍构造前fake：每个实际owned WAV都验证sf.info，verified_audio_total递增，但仅保存前6个WAV；后面不复制、不新增测试音频文件。每turn以verified delta判成功而非len(captures)增长，保持原常规/对照模式行为。生成calls列表硬上限1024，rows120、资源checkpoint<=122，超界失败即收尾，不截断伪造通过。
- long逐turn checkpoint入report但只每10turn打印，observed_synth/convert不逐次打印，避免MAX_STAGE_LINES200丢最终report；不在calls/report写LLM回复text。原UUID report与默认6句、首段三case不变。

## 收尾与报告

- long finally先cancel/join active timer，再app.shutdown有界至多3次，确认_shutdown_flow_done与持有speech_player的OwnedThreadTask snapshot.quiet；普通/fatal失败独立记录后仍继续engine、TFM、LLM warmup/guard/隔离模型卸载各自回收。非long不改变app关闭行为；不能引入cleanup_now或设备。
- report加soak_state、verified_audio_total、saved_capture_limit=6、app_shutdown_complete、timer_quiet等真实字段，失败也落部分报告。最终只有steady>=1800、成功>=60、所有请求/音频成功、App/Timer/aggregate/TFM/warmup/guard实际quiet才通过；父Supervisor必须同时child/server quiet。不拿子报告瞬时global commit当进程泄漏。

Root放行条件：复核最末diff，fake clock完整1800/慢turn/中途fatal部分记录/flag冲突/36次后仍中止/stop文件回收/默认行为；既有监督器24与Root9回归。然后一次默认30秒准备的真实30分钟长测，Root每分钟读live报告与额度，primary<=10时写自有stop文件并观察回收后暂停，不自动reset。不创建新监督器、不绕过SafetyPolicy、不给模型额外设备权限。
