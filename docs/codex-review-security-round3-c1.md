# Codex Review：安全硬化 Round 3C1

> 最终结论：**通过。** F1–F13 已全部闭环；下文保留各轮发现与复验依据。

## Finding 1 — P0：现有硬链接可把日志写入 `logs/` 外部文件

- 位置：`src/core/log_privacy.py:122-163`
- `resolve_log_file_path()` 只拒绝 symlink/junction/目录/非普通文件。硬链接仍被 `is_file()` 视为普通文件。
- 独立复现：在临时目录把 `logs/miku.log` 硬链接到外部 `outside.sqlite3`，当前实现接受该路径；写一条日志后外部文件出现 `CORRUPT`，`st_nlink=2`。
- 影响：与本轮要阻止的“日志误写记忆数据库”属于同一破坏面；攻击者或错误准备的文件可绕过直接子级检查。

要求：

1. 现有目标必须拒绝多硬链接身份（至少 `st_nlink != 1` fail closed）。
2. 文件 handler 每次打开/轮转重新验证目标：拒绝 reparse/link/目录/非普通/多硬链接，并在打开后比较句柄与当前路径的文件身份，避免验证后替换。
3. 测试只用临时普通文件，不触碰 `data/memory`；证明外部 sentinel 保持字节不变、零越界写入。

## Finding 2 — P1：Windows 安全文件名校验不完整，且日志根目录本身未校验

- 位置：`src/core/log_privacy.py:46-78, 122-163`
- 当前接受 `bad?.log`、`bad*.log`、`bad|.log`、`bad<.log`、`bad>.log`、`bad".log`、控制字符、`COM1.foo.log`、`NUL.foo.log`。
- Windows 设备名按第一个点之前的组件判定；`Path.stem` 只去掉最后一个扩展名，因此多扩展名形式可绕过。
- `logs_dir.resolve()` 会跟随根目录 symlink/junction；若 `LOGS_DIR` 本身被重定向到外部，后续 `resolved.parent == logs_root` 仍会成立。

要求：

1. 拒绝 Windows 非法字符 `<>:"/\\|?*`、U+0000..U+001F 控制字符，以及任何首组件为保留设备名的多扩展形式。
2. `LOGS_DIR` 本身必须存在且是普通目录，不得为 symlink/junction/reparse point；验证时不得先把它 resolve 掉从而丢失链接身份。
3. 增加表驱动测试与日志根 symlink/junction 测试；平台不能创建某一类链接时可跳过该专属用例，但实现不能弱化。

## Finding 3 — P1：重复初始化会静默保留旧的弱隐私策略

- 位置：`src/core/logging_setup.py:73-92, 114-133`
- `_has_handler_for_file()` 只按路径判断；相同文件已有 handler 时，新配置完全不生效。
- 独立复现：先 `include_tracebacks=True`，再对同一路径调用 `include_tracebacks=False`，最终唯一 handler 的 `include_tracebacks` 仍为 `True`，但日志却宣称本次配置为 `False`。
- 任意普通 `RotatingFileHandler` 也会被视为合格，绕过 `PrivacyRotatingFileHandler` 的 traceback 边界。

要求：

1. 只有相同路径、相同安全 handler 类型且有效安全参数一致时才可直接复用。
2. 重复调用配置变化时，要么原子更新 `maxBytes`、`backupCount`、`include_tracebacks`，要么 fail closed；不得打印与实际 handler 不一致的状态。
3. 同路径已有非隐私 handler 时拒绝或安全替换并关闭旧句柄，不得静默接受。
4. 增加 `true → false`、容量变化、普通 `RotatingFileHandler` 占位的测试，并确认无句柄泄漏。

## Finding 4 — P1：已点名模块之外仍有直接绝对路径和原始异常落盘

本轮任务的不变量是持久日志正常/错误路径不含绝对路径、远端异常正文、音频/模型/解释器路径，而不只限于已有 sentinel 用例。当前至少还有：

