# CPU权重加载内存映射窄优化（已放行，真实收益待Root）

FP16实验已因变慢撤销，闭环记录docs/codex-review-cosy-precision-2026-11-05.md。此切片只允许src/voice/cosyvoice/checkpoint_loading.py，新增tests/test_cosyvoice_checkpoint_mmap.py。先读AGENTS、四skills和安全规则，保留其它dirty，不改vendor、config、settings、超时、资源线、旧tests/Root tmp/正式data/memory，不运行tests/models/devices，只AST后STOP。

依据：固定vendor Python的torch 2.3.1 serialization.py明确支持mmap，三个固定权重llm/flow/hift均PK0304 ZIP格式。当前CPU staging已避免GPU双加载，但torch.load仍把整份storage复制到CPU。拟在既有严格staging边界内给支持的现代ZIP加载启用mmap，减小中间复制；精度、模型、声学与预热顺序不变。不宣称必然更快，不修改vendor或默认超时。

边界：
- 保持现有owner线程/active alias/单scope锁/退出恢复全部语义；只有当前_should_stage原先会返回True的调用（固定model_dir下llm/flow/hift、CUDA映射、weights_only is True）才可加速。
- 在每scope捕获original load后用inspect.signature检查是否显式支持名为mmap且为keyword可传的参数。未知/反射失败/只有**kwargs时保持原CPU staging，不能靠识别fake/模块名/版本字符串做特判。不要试调用后TypeError重试，任何实际load错误/fatal原样传播，original最多一次。
- 仅kwargs未显式给mmap时注入True；调用方显式mmap=False/None/True一律保留。只有固定路径用with open/read前4字节确认PK0304才注入；最多4字节，不读整个权重。头读取普通OSError则不注入mmap、原CPU staging仍调用原load并保留其真实错误；fatal自然传播。
- legacy非ZIP、无支持签名、非staging文件/线程/CPU/weights_only不严格True/文件对象，保持原行为与CPU staging条件，不新增锁/线程/缓存、不更换dtype。位置map_location与关键字map_location都正确，不能丢弃其它args/kwargs。
- 新测试fake load显式mmap签名及不支持签名；固定私有tempdir权重头；覆盖True注入/显式False保留、老ZIP路径无注入、非目标原样、positional/keyword、原异常一次不重试、scope别名退役及异线程委托（实际join确认后恢复patch/删目录）。所有读写只private tempdir，不导入真实torch/设备。

Root将审最终diff、fixture和调用链，运行新模块+既有CPU staging/能力相关回归，再同默认30秒、相同FP32六句真实短测，与precision_fp32_nov5u对比准备/峰值/稳态资源。无收益或有风险即撤本切片，不将性能建议当已验证成果。
