# 安静环境语音误触发优化：最终代码验收

日期：2026-11-05。结论：**Phase A 代码与离线验收通过；真实麦克风效果待用户重启复测。**

补充研究：用户报告仍有噪声转写后，已完成[实时语音策略对比与扩展负样本实验](research-real-time-voice-input-strategies-2026-11-05.md)。当前VAD放行19个超过最低门限的合成非人声片段中的11个，mode3仍放行10个；没有运行ASR，不是实际误转写率。下方低幅负样本结果保持有效，但覆盖不足，不能作为“防噪声问题已解决”的验收。生产代码本轮未变，后续应补前处理/概率型VAD与真实环境验收。

用户确认异常在安静且助手没有播报时发生。本轮针对这个场景修复录音入口，不将未采集的异常声音来源写成已证实根因。由官方 Cursor Agent CLI（composer-2.5，session 027e8438-6142-4d10-a86d-3340dafa3eec）编写源码与正式测试，Codex 设计、逐次审查、独立执行验收。未提交、未推送。

## 最终行为

- 默认 Paraformer 的真实麦克风路径：有界动态能量门限 → 16kHz mono s16 PCM → 本地 WebRTC VAD → 通过后才调用 Paraformer。拒绝的声音不进入回复、记忆提取或工具路由。
- 真实 SpeechRecognition Recognizer 子类约束每一次门限写入，覆盖环境校准、等待起句、录音中的动态变化；默认上下限 300/12000。不修改第三方安装源码。
- `voice.stt.input_audio` 默认启用：VAD mode 2，30ms 帧，最少人声 180ms、最少连续人声 90ms、比例 0.12。它们是待真实环境校准的默认值，不是准确率保证。无 VAD/DLL/原生执行错误时安全暂停，提供安装提示，不悄悄接受全部声音或回退云识别。
- `voice.stt.microphone_device_index` 可显式指定设备；默认仍是系统默认输入。无效设备报错，不悄悄切换；枚举结果有界缓存，避免每轮全量枚举。
- 所有 Paraformer 转写入口先验证 PCM 类型、对齐、非空和 480000 字节上限。实际录音按源 CHUNK（采样帧）/采样率及前导缓冲预留长度，不静默截断超长输入。直接受信任 AudioData 诊断保留不执行麦克风 VAD 的入口，仍受 PCM 硬边界约束。
- VAD 是主进程内的轻量 CPU 判定，没有附加神经网络模型、GPU 任务、子进程、队列或线程。每个片段重新创建廉价 VAD 状态，避免状态污染下一次输入；PCM/帧数仍有 15 秒硬上限。文本模式无原生 VAD/模型初始化。

## 独立验证依据

1. `tmp/codex_gate_voice_input_final_integration_nov5m_2026_11_02.json`：**182 tests OK**，11 个相关模块，12.735 秒监督器总时长。覆盖声学准入、配置、STT、预检/懒加载、装配回收、双工文字门控和会话回归。
2. `tmp/codex_gate_voice_input_final_native_nov5n_2026_11_02.json`：**7 tests OK**。真实 SR 方法使用虚拟 AudioSource 验证内部门限变化；实际 WebRTC 使用固定音频/合成信号验证，没有打开麦克风或播放声音。
3. 原生结果：`tmp/voice_input_native_replay_20261105_72c3f83871e0.json`。3 个固定 RVC 语音样本，原幅度/0.25/0.1 倍幅度共 9 例全部通过；全零、DC、低幅白噪声、50/60Hz 模拟嗡声 5 例全部拒绝；6 组语音/低幅非人声交替判定正确。语音 fixture 自身 hash 前后相同；它们不是用户口音或真实房间噪声语料。
4. 500 次 1 秒非人声片段连续调用：VAD 热 p50 **0.160ms**、p95 **0.198ms**（含 tracemalloc 采样开销）；Python 保留增量 16400 字节、峰值 51025 字节；本探针进程 RSS 33828864 → 33869824（+40960）字节，private 24170496 → 24174592（+4096）字节。没有看到明显累积，但短测不能证明长期绝无泄漏。上述耗时只属于声学门控，不包括录音停顿、ASR、LLM、合成和播放。
5. 生产 YAML 与任务前快照比较：仅新增 STT 声学门控/设备配置，其他配置不变。最终两个门禁均记录共享 WAV 集合 728 → 728，无增删。此前历史文档的 828 是更早时点；本任务不推断其间差异来源，也没有删除那些文件。
6. 相关 tracked 文件 `git diff --check` 通过（仅 LF/CRLF 提示），新增代码/测试无行末空白。失败证据保留，不用通过报告覆盖它们。最终源码清单见 `tmp/voice_input_final_source_hashes_20261105.json`。

## 测试隔离纠正

早期新增 `test_mic_preflight_lists_once` 只 mock 设备列表，遗漏真实 Microphone 构造/进入。它存在打开默认输入流的路径，因此早期 173 项通过不作为完整离线验收依据。该检查代码未调用 stream.read、未保存或发送音频。已向用户说明，并改为完整 FakeMicrophone；同时测试模块增加真实 Microphone.__init__/__enter__ 阻断，遗漏隔离会立即失败。最终 182 项在这些保护下重验通过。真实 Recognizer 的时间/门限测试只使用虚拟流。

## 环境与改动范围

主程序 Python 3.12.3 安装 `webrtcvad-wheels==2.0.14.post1`；下载 wheel 约 20KB，SHA256 `a085ee7fa3f96ac7985ef0c1e3194e4c9c544cc9a0579b6dbd12c84d610271cc`，从 PyPI 下载、离线无依赖安装。RVC/ASR 独立环境与模型未改，未加载真实语音/语言模型；正式记忆库未读写。任务前源码快照保留在 `tmp/voice-false-trigger-before-20261105/`，不可整仓回退覆盖用户其他修改。

源码主要是 `src/voice/input_audio.py`、`src/voice/stt.py`、`src/core/assembly.py`、`src/core/config.py`、`src/cli/voice_preflight.py`；测试为 `tests/test_input_audio.py`；配置/依赖/README/VOICE/ADR 同步。Codex 的独立回放探针放在 tmp，由现有 scoped gate 监督，不新增常驻任务。

## 仍需验证 / 不在本轮内

- 用户自己的麦克风、轻声/短语气词、真实房间的风扇/键盘/呼吸声、连续语音和低配设备资源表现。重启后先做安静 3–5 分钟，再正常/轻声短句复测；参考 `tmp/manual-voice-test-20261105/QUIET_INPUT_RETEST.md`。
- VAD 不能证明说话者是本人，也不能保证拒绝电视或其他人的真实讲话；未注册声纹、未新增独立唤醒服务。
- 审计记录的双工回声计时窗口属于后续 Phase B，本轮没有改动 `voice_session.py` 或声明已有声学 AEC。真实扬声器场景仍需独立修复/验收；此次安静误触发优化不能替代它。
- 当前运行中的旧进程不会自动加载新代码；请正常退出后单实例重启。未替用户启动麦克风复测，保留用户自行测试的安排。
