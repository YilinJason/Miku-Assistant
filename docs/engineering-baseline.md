# 工程基线：现状审计与行为梳理（第一阶段）

> 审计起点：分支 `yilin_dev`。  
> 章节区分：**历史发现**、**路由可测化（第一小步）**、**助手装配分离（第二小步）**。  
> 依据：`AGENTS.md`、`skills/safe-refactoring`、`skills/architecture-contracts`、`skills/engineering-quality`。

**范围说明**：默认启动仍会创建真实引擎（副作用未从生产路径消除）；离线测试通过 `from_dependencies` 绕过。审计起点尚未解决播报取消或端到端流式；后续 6.9–6.10 已渐进补齐应用层取消、增量显示/分句播报与语音轮流说话契约，底层音频硬打断仍未实现。

---

## 1. 当前架构与主要调用链

### 1.1 模块分层（实际依赖方向）

```text
main.py
  └── src/cli/app.py
        └── src/core/assistant.py     # 业务编排
              ├── 默认：assemble_default（config/日志/目录/引擎/清理器）
              ├── 测试：from_dependencies（仅绑定传入组件）
              ├── IntentRouter ← build_intent_router
              │     → System → Search → LLM handlers
              ├── OllamaClient / TTSEngine / STTEngine / …
              └── FileCleaner（默认路径启动 Timer）
```

依赖大体自上而下。例外与耦合点：

| 观察 | 位置 | 说明 |
|------|------|------|
| 装配与编排已拆分 | `assembly.py` + `assistant.py` | 默认路径仍有完整副作用；注入路径无设备初始化 |
| 路由装配可注入 | `router_factory.py` | 只接收已有依赖 |
| 生命周期 | ADR | TTS/清理器/角色由助手 `shutdown`；其余由调用方管理 |
| TTS 兼容导出 | `src/voice/tts.py` | 旧导入路径保留 |
| RVC 进程隔离 | `scripts/rvc_*.py` | 独立 `.venv-rvc` |
| 角色扩展 | `character_interface.py` | 占位 |

### 1.2 文本路径（interactive / text）

```text
input()
  → check_mode_switch / help / tts 开关（仅 CLI）
  → MikuAssistant.process_text_input(text, use_tts=…)
       → IntentRouter.dispatch(text)
            → SystemIntentHandler → WindowsController.check_and_execute
            → SearchIntentHandler → ChromeSearcher
            → LLMIntentHandler → OllamaClient.generate(text)   # stream 默认 False
       → [可选] daemon Thread → TTSEngine.speak(strip_prefix(response))
       → [可选] CharacterInterface.send_message(response)
  → print("Miku: …")
```

退出：`quit`/`exit`/`退出` 跳出循环；`app.main` 的 `finally` 调用 `assistant.shutdown()`  
（停 RVC Worker、停清理 Timer、再 `cleanup_now`、关角色接口）。

### 1.3 语音路径（voice / 嵌套语音）

```text
ensure_voice_ready（STT + 麦克风 + Ollama 连接/模型）
  → VoiceSession.run 循环
       → process_voice_input → STTEngine.listen(continuous=True)
            → recognize_google（需网络）
       → 模式切换 / 停止词
       → process_text_input(..., use_tts=True)  # 同上，TTS 常开
```

### 1.4 TTS → RVC → 播放

```text
TTSEngine.speak
  → sanitize_for_speech
  → _speak_lock（进程内串行）
  → [pipeline] 分句：daemon 预合成线程 + Queue → 主线程顺序 AudioPlayer.play
  → [单段] edge-tts → MP3→WAV(默认 40kHz) → RVC.convert_file
       → run_rvc_infer：Worker 优先，失败回退 rvc_infer 子进程
       → polish_rvc_wav（后处理，可配置关闭）
  → 失败可回退 pyttsx3（无 RVC）
  → TempFileManager 删除临时文件
```

`TTSEngine.__init__` 副作用（构造即发生）：

1. `AudioPlayer` → 可能 `pygame.mixer.init()`  
2. RVC 健康检查；通过且 `worker_enabled` 时 **`warm_rvc_worker` 拉起子进程并加载模型**  
3. 导入/探测 edge-tts 或初始化 pyttsx3  

