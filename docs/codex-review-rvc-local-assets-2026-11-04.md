# RVC本地资产：Root最终PASS

两轮定点返修已核对源码及脚本调用：metadata预期异常规范，origin类型拒绝；单次nofollow stat+S_ISREG非空；两个脚本均在torch patch/VC导入前检查，不再调用第三方自动下载。NativeJob early join未改。Root scoped gate rvc_assets_root_final_20261104：91 tests/34.912s OK(skip1为Windows链接权限)，总37.109s，WAV826零差；编译/差异检查通过。额外实际RVC Python3.11仅调用metadata检查成功，sys.modules确认无torch/rvc_python导入，不读取权重内容或启动模型。此切片闭环；存在性检查不保证权重内容可信，未来资产管理需独立确认。下列为已修正初审历史。

两个下载入口已移除，NativeJob早期入口未改；检查前于torch patch，方向正确。一次统一修正以下边界，勿扩scope。

1. local_assets._resolve_lib_dir_from_spec没有把find_spec的ImportError/ValueError/OSError等普通metadata错误规范为RvcLocalAssetsError；infer只捕获后者，会出现原始异常而不是约定stderr/exit1。限制捕获可预期metadata异常并固定标签，不吞KBI/SystemExit。spec.origin须为有效字符串且spec是文件包，namespace/无origin/错误类型明确拒绝；真实文件包origin __init__.pyc同样是文件，应取dirname，不能把非.py origin本身当目录。对无包/不明metadata稳定失败，不把不相关单文件模块的旁目录当RVC包。
2. _is_nonempty_regular_file用了stat(follow_symlinks=False)却又os.path.isfile（跟随链接），链接本身长度>0可把链接目标误判为本体普通文件。一次stat后用stat.S_ISREG(st.st_mode)及st_size，避免二次不同对象检查；这是存在性检查，不承诺并发替换安全或权重内容可信。
3. 测试当前未覆盖worker真实main ready:false协议，以及infer齐全成功路径。缺资产两入口必须证明patch/Config/VC不调用，无下载模块import；齐全两入口用fake Config/VC/写wav保持顺序/设备参数/ready行为。当前assertNotIn(torch,sys.modules)依赖同进程测试顺序，移至fresh受控子进程或明确patch重库入口并断言零调用，不删全局sys.modules。加metadata普通错误/KBI、.pyc包origin、stat非regular行为测试；fake临时文件别写全局固定out.wav，用本fixture目录。

此轮完成以上合同即可STOP，不再多跑整套或改其它功能。不重写Root探针，不动正式资产/memory/vendor/声学/Safety。测试与compileall后返回精确结果；Root独立复核才能验收。
