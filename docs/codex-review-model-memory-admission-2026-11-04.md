# 模型内存准入：Root最终验收通过

## 最终结论（2026-11-04，以下返修记录均为历史）

最终里程碑补充：其后官方Cursor窄幅移除冗余_require_active与无作用异常转抛（无协议变化），Root逐行审查后独立12及相关88再次通过；资源200次交替请求、25次默认实际类装配关闭与实际低内存拒绝3项通过。详细最终证据及真实短测资源限制见docs/codex-review-performance-milestone-2026-11-04.md，Accepted结论保持。

官方Cursor会话cb942101-e44a-48a2-8250-95b6306b09c0已STOP，无在途修改或测试。Root逐一比对本切片事前快照与最终调用方；默认assembly创建同一guard传入实际OllamaClient和MemoryRuntime→默认Extractor→Transport，每次使用实际sealed endpoint；Core独立close要求严格True，失败保留重试。所有已确认问题已修复，ADR Accepted。

| 独立监督器label | 实际验证 | 结果 |
|---|---|---|
| model_admission_root12_final_20261104d | Root12：低资源/非法采样/冷热整数估算/端点/fatal/超时单owner/关闭/卸载/HTTP边界 | 12通过 |
| model_admission_root_repo_20261104d | 新增仓库模块，审查fixture后运行 | 21通过 |
| model_admission_root_integration3_20261104f | 默认实际类接线（fake Store/HTTP）、装配回滚、已启动但start确认失败 | 3通过 |
| model_admission_root_clients_final_20261104g | 原生成/流/会话历史/system消息契约 | 47通过 |
| model_admission_root_assembly_20261104g | 原Core/装配/rollback/惰性默认工厂 | 73通过 |
| model_admission_root_memory_net_20261104h | 原记忆集成与网络安全Round3B及返修回归 | 65通过 |

以上各组均exit0、未超时、WAV826→826且无增删，测试组存在重叠，不汇总成单次全仓通过。Root独立探针文件位于tmp，报告为tmp/codex_gate_<label>_2026_11_02.json。语法错误、Root曾误用方法/模块名的失败报告保留，不作验收证据。fake测试没有真实Ollama、音频设备或正式记忆读写。

资源边界：每guard最多1个metadata owner，require非阻塞单飞；前台预算min(2秒,调用timeout)，GET connect/read各0.5秒、64KiB响应/512行上限，超时不晚发POST、不再开替代worker；actualquiet后才可重用。估算是保守启动保护，无法预留内存，也不能保证第三方应用并发分配时绝不卡顿。低层库guard=None仍兼容，保护默认应用装配；独立诊断必须显式注入并回收，不能推断全仓裸客户端自动受保护。最终跨模块资源review与真实短测仍另行处理。

## Root复审c：12项中只剩非法采样放行

Root探针曾有一次插入语法错误，model_admission_root12_review_20261104b没有执行，不作证据；已修probe，model_admission_root12_review_20261104c真实执行12项，只有非法snapshot的NaN/Inf三个子例失败。原9项问题已通过，外加驻留每次重查、HTTP redirect/body cap、响应close均通过。

**窄修**：pre/post snapshot的physical/commit必须严格非负int（bool/float/NaN/Inf/缺字段皆不可观测），在任何比较前验证；非法pre零GET/POST、非法post不得许可。可以小纯函数统一验证再读值，复用HostMemorySnapshot不改host_memory模块。不能仅math.isfinite从而接受float，不扩大生产配置。

**repo fixtures尚不运行**：三个blocking_get/hang_get/timeout的release.wait(30)仍忽略返回值；改<=3秒且若False抛AssertionError。_start_require捕获_admission_errors后main不校验等于吞异常：singleflight释放后应errors=[]；close-cancel case应捕获受控ModelMemoryAdmissionError（允许closing/metadata_failed/metadata_timeout），非Exception不能被吞。assert actual callerThread.dead与guard.close严格True都在host fake patch内。_FakeResponse.iter_content目前只yield body[:4096]，body-over64k测试其实只测截断JSON；改按chunk_size遍历整个body，才证明上限。无owner的deny/count guard也可用try/finally close，但不为无资源case加大框架。

其它生产行为冻结，局部compile/diffcheck与handoff STOP，仍禁tests/gate/Rootprobe/监督器/报告修改。Root之后跑12矩阵和新增repo模块、客户端/装配/关闭相关组；真实资源紧张暂不启动模型。

## Root独立9项初审结果与统一返修

报告model_admission_root9_review_20261104a，WAV826零差：低内存零GET/零POST、主/提取器结构化拒绝、冷热和真实endpoint/伪造分类、timeout单owner等已有通过证据；以下实际复现失败需修。