`MikuAssistant.__init__` 另启动 `FileCleaner.start_auto_cleanup()`（daemon `Timer`）。

### 1.5 流式输出实际覆盖层

| 层级 | 现状（代码核实） |
|------|------------------|
| `OllamaClient.generate(stream=…)` | API 支持 `stream=True` 时按行累加，**仍一次性返回完整字符串** |
| `LLMIntentHandler` | 调用 `generate(text)`，**从不传 `stream=True`** |
| CLI / TTS | 只消费完整 `response` 字符串；**无 token/句级增量回调** |
| 分句流水线 | 是 **TTS 侧** 对完整文本的切分并行合成，不是 LLM 流式 |

结论：不存在端到端「模型增量 → UI/语音」流式契约；文档若写「流式对话」需限定为 Ollama HTTP 能力未接到调用链。

---

## 2. 已有能力及应保留的行为

以下为当前实现中应视为**对外可观察契约**（重构时优先保持，除非单独列为行为修复）：

1. **入口**：支持的启动方式为仓库根目录 **`py main.py` / `python main.py`**（委托 `src.cli.app.main`）。`python -m src.cli.app` **不会**自动进入 CLI（`app.py` 无 `if __name__ == "__main__"`）；本阶段**不**新增模块入口。参数：`--mode text|voice|interactive`、`--config`、`--auto-tts`。  
2. **意图优先级**：系统控制 → 搜索 → LLM 兜底。  
3. **系统控制**：仅关机/重启/锁屏；须 `system_power` +（默认）确认对话框；`MIKU_DRY_RUN` / 无权限 / 显式 `dry_run` 时返回拦截文案，不执行。  
4. **LLM 上下文**：请求 payload = **至多 10 条历史消息**（`ConversationHistory` 请求窗口，计的是 **message 条数**，不是「轮」）**+ 当前用户输入** 一条。一对 user/assistant 通常算 2 条消息；故约等于最多 5 轮历史对话再加当前句。内存存储仍可能继续增长（见 P3；有界存储另案）。仅 `generate_result` 的 `SUCCESS` 非空回复提交一轮；失败/空回复不写半轮。`generate()` 仍返回字符串：失败为以 `错误:` 开头的兼容文案（**不**用此前缀判断结构化成败）。公开属性 `OllamaClient.conversation_history` 为可修改副本（改副本不影响内部）。默认客户端历史相互隔离；显式注入同一 `ConversationHistory` 时共享。旧 Handler 仍只调 `generate()`，错误播报问题尚未接入结构化结果。
5. **TTS**：edge-tts 为主，可 RVC；edge 失败可 pyttsx3；朗读前清理 Markdown 星号等；长文可分句流水线。  
6. **RVC**：独立 venv；Worker 常驻 + 子进程回退；失败时回退播放 edge-tts 音频。  
7. **语音模式**：预检不过则不进入；空识别/`WaitTimeout` 继续循环；停止词与「关闭语音模式」可退。  
8. **退出清理**：`shutdown` 停 Worker、停自动清理并再清一次、关角色接口。  
9. **安全**：系统副作用经 `SafetyPolicy` / `safe_*`；注册表写死禁用。  
10. **兼容路径**：`src.voice.tts.TTSEngine` 仍可用。
11. **路由语义**：handler 返回 `None` = 继续交给下一处理器；返回任意字符串（含 `""`）= 已处理并终止链；链上皆 `None` 时 `dispatch` 返回 `None`。

**明确不是已交付能力**：长期记忆、Quest/3D、`CharacterInterface` 真实通道、端到端 LLM 流式播报、用户取消当前播报/当前生成、延迟初始化以消除构造副作用。
---

## 3. 问题清单

分类：`已确认`（源码直接成立） / `待验证`（需运行或环境） / `设计建议`（非缺陷，方向性）。

