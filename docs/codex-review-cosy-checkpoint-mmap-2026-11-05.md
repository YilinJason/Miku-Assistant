# CPU checkpoint mmap：Root代码、离线与真实短测通过

官方Cursor18a41f74-6d7b-426c-85f6-e3ac0d7f0375已STOP；仅checkpoint_loading.py与新test_cosyvoice_checkpoint_mmap.py。Root逐行审查及返修后：原owner/active alias/单scope锁不变，反射能力明确支持mmap、固定三文件、CUDA映射与weights_only严格True才进入；只读4字节ZIP头，显式mmap参数保留，不支持/普通探测失败仅不加速；original只调用一次，原错误自然传播。没有精度、超时、资源门槛、vendor和正式memory改动。

Root在测试前发现fake未定义kwargs及命名参数录制不完整，均修复；并要求线程在scope退出前finally释放gate并实际join、检查wait返回和主线程错误。源码重复helper已收敛。独立gate cosy_mmap_repo_20261105x：162项OK、skip2（symlink权限与主解释器wetext可选集成）；22.056秒，WAV826零差。此前撤回精度切片的基线36项通过（1skip），四个基线文件字节一致。

真实对照：mmap_fp32_nov5y与precision_fp32_nov5u，同默认30秒、FP32、同13字六句、capture-only。Cosy准备均14.328秒，不能声称提速；准备完成子树private14235.09→13185.17MiB，RSS7275.99→6485.12MiB，减少中间驻留/提交约1GiB。父树离散采样峰RSS7070.35→6704.43MiB，系统最少提交余量29973.36→31070.71MiB。不同采样频率与背景负载不能混作精确全局峰值或通用硬件保证。

六句全部成功，后续private约10970–11022MiB平台，无持续递增；关闭后RSS105.86/private1043.23MiB，线程38/句柄342，aggregate与TFM完整、child/server quiet、cleanup_errors为空。子报告data/test/voice-naturalness/cosyvoice-production-two-model-control/46afd9e3188f/report.json。接受为有界内存优化，非冷启动速度达标或长期无泄漏证明。
