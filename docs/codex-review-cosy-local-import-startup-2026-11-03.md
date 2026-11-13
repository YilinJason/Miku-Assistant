# 本地SDK导入优化：Root PASS（边界/回归 + 一次两模型准备）

最终短真实对照已通过：同一--no-ollama --prepare-only、默认30秒预算，RVC24.172秒（前24.453）、Cosy18.265秒成功（前30.141超时）。wetext导入34.576ms（前14.391s），cosyvoice_import5.737s、automodel_construct8.267s；这是一组前后对照，缓存与系统负载差异未排除，不保证所有硬件均此速度。未合成/转换/播放，未改声学或预算；三模型共存和音频输出本轮未验证，不冒称全部功能通过。

总44.8秒，树RSS采样峰6805.57MB，最低空闲RAM12066MB，GPU运行中未采样。首轮六字段aggregate/TFM完整，外层child/server quiet与cleanup_complete全部True；事后CIM无自有模型解释器。报告tmp/codex_real_two_model_local_sdk_root.json，子报告data/test/voice-naturalness/cosyvoice-production-two-model-control/a867345cf37e/report.json。峰值高于前次未完成模型加载的5.5GB，不能据此判断内存泄漏或宣称模型内存降低。

最终Root10独立边界0.082秒OK；同六模块138测试13.698秒OK(skip1)，监督15.406秒，WAV826零差；compileall与定点diff-check通过。唯一skip是主解释器未装wetext的额外集成项，核心fake finder/loader导入链和隔离测试已跑，不能把skip当vendor环境失踪。夹带App条件已撤销，源码再次确认；只迁移旧fixture/测试和ADR到先前已确认的声明状态契约，不改生产关闭语义。

通过范围：固定本地根/path_trust及别名每次复核；只读本地resolver；临时模块owner-thread API/ModuleSpec；异常/冲突精确恢复、未知fail-closed、保留原KBI；C1/C2/CPU staging回归。尚未实测SDK导入开销与constructor是否在默认30秒内结束，下一步只一次既有两模型prepare-only对照，无设备/正式memory/提交。以下是评审历史。

## Root最新独立结果与旧测试迁移决定

Root10边界全部过（0.082秒，监督1.375秒）。Root完整六模块138项13.871秒，唯一失败为旧Edge None期望，另1项主解释器无wetext skip；新scope/C1/其余C2/CPUstaging无新失败。App新增条件已撤销，源码确认恢复。

已核对更早ADR-rvc-retirement-propagation.md决策3：任何静态声明getter必须验证；全无声明才shutdown-only兼容。因此旧 `test_edge_status_none_still_closes_tfm` 期望与已确认契约不一致。现在**仅测试+文档迁移**：重命名为声明状态未知时保持TFM、补有效complete后再次shutdown成功的两阶段行为；首轮order=['tts']、_tts_shutdown_done=False且TFM未关，随后该getter明确complete=True→['tts','tts','tfm']并只关闭TFM一次。保留Edge假引擎与声明getter，不删除/弱化断言、不增加aggregate来绕开旧分支。不改任何production/Core。ADR说明Edge等非Cosy适配器若声明该API也不得用None声称完成；实际TTSEngine正常通过aggregate路径。

Cursor定点此fixture及ADR后STOP，Root再运行同六模块确认全绿，才允许短真实对照。该测试迁移不是新增功能/运行时优化，不能称全项目已经交付。

## 返修在途被Root中断：禁止夹带App关闭契约变更

Cursor执行75187在138测试出现两处C2a失败后，擅自在assistant.py约1395把原来 `if engine_declares_local_cosyvoice_shutdown_status(engine)` 改成 `if getattr(engine, 'engine_type', None) == 'cosyvoice' and engine_declares_local_cosyvoice_shutdown_status(engine)`。Root已中断该CLI（exit1）。这会改变已验收的声明API验证语义，不能在导入提速切片夹带。