| ID | 分类 | 位置 | 触发条件 | 影响 | 证据 | 优先级 |
|----|------|------|----------|------|------|--------|
| P1 | 已确认 | `src/llm/ollama_client.py` `generate`；`llm_handler.py:48` | 任意 LLM 对话 | 无增量事件；CLI/TTS 必须等整段完成 | `generate` 默认 `stream=False`；handler 未传 `stream`；即使 `stream=True` 也聚合后返回 | P0（实时对话） |
| P2 | 已确认 | `assistant.py:222-230` | `use_tts=True` 且连续多轮输入 | 每轮起一个 **daemon** 播报线程；`_speak_lock` 串行但仍排队；无取消、无 turn_id；退出不等待播报结束 | `threading.Thread(..., daemon=True)`；`TTSEngine` 无 cancel API；`shutdown` 只 `stop_rvc_worker` | P0 |
| P3 | 已确认 | `conversation_history.py` 存储 | 长会话 | 请求只用最近 **10 条历史消息** + 当前输入，但内存列表**只增不减** | `commit_turn` 无截断；仅请求窗口截取 | P1 |
| P4 | 已确认→部分缓解 | 助手已按 `DispatchOutcome` 抑制失败/空的 TTS 与角色转发；CLI 仍打印错误串 | Ollama 不可达等 | 失败不再当台词播报/转发；仍可能打印错误提示 | 见 `adr-structured-dispatch.md` | P1 |
| P5 | 已确认→结构化路径已解决 | `recognition.py`、`stt.py`、`assistant.py`、`voice_session.py` | 超时 / 无法识别 / 服务、设备或内部错误 / 取消 | 新路径以 `RecognitionResult` 区分并由会话选择重试或暂停；旧 `listen()` / `process_voice_input()` 仍以空串兼容，信息会丢失 | 见 `adr-stt-recognition-contract.md` 与 `tests/test_stt_contract.py` | 已完成（兼容入口限制保留） |
| P6 | 已确认 | 工程树 | 跑自动化回归 | **无** `tests/`、无 pytest/mypy/ruff/tox/CI 配置 | 仓库检索无匹配；根目录仅有 `diagnose_rvc.py`、`test_rvc_conversion.py` | P0（工程化） |
| P7 | 已确认 | `TTSEngine.__init__` + `assistant.__init__` | 仅构造助手（含 `--mode text`） | 可能初始化 pygame mixer、预热 RVC Worker、启动清理 Timer——文本模式也付语音/GPU 成本 | `warm_rvc_worker`；`AudioPlayer._detect_backend`；`FileCleaner.start_auto_cleanup` | P1 |
| P8 | 已确认 | `voice_session` / `process_text_input` | 语音轮次重叠感知 | 无 `session_id`/`turn_id`；旧轮 TTS 无法丢弃 | 全链路无轮次标识 | P1 |
| P9 | 已确认 | `character_interface.py`；`config` extensions | `enabled: true` | 仅日志，无真实渲染；`_initialized` 在占位路径仍为 True | `_initialize` 对 unity/other 均为「待实现」日志 | P2（文档易误解） |
| P10 | 已确认 | `text_session.py:60` vs `config.yaml` / `assistant.py:220` | 缺省键或读配置失败 | interactive 本地默认 `auto_tts` 回退值为 `False`，而 `process_text_input`/`config.yaml` 为 `True` | `get(..., False)` vs `get(..., True)` | P2 |
| P11 | 已确认 | `ollama_client.py:20` vs `config.yaml` | 未走 Config 直接构造客户端 | 默认模型名 `Miku_Assistant:latest` 与配置 `Miku_Assistant-emotion:latest` 不一致 | 构造函数默认参数 | P2 |
| P12 | 已确认 | `docs/VOICE.md` vs `config.yaml` rvc 节 | 按文档调音质 | 文档未列 `protect` / `rms_mix_rate` / `postprocess_*` / `input_sample_rate` | VOICE.md「配置」节较短；yaml 已有项 | P2 |
| P13 | 待验证 | RVC Worker 关闭 vs 播报中 | 退出时仍有 daemon `speak` | Worker 已停可能导致转换失败或半截音频；临时文件残留 | 生命周期交叉；需实测 | — |
| P14 | 待真机验证→失败策略已缓解 | `recognize_google` | 无外网 / 墙 | 预检仍可能通过，但结构化路径返回 `SERVICE_ERROR` 并暂停语音会话，不再持续空串忙循环 | fake 已验证状态与会话策略；真实 Google 服务未测 | — |
| P15 | 待验证 | `config.yaml` `system_power: true` | 用户说「关机」并确认 | 会真实安排关机（60s）；与代码默认权限 `False` 不同 | yaml 显式 true；本轮**未**触发确认或命令 | — |
| P16 | 设计建议 | `assistant.py` 上帝对象 | 后续加记忆/流式/UI | 装配与会话编排耦合，难单测 | 构造函数职责过多 | — |
| P17 | 设计建议 | 无有界播报队列策略 | 积压时 | 现状为无线程数限制的锁串行；应明确丢弃旧轮或拒绝入队 | architecture-contracts 队列满载策略 | — |

