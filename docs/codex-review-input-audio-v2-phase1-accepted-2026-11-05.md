# 输入优化 Phase 1 里程碑（候选适配器）

状态：代码/有限固定音频验证 Accepted，**仅在数值库单线程启动条件下通过资源准入**；尚未接入默认生产。后续阶段不得遗漏正式入口的线程预算。

官方Cursor作者实现，Codex独立审查并执行：

- `input_v2_adapter_independent_nov5f`：101项相关fake测试通过；模型固定SHA/verified bytes、CPU图契约、真实输出tensor、关闭有界重试、末帧取消与文件边界已评审。
- `input_v2_native_first_nov5d`：暴露实际图None维兼容问题，已修复；失败证据保留。
- `input_v2_memory_attribution_nov5h`：未经线程限制时，NumPy首次导入使private约14.96→530.58MB（RSS只增加约12MB）；不是Silero权重单独占500MB，但也不能忽略该提交量。
- `input_v2_numeric_threads_probe_nov5i`：只给受控子进程设置OPENBLAS/OMP/MKL/NUMEXPR线程=1后，通过原256MiB增量private准入。主/用户进程环境未改。

原生报告：`tmp/silero_native_20261105_e177fce324c1.json`。同25个合成非人声WebRTC误放行13、Silero概率0.6门限和既有时长/比例标准放行0；9个固定RVC语音/减幅对照通过9。不是用户房间错误率或ASR准确率，未开麦/播放/ASR/其他模型。

冷首次239.086ms；1秒音频热评分p50=4.081ms/p95=5.507ms。before RSS/private=23678976/14819328，loaded=73277440/52744192字节；500次循环前后RSS/private均79040512/56659968。close后session weakref消失，private降到46108672。root监督总3.375秒，WAV728→728无增删。没有看到短窗口持续增长，不代表长期绝无泄漏。

模型固定commit066409f3d9db45a30d21dfcbf9147826ffdd7052、2327524字节、SHA256=1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3。主Python新增CPU ORT1.24.1及固定小依赖，未装torch/GPU包或改变其他模型环境。生产输入链路/默认尚未切换。

下一步为Phase2a：安全固定启动线程预算 + STT拥有并回收候选 + 同源概率判定集成。随后才做连续输入/NS与双工；不能将Phase2a叫作全部方案完成。
