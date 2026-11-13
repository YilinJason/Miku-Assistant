# 真实模型资源短测（持续更新）

诊断源由官方Cursor实施，Root逐行审查并用fake实际执行正常/失败/中断路径；不启动主程序或设备。Root6矩阵、监督器24、资源中止fake3分别通过，WAV826零差后才运行真实模型。早期诊断假共存/失败不完整回收/中断丢记录问题均在放行前修复，不能把它们当生产缺陷或真实成功。正式data/memory禁访问，未提交/推送。

## 已通过：RVC三生命周期、30次真实转换

监督报告tmp/codex_real_memleak_rvc3_nov5a.json；子报告tmp/codex_rvc_real_exit_7e013bec9ff1490e82c2b5ccf69eaa0b.json。总80.53秒，3轮各10次固定参考WAV转换，原文件仅复制到TFM-owned路径、只读且SHA256保持。没有播放或麦克风。

| 轮次 | 关闭后RSS MiB | 关闭后私有提交 MiB | 线程 | 句柄 | TFM |
|---|---:|---:|---:|---:|---|
| 1 | 111.47 | 1046.73 | 38 | 345 | complete、owned/pending/inflight/leases均0 |
| 2 | 111.63 | 1046.73 | 38 | 345 | 同上 |
| 3 | 111.59 | 1046.61 | 35 | 343 | 同上 |

冷基线RSS99.31、私有提交1037.27MiB，第一轮后有少量库/缓存常驻；后两轮没有继续明显增长。RVC模型Job、raw进程及IO读写/lease关闭后均不再持有；aggregate六字段complete。每轮关闭约47ms。父监督观察峰值RSS4200.14MiB、最少物理余量44688.3MiB、最少提交余量36273.09MiB；没有触发中止，child_quiet/cleanup_complete=True，循环无外部GPU查询。RSS是物理驻留、private是私有提交，二者不能相加或混称物理RAM。

此短样本没有复现逐轮持续增长的RVC泄漏，但不能排除更长会话/其它分支。退出后全局临时WAV仍826个，选定样本SHA256=7C4AEE8B69428E4BBE3BB773CFC47EFAE1618EFB46B7B80EBAF1465739FE19BA。系统nonpaged1767436288→1789853696 bytes、paged1885024256→1942024192 bytes；背景应用并未冻结，不能将这两点差值全归于RVC，且未出现旧约60GB系统占用。

## 未通过：默认30秒CosyVoice冷启动

tmp/codex_real_memleak_voice6_two_nov5b.json：RVC准备11.344秒，Cosy准备30.156秒返回False，阶段已到automodel_construct，未生成音频。resource_admission约6.0秒、cosyvoice_import累计到23.7秒；因此六句重复合成未实际执行。总42.89秒、峰值RSS5314.77MiB，child_quiet/cleanup_complete=True，TTS/RVC aggregate与TFM complete；临时文件无新增。不能把此结果写成六句通过，也不能据此宣称Cosy长期无泄漏。

接下来只允许明确标注extended-cold诊断（已存在child固定60秒模式，父仍180秒且所有资源线保持）；生产默认30秒与全部模型参数不改，原失败保留。该模式用于分离冷启动慢与重复推理泄漏，不是生产性能优化完成的证据。

## 已通过：显式extended-cold双模型六句

tmp/codex_real_memleak_voice6_two_ext_nov5c.json，总55.09秒，子报告data/test/voice-naturalness/cosyvoice-production-two-model-control/7684e77aa4a7/report.json。固定13字短句六次真实Cosy合成+RVC转换，捕获6个WAV、不播放。单句wall为5500/4375/4391/4485/4282/4485ms；不是端到端麦克风对话或首段延迟指标。

六次检查点私有提交10987.93/10969.95/10972.35/10965.22/10984.77/10975.98MiB，RSS约4833–4861MiB，没有在这六次中持续递增。关闭后RSS107.23、private1044.83MiB、线程38、句柄341，模型进程与TFM实际退出。监督峰值RSS7026.72MiB，最少物理余量41759.77MiB，最少提交余量30357.17MiB，cleanup_complete=True。其后系统提交余量恢复到44339.71MiB；不能把即时全局差额全部当成该程序泄漏。