---

## 4. 测试与工程检查盘点

### 4.1 现有入口

| 文件 | 性质 | 依赖 | 副作用 |
|------|------|------|--------|
| `tests/test_intent_router.py` | **离线** unittest | fake/mock | 路由 + Windows 安全边界 |
| `tests/test_assistant_orchestration.py` | **离线** unittest | fake + 可替换装配工厂 | 注入编排 / 默认接线；不启真实设备 |
| `tests/test_assembly_rollback.py` | **离线** unittest | fake 构造器 | 装配失败回滚 |
| `tests/test_conversation_history.py` | **离线** unittest | fake HTTP | 会话历史窗口/提交/隔离；不连真实 Ollama |
| `tests/test_generate_result.py` | **离线** unittest | fake HTTP | 结构化结果分类与 `generate` 兼容层 |
| `tests/test_structured_dispatch.py` | **离线** unittest | fake | Handler/路由/助手消费结构化结果 |
| `tests/test_generate_stream.py` | **离线** unittest | 可控 HTTP 流 | `generate_stream` 增量事件；不连真实 Ollama |
| `tests/test_stream_playback.py` | **离线** unittest | fake 流/队列 | 分句播报、CLI 增量、优先级 |
| `diagnose_rvc.py` | 诊断 CLI | Config、`.venv-rvc` | 非离线门禁 |
| `test_rvc_conversion.py` | 人工试听集成 | 网络/RVC/播放 | 非离线门禁 |

### 4.2 静态检查 / CI

- 仍无 ruff/mypy/flake8/black/pre-commit/GitHub Actions  
- 主环境 / RVC 环境仍无完整锁文件（仅 `requirements*.txt` + setup 脚本）

### 4.3 可离线行为测试（规划 vs 本轮）

| 编号 | 场景 | 本轮 | 说明 |
|------|------|------|------|
| T1 | 系统命中后不调搜索/LLM | **已测** | fake controller/searcher/llm |
| T2 | 搜索命中后不调 LLM | **已测** | |
| T3 | 前两者未命中则调 LLM | **已测** | |
| T3b | `None` 继续 / `""` 已处理终止 | **已测** | |
| T3c | 全未命中 → `None` | **已测** | |
| T3d | 可选依赖缺失 | **已测** | controller/searcher/llm 为 `None` |
| T3e | `build_intent_router` 契约（系统 / **搜索命中** / LLM / 缺失依赖） | **已测** | 搜索断言 query 参数与返回值，且 LLM 未调用 |
| T9–T10 | 权限拒绝 / 显式 dry-run；关机·重启·锁屏 subTest；隔离 `MIKU_DRY_RUN` | **已测** | mock 确认框与 `safe_subprocess_run`；显式 dry-run 先断言 `SYSTEM_POWER` 允许 |
| T4–T5、T11–T13 等 | 错误语义、历史截断、清理、TTS 串行 | **未做** | 超出本轮路由边界 |

**命令（可重复）**：

```powershell
cd D:\Miku_Assistant_LLM
py -3 -m unittest discover -s tests -v
```

---

## 5. 配置 / 依赖 / 文档一致性

