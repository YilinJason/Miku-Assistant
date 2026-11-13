# 当前资源切片：避免重复RVC重导入探测（已放行Cursor）

前置本地基础资产快失败已Root91测试PASS+实际3.11 metadata无重导入通过。本稿已放行。用户要求Codex设计/review，官方Cursor实施，保留所有dirty/声学/默认模型/权限/NativeJob协议，正式memory禁读改删，未提交推送。读取AGENTS与相关skills。当前RVC真实prepare约24秒，包含健康探针单独重导入RVC，然后真正worker再次导入；不据此承诺固定提速。

只改health_check、TTSEngine.prepare_rvc及必要fake/ADR。check_rvc_health保留默认完整探针，可增加严格bool关键字probe_import=True；False只执行现有文件/解释器/脚本检查，不生成额外Python子进程，报告与文档明确只是本地预检。prepare_rvc仅在worker_enabled=True且随后确实启动/等待worker ready及可选warmup时用False，非worker路径仍完整探测。共享SafetyPolicy/封存profile/worker能力验证继续由真实worker路径执行，绝不因省探针授予新权限。

_rvc_ready只在所需worker ready/warm完成后设True，不能本地文件检查一过就暴露True。warm失败/异常/取消/KBI保留False，旧sticky失败缓存仍防止自动重试风暴，本轮不改变显式重试协议/不改外部CUDA探针。并发prepare不应启动两个worker，利用现有owner gate，不新增等待长锁/线程。health默认API/30秒cap保持，原诊断命令不会悄悄跳过探针。

fake验证：持久路径IMPORT_RVC探针0次、真正worker start/warm一次；非worker/health默认探针一次；仅文件通过而worker失败/未ready不能算就绪；权限拒绝/部分失败/重复调用/KBI不伪成功，实际相关函数穿透而非只mock上层返回。旧测试若mock健康函数签名不接受新kw，精确迁移fixture，不能改关门断言或Root探针。只相关RVChealth、worker生命周期、Cosy c2b2a、App/前台回归；Root scoped gate、180秒、WAV零差。离线测试通过后STOP待Root，不自行运行模型，不调超时/声学。