这是明确60秒冷启动诊断模式。第二轮受前次文件缓存影响，Cosy准备仅14.672秒；不能推翻默认30秒首次冷启动失败，也未修改生产默认超时。

## 三模型诊断的前置失败与就绪修复

memleak_three_ext_nov5d在加载前因首个API元数据超时拒绝，六句未执行，owned进程全部回收。两次元数据only实验定位为TCP已开但API未ready，默认与直连均复现。诊断ready门修复及Root独立8/24/7和真实元数据only均通过，详见docs/codex-review-isolated-service-readiness-2026-11-05.md；原失败报告不覆盖。

## 已通过：三模型准备后六句与退出（显式extended-cold）

监督报告tmp/codex_real_memleak_three_ready_ext_nov5e.json，子报告data/test/voice-naturalness/cosyvoice-production-coexist-extended/e2a46d4a6655/report.json。总87.88秒，无超限、无强制超时，cleanup_errors为空，child_quiet/server_quiet/cleanup_complete均True；保留前次失败而非覆盖。准备后/api/ps确认同一Miku_Assistant-emotion:latest仍驻留，size/size_vram均8360946564 bytes、context8192，未预先卸载模型。驻留只在准备后确认一次，未逐句重复查询；本测也不包含六次LLM真实生成、STT或播放，不能当完整对话soak。

准备串行共55.969秒，其中Ollama29.859、RVC11.891、Cosy14.156秒。六个13字短句合成+转换wall为4953/4329/4359/4391/4500/4484ms，均捕获音频。此耗时仍有明显等待，不能宣称冷启动或端到端流畅度目标已达成。

| 检查点 | 子诊断树RSS MiB | 子诊断树private MiB | 线程 | 句柄 |
|---|---:|---:|---:|---:|
| 基线 | 103.14 | 1040.76 | 38 | 329 |
| 准备完成 | 7614.26 | 14734.16 | 122 | 2451 |
| 第1句 | 4872.63 | 11037.49 | 120 | 2457 |
| 第2句 | 4844.84 | 11016.29 | 119 | 2458 |
| 第3句 | 4843.89 | 11017.67 | 118 | 2456 |
| 第4句 | 4842.86 | 11014.36 | 116 | 2453 |
| 第5句 | 4842.82 | 11024.76 | 116 | 2453 |
| 第6句 | 4843.48 | 11016.70 | 116 | 2453 |
| 子诊断清理后、进程退出前 | 108.57 | 1045.57 | 36 | 363 |

子树检查点不含作为其兄弟进程的隔离Ollama服务；父监督树包含服务，峰值RSS9373.86MiB，最少物理余量39790.57MiB、提交余量17664.18MiB。不能用子树108MiB冒充全机或完整服务占用。子报告关闭时系统提交余量26998.71MiB；父监督回收隔离服务后另行只读采样，可用物理49092.53MiB、提交总24666.13/限69225.99MiB、提交余量44559.86MiB。自有supervisor/launcher/child三个PID均消失，未检测到Python/Ollama模型进程；没有停止用户服务。原选定音频哈希保持、共享WAV仍826。

退出后单次GPU/内存元数据另保存为tmp/codex_memory_post_exit_20261105o.json：可用物理49236.67MiB、提交余量44704.69MiB、GPU used1121/free14941MiB。只读nvidia-smi通过既有SafetyPolicy执行，不是后台GPU轮询；背景桌面负载仍存在，不能将全部显存占用归于程序。

## 仍未验证/未解决

- 默认30秒首次Cosy冷启动失败仍有效，短句4.3–5.5秒仍有优化空间；本轮诊断没有修改生产配置来隐藏问题。
- 30–60分钟真实语音soak仍按用户既有约定后置；旧系统未归属的大块占用仍需异常当时RAMMap/池标签等数据，不能用本短测给出确定归因。