| 项 | 结论 |
|----|------|
| 主依赖 `requirements.txt` | 声明 pyyaml/requests/edge-tts/librosa/soundfile/numpy/scipy/SpeechRecognition/pyaudio/pygame；与代码主路径大体一致 |
| 可选 `requirements-optional.txt` | 仅 pyttsx3；与 fallback 一致 |
| RVC | 不在主 requirements；由 `scripts/setup_voice_env.ps1` / `upgrade_rvc_cuda.ps1` —— 与 README/VOICE 一致 |
| `config.yaml` RVC 音质参数 | 与 `vc_params`/`postprocess` 实现一致；**VOICE.md 未同步全量**（P12） |
| `security.permissions.system_power: true` | 与「代码默认 false」不同，属仓库示例配置偏开放；README 已提示须显式开启 |
| README 流式/流水线表述 | 「分句流水线」属 TTS，易被读成 LLM 流式——建议后续改文案对齐 P1 |
| `AGENTS.md` + `skills/` | 工作区未跟踪文件；与 GitHub 已推送内容无关，本审计保留不提交 |

---

## 6. 重构进度

### 6.1 第一小步（路由）— 已完成

| 项 | 说明 |
|----|------|
| `router_factory.py` | `build_intent_router` |
| `tests/test_intent_router.py` | 9 项契约（含安全 subTest） |

### 6.2 第二小步（助手装配分离）— 已完成

| 项 | 说明 |
|----|------|
| `src/core/assembly.py` | `assemble_default` + 可替换构造器 |
| `MikuAssistant.from_dependencies` | 注入路径；缺失组件不自动创建；可选 `logger` 真正绑定到实例 |
| 共用业务方法 | `process_*` / `speak` / `shutdown` |
| `docs/architecture/adr-assistant-assembly.md` | 决策与生命周期 |
| `tests/test_assistant_orchestration.py` | 注入副作用守卫、logger、TTS Thread.start、默认接线 |

### 6.3 第三小步（关闭容错与幂等）— 已完成（**行为修复**）

| 项 | 说明 |
|----|------|
| `MikuAssistant.shutdown` | 顺序不变；单步异常继续；stop 失败跳过 cleanup；整次只跑一轮 |
| 日志语义 | 区分「流程已执行」与「全部释放成功」；失败不写无条件成功 |
| 测试 | 正常/重复、TTS 失败、stop 失败、cleanup 失败、角色失败、多失败不重试、可选缺失、调用方未关 |
| ADR | 已写明变更前/后对照与未解决范围 |

### 6.4 第四小步（装配失败回滚）— 已完成（**行为修复**）

| 项 | 说明 |
|----|------|
| `assembly._RollbackRegistry` | 创建成功即登记；成功 `abandon`；失败逆序回收 |
| 回收范围 | cleaner.stop → character.close → tts.shutdown；无 `cleanup_now` |
| 清理器约定 | 构造失败继续；**已取得对象即登记 stop**（含 `start_cleaner=False`）；启动失败为两阶段：阶段一立即 stop，失败则登记供阶段二致命回滚再试一次 |
| 测试 | `tests/test_assembly_rollback.py`（11 项，含 start_cleaner=False / 多资源失败 / 两阶段 stop） |
| 限制 | 构造器未返回对象时的内部泄漏；LLM/STT/系统/搜索不在回滚清单 |

### 6.5 第五小步（内存会话历史拆分）— 已完成（**保持行为的迁移**）

| 项 | 说明 |
|----|------|
| `src/llm/conversation_history.py` | `ConversationHistory`：存储 + 请求窗口选择；默认窗口 10 条消息 |
| `OllamaClient` | 委托历史；`generate(...)` / `clear_history()` 调用方式不变 |
| 请求窗口 vs 存储 | 窗口只影响 payload；存储本轮不截断（P3 另案） |
| 提交规则 | 成功非空才 `commit_turn`；失败/空回复不写；流式聚合与非流式共用 |
| 快照兼容 | `conversation_history` / `snapshot()` 返回可修改副本，改副本不影响内部；仓库检索未发现可变内部列表依赖 |
| 隔离 | 默认新建实例相互隔离；显式注入同一 `ConversationHistory` 则共享，`clear_history` 影响共享者 |
| 测试 | `tests/test_conversation_history.py`（14 项，含解析失败 / 流中非法 JSON / 流中断连） |
| ADR | `docs/architecture/adr-conversation-history.md` |
| **未做** | 持久化、长期记忆、有界存储、改 Ollama 协议、端到端流式 |