- `src/core/assembly.py:310-319`：`runtime_layout_summary()` 和 `log_file` 直接记录绝对目录/文件；`safety_policy.summary()` 也需确认不含路径。
- `src/voice/tts/engine.py:142`：仍把含 model/index/python 路径的 `RVCHealthReport.summary()` 原样记录。
- `src/voice/audio/player.py:71`：记录完整音频路径。
- `src/core/file_cleaner.py:317,395,406-410,436-440,452`：目录/文件绝对路径与原始异常。
- `src/core/assistant.py:370,544,612,761-763,1172-1185,1364-1376,1443,1479-1484`：原始异常或包含原始异常的 `errors` 列表。
- `src/voice/stt.py:125-159,297,362,454-456`、`src/voice/tts/engine.py:153,189-224,683-690`、`src/core/speech_queue.py:331`：原始异常字符串；其中云服务异常可包含 URL、设备/文件路径或远端正文。

要求：

1. 审计上述路径并使用安全元数据（exception type/status、stage、count、engine、是否存在资源）；不记录绝对路径和 `str(exc)`。
2. 保持 CLI/用户可见错误正文契约，不要为了日志整改吞掉返回信息。
3. 新增集成 sentinel：至少覆盖装配启动、RVC health 经 TTS、AudioPlayer、FileCleaner、Assistant 懒加载/关闭、STT/TTS 远端异常；格式化后的持久日志中 sentinel 零出现。

## Finding 5 — P2：`PersistentLogPrivacyFilter` 是未使用的空实现

- 位置：`src/core/log_privacy.py:183-196`
- 类名/文档声称过滤 traceback，但 `filter()` 恒返回 `True`，且没有挂到 handler；实际剥离只在自定义 handler 中完成。

要求：删除这段误导性死代码，或让它承担明确且经过测试的职责。不要保留名义上的“隐私 filter”。

## 复验门槛

- 以上 F1–F5 全部闭环，新增回归测试。
- 3C1 专项、Round 3A/3B 相关、全量 unittest、`compileall`、`git diff --check` 全部真实通过。
- 不启动真实主程序、网络、浏览器、音频设备或 RVC；不读取/删除 `data/memory`；不进入 3C2/自然陪伴；不提交、不推送。

## Round 2 复验（F1–F5 返修后）

> 结论：**仍未通过。** 原 F1–F5 的主要场景已修复，专项 35 项测试通过；但独立攻击复现确认日志根目录在 handler 已建立后仍可被替换，且相邻错误路径仍有原始异常正文落盘。

### Finding 6 — P0：handler 重开时未复验日志根，junction 替换可再次越界写入

- 位置：`src/core/log_privacy.py:234-303`
- `_open()` 只验证 `baseFilename` 目标本身；没有在每次打开/轮转前后重新验证父级日志根的 reparse/junction 身份，也没有绑定初始化时的可信目录身份。
- 独立复现：在临时目录创建 handler 后关闭其 stream，把原 `logs/` 重命名，再将同名 `logs/` 建为指向外部目录的 junction；随后 `emit()` 未抛错，外部目录创建 `miku.log` 且含 `BOUNDARY_ESCAPE`。
- 这不是理论竞态：当前 Windows 环境可稳定复现，现有 35 项专项测试未覆盖 handler 建立后的父目录替换。

要求：

1. handler 初始化时记录已验证的日志根规范路径与目录身份；每次 `_open()` / 轮转重开前、打开后都复验父目录不是 symlink/junction/reparse，并与初始化身份一致。
2. 打开后继续比对目标路径、文件句柄身份及 `st_nlink == 1`；任一变化均关闭句柄并 fail closed。
3. 新增 Windows junction 回归测试，完整执行“初始化 handler → 关闭 stream → 替换父目录 → emit/重开”，断言抛错、外部目录零文件/零字节改变。平台无法创建 junction 时才允许跳过。
4. ADR 不得在测试通过前声称“每次打开/轮转重新验证根目录”。

### Finding 7 — P1：仍有原始异常正文进入持久日志