- **严格schema没有落实**：PS models缺失/null仍returnNone然后tags成功便放行。应抛受控metadata失败，不许降为冷载许可；只有明确models=[]或合法非匹配list才走tags。保持name冲突和size_vram不能替size已修逻辑。
- **fatal处理仍仅KBI/Exit**：Owner.error的GeneratorExit/自定义BaseException被包装，改`not isinstance(err, Exception)`原对象raise。start普通RuntimeError应cancel保留owner并映射ModelMemoryAdmissionError，非Exception仍原样传播；不要为了传播fatal把所有普通start错误也裸抛成RESPONSE_ERROR。
- **Core.close只判断truthy**：`if not guard.close(...)`会接受object/MagicMock为完成。必须`is True`；guard.close自己也返回strict实际quiet。Core新字段访问对旧手工__new__ fixture用保守getattr默认，不能破坏原关闭断言/提前声明完成。
- **准入应进入公共私有POST通道**：目前只warm/generate/stream手动_enforce，client._request_post直接可加载模型绕过guard，与ADR统一边界不符。在固定chat请求的_request_post统一调用_enforce一次，移除三处重复调用（错误mapping try范围保留），防止双GET/双2秒等待。keep_alive=0+messages=[]仍豁免，非法bool zero不豁免。memory transport现有正确gate保留。
- **新fixture尚不可靠，不运行repo tests**：blocking_get/hang_get wait返回未校验；daemon lambda/worker未捕获全部BaseException且join后未assertdead，addCleanup guard.close返回未断言、patch先退出后才close。统一finally gate释放→实际join调用线程→guard.close严格True/ownerquiet→退出fake host/网络patch，错误主线程assert，不能用sleep等首请求已启动，改entered事件。metadata_timeout不要sleep3秒，使用有界gate和短call_timeout≤.1、finally release+actualquiet；既不拖测试又能证明迟返。remote case构造时给原explicit允许remote的SafetyPolicy，不绕过reseal。所有guard包括成功/异常/小helpercase都保证close，不碰真实host/GET/POST/DB/设备。

只上述窄源与fixture+局部compile/diffcheck/handoff STOP，不运行tests/gate/Rootprobe，不改报告，不进真实短测。Root之后独立矩阵和相关客户端/assembly/runtime关闭回归。

Root在初稿构建中提前读源，发现跨端点与owner契约偏离，已停止官方CLI(exec10829)避免继续扩散，未运行tests/真实metadata。继续前完整统一以下项，不能仅修一处变绿。

1. **共享guard不能固定使用主聊天endpoint查memory override**：当前require_model_post只收model，_collect_metadata只读self._endpoint；memory.extractor.base_url可为另一个loopback或remote。必须每次接收实际调用方的sealed_endpoint（如require_model_post(..., sealed_endpoint=self._endpoint)），用guard策略重算/校验，再将其作为不可变局部传worker。不能改self._endpoint做可变切换。默认constructor endpoint仅可作兼容fallback；主client与memory transport都显式传实际端点。远端不查本机RAM/metadata；伪造kind先拒绝。相同guard一个owner，不为override偷偷多开worker。
2. **单飞与终态**：当前metadata quiet后至post-memory决策这段无require-active保护，第二require可覆盖owner；需要require级nonblocking单飞flag/lock（覆盖precheck/start/join/postcheck，finally释放），close不等该锁。close后remote/nonWin也不能继续require；所有return允许前检查closing/cancel/前台总deadline。不只把join(timeout=budget)当整个预算，入口计算deadline，join用剩余；预算耗尽不得发POST，即使迟返回allowed。
3. **fatal异常必须原样**：owner.start except BaseException不能一律变ModelMemoryAdmissionError；cancel并保留owner，非Exception同对象raise。actualjoin后复核owner.error：fatal原对象传播，普通异常映射稳定resource failure。普通Exception日志不含URL/原prompt。未知启动不允许新owner覆盖，close严格actualquiet。
4. **严格schema/整数估算**：PS的models缺失/null不能当冷载成功，size非法不能拿size_vram补成resident许可；要求匹配model正int size，若同item model/name冲突不能认其它模型。tags同样shape严格；不使用float(size)估算，按ADR整数ceil，避免巨大合法JSON整数overflow或精度绕过。body限64KiB/rowlimit保留，不缓存驻留许可。
5. **完整集成与关闭**：完成默认assembly把同guard传主client与runtime→extractor→transport，远端override测试；Core独立close+strict True参与nonmemory completion，失败保留重试。旧DI不传guard不增加网络，default实际入口必须有guard，不能仅kwargs fake证明。明确卸载豁免只keep_alive数值0/字符串0且messages=[]，bool不能作为零豁免；非流/流资源拒绝零POST/零history/touched，memory error_kind=resource且不写库。rollback与device fake/guard owner实际退出后restore patch。

原ADR其它所有要求有效。此次仍只源/必要tests/docs+局部compile/diffcheck后STOP，禁止tests/gate/Rootprobe/新监督器/改报告、正式memory/真实模型设备/系统或声音模型配置/提交推送。主机RAM/commit刚采样仅1439/1218MiB，绝不运行真实模型，仅fake验证。不要将源码初稿当完成，先检查最终调用方再交回。