### 6.6 第六小步（结构化生成结果）— 已完成（**新接口 + 兼容层 + 边界补齐**）

| 项 | 说明 |
|----|------|
| `src/llm/generate_result.py` | `GenerateStatus` + `GenerateResult`；`failure` 对空 detail 有非空兜底 |
| `generate_result(...)` | 区分成功、空、连接、超时、HTTP、响应解析/结构/非字符串正文 |
| 异常兜底 | 无参 `Timeout()` / `ConnectionError()` 等用稳定非空 detail，兼容接口不抛错 |
| JSONDecodeError | 在宽泛 `RequestException` 前捕获 → `RESPONSE_ERROR`；兼容串改为 `错误: {detail}`（不再误用连接文案） |
| 流式 message | 存在则必须为 dict；非 dict → `RESPONSE_ERROR`，不提交部分回复；`done` 判定未改 |
| `generate(...)` | 只委托一次再 `to_legacy_string()`；文案见 ADR（**非**笼统「完全不变」） |
| 历史 | 仅 SUCCESS 非空提交；失败/空不改 |
| 测试 | `tests/test_generate_result.py` |
| ADR | `docs/architecture/adr-generate-result.md` |
| **未做** | （第七小步已接入 Handler/路由/助手；见 6.7） |

### 6.7 第七小步（消费结构化生成结果）— 已完成

| 项 | 说明 |
|----|------|
| `DispatchOutcome` | 展示文本 + `allow_tts` / `allow_character` |
| LLM Handler | `handle_outcome` → `generate_result`；`handle` 委托，单次输入单次模型调用 |
| Router | `dispatch_outcome`；`dispatch` 委托 |
| Assistant | 按许可播报/转发；成功含「错误:」台词仍播报；失败/空不播报不转发 |
| 系统/搜索 | 行为与优先级不变，字符串结果仍允许 TTS/角色 |
| 测试 | `tests/test_structured_dispatch.py` |
| ADR | `docs/architecture/adr-structured-dispatch.md` |
| 工作区 | `src/voice/stt.py` 为独立语音修复，**保留且不纳入本轮验收** |

### 6.8 第八小步（Ollama 独立增量输出）— 已完成（**未接入产品链**）

| 项 | 说明 |
|----|------|
| `stream_events.py` | `TEXT_DELTA` / `COMPLETED` / `FAILED`；恰一终结事件 |
| `generate_stream` | `stream=True` 逐行读取；仅 `done:true` 后提交非空一轮 |
| 失败/提前关闭 | 不提交；`finally` 关闭 HTTP 响应 |
| 兼容 | `generate` / `generate_result` 聚合路径未改 |
| 测试 | `tests/test_generate_stream.py` |
| ADR | `docs/architecture/adr-generate-stream.md`（含官方协议字段来源） |
| **后续** | 见 6.9：CLI/助手可选消费流；硬打断仍未做 |

### 6.9 里程碑（增量显示 + 分句播报）— 已完成（含 round-1 修复）

| 项 | 说明 |
|----|------|
| `done` 修正 | 增量路径 `done` 必须为 bool；非法类型 FAILED，旧聚合不变 |
| `sentence_splitter` | 中英文句末（含 `.`）；超长连续切分；残留有上界 |
| `process_text_input_streaming` | 入口拒取消/关闭；`next` 前检查；归属临界区 + CAS（无 fallback invalidate） |
| `DispatchOutcome.stage` | `OK` / `GENERATION_FAILED` / `CANCELLED` / `PLAYBACK_FAILED`；CLI 不据 `allow_tts` 猜失败 |
| `speech_queue` | 背压阻塞；`compare_and_invalidate`；过期 speak 不污染新轮 |
| CLI | `--stream`；播报失败短提示不重复全文 |
| ADR | `adr-stream-playback.md` |
| 测试 | `test_generate_stream` + `test_stream_playback`（B/C 交错、真实客户端播放失败、CLI stage） |
| 最近离线结果 | `unittest discover -s tests -q` → **191 tests / OK**（含持续监听、模型/音频预热与 RVC Worker 生命周期故障覆盖） |