- `src/core/assembly.py:95-100`：回滚失败直接把 `exc` 作为 `%s` 参数记录；异常可含模型、解释器或临时文件绝对路径。
- `src/cli/app.py:100-101`：顶层异常直接 `logging.error("程序错误: %s", exc, exc_info=True)`；根 logger 的文件 handler 会收到原始异常正文。控制台 `print()` 可保留用户可见正文，但持久日志调用点必须只记录 `exception_log_label(exc)`。
- 当前装配 sentinel 测试只覆盖启动摘要，没有触发 `_RollbackRegistry.rollback()` 的失败路径；STT/TTS 远端异常测试只是直接调用 logger，未执行真实 STT/TTS 错误分支，因此不能证明调用点已收口。

要求：

1. 修复上述两个确定泄露点，并以真实调用路径 sentinel 测试覆盖；持久 handler 默认输出不得含 sentinel/绝对路径，控制台或用户可见返回仍保留原契约。
2. 将 `test_stt_tts_remote_exc_omit_url_body` 改为注入并执行 STT 与 TTS 的实际失败分支，禁止用测试代码自己调用安全 logger 来代替生产路径验证。
3. 再审计本轮修改相邻错误日志；`type(exc).__name__` 或 `exception_log_label(exc)` 可保留，直接 `%s` 传异常对象/`str(exc)` 不可落盘。

## Round 2 复验门槛

- F6–F7 修复及真实回归测试通过；原 F1–F5 不回归。
- 3C1 专项、3A/3B 相关、全量 unittest、`compileall`、`git diff --check` 重新真实通过。
- 仍不得进入 3C2/自然陪伴，不启动真实服务/设备，不读取或删除 `data/memory`，不提交、不推送。

## Round 3 复验（F6–F7 返修后）

> 结论：**仍未通过。** F6 的日志根替换攻击已经 fail closed，F7 的确定泄露点也已收口；独立探针继续发现容量上限、运行时重配置和错误分类存在缺口。以下 F8–F11 是 C1 的最终收口清单，修复时应一次覆盖同类状态转换。

### Finding 8 — P1：普通业务 `ValueError` 被误判为安全边界失败并冒泡

- 位置：`src/core/log_privacy.py` 的 `PrivacyRotatingFileHandler.emit()`。
- 当前实现手写了标准 handler 的 emit 流程，并对所有 `ValueError` 执行 fail closed、关闭 stream、重新抛出。格式化消息对象的 `__str__()`、自定义 formatter 或普通应用代码同样可能抛 `ValueError`，这不等于日志目录身份被篡改。
- 独立探针：消息对象 `__str__()` 抛 `ValueError("application formatting failure")` 时，当前输出 `application-valueerror-propagated True`、`stream-closed-after-application-error True`。一条坏日志可能中断主程序并让后续日志丢失。

要求：

1. 为日志路径/身份/reparse/硬链接等安全边界失败定义专用异常（例如 `LogBoundaryError(ValueError)`）；只允许该异常 fail closed、关闭流并向调用方抛出。
2. 普通格式化、编码或写入错误保持 Python logging 契约：交给 `handleError()`，不得从 `logger.info/error()` 冒泡，也不得仅因应用格式化错误永久关闭安全 handler。
3. 回归测试同时证明：业务 `ValueError` 不冒泡且 handler 后续可写；F6 日志根替换仍必须抛专用边界异常、关闭句柄并保持外部零文件/零修改。

### Finding 9 — P0：两个已接受配置会把轮转上限静默关闭

- 位置：`src/core/log_privacy.py`、`src/core/logging_setup.py`。
- `RotatingFileHandler(maxBytes=64, backupCount=0)` 不会截断主文件。独立探针写入 20 条后得到 `backup-zero-file-bytes 1000`、`backup-zero-bounded False`。
- `validate_max_size_mb()` 接受 `1e-12`，随后 `int(value * 1024 * 1024)` 得到 `0`；探针输出 `tiny-maxBytes 0`。`maxBytes=0` 禁用轮转。
- 这两种配置都通过当前验证，却破坏本项目明确要求的本地存储有界不变量。

