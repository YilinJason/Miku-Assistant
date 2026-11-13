# Cursor 任务：安全硬化 Round 3A——专用子进程能力与临时文件所有权

> 状态：Round 3A 已由 Cursor 实现，等待 Codex Review。  
> 基线提交：`7dff31b`（工作区在本任务文件加入前应保持干净）。  
> 范围：只做本文件 3A。不得进入远端 HTTP、云服务许可、日志隐私或自然陪伴功能。

## 先读取

- `AGENTS.md`
- `skills/architecture-contracts/SKILL.md`
- `skills/safe-refactoring/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `skills/collaboration-review/SKILL.md`
- `.cursor/rules/system-safety.mdc`
- `src/core/safety.py`
- `src/voice/rvc/{worker,infer,health_check,device}.py`
- `src/voice/{stt.py,tts/engine.py}`、`src/voice/audio/temp_files.py`
- `src/core/assembly.py`、`src/core/assistant.py`

## 目标与安全不变量

1. RVC Worker、一次性 RVC 推理和 RVC 环境/设备探测都不再直接调用 `subprocess.run/Popen`。整条调用链使用装配时创建的同一个 `SafetyPolicy`，不得在下层用新策略绕过上层配置。
2. 把宽泛 `subprocess` 权限与 RVC 必需能力分开。新增具名、固定目的的能力（名称可调整，但语义至少覆盖 worker / one-shot infer / probe）：
   - 只允许参数列表，永久拒绝 `shell=True`、字符串命令和额外 shell 拼接；
   - executable 必须是经解析后与本次注册/推导的 RVC Python 完全一致的普通文件；
   - script 必须是仓库内预期的固定脚本（worker 或 infer），不能由模型输出、用户文本或任意附加参数替换；
   - 固定 flag 集合、重复 flag、缺值、未知 flag、路径越界、意外解释器均在创建进程前拒绝；动态值必须作为独立 argv 元素；
   - `MIKU_DRY_RUN=1` 时所有子进程能力都拒绝；拒绝日志不得打印完整命令、用户目录或正文。
3. 通用 `Operation.SUBPROCESS` 默认改为拒绝；兼容入口可以保留，但不能成为 RVC 的回退旁路。RVC 正常功能依赖专用能力，而不是重新开启通用执行。系统电源继续保持 `system_power` 权限 + 用户确认双门，注册表写入继续永久拒绝。
4. 临时文件删除采用“白名单目录 + 本进程创建/登记的精确文件”双条件。仅目录白名单不足：
   - TTS、RVC、STT 创建临时文件时立即登记所有权；删除只接受同一所有权管理器登记且仍位于白名单内的普通文件；
   - 任意现有文件、伪造路径、父目录穿越、目录、未登记文件和逃逸 symlink/junction 都拒绝；
   - 删除/关闭幂等，失败有界重试且不一文件一线程；关闭后不能登记新文件；
   - 不删除 `data/memory`、仓库根 `tmp/`、模型、索引、用户文件或目录。
5. 不增加启动期模型、设备、网络或磁盘扫描副作用；纯文本模式仍不装配语音栈；RVC Worker 所有权、插话取消和 shutdown exactly-once 语义保持。

## 设计要求

- 在 `src/core/safety.py` 提供窄而可测试的能力契约。可以使用枚举 + 不可变请求/spec，也可以使用等价设计；调用者不能自行声明任意 executable/script 即获得许可。
- 进程启动器需可注入 fake runner/popen，行为测试不得创建真实子进程。不要靠源码字符串搜索充当安全测试。
- `SafetyPolicy.summary()` 只返回能力布尔值或安全摘要，不返回路径、argv、认证信息。
- 临时文件所有权应集中到现有 `src/voice/audio/temp_files.py` 或清晰的窄模块，不要在 STT/TTS/RVC 各自复制不同规则。
- `build_stt_engine`、`build_tts_engine`、懒加载、voice 装配和直接依赖注入测试路径都要维持兼容；新增参数使用关键字可选参数，并由正式装配显式传入共享策略/管理器。
- 对配置做严格布尔/类型校验，配置错误要在设备或子进程副作用前失败。更新 `config/config.yaml` 和 `src/core/config.py` 默认值及说明。

## 必须新增的行为测试

至少覆盖：

1. 三种 RVC 专用能力的合法 argv 能到达 fake runner，非法 executable/script、字符串命令、`shell=True`、未知/重复/缺值 flag、路径越界在 fake runner 调用前拒绝。
2. 通用 `subprocess=false` 时合法 RVC 专用能力仍按配置工作；专用能力关闭或 dry-run 时拒绝；不存在退回直接 subprocess 的路径。
3. 从 `assemble_default` 到 TTS/RVC 与 STT/临时文件使用同一 `SafetyPolicy` 实例；text 模式不触发这些能力。
4. 已登记临时文件可 exactly-once 删除；未登记文件、预先存在文件、目录、越界路径、symlink/junction 逃逸拒绝；close 与并发登记/删除线性化且线程/队列有界。
5. STT Whisper 临时 WAV、edge-tts 中间文件、RVC 输入输出在成功、异常、取消、超时和 shutdown 路径都走统一所有权清理；失败不能掩盖原始业务错误。
6. 系统电源仍需权限与确认；注册表写永久拒绝；既有 Round 2 生命周期/资源测试不回归。

## 验证边界

仅运行 Python 3.12 的纯离线 fake/临时目录测试：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py diagnose_resource_soak.py
git diff --check
```

不得启动 `main.py`、真实 Ollama、RVC、麦克风、扬声器、浏览器、文件清理实扫或系统电源操作；不得读取/删除 `data/memory`；不得安装依赖；不得提交或推送。

## 交付

- 新增安全能力 ADR，记录威胁边界、兼容迁移、失败语义和明确未覆盖项。
- 更新 `docs/engineering-baseline.md` 与 `docs/cursor-handoff-status.md` 顶部。
- 返回实际修改文件、测试数量、未运行项、剩余风险。
- 完成后停止，不要自行进入 Round 3B。
