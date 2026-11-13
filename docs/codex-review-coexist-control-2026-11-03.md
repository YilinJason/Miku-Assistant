# 恢复后的真实共存诊断：FAIL，停止追加模型负载

## 后续两模型受控对照：范围缩小，完整回收 PASS

修复监督器并Root16+6fake及嵌套Job实证通过；两模型路由Root8fake0.011秒通过。随后一次不加载/连接Ollama、prepare-only对照：RVC24.453秒成功；Cosy30.141秒超时。启动进度 resource_admission2.047秒、wetext_import14.391秒、cosyvoice_import4.419秒，automodel_construct开始但30秒预算到达未完成。不能把两模型也失败说成必然仅Ollama竞争。

总56.78秒；owned树采样RSS峰5481.8MB，最低空闲RAM14285MB，运行循环不查询GPU所以无VRAM峰值证据。第一次engine.shutdown六字段全True；TFM完整；外层child/server quiet及cleanup_complete全True。无合成/转换/播放，captures0。报告 tmp/codex_real_two_model_control_root.json；子报告 data/test/voice-naturalness/cosyvoice-production-two-model-control/b2f32668ee37/report.json。比旧监督明确改善的是退出与结果可靠性，真实加载仍FAIL。

源码定位一个可验证的优化假设：已安装wetext.wetext仅为snapshot_download导入Modelscope整套SDK，生产随后立即把该函数替换为固定本地FST路径；固定vendor cosyvoice.cli.cosyvoice也只用该SDK下载函数，固定本地模型路径存在时根本不调用。14.391秒是整个wetext导入，并非已单独测得Modelscope成本。下一切片尝试仅隔离worker内有边界的local resolver导入适配，保持原Normalizer/模型/声学/预算，不改vendor或site-packages；必须fake严格验证以及短真实前后对照，未测不得宣称提速。

前台 request 已 Root 分层 PASS，见对应评审；本页记录另一项真实性能问题，不倒推 fake 验证为真实通过。

本次固定公开短句、capture-only（从未打开音频设备）、临时自有127.0.0.1:11445服务，原默认预算不改。Ollama加载88.5秒，RVC阶段87.297秒（Worker准备63.112秒后无ready，health等其他开销另计）；175.875秒协调器失败于RVC，根本未进入Cosy。captures=0。真实aggregate三次均persistent_rvc_complete=False，manager未关闭，正确未伪报完整。诊断子进程退出时Native Job父退出兜底；Root外层监督迟于预算收尾，手工Ctrl+C停止本次自有监督。

保留原始日志 `tmp/codex_real_resumed_coexist_control.{stdout,stderr}.txt`、临时服务日志；子报告 `data/test/voice-naturalness/cosyvoice-production-coexist/59204f7029a7/report.json`。外层未产生最终采样JSON，不能声称有完整资源峰值证据。第二次CIM确认无python/llama-server/nvidia-smi残留，11445端口关闭；存在用户Ollama app及其serve/gpu-discover，未停止用户进程。

与前次Ollama17.703秒/RVC98.672秒/Cosy资源准入超时、单独RVC21.25秒退出47ms相比，说明耗时和退出在共存负载下失败，但尚不能确定是冷文件缓存、模型竞争、驱动/系统负载或别的原因。禁止自动关闭其他应用、改杀毒/注册表/驱动、盲增预算、静默更换音色。

下一步先修开发监督器可靠性：运行循环不应同步等待GPU外部查询，deadline必须优先、所有BaseException均清自有child/server、输出自有PID及逐阶段元数据到独立报告；故障注入验证超时/中断真正回收。不启动更多真实模型直到该测试工具切片独立PASS。之后只一次RVC+Cosy无Ollama对照，并在资源允许下阶段剖析；如仍失败先定位真实阻塞，不能宣称共存改善。预取规格已备好但未放行。
