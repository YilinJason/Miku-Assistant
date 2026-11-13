# FP16真实对照接线（仅tmp，已放行）

生产精度开关Root202(2skip)+独立2已通过，生产源/config/全部测试冻结。只允许tmp/codex_real_voice_supervision.py与tmp/codex_app_real_voice_smoke_2026_11_03.py；其它tmp/Root probe/gate不改，不运行tests/models/devices，只AST后STOP。

在既有GateFlags增加cosy_fp16:bool=False；parse固定--cosy-fp16；禁止和rvc_only/torch_only混用；child_script_and_args只在smoke路径按显式flag追加--cosy-fp16；报告标注cosyvoice_fp16_diagnostic。保持TOTAL180、EXIT10、MAX_SAMPLES36、ready15、全部RAM/commit/VRAM门槛与就绪逻辑原样。

smoke解析--cosy-fp16，构造cosyvoice_config只在显式flags需要时用dict，extended-cold只写startup_timeout_sec=60，cosy-fp16只写fp16=True；两项组合正确，缺省仍None。不改变任何正式config或声学参数。不用其它环境变量/任意argv/替换SDK/修改worker本体来绕权限。子报告精度字段标明请求值并在构造后确认backend.client.settings或其已有_settings.fp16与请求一致；报告只称已请求/设置，不能伪造直接观测GPU dtype。

现有6句、capture-only、全部异常finally与UUID报告、隔离服务warmup/guard/unload门保持。Root随后以fake验证默认不变/flag冲突/设置组合、监督24与harness回归后再真实A/B。这里不是生产默认采用决定。