要求：

1. 保持已定义的 `backup_count=0..10` 契约：当为 0 时实现安全的单文件有界轮转（超过阈值后在同一路径安全截断/重开），不得无限追加，也不得创建 `.1`。轮转重开继续执行所有根目录、路径、句柄身份检查。
2. 正数 MB 到字节的换算不得得到 0；使用上取整并保证至少 1 字节，或明确设置可解释的最小配置并同步配置验证/文档。不得把已接受配置转换成“禁用轮转”。
3. 加入确定性小容量测试：`backup_count=0` 长写入后仅一个文件且大小保持在“上限 + 单条最大日志记录”的软界内；极小正值的运行态 `maxBytes > 0`。

### Finding 10 — P1：运行时关闭或更名会遗留应用自有 handler 和打开句柄

- 位置：`src/core/logging_setup.py`。
- `file_enabled=True → False` 后探针输出 `handlers-after-disable 1`；Windows 随后无法删除临时日志目录，报 `PermissionError [WinError 32]`，证明句柄仍打开。
- `file_name=a.log → b.log` 后探针输出 `handlers-after-rename ['a.log', 'b.log']`；旧文件仍继续接收日志，造成双写、隐私策略漂移和容量预算翻倍。
- 同一路径参数直接赋值时也未持有 handler lock；若未来运行时重配与日志写入并发，可能观察到半更新状态。

要求：

1. 明确标记由 `setup_app_logging()` 创建/接管的应用文件 handler；关闭文件日志时从 root 移除并关闭全部应用自有文件 handler。
2. 文件名变化时原子切换：新 handler 完整创建成功后移除并关闭旧的应用自有 handler；失败时不得破坏原来仍可用的 handler。保留不属于本应用的无关第三方 handler。
3. 同一路径更新 `maxBytes`、`backupCount`、`include_tracebacks` 与 formatter 时持有 handler lock，并保证调用者只观察到完整配置。
4. 新增 true→false、a→b、失败回滚、并发/锁契约测试；断言没有重复 handler、没有遗留打开流、临时目录可删除。

### Finding 11 — P1：现有日志文件打开后没有比对“当前路径”与已打开句柄

- 位置：`src/core/log_privacy.py` 的 `_open()`。
- 对已有文件，代码只比较“打开前 stat”与 fd；对新文件才在打开后重新 stat 路径。若路径在打开后被替换，旧句柄仍可通过比较，而当前路径已指向另一个对象。这没有完整满足 F1 的“打开句柄与当前路径身份一致”不变量。

要求：

1. 无论文件打开前是否存在，打开后都重新执行完整的当前路径安全校验，并比较当前路径身份、打开前身份（如有）和 fd 身份；任一不一致即关闭句柄并抛专用边界异常。
2. 加入可控 mock/同步点竞态测试，覆盖已有目标在 `open` 前后被替换；不依赖概率竞态，不触碰真实运行数据。

## Round 3 最终复验门槛

- F8–F11 修复且 F1–F7 不回归；新增测试验证行为而非只检查源码字符串。
- 3C1 专项、Round 3A/3B 相关、全量 unittest、`compileall`、`git diff --check` 全部真实通过。
- Cursor 完成后停下等待 Codex 独立复验；不得进入 3C2/自然陪伴，不启动真实服务/设备，不读取或删除 `data/memory`，不提交、不推送。

## Round 4 复验（F8–F11 返修后）

> 结论：**仍未通过。** F8、F10、F11 及 F9 的表面容量行为已达到预期，但 F9 的单文件截断存在可稳定复现的 TOCTOU，边界异常发生前已经破坏外部文件。

### Finding 12 — P0：`backup_count=0` 的路径截断可在 fail closed 前破坏外部硬链接目标

