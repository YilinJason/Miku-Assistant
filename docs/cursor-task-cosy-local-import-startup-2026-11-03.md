# Cosy本地依赖导入优化（生产小切片，先fake，禁止擅自跑模型）

前置读 AGENTS、架构/工程质量/协作技能、docs/codex-review-coexist-control-2026-11-03.md最新两模型结果。默认startup30秒不改；不改声学/模型/Normalizer语义/TFM/NativeJob/SafetyPolicy/正式memory、vendor或site-packages，不提交推送。仅worker内导入适配，最终Root验收后一次短真实对照。预取尚未放行。

## 已确认与假设

两模型实测Cosy resource admission2.047s、wetext import14.391s、Cosy import4.419s，然后constructor未及30s预算。当前_apply_local_wetext先import wetext.wetext（其from modelscope import snapshot_download触发SDK导入），然后立即把snapshot_download改本地固定FST。固定vendor cosyvoice.cli.cosyvoice唯一Modelscope依赖也为该下载函数。整个SDK在本路径无需网络模型发现。SDK具体成本尚未单独测量，不把14.391s全归给它。

## 建议设计（风险必须完整考虑）

在**专用Cosy runtime worker**内，为已验证固定本地资产的已知导入提供小scope：仅在import wetext及固定cosyvoice模块期间，让 `from modelscope import snapshot_download` 绑定本地resolver；其余未支持API明确ImportError/拒绝，绝不静默回退真实SDK/网络。可用短生命周期ModuleType+sys.modules精确保存/恢复实现，或同样窄的机制；不能把整个SDK永久stub、不能篡改模块源码，不复制文本规范化实现。

resolver只接受已知 `pengzhendong/wetext` 的正常调用并返回已验证固定wetext根；其他ID/路径/kwargs拒绝，不接受任意模型目录。不许因本地模型路径缺失去下载Cosy。继续先走现有path_trust/pinned assets检查，再scope；保留原Normalizer类、FST、AutoModel构造和CPU staging。

scope窄锁只用于同步导入互斥/登记，避免其他线程从临时模块拿到假API；只worker主线程调用，线程其它已知watchdog不导入第三方。嵌套/并发快速失败，不长等待。保存原sys.modules对象精确恢复（包括原本不存在、已有真实模块、导入抛错/KBI/SystemExit），清理不覆盖不属于自己的后续模块。已导入consumer保留的snapshot函数alias必须仍严格本地，不能退出scope后又变成全局任意下载入口。不得污染主程序进程/其它引擎/评估脚本。

不要为了绕过SDK把find_spec、importlib、torch等全局函数大范围patch；未知依赖行为fail-closed，回归清楚报告。若实现方案仍可能引入全局污染或正常化变化，先停交设计问题给Root，不自加回退开关或SDK副本。

范围建议一个窄src/voice/cosyvoice helper + scripts/cosyvoice_runtime_worker.py接线 + 有意义fake测试/短ADR；保持现有_apply_local_wetext和_import_cosyvoice_automodel_class可测试入口，少重复包装。不要为减少30秒等待暗增线程/GPU模型并行。

## 验证与交付

fake模块导入证明只有已知下载函数绑定本地、真实SDK importer未触发、已有模块精确恢复、原本无模块不残留、异常/中断恢复、嵌套/并发拒绝、退出后alias仍本地、未知repo/参数拒绝、缺失/链接资产拒绝、其它引擎不受影响。要测实际runtime调用链顺序（asset校验在适配之前）及原C1/C2/CPUstaging边界，不能仅源码字符串匹配。

一次120秒targeted gate，失败STOP；Root现有监督器运行显式fake模块，不discover，不用模型/设备/main。handoff/ADR说明具体SDK减少点与未验证性能，STOP待Root。Root独立review/fake后才允许一次现有--no-ollama --prepare-only短真实对照，未达效果继续定位，不称全部完成。
