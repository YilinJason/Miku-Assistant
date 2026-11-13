# 输入链路第二轮优化：执行顺序与 Phase 1

用户已同意 `docs/research-real-time-voice-input-strategies-2026-11-05.md` 的方案并授权实施。Codex设计/评审/独立验收，官方Cursor作者编写生产代码与正式测试。主额度剩余<=10%必须停止并保存检查点；不能擅自宣称全任务完成。

## 总顺序（一个阶段通过后才进入下一阶段）

1. **本地概率VAD候选、噪声验收与资源准入**：先实现Silero CPU ONNX窄适配器，与当前WebRTC在相同数据上比较。候选不合格就修复/重新选型，不直接默认启用。
2. **有界连续输入与本地降噪**：一个会话拥有输入流，明确帧/语音段时间、缓冲、取消与关闭；补可用的本地NS，接入已验证的人声准入，保留Paraformer。真实设备测试由用户自行进行，不能默认开mic。
3. **双工与误打断**：按采集与实际播放时序处理回声，分开疑似/确认插话；具备恢复契约后处理误打断恢复。声学AEC/语义断句有独立成本和依赖，不能拿计时器伪装AEC或任意增加大型模型。

本文件当前只授权Cursor实现下面的 **Phase 1 adapter**；其余阶段由Codex阶段验收后下发。

## 共同边界

读 AGENTS.md、skills/architecture-contracts/SKILL.md、engineering-quality/SKILL.md、collaboration-review/SKILL.md、safe-refactoring/SKILL.md 与 .cursor/rules/system-safety.mdc。保留大量已有脏修改，禁止reset/restore/clean/commit/push。任务前相关文件快照 `tmp/input-audio-v2-before-20261105/`。

不改RVC/Nahida/TTS/Paraformer模型或独立环境，不访问data/memory，不启动应用、录音、播放、云API、模型下载或pip。Codex负责依赖/资产准备及真实固定文件实验。可运行下方指定的有界fake门禁；禁止raw discover/pytest以及原生模型测试。

## Phase 1：小型CPU ONNX适配器（先不接入默认生产配置）

创建 `src/voice/silero_vad.py`，stdlib-only导入面，按需导入numpy/onnxruntime，**不导入torch、silero_vad官方Python工具模块或torchaudio**。官方工具模块会import torch；本任务只实现numpy/ORT的已知张量契约，不下载/执行远端Python源码。

固定资产已由Codex准备：

- vendor/silero-vad/silero_vad.onnx
- 官方commit `066409f3d9db45a30d21dfcbf9147826ffdd7052`
- bytes `2327524`
- SHA256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`
- 来源与许可证：tmp/silero_asset_manifest_20261105.json、vendor/silero-vad/LICENSE
- 候选CPU ORT版本1.24.1，主Python3.12；目前未承诺安装完成。不要修改主requirements或自动安装torch。用独立requirements-input-audio.txt记录必要的固定CPU运行时（numpy与现有主环境2.3.5兼容；不强制重装不必要包），Codex审查后安装。

适配器接口建议：`SileroOnnxVAD.score_pcm(pcm: bytes, *, cancel_event=None) -> VadScores`；`VadScores` frozen、明确ok/reason/probabilities/valid_samples（每帧真实样本数）、耗时（可选）。错误不是文本成功，不编造ASR置信度。模型是VAD概率，不是用户身份认证。可加 `close()` 明确终止会话所有权。真实会话/配置接入留下一阶段。

实现约束：

- 固定repo-root资产路径，加载前验证哈希/长度、普通文件/符号链接边界，拒绝外部模型、自定义算子库/自动下载/任意传入路径。有效资产身份可缓存，但文件发生变化不可继续信任旧校验。资产缺失/哈希不符返回稳定错误，不自动降级全接受。
- ORT显式CPUExecutionProvider，intra/inter线程均1、顺序执行，无GPU。单个实例至多持有一个InferenceSession；懒加载single-flight，不为每片段重建模型。验证模型输入输出名称/类型/维度与可见providers后再接受；只支持固定已知图。不要为每帧开线程或子进程。
- 16kHz mono signed16，输入最多480000字节；空/错类型/奇数/超限在native初始化前拒绝。先查cancel/closed再做验证/加载，逐帧查取消；取消或close后不得晚成功。初始weights加载与close需有明确并发语义，不以daemon线程泄漏来实现超时。
- 官方契约：每512 samples（32ms），在其前拼64 samples上下文；输入input float32 [1,576]、state float32 [2,1,128]、sr int64 scalar；输出output和stateN。以固定commit utils_vad.py与实际图meta为准。state/context在一个独立片段开始归零，片段内连续传递；不得跨独立片段泄漏。最后不足512可pad，但valid_samples只能记真实长度，不把padding计成人声证据。
- 每帧概率必须finite且0..1；state正确shape/finiteness；图/原生异常安全失败，不将原始输入/本地私密路径写入错误消息。概率/有效样本数组长度有界，不能积累过去录音。每个片段的state/context由该调用持有，互不污染。
- close等待/拒绝并发语义明确：不得从另一线程释放正在run的session或复活已关闭对象。关闭失败/等待不能伪称已回收；不创建常驻新线程。短批处理本身最多15秒音频，以单线程CPU实际耗时作为准入结果。
- 可注入fake session factory用于正式测试，但不在生产配置开放用户模型路径/provider；保持实现简洁，固定资产与adapter可同模块，勿引入通用插件框架。必要注释解释张量、状态、所有权与取消。

测试 `tests/test_silero_vad_adapter.py`：fake ORT session/numpy输入、资产替换/损坏/外链、CPU选项、single-flight、状态reset、尾帧valid_samples、非finite概率/state、缺依赖、cancel/close在加载/推理期间、关闭后不重启、错误不成功、大小边界。**fake测不得开mic/播放/真实ORTsession/模型**；可使用主环境numpy，不需要torch。为真实native入口加fail-fast替身，遗漏mock立即失败。

加一个简短ADR记录此阶段候选未默认启用。不要编辑Codex task/review或把交接状态写成已验收。

允许自验命令（主解释器固定，用新唯一label，失败产物保留）：

```powershell
& 'C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 .\tmp\codex_scoped_test_gate_2026_11_02.py --label input_v2_adapter_cursor_nov5a --modules tests.test_silero_vad_adapter tests.test_input_audio --timeout 90
```

修复自验失败可换新label重跑。完成后STOP，返回实际改动、门禁报告、未验证边界。Codex独立review和固定音频原生A/B通过前，禁止下一阶段或改当前默认后端。