- 位置：`src/core/log_privacy.py` 的 `PrivacyRotatingFileHandler.doRollover()`。
- 当前顺序为：校验当前路径 → `open(self.baseFilename, "w")` 截断 → `_open()` 再校验。路径校验和截断是两个独立操作。
- 独立确定性探针在 `open(..., "w")` 前把 `miku.log` 换成外部 `outside.sqlite3` 的硬链接。当前结果：`race-triggered True`、`rollover-raised LogBoundaryError`，但 `outside-unchanged False`、外部文件已从 27 字节变成 0。事后抛错不能恢复数据，违反 fail-before-side-effect。

要求：

1. `backup_count=0` 不得再按路径二次 `open(..., "w")`。在 handler lock 下取得通过 `_open()` 全套校验的 stream，对该已打开文件描述符执行 `seek(0)` / `truncate(0)`；截断前后比较 fd、当前路径与可信根身份。路径被替换时只能截断已验证的原句柄，随后 fail closed，绝不能触碰换入的外部对象。
2. 若进入 rollover 时 stream 为 `None`，先通过安全 `_open()` 建立并验证句柄；任何边界失败发生在截断之前。
3. 增加确定性回归测试：在“安全打开后、截断前”替换当前路径为外部硬链接，断言抛 `LogBoundaryError`、stream 关闭、外部 sentinel 字节完全不变。测试钩子只能用于同步，不得让生产调用方改变安全流程。
4. 重新运行 F1–F12 专项、相关、全量、compileall 与 diff-check；完成后停止等待 Codex，不进入后续板块。

## Round 5 复验（F12 返修后）

> 结论：**安全越界已封住，但可靠性契约仍未完全闭环。** F12 独立探针现为路径替换已发生、`LogBoundaryError` 抛出、外部 sentinel 完全不变；专项 49 项通过。普通截断 I/O 错误仍被错误升级为安全边界异常。

### Finding 13 — P1：普通截断 `OSError` 被包装成 `LogBoundaryError` 并从日志调用冒泡

- 位置：`src/core/log_privacy.py` 的 `_rollover_truncate_verified_fd()`。
- `stream.seek/truncate/flush` 的普通 `OSError` 当前被关闭 stream 后包装成 `LogBoundaryError("日志文件截断失败")`。`emit()` 会把所有 `LogBoundaryError` 重新抛给业务调用方。
- 独立故障注入用一个 `truncate()` 抛 `OSError` 的 stream proxy 强制轮转，结果为 `truncate-io-propagated LogBoundaryError`、`stream-closed True`。磁盘/文件系统短暂故障会让一条普通 `logger.info()` 中断主程序，违反 F8“普通编码/写入错误走 handleError”的契约。

要求：

1. 截断 I/O 失败时可关闭处于不确定状态的 stream，但必须保留/抛出普通 `OSError`（或非边界日志 I/O 异常），让外层 `emit()` 进入 `handleError()`，不得从 `logger.info/error()` 冒泡。
2. 只有路径、根目录、reparse、硬链接、路径/fd 身份不一致等边界失败可使用 `LogBoundaryError` 并向业务方抛出。
3. 新增通过真实 `logger.info()` 触发的故障注入测试：普通 truncate `OSError` 不冒泡；handler 不保留不确定流；后续允许重试。并同时确认 F12 路径换入仍抛 `LogBoundaryError`、外部 sentinel 不变。
4. 重新运行 C1 专项、相关、全量、compileall、diff-check，随后停止等待 Codex。

## Codex 最终复验

- 结论：**安全硬化 Round 3C1 通过，可以进入 3C2。**
- 独立探针：普通格式化异常不冒泡且 handler 可续写；日志根 junction 替换被 `LogBoundaryError` 阻止且外部零文件；`backup_count=0` 有界；关闭/更名零遗留 handler；极小正容量仍为正字节；截断竞态发生时外部 sentinel 逐字节不变；普通 truncate `OSError` 不冒泡且不确定 stream 已关闭。
- Codex 全量：`561 tests / OK (skipped=2)`；`compileall` OK；`git diff --check` OK（仅既有 LF/CRLF 提示）。
- 全程只用临时目录/fake/离线测试；未启动真实 Ollama、网络、RVC、麦克风、扬声器或主程序；未读取/删除 `data/memory`；未提交、未推送。
