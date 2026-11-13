# CosyVoice CUDA checkpoint staging：针对实际构造卡点的窄修复

读AGENTS/相关skills、ADR、最新C2b2a评审。只本切片，不接App/预取；禁止data/memory、真实模型/设备、提交推送。保留全部dirty/音色/权限/默认引擎/超时/线程数/资产pin/vendor不变。主Python312 fake；真实主观与性能测量由Codex复审后做。

## 实测依据

Cosy60秒三模型与两模型（省RVC）均失败，排除单纯RVC同时驻留。两模型报告`data/test/voice-naturalness/cosyvoice-production-coexist-two-models/6f88b5abc6af/report.json`，prepare62.047秒False；最后stage automodel_construct。只读py-spy0.4.2（仅tmp工具、不入依赖）对本轮已验证后代PID采样，明确主线程在`torch._utils._cuda -> serialization.load_tensor -> torch.load -> CosyVoiceModel.load line66`。采样tmp/codex_cosy_model_stack.json，未采集locals。资源准入torch导入约17秒，其他约5秒，模型构造剩余38秒仍未完。

已读pinned vendor：`CosyVoiceModel.load`把state_dict map_location=self.device（CUDA）载入，再load_state_dict到仍在CPU的module，然后module.to(CUDA)。导致多余GPU→CPU→GPU传输及checkpoint临时显存。**这是代码确认的问题；CPU staging能改善多少尚待实测，不得提前声称性能通过。** 不直接修改vendor以免破坏pin。

## 设计

给runtime worker构造AutoModel的窄边界增加独立可测CPU checkpoint staging适配（如src/voice/cosyvoice/checkpoint_loading.py），由`_construct_automodel`包住，仅在本次AutoModel构造期间生效。优先保留vendor原load逻辑而非复制整套模型构造。

可用有界作用域临时torch.load wrapper：仅本构造调用线程、仅已验证model_dir下精确llm.pt/flow.pt/hift.pt三文件、仅原来map_location是CUDA且weights_only=True的受支持调用，将map_location改CPU。其他参数/调用原样，spk2info.pt及未知路径/文件流/CPU加载等不得改。保留weights_only=True、strict=True、原dtype/keys/eval、后续vendor .to(device)，无半精度/量化/缓存/懒权重模式。

处理kwargs与支持的第二位置参数map_location，不能重复传map_location，不能捕获内部TypeError后重试。明确互斥所有权：作用域重入/并发构造快速失败或安全非嵌套，不无限等；构造异常/KI/SystemExit均finally恢复原函数，锁释放。其他线程的torch.load调用应原样委托，不能改变其它加载行为。仅独立worker使用，主程序不导入真实torch。不要借此放宽资产/任意checkpoint路径。

若选择更窄的等价适配，需说明为何不会复制/偏离vendor加载契约；不能跳过真实CUDA权重准备假报ready。

## fake门禁

覆盖：精确三个支持文件CUDA→CPU、其他参数保留/无第二调用；CPU原样；未知路径/文件对象/spk2info原样；weights_only不满足不静默改安全语义；位置/关键字参数；异常/KI后torch.load精确恢复；其他线程原样；并发/嵌套有界；Worker实际_construct_automodel调用该scope且成功后恢复。fake torch/sys.modules，不安装主环境torch。

先专项+sliceB/C1/C2b1/C2b2a+RVC lifecycle，用tmp具名120秒精确PID树/UTF8/644文件集合双向不变。compile/diff。新增自己负责的测试文件；不改root probes，不绕过原断言。更新ADR/交接，交付停止Codex严格review。**不得真实模型性能测试**。C2b2b仍禁止。
