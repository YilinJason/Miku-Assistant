# 最终决定：FP16实测无收益，不纳入生产

FP32 precision_fp32_nov5u：后五句Cosy平均3290.4ms、RVC965.6ms，树峰值RSS7070.35MiB；FP16 precision_fp16_nov5v：Cosy3856.4ms、RVC1006.2ms，峰RSS7172.94MiB。两次均默认30秒准备预算、六句成功并完整回收；FP16的0.438秒准备差不足以抵消稳态变慢约17%。本对照不是严格冷缓存实验，不能普遍外推所有GPU，但已不足以支持在本设备采用。

因此只撤销本轮自己的生产开关、新test及tmp接线；保留全部此前已验收改动。官方撤销会话32c3ec7f-0d1c-4cbb-b32c-f96b251ba1ac已STOP，Root核对settings/runtime/supervision/smoke四个基线字节完全一致；worker_cli按精度窄差异复核。全部实验源码归档tmp/cosy-precision-experiment-20261105，真实报告保留。没有用git reset/restore/checkout/clean，没有触及用户原有改动。以下是被撤销实验的离线验收历史，不代表当前有此生产配置。

# CosyVoice精度开关：源码与离线边界已通过，真实性能待对照

官方Cursor会话83034f85-6d35-4679-be15-ab7115845147已STOP。仅settings.py、worker_cli.py、scripts/cosyvoice_runtime_worker.py及新tests/test_cosyvoice_precision.py；未改vendor/config或原有测试。Root复核最终实现：默认FP32行为/原argv兼容；配置只接收严格bool，固定argv只接收true/false，builder拒绝非bool；CPU checkpoint staging与stdout作用域保留，构造同样遵守准入和模型所有权。

Root修正了初稿任务对CLI的错误假设（实际固定argv、不是JSON），将必要worker_cli纳入同切片。代码评审还收敛重复校验、修正依赖sys.modules全局干净的测试，并在执行前发现并修正测试绕过import guard的调用，避免真实torch导入。正式data/memory未访问，未运行设备，未提交推送。

实际验证：

- cosy_precision_repo_20261105p：6模块202项，OK、skip2（Windows symlink权限与主Python未安装wetext的额外集成项；非本切片核心验证）；23.316秒。新严格bool/argv/fake构造与既有能力、准入、输出、CPU staging、本地导入边界通过。
- cosy_precision_root2_final_20261105r：Root独立2项通过，从配置到真实worker main的构造参数链和FP16不得绕过低RAM拒绝；stdin线程被显式fake。旧q报告实际运行了11项（导入TestCase被额外收集），保留但不把它当最终纯Root2证据。
- 上述gate共享WAV826→826，无增删。

固定SDK fp16主要改变autocast计算路径，不能在未实测前承诺速度、常驻权重减半或音质相同。真实A/B需要相同固定文本、资源线与准备模式，记录准备时间/每句耗时/峰值/音频有限性/退出；生产默认暂不切换。
