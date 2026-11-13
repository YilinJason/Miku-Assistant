# CosyVoice半精度窄切片（实施已放行，是否采用待Root实测）

先读AGENTS及四repo skills、存在的.cursor/rules/system-safety.mdc。巨量dirty保留，正式data/memory禁读写删，禁止运行测试/模型/设备/清理，不改vendor、config或已有测试，不提交推送。官方Cursor实施，Root评审并独立测试。

已确认：固定vendor/CosyVoice/cosyvoice/cli/cosyvoice.py中的CosyVoice/AutoModel支持fp16=False参数，cli/model.py按该bool控制半精度权重与autocast。当前_construct_automodel只传model_dir，默认FP32。真实13字六句中Cosy约3.3秒、RVC约1秒。不能先声称FP16更快或音质不变；保留默认并提供严格opt-in用于Root A/B。

仅允许修改src/voice/cosyvoice/settings.py、src/voice/cosyvoice/worker_cli.py、scripts/cosyvoice_runtime_worker.py，新增tests/test_cosyvoice_precision.py。可以只读worker_cli、能力封装、Config与现有测试确认调用链；不改其它源或旧fixture。

设计：

1. frozen CosyVoiceSettings最后新增fp16: bool=False，保持既有构造默认兼容。严格解析voice.tts.cosyvoice.fp16，默认False，只接受type(value) is bool；拒绝0/1、字符串、None、容器；不将布尔字段塞入现有数值上下限循环。其余未知字段与数值策略不变。
2. Root已更正初稿误判：worker_cli实际为固定argv，而非JSON。False保持旧argv，True只追加固定--fp16 true；parser非required、choices=(true,false)、default=false，严格小写；builder验证type(settings.fp16) is bool；解析成真正bool。保留老argv兼容，拒绝0/1/字符串变体/未知flag，不新增环境变量或任意argv能力，不改权限。
3. _construct_automodel新增keyword-only fp16:bool=False，在既有stdout重定向和cpu_checkpoint_staging_scope内给上游传fp16=True（False路径可维持原来只传model_dir以兼容既有fake）。worker main从已验证settings.fp16传入。不得开启JIT/TRT、改采样率/声音/风格/模型/线程/显存配额/超时/生产默认，不引后台缓存或并行GPU任务。
4. 新离线测试覆盖严格bool、默认兼容、既有数字边界、CLI JSON往返及恶意数值/字符串拒绝、fake AutoModel正确kwargs且CPU staging/输出重定向作用域有效、构造异常原样传播并恢复scope。使用fake torch/类，不导入真实torch/SDK，不触设备或正式DB。

只AST/局部compile和diff-check后STOP，返回精确改动与局限。Root将独立审最终diff和fixture，再运行受监督fake及既有相关回归，之后才以现有资源门槛进行同文本A/B真实捕获，不播放。默认采用须依据实测而非预期。
