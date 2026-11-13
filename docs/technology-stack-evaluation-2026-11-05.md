# 当前技术栈评估与官方资源核对

日期：2026-11-05。目标：Windows个人陪伴助手、中文对话、Nahida音色、少等待、有限RAM/显存与较小权限。依据当前代码/配置、既有真实测量，以及本日访问的官方仓库、模型卡和文档；没有安装、下载模型、变更配置或运行新模型比较。

## 总体判断

当前技术栈适合一期工程交付，但不能称每一项都是最优解。主体架构值得保留；更明显的改进空间集中在语音采集/识别、音频分块交付、双工处理，以及记忆的事实更新与召回。市场上能力更强的模型并不自动适合Windows单用户、16GiB显存或既有Nahida权重。

本文“建议保留/优先验证”是工程判断，尚未做候选本机A/B。官方测试、模型参数量、理论流式能力，均不等于本机整条链路表现。

## 实测基线与代码事实

- 已有[最终语音链路验收](codex-review-final-voice-soak-milestone-2026-11-05.md)：约63GiB RAM/16GiB显存；1800秒观察（28分钟轮次+2分钟空闲），112轮、114段音频；capture-only，不含麦克风/扬声器及正式记忆库。
- 首音频就绪中位6094ms，p95 7188ms；受控进程树RSS峰值10448.66MiB。不是说出最后一个字到声卡出声的端到端延迟。
- 重新统计[同一子报告](../data/test/voice-naturalness/cosyvoice-production-soak-30/1729765c168d/report.json)：114段Cosy合成中位4437.5ms、p95 5531ms；114次RVC中位992.5ms、p95 1078ms。不同阶段中位数不能直接相加作精确分解，但足以支持先调查语音准备链路。
- [worker_output.py](../src/voice/cosyvoice/worker_output.py)调用`inference_instruct(..., stream=False)`，再完整收集、写WAV。[TTS预取](../src/voice/tts/turn_stream_prefetch.py)已经存在；它是句/段之间的预取，不是模型PCM逐块传递。
- [STT](../src/voice/stt.py)默认Google识别，停止判断使用能量/停顿；Whisper可选路径直接加载`base`。[语音会话](../src/cli/voice_session.py)有转写后过滤和取消，没有声学AEC。
- 正式数据库未读取；关于记忆的结论来自[实现](../src/memory/)，不是对用户当前库内容的审计。

## 分功能技术选型矩阵

“保留”表示适合当前规模；“验证”表示应先隔离比较；“完善”通常是现有设计的数据或协议改进，不意味着更换整套库。