恢复时**只撤销本次新增engine_type条件这一hunk**，保留其余既有assistant修改，禁止git checkout/reset。C2a测试TempFileShutdownStatus补active_io_leases=0属于新增字段fixture适配可保留；Edge legacy getter None的失败先明确记录为此前契约/旧测试差异，不为绿灯修改已通过Core。若认为必须改变契约，另交Root具体触发/风险后单独切片。

Rootprobe已覆盖原任务明确边界扩到10项：新增alias调用后资产被换成file要拒绝、ModuleSpec有效、foreign替换要fail-closed且清scope票据、foreign冲突不能掩盖原KBI。都仅tmp新fixture，无真实SDK/模型/资产修改。请按已有清单实现，不改Root断言；缺的行为不能仅在ADR写已完成。

Root源码和调用方完整审读；独立tmp/codex_local_import_root_boundaries.py 6项中4失败（2.156秒，WAV826零差），未放行真实模型。Cursor7OK/1skip只覆盖主解释器未装wetext的情况，不代表vendor虚拟环境缺wetext，也未满足原任务的假导入/资产/线程边界。

1. **P1 固定资产边界没实现**：make_local_snapshot_download仅resolve+is_dir，接受任意现有目录与symlink逃逸，别名调用后不复核。Root arbitrary dir明确未拒绝。应调用既有path_trust，先验lexical链接/junction与固定PROJECT_ROOT/WETEXT_DIR_REL，然后返回已验证根；正常resolver调用也确认未被替换。复用现有pin/asset次序，不增CLI路径、不碰真实资产。fake测试可patch项目根常量/构造固定相对布局，不能把生产接受任意目录当测试便利。
2. **P1 foreign module被无条件覆盖**：_restore_modules无论当前是否自己安装对象都pop/恢复旧值。Root scope中另一方替换后，退出错误覆回original。保存installed对象身份，仅restore仍属本次安装的entry；foreign替换保留并fail-closed报告冲突。恢复遇错也必须finally清depth/锁票，原KBI/SystemExit不能被restore错误掩盖。
3. **P1 线程隔离没有**：当前临时stub普通ModuleType且公开snapshot_download，另一线程直接取到缓存假API，Root已捕获。对临时模块的敏感属性访问做owner-thread检查，foreign线程明确拒绝；不占全局import锁长等待，不复制SDK。现有scope互斥只阻第二scope，并不阻普通import。退出scope保留在已知consumer的alias仍严格local。
4. **P2 repo输入宽化**：str(object)/strip/casefold可把任意对象转成known ID，Root非str伪装明确被接受。只接受已知精确str及正常零额外args/kwargs；不执行未知对象__str__、不输出任意repo/path原文。别添加未需要的modelscope.hub API；两处固定from modelscope import snapshot_download只需最小顶层适配，未知API失败。设置合适ModuleSpec避免find_spec异常，不回退真实SDK。
5. **P1 测试busy-spin与可靠性**：concurrent test while gate.is_set(): pass会满占CPU。改Event release.wait有界、finally释放、捕获所有thread异常、真实join并assert dead。保留Root6断言。已有integration有依赖skip不算核心验证：用fake finder/loader真实执行已知consumer导入，证明SDK loader不触发、runtime先asset后scope、缺失/链接/未知拒绝、异常与并发清票。不只检查源码字符串。保持旧_import_cosyvoice_automodel_class(cosy_root)内部可测入口兼容（新增wetext_dir可选固定默认），不能让旧fake/C1/C2无端回归。
6. **P1 再次绕开Policy/漏回归**：新tmp/codex_cosyvoice_local_modelscope_import_gate_2026_11_03.py仍裸subprocess.run（已多次明确禁止）。不要再写重复supervisor，直接既有Root gate --modules。删/停用本轮新裸gate，保留历史产物。按原任务一次120秒：新模块+tests.test_cosyvoice_production_slice_c1、c2a、c2b1、c2b2a、cpu_checkpoint_staging；失败STOP。Root不会把7项绿灯当这些回归已过。

统一按清单补完，更新ADR/handoff，禁止真实模型/设备/main/正式memory/vendor/sitepackages/预算/声学/Native改动或提交推送。STOP待Root。安全隔离不完整不能以提速为由放行。