### 6.10 语音会话增量 + 播报完成契约 — 已完成

| 项 | 说明 |
|----|------|
| `wait_for_turn` | 按 turn_id；COMPLETED/FAILED/INVALIDATED/STOPPED/TIMEOUT |
| `wait_for_playback` | 语音流式默认等待播报；文本/交互默认非阻塞 |
| `safe_to_resume_listen` | 串行兼容模式仅 COMPLETED 或无需/从未播报时可再开麦；超时/取消/部分播报失败/流迭代器异常 → `unsafe_pause`；全双工模式不用此字段开关麦克风 |
| 等待取消 | CAS `_release_own_speech` 丢弃未开始句子 |
| `VoiceSession.stream_reply` | `--stream` / 交互 / 配置传入；关闭回归非流式 |
| ADR | `adr-voice-stream-turn-taking.md` |
| 测试 | `tests/test_voice_session.py`（含真实 `run()` Event 边界） |

### 6.11 STT 结构化结果 + Provider 边界 — 已完成

| 项 | 说明 |
|----|------|
| `RecognitionResult` | `SUCCESS` / 静音超时 / 无法识别 / 服务、设备、未就绪、取消、内部错误；非成功不得携带用户正文；`coerce()` 收口破损对象 |
| `RecognitionProvider` | 可注入 Protocol；注入 `STTEngine` 时不初始化默认引擎或设备 |
| 兼容入口 | `listen()` / `process_voice_input()` 保留字符串返回；新 `recognize()` / `recognize_voice_input()` 返回结构化结果 |
| `VoiceSession` | 静音超时静默重试；无法识别节流提示；取消退出；永久/未知错误返回 `stt_error` 并停止监听 |
| 取消边界 | 打开麦克风前与录音完成后检查；不声称硬中断阻塞的底层录音 |
| ADR | `docs/architecture/adr-stt-recognition-contract.md` |
| 测试 | `tests/test_stt_contract.py`（fake provider、假 speech_recognition、coerce/SystemExit/Whisper 清理、连续会话状态序列） |

### 6.12 回复期间持续监听 + 输入门控 — 已完成，待真机调参

| 项 | 说明 |
|----|------|
| 单一麦克风所有者 | `VoiceSession` 主线程持续执行短窗口 STT；回复使用一个可追踪后台任务 |
| 输入门控 | 输出期间过滤助手文本回声、重复语气音和极短转写；显式控制词与有效新问题可通过 |
| 插话 | 取消生成、按轮次失效未播放句子、停止当前播放器；终止活跃 RVC 工作但保留空闲 Worker；旧任务超时未停则暂停会话 |
| TTS 并发 | 中断代际阻止排队旧轮清除新中断；仍在退出的合成线程所用文件不并发删除 |
| 兼容 | `duplex_listening=false` 保留 6.10 串行契约；旧 STT provider 不接收新增的 None 关键字 |
| ADR | `docs/architecture/adr-duplex-listening.md` |
| 测试 | `tests/test_voice_input_gate.py` + `test_voice_session` + `test_rvc_worker_lifecycle` |
| 能力边界 | 当前为 STT 转写后门控，不是声学 AEC/语义 VAD；真实外放环境仍需设备验收 |

### 明确仍未做

- 生产默认路径延迟初始化（构造仍会预热 TTS/RVC 等）  
- 并发 `shutdown` 与第三方阻塞音频后端的硬打断尚未完整验证；RVC Worker 正常/超时回收已有离线覆盖  
- 构造器内部泄漏、LLM/STT 等装配失败时的关闭策略  
- 会话历史有界存储、持久化、长期记忆  
- 声学 AEC、音频/语义 VAD、连续 PCM provider 与播放文字时间对齐  
- 仅字符串客户端的失败语义仍弱  
- DI 框架、pytest 依赖、`python -m src.cli.app` 入口  

### 回退