| 功能 | 当前栈 | 评估 | 建议 |
| --- | --- | --- | --- |
| CLI/配置 | Python3.12、argparse、PyYAML | 保留 | 单用户编排适用；GUI另作产品需求，不为语音延迟重写语言 |
| LLM推理服务 | Ollama HTTP、requests、流式NDJSON | 保留 | 优先测模型大小、上下文、驻留与并发；无需先换服务框架 |
| 对话模型/Modelfile | 自定义Miku模型，模板v1基于Gemma3 12B | 验证 | 保留基线，隔离对比4B/9B量化候选；模板不等于当前安装模型的完整血缘证明 |
| 短会话历史 | 完整轮裁剪、进程内有界存储 | 保留 | 再按请求token预算选择上下文；500轮存储不应全部塞入每次请求 |
| 长期记忆存储 | SQLite、sqlite3、事务/RLock | 保留 | 适合本地个人应用；无需另起PostgreSQL/向量数据库服务 |
| 记忆写入可信性 | 本地UserFactUnit、模型只选unit_id、本地render | 保留并完善 | 扩充有验证的自然表达；不能以模型自由概括替代可信边界 |
| 记忆更新/冲突 | 文本事实、去重/时间字段、词法规则 | 完善 | 显式关系/对象类别、变更时间、证据与失效规则，区分饮品和冰淇淋偏好 |
| 记忆召回 | 确定性中文字符/双字词法特征、候选预算 | 完善 | 先结构化过滤与词法回退；再评估CPU轻量embedding辅助召回 |
| SQLite保留维护 | 空闲prune、有界批次、保护确认记录 | 保留 | 增加可观测容量/备份策略；保留目标不是文件体积硬上限 |
| 默认STT | SpeechRecognition/Google、PyAudio | 优先验证替代 | 为隐私、中文与弱网目标测SenseVoice INT8/ONNX；云识别保留显式选项 |
| 本地STT可选 | 主进程原版Whisper base | 验证替代实现 | 若用Whisper，比较faster-whisper INT8或whisper.cpp；不可不经预算再加GPU模型 |
| 语音端点/插话 | 停顿阈值、转写后回声/低信息门控 | 优先完善 | VAD、播放参考、插话确认/回退；VAD与AEC是不同功能 |
| 默认TTS | edge-tts、句级韵律 | 保留为低本地负担路径 | 需联网且发送朗读文本；不作为最高自然度/完全离线承诺 |
| 离线应急TTS | pyttsx3 | 保留 | 可用优先的后备；若要更自然CPU语音，再比较小型本地TTS |
| 本地表达TTS | CosyVoice300M-Instruct、固定warm_companion | 优先验证 | 先隔离测现pin的流式能力，再比较CosyVoice3；不直接覆盖生产环境 |
| Nahida音色 | RVC、fairseq/HuBERT/RMVPE、FAISS | 有条件保留 | 保住已选音色；比较RVC前后质量和直接克隆候选，不能凭新模型名称宣布替代成功 |
| 音频DSP | NumPy/SciPy/librosa/soundfile | 保留 | 精确定位重采样、削波与伪影；高通不能普遍修复模型生成的电流感 |
| 音频播放 | pygame文件播放 | 当前可用、流式阶段验证替代 | 若做PCM流和声学AEC，比较PortAudio/sounddevice等统一音频层 |
| 陪伴风格 | 固定人格、确定性SpeechProjector、固定语音风格 | 保留并完善 | 模型/措辞/韵律共同评估；限定风格表与句长，不另挂一个情绪大模型 |
| 资源/生命周期 | 线程owner、Native Job、准入、IO租约、有界预取/队列 | 保留 | 新后端继续履行这些契约；不能只替换API而漏掉退出/晚到结果/显存预算 |
| 日志 | stdlib logging、RotatingFileHandler、隐私过滤 | 保留 | 现规模无需ELK；只在日志阻塞有测量依据时引入有界异步写入 |
| 定期文件清理 | scandir跨批游标、TTL、单删除worker | 保留并完善 | 给自有嵌套评估目录增加清单/容量预算，避免递归误删未知文件 |
| 安全与权限 | SafetyPolicy、具名能力、路径/端点检查 | 保留并补边界 | 应用层授权不能代替OS隔离；补模型与依赖来源、RVC子环境最小化 |
| 搜索 | URL构造、受控浏览器打开 | 按现功能保留 | 若未来要助手阅读网页，再单独设计搜索/抓取服务及不可信内容边界 |
| 系统控制 | 固定意图、Win32确认、默认禁用 | 保留 | 不扩为任意Shell或自主桌面执行器 |
| 诊断/测试 | unittest/mock、隔离探针、AST、资源长测 | 保留并扩语音评价集 | 增加真实声卡、CER、打断及盲听指标；不以测试数量代表全部体验 |
| 角色/唤醒词 | CharacterInterface占位，wake_word配置 | 尚未形成实现选型 | 后续独立验收；Quest3/Unity/Live2D不是既定一期技术决策 |

## 官方资源与关键判断

### 1. LLM：保留Ollama，模型大小与预算先于换框架

