# RVC重复预检：Root最终PASS

Root前一轮117测试/29.913秒OK，总32.110秒；最后仅删除新增吞异常分支后，Root定点39测试/0.280秒OK（监督总2.421秒）。两次WAV826零差，编译/差异检查通过。最终diff仅严格bool预检、持久路径本地检查、实际worker warm返回后发布ready；普通及控制异常原样传播，sticky失败避免自动重启。无CUDA/Safety/NativeJob/声学/超时变化。该小项闭环；没有运行真实模型，尚未实测提速。下列为已修复审查历史。

## 最后契约修正（返修引入，需保留原异常）

TTSEngine新except Exception: return False吞掉了原本向协调器传播的warm异常，既无稳定日志也丢掉具体故障分类；Root要求只是“异常发生后ready仍False”，并未授权把所有异常改为False。由于调用前已经置False，直接调用warm_rvc_worker即可，删除新try/except（KBI/SystemExit也自然向外传播），普通异常与控制异常保留原对象，sticky done仍防止第二次重启。把新test_warm_exception_sticky_not_ready改为断言同一个RuntimeError上抛、ready=False、第二次False且warm仅一次；保留warm返回False测试。仅重跑preflight/health/c2b2a定点，不扩scope。这也缩减了无必要分支。

差异符合范围：持久路径改本地文件预检，真实warm_rvc_worker返回前不发布ready；默认health仍完整，NativeJob/Safety/声学不改。收口两项后Root一次独立回归：

1. 新probe_import是任务要求的严格bool，目前直接if not probe_import，0/None/空字符串都会跳过子进程探测。函数开头（文件访问前）type(value) is bool，否则ValueError固定参数标签；参数化测试0/1/None/字符串拒绝、零探针，默认True/显式False保持。
2. 补warm_rvc_worker回调正在执行时观察engine._rvc_ready=False的行为断言，及warm抛普通异常/KBI时仍False、失败sticky重复调用不再重启；真实协调器穿透warm.ready=False/失败不能进入Cosy的旧c2b2a合同保持，不改Root或原关门断言。

只两项，不扩成CUDA设备选择/失败显式重试/模型加载或其它优化。新测试+指定相关回归一次前台gate，完成STOP。真实提速尚未测试，不以单元测试数字替代时延证据。