结构化结果：删除 `generate_result.py` 与 `test_generate_result.py`，恢复仅返回 str 的 `generate`。  
会话历史：将逻辑内联回 `OllamaClient`，删除 `conversation_history.py` 与 `test_conversation_history.py`。  
装配回滚：恢复无 `_RollbackRegistry` 的线性装配，并去掉 `test_assembly_rollback.py`。  
关闭/装配分离回退见 6.2–6.3。

---

## 7. 已验证 / 未验证 / 环境限制

### 已验证（离线，最近一次）

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 191 tests in 4.726s … OK
```

含语音里程碑：播报完成契约、Event 驱动取消、STT 状态映射、provider 注入、持续监听输入门控、有效插话、TTS 中断代际、Ollama 预热/保活、RVC 设备探测去重、首段推理/重采样预热，以及空闲 Worker 插话复用。第二阶段音频性能修改仍仅完成离线契约测试。

### 历史发现仍成立（只读）

生产构造副作用（成功路径仍预热引擎）等历史发现继续按第 3 节标注。STT 结构化路径已解决多状态合并问题；字符串兼容入口仍会丢失失败类别。流式和持续监听产品路径已接入 CLI/助手（见 6.9–6.12），真机未测。

### 真机反馈：响应延迟、资源背压与持续监听 — 离线修复，待复验

| 项 | 当前处理 |
| --- | --- |
| 播报期间监听 | 默认持续短窗口 STT；助手回声、重复语气音和极短内容经转写门控丢弃，有效插话取消旧回复 |
| 感知响应速度 | `assistant.stream_reply` 默认开启；日志增加 Ollama 首段/完成耗时 |
| 首轮模型冷加载 | 装配成功后后台空 chat 预热；`keep_alive` 默认 30 分钟；正式请求与预热重叠时等待同一次加载 |
| 延迟定位 | Ollama 终结响应记录加载/提示词/生成/token 速率；与 TTS/RVC/播放日志可逐段对齐 |
| RVC 重复探测 | Worker 缓存键不再按句调用独立 torch CUDA 探测；设备仍在新 Worker 构造时解析 |
| 首段音频冷启动 | 默认执行一秒不播放的 RVC 测试推理；librosa 重采样预热可选且默认关闭；失败开放 |
| 插话后下一轮 | 只终止活跃 RVC 工作，空闲 Worker 保留；取消代际阻止尚未发送的旧任务 |
| 任务堆积 | 语音路径不再创建不可追踪的后台 TTS 线程；单消费者队列维持有界背压 |
| Worker 卡死 | stdout 真正有界等待、stderr 持续排空、超时后有界终止并关闭管道 |
| Worker 失败风暴 | 默认回退 edge-tts，不逐句冷启动一次性 RVC；诊断时可显式恢复旧回退 |
| 异常退出 | TTS 主动停止 pygame 与 RVC Worker，再等待播报消费者退出；Worker 同时监视主进程 PID，硬退出后自行结束 |

资源取舍见 `docs/architecture/adr-voice-runtime-backpressure.md`，持续监听见 `docs/architecture/adr-duplex-listening.md`，首轮延迟见 `docs/architecture/adr-voice-latency-prewarm.md`。第一阶段真机日志已确认 Ollama 首增量从 46.2 秒降至 2.877 秒；第二阶段音频预热、扬声器回声、GPU 竞争与新的端到端首句延迟尚待复验。

### 仍未验证

- **完整 `main.py` / 默认装配真实失败与真实资源回收**  
- 真实 Ollama、edge-tts、RVC Worker、麦克风、播放、电源确认框  
- 并发关闭、阻塞录音/网络合成的即时硬取消、声学 AEC/语义 VAD、构造器内部泄漏  

### 环境限制

- Python 3.12 + 标准库 unittest；未装新测试依赖；未启动真实语音或系统操作。

---

## 8. 阶段结论

增量显示、分句播报、串行兼容模式、持续监听输入门控与结构化 STT 路径已离线验证；旧字符串识别入口继续兼容。声学 AEC/语义 VAD、真实 Google/Whisper 与真机试听未做。  
**不得**将 191 项 unittest 通过视为第二阶段真实音频链路已验收。既有 RVC Worker 修改与本里程碑一并保留、分开看待。
