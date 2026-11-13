# RVC运行时禁止隐式下载：本地资产快失败

前置装配lazy已Root75 tests PASS，见对应review。本切片生产/测试由Cursor写，Root复审；只完成此项然后STOP。读取AGENTS、相关四skills、Safety规则，保留所有dirty，未提交推送。正式data/memory禁读改删。不启动真实模型/主程序/设备/清理，不改资产、vendor/site-packages、声学/引擎默认/权限或Root探针。

已确认scripts/rvc_worker.py:_load_vc和scripts/rvc_infer.py:main调用第三方download_rvc_models。已安装函数缺base_model/hubert_base.pt或rmvpe.pt时直接requests.get无timeout，且rvc_python.__init__会导入rvc_python.infer（重依赖）。所以不能先import rvc_python再宣称快失败。当前真实资产存在，不删除或移动它们来测。

## 实施范围

新增一个stdlib-only窄helper src/voice/rvc/local_assets.py，用importlib.util.find_spec('rvc_python')的包origin metadata（不import第三方）解析固定base_model目录，验证两个固定文件为非空普通文件。只stat，禁止read_bytes/hash大权重/网络/建目录/下载；无模块/namespace origin不明/文件缺失/空/目录/不可读metadata均稳定失败，不记录用户路径/模型正文。明确本次只检查存在性不是权重内容可信保证；KBI/SystemExit不吞。返回解析后lib_dir供原有Config/VC沿用，避免__import__二次定位。

两个脚本改用此helper，删除download_rvc_models的import和调用。worker _load_vc及真正main入口都应在torch patch/Config/VC重导入之前检查，避免缺资产仍先加载CUDA；可把patch调用移入_load_vc的资产检查之后，保证fairseq兼容仍在真正加载前。infer main同样先检查本地资产，再patch/重imports。失败沿原worker ready:false/exit1或infer stderr/exit1协议，稳定路径无关错误，不新增抛栈或伪ready。保留early NativeJob join与watchdog及已有close协议，不扩大到设备选择/健康检查/prepare sticky缓存，本轮不改它们。

## 验证

fake包spec+临时小文件（不要真实权重）：缺/空/目录/metadata错误/无包/namespace拒绝，齐全返回目录，不导入第三方包；meta_path拒绝torch/rvc_python实际执行及download模块验证缺资产零重import/零网络。必须调用两个脚本真实函数/入口验证先拒绝再patch/模型初始化，不只测helper。齐全时fake Config/VC与torchpatch验证原顺序/设备参数/ready输出仍在，无真实torch/推理/设备。KBI原样传播。主Python3.12离线，helper也语法兼容RVC3.11；不pip install。

测试不得清空全局sys.modules污染回归，子进程用已有SafetyPolicy+固定命令/timeout，或patch.dict精准恢复。只运行新tests.test_rvc_local_assets及相关rvc_health_probe_budget、rvc_owned_worker、rvc_worker_lifecycle，现有Root scoped gate总180s，失败停止修正，不全量discover。compileall与diffcheck；交接区分Cursor结果和Root验收。不得声称本项已解决当前真实三模型共存慢。
