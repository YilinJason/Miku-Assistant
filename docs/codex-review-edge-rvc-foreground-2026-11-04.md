# Edge/RVC有界前台：Root最终PASS

最终源码逐项核对，Root先8模块115 tests/15.817s OK(skip1)；最后绑定pyttsx3/声明矩阵修正后独立Root8/0.032s PASS（含真实Thread+Config协议pending与最终join）及元数据/App定点42/4.785s OK，均WAV826零差，编译/差异检查通过。固定共享元数据保持轻量、Config协议正确、warmup_events=None兼容、声明未知failclosed、警告全链保留，原sync ensure及Cosy合同保留。此小项闭环；无真实模型/设备或时延样本，不宣称共存性能达标。

监督器已拒绝重用任何存在artifact的label。下面是历史审查及返修细节，不代表还有这些未修问题。

## 声明判别最终微修（统一处理，避免逐例补丁）

engine_declares_rvc_prepare_state仍只查class且仅callable/property算声明，明确`get_rvc_prepare_state=None`、实例字段getter或classmethod会被错当legacy。声明=inspect.getattr_static(实例, name)能找到该名称即可，成员不可调用/读取异常/返回非法值都交现有failclosed marker。仅真正缺失名称legacy兼容，不区分Mock、不循环猜测签名。这个改法更短，也覆盖继承/实例属性/静态或类方法。参数化验证无属性legacy；None/非callable/坏getter/坏返回failclosed；实例getter、classmethod/staticmethod返回有效pending/complete状态按值处理；KBI/SystemExit保留。只小metadata/helper测试，不改其它机制或加框架。

Root probe新增上述声明矩阵为第8项，原7项不改。完成此微修后本轮只需Root8及相关40级定点，不再全量重复。仍须唯一label、禁止回退/改Root探针。

## 四项返修Root独立已过；最后bound engine判定

Root独立6项0.036s PASS（额外穿透未绑定Config协议的真实request短等待、后台实际Thread退出与警告），以及8模块115 tests/15.817s OK(skip1)，均WAV826零差；Core元数据已移轻量模块，之前四项已修。最后 `edge_rvc_warmup_required` 对**已绑定**引擎仍只排除Cosy，pyttsx3也返回True；配置未绑定路径已正确排除。请补严格Edge标签（含实际映射别名的兼容），并测试绑定pyttsx3不走Edge RVC前台/不等待该预热。只此小修，禁止扩大范围/回退命令。Root probe现7项，新增bound_pyttsx3断言不可改。

记录：Cursor误复用了两个已有label，覆盖旧Root失败报告；Root不把它当独立验证。Root监督器现对已存在json/stdout/stderr拒绝覆盖，以后必须唯一新label，不能改Root监督器解除保护。Root已用新label edge_foreground_root_verified_v2_20261104与edge_foreground_root_regression_v2_20261104独立验证。

工作区巨大dirty。**禁止git checkout/reset/restore/clean等回退命令**，仅git status/diff/read允许；不可覆盖整个旧文件。Root tmp/edge-foreground-root-before-20261104是**本轮前的原始基线快照，不是预备实现**。Cursor上一轮误checkout engine后已从它恢复再加新状态，Root AST核对：原TTSEngine方法无删除，仅__init__/prepare_rvc改变+新增getter，其余合成/转换/关闭方法保留；此后不要再做回退。

Root自有probe tmp/codex_edge_foreground_root_2026_11_04.py（不可修改）5项中3FAIL+1ERROR，WAV826零差，报告tmp/codex_gate_edge_foreground_root_boundaries_20261104_2026_11_02.json。不是单测数量不够，而是实际调用边界漏测。仅修以下范围：

1. **P1实际配置没进foreground**：voice_activation.config_expects_edge_rvc_warmup只接受isinstance(config,dict)，实际src.core.config.Config是get协议对象，未绑定TTS时返回False，仍走同步ensure。支持真实get协议及旧dict，按当前get_rvc_config/model_path语义，不读memory或开服务。新增Config类协议/未绑定App真实request限时pending测试，不能只用dict。
2. **P1默认--mode voice启动报错**：assembly新defer分支对warmup_events无条件append，但默认None。Root未传可选参数即AttributeError。保留None默认兼容，只有显式events列表才追加，实际装配默认参数fake用例必须过，零RVC模型预热。
3. **P1未知声明状态假ready**：read_rvc_prepare_state把没有API、getter抛错、类型错都折成None；bound_tts_models_ready和warmup_registered把None当legacy成功。必须用inspect.getattr_static或同等声明能力判别，缺API legacy兼容，已声明getter异常/无效值failclosed，KBI不吞，不能因为Mock动态属性就导入重模块。RvcPrepareState和公共warning移至stdlib-only共享小模块（Core或轻量rvc metadata），engine保留兼容导出同identity，Core不反向import重engine验证。冻结状态字段严格bool/字符串，畸形声明不当legacy。
4. **P2警告全链丢失**：lane bound准备成功和新candidate发布都返回裸VoiceStackLoadResult(ok=True)，_fg_wait_outcome等ready fast path又裸ok，Nahida失败warning丢失。保持warnings经同步bound、新candidate发布、foreground等待完成、已失败fallback再请求/CLI一路传达；只输出固定路径无关文案，不改fallback是否启用/播放策略。不通过共享全局临时变量拼接，状态快照可只读生成警告。补实际App request/CLI结果assert warning，不能只测试warmup函数返回3元组。

补充：选择条件严格Edge（含已映射旧engine别名），不能把pyttsx3也当Edge+RVC强制加载。预热开关false、无RVC、legacy注入保留行为。原Cosy及unknown OS start/关闭gate保持，不能放宽已通过六字段回收/资源保护或修改Root探针。

## 验证/交付

先新增真实边界测试+Root自有5一次（若失败停止）→原foreground/App/assembly lazy/模式/语音CLI/RVC准备回归分组一次。所有命令必须现有tmp/codex_scoped_test_gate_2026_11_02.py --modules/--probe，唯一label/总<=180秒，前台顺序并返回实际json路径，不detach。源码静态检查不替代测试；Root报告不能改或覆写。

只此返修，暂不实现显式重试/预取。保持正式data/memory禁读改删、声学/预算/权限/模型/系统设置不改，不跑真实模型/设备，不提交推送。读取当前task和skills再动手。完成STOP待Root严格复审。