[Ollama FAQ](https://docs.ollama.com/faq)说明并发请求会放大上下文内存占用，并提供驻留、Flash Attention及KV缓存量化控制。建议继续限制单用户前台与后台提取竞争，先测合适上下文和量化，不同时放开多模型/多请求。服务级参数会影响同机其他客户端，当前评估没有修改它们。

[llama.cpp](https://github.com/ggml-org/llama.cpp)提供量化、CPU/GPU混合及多硬件后端，可作为将来精细部署候选；这不能证明同一模型搬离Ollama就一定更快。[vLLM官方GPU安装要求](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/)仍以Linux为主，Windows需要WSL或社区路线；对当前单用户Windows应用，不建议为追求服务框架名称增加这一层。

[Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B)与[9B模型卡](https://huggingface.co/Qwen/Qwen3.5-9B)是可比较的较小候选，不是已证实胜过当前Miku人格/中文陪伴模型。模型卡区分思考与直接回答；[Ollama thinking接口](https://docs.ollama.com/capabilities/thinking)也区分thinking与content。若换模型，应确认实际服务版本、聊天模板和思考开关，避免思考等待进入闲聊路径，或把思考字段朗读。不能直接沿用旧Modelfile断言兼容。

### 2. STT：优先验证CPU本地中文识别

[sherpa-onnx的SenseVoice文档](https://k2-fsa.github.io/sherpa/onnx/sense-voice/index.html)列出Windows及中文/粤语等语言支持和INT8模型。[麦克风示例](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)明确有VAD加模拟流式示例；不能把离线模型分块调用称为原生流式识别。建议首选评估CPU、1–2线程的固定离线ONNX模型，给LLM/TTS保留GPU空间。中文正确率和名称识别需用用户场景验证。

如果保留Whisper，官方[faster-whisper](https://github.com/SYSTRAN/faster-whisper)使用CTranslate2并支持INT8；[whisper.cpp](https://github.com/ggml-org/whisper.cpp)是C/C++实现并有麦克风示例。前者的GPU运行有CUDA/cuDNN依赖，后者仍需选择模型/量化/分段，均不保证所有句子更快、更准。不要拿长音频批量吞吐宣传值推导单句交互延迟，也不要直接混装到Cosy/RVC环境。

### 3. 双工与播放：VAD/AEC/音频时钟比转写相似度更底层

[Silero VAD](https://github.com/snakers4/silero-vad)提供ONNX可用的语音活动检测；适合评估CPU端点判断，但它不能识别是否是用户说话，也不能单独消除扬声器回声。

[WebRTC APM](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/g3doc/audio_processing_module.md)包含AEC/降噪等；其[接口](https://webrtc.googlesource.com/src/+/refs/heads/main/api/audio/audio_processing.h)要求采集与播放参考通路及延迟信息。当前pygame文件播放加独立麦克风采集没有这套统一的PCM参考通道，因此AEC不是装一个Python包就完成。使用耳机可以先完成基本设备验收；外放双讲需要单独集成和真机测试。

[sounddevice/PortAudio stream文档](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html)适合设计统一输入输出与有界PCM缓冲。声卡callback只搬运已准备帧；推理、文件访问、阻塞锁与大量内存分配不能放进实时callback。否则新播放库同样可能卡音。

### 4. TTS：当前选择可用，整段交付方式是重要限制

[CosyVoice官方仓库](https://github.com/QwenAudio/CosyVoice)已有CosyVoice3及双向流式能力，值得隔离评估。当前固定300M-Instruct与新模型的接口、资产、环境、资源需求均可能不同；禁止把vendor更新当成一行替换。先测试当前pin在stream=True时能否较早给出可用音频；只把flag改True、后面仍完整collect写WAV，不会消除整段等待。

[Qwen3-TTS官方仓库](https://github.com/QwenLM/Qwen3-TTS)提供0.6B/1.7B不同模型，区分Base克隆、CustomVoice和VoiceDesign。不能把0.6B克隆能力与1.7B指令控制看作同一功能；模型宣称流式也不表示选定的本地SDK/部署方式已经逐块输出。它是候选，不是当前推荐直接默认切换的结论。

当前固定warm_companion也会限制语气变化；更换模型后若仍每句话同一风格，体验未必明显改善。可先设计小规模固定风格/句长策略，但要有音色盲听与失真对照，保留用户选定参考。

### 5. RVC：音色目标成立时仍有价值

[RVC官方项目](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)的用途是音色转换。现有Nahida .pth/.index不能直接变成新TTS的克隆权重。直接克隆可能少一阶段，也可能丢失相似度或复现参考音频伪影；是否能停用RVC必须由试听和资源比较决定。

建议比较：当前TTS+RVC、相同TTS不经RVC、新候选直接克隆、必要时新候选+RVC。源音、转换音都留在独占有界目录，检查失真/削波/接缝。RVC分块需要上下文与接缝处理，不能简单把每100ms写文件推理就宣称真正实时。

[edge-tts](https://github.com/rany2/edge-tts)仍适合本地负担较低、允许联网的路径；其在线服务依赖应保留显式权限与失败后备。[Piper](https://github.com/OHF-Voice/piper1-gpl)或[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)可作为低配本地音色后备候选，但要核验具体中文音色、Windows部署及许可；它们并不天然满足Nahida相似度。此处不据参数量承诺实际RAM峰值。

### 6. 记忆：SQLite合适，先提升事实语义再讨论向量库

[SQLite官方适用场景](https://sqlite.org/whentouse.html)包括设备与桌面应用本地存储。对当前个人数据规模，保留事务、现有证据边界和单文件部署更合适。

设计建议是让事实具有可比较的主体/关系/对象类别/值/肯否定/证据/有效时间，而不只是相近文本。明确“现在改为喜欢香草冰淇淋”与“喜欢抹茶饮品”可以共存；同槽位偏好变更应按证据和时间规则处理，不能靠embedding相似度决定删哪条。

[FTS5](https://www.sqlite.org/fts5.html)可提供全文检索，但其trigram对少于3字符的全文查询有限制，中文单字/双字不能盲目依赖；当前字符/双字匹配应有保留或替代测试。[BGE small中文模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)可作为CPU语义召回候选，不是错误写入的补救。可选[sqlite-vec](https://github.com/asg017/sqlite-vec)便于仍留在SQLite，但官方标注pre-v1，需锁版本和迁移测试；小规模也可先比较有界向量扫描，未必需要扩展或独立数据库服务。

### 7. 安全：当前控制有价值，但信任边界仍需补强

[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)适合进程组管理和资源控制；[AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)才提供更明确的权限隔离机制。不是有Job就能限制第三方worker读取所有用户文件/联网。AppContainer/受限令牌与CUDA、模型目录、IPC的兼容性需另做试验，不能承诺套上即用。

代码确认：`scripts/rvc_worker.py`与`rvc_infer.py`为fairseq兼容，给torch.load设置weights_only=False默认值。[PyTorch安全说明](https://github.com/pytorch/pytorch/blob/main/SECURITY.md)与[torch.load文档](https://docs.pytorch.org/docs/stable/generated/torch.load.html)要求信任模型来源。这个兼容路径意味着可信权重前提，不能因为文件扩展名是.pth或脚本路径固定就当作安全数据。它不是本轮发现已被利用或远程入侵的证据。

另外，`SafetyPolicy.spawn_rvc_worker`默认以父环境为基础，CPU预算helper复制后覆盖BLAS变量；Cosy已有更严格的worker_env白名单。建议给RVC补同类环境最小化、权重来源/哈希清单、依赖版本锁与离线可复现准备。不应未经兼容试验把weights_only改True，也不能把算出未知文件的hash当作其来源可信的证明。

### 8. 工程底座：继续使用已经验证的小组件

[Python3.12 logging文档](https://docs.python.org/3.12/library/logging.handlers.html)提供日志轮转；当前使用标准库加隐私过滤适合本项目。自有文件清单/配额/备份比扩大递归删除范围更重要。磁盘占用、RAM、显存是不同指标。

Python/requests/标准线程配合独立推理进程适合当前单用户应用；迁移C++、全量asyncio或引入多智能体编排框架不会自动减少模型计算、提升音质或补出OS权限隔离。GUI、网页阅读、Quest3等需要独立需求与验收，不为“技术更新”加入本期热路径。

## 推荐验证顺序与通过门槛

| 顺序 | 切片 | 比较与准入条件 |
| --- | --- | --- |
| 0 | 保留当前基线；记录资产与依赖信任清单 | 不覆盖选定音频、不改正式库、不放宽资源门槛；RVC环境/权重边界作定点任务 |
| 1 | CPU中文ASR+VAD隔离探针 | 同一批中文/专有名词/安静与噪声音频；CER、句末到最终文本p50/p95、误打断、CPU/RAM；不抢现有GPU |
| 2 | 原pin流式能力与新TTS候选小样比较 | 至少30条长短句与六种语气；当前TTS/RVC基线一致；记录首有效PCM、RTF、音色相似度与伪影盲听 |
| 3 | 流式音频协议/播放器与AEC切片 | 有界缓冲/背压、轮次ID、晚到帧丢弃、取消与设备丢失、真实线程/进程退出；模型不在音频callback运行 |
| 4 | 记忆槽位/变更/召回对比 | 用隔离库覆盖用户陈述、引用、问题、否定、改口与跨会话；召回率、错主体率、错误记忆率与耗时；完整保留原出处契约 |
| 5 | 较小LLM与硬件档位 | 相同任务/上下文/输出长度下测质量、首token与首音、驻留/峰值；小模型变快不代表陪伴质量合格 |

候选实验只作为后续任务建议，当前没有替换实现。所有候选仍须依次经Cursor实施、Codex评审、离线边界门禁，再实际设备/模型验证。声卡、外放AEC、中文识别准确率与Nahida主观听感尚无本轮新对比结论。

## 判断最优应采用的标准

同时比较：中文准确率/记忆正确率、Nahida相似度、自然度、用户停话到首声p50/p95、RTF与断音、RAM/commit/显存峰值及退出、低配可用性、离线数据边界、依赖与模型信任、Windows部署/维护成本。只要没有在这一共同约束下获胜，就只能称候选，不称本项目最优。
