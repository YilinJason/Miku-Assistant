# Cursor 任务：语音自然度 Round 1——edge 句级韵律与 RVC 保真调优

> 先读取根 `AGENTS.md`、`.cursor/rules/system-safety.mdc`，以及 `skills/safe-refactoring/SKILL.md`、`skills/architecture-contracts/SKILL.md`、`skills/engineering-quality/SKILL.md`、`skills/collaboration-review/SKILL.md`。当前工作区基于检查点 `7dff31b`，包含安全硬化、自然陪伴和记忆修复的大量未提交改动；必须原样保留。只完成本文 Round 1，完成后停止等待 Codex Review。不得提交或推送，不得读取/修改/删除 `data/memory`，不得启动主程序、麦克风、扬声器、Ollama 或真实 RVC/edge 服务；本轮实现与离线测试只用 fake。

## 已确认现状

1. 生产链路为 `speech projection → split_for_speech → edge-tts → MP3/WAV → RVC → playback`。
2. `config/config.yaml` 的 `voice.tts.rate: 150` 只用于 pyttsx3。生产 edge 路径当前调用 `edge_tts.Communicate(text, voice)`，没有传 `rate`、`pitch`、`volume`；`save_to_file()` 也一样。
3. 已安装 edge-tts 的 `Communicate.__init__` 支持关键字 `rate='+0%'`、`volume='+0%'`、`pitch='+0Hz'`。
4. RVC `pitch` 是整段半音偏移，不能制造句内抑扬。当前 `filter_radius=3`、`rms_mix_rate=0.75` 偏稳健，但可能进一步平滑 F0/响度变化。
5. 现有分句流水线已经按 `。！？!?；;…` 分段，超长句才按逗号拆分。必须复用它，不新增模型请求、网络请求数量或播放线程。

## 目标与可观察结果

在不改变显示文本、会话历史、memory、RVC Worker 生命周期、取消/关闭和 SafetyPolicy 边界的前提下：

- edge-tts 每个已有句段获得有界、确定性的 `rate/pitch/volume`；问句、感叹句、省略/舒缓句与普通陈述有轻微差异。
- 不生成或接受 SSML，不让模型直接控制语音参数，不记录朗读正文。
- 当前真实配置采用温和基线，并减少 RVC 对韵律的平滑；所有参数可单独回退。
- 同一文本、同一配置输出参数完全确定；配置非法时在任何网络、音频、线程或 RVC 副作用前失败。

## F1：纯本地 edge 韵律值对象

建议新增 `src/voice/prosody.py`，职责只包含配置解析与句级参数推导：

1. 定义不可变 `EdgeProsody(rate: str, pitch: str, volume: str)` 或等价窄类型。
2. 新配置位于 `voice.tts.edge_prosody`：

   ```yaml
   edge_prosody:
     enabled: true
     rate: "-4%"
     pitch: "+0Hz"
     volume: "+0%"
     question_rate_delta_pct: 2
     question_pitch_delta_hz: 4
     exclamation_rate_delta_pct: 2
     exclamation_pitch_delta_hz: 3
     ellipsis_rate_delta_pct: -3
     ellipsis_pitch_delta_hz: -2
   ```

3. 接受严格带符号格式：rate/volume 为 `+N%` 或 `-N%`，pitch 为 `+NHz` 或 `-NHz`；bool 不得冒充 int。基值和 delta 均在构造 TTS 前校验为有限整数。
4. 最终值必须 clamp：rate `[-30, +30]%`，pitch `[-50, +50]Hz`，volume `[-50, +50]%`。格式规范为显式符号，例如 `-4%`、`+0Hz`。
5. `enabled=false` 时始终返回 edge 中性值 `+0% / +0Hz / +0%`，不应用基值或 delta。
6. 句型只看已经投影后的当前句段末尾标点，优先级：问号 `？?` > 感叹号 `！!` > 省略号 `…` 或 `...` > neutral。不得基于秘密词、人格词表或外部模型猜情绪。
7. 只改变传给 edge 的参数；不得修改、包裹或复制正文，不拼接 SSML。

## F2：接入所有 edge-tts 出口且保持生命周期

1. `TTSEngine` 增加向后兼容的可选 `edge_prosody` 参数，放在现有参数末尾或用关键字，不能改变旧 positional 调用的含义。
2. `build_tts_engine()` 从 `voice.tts.edge_prosody` 传入配置。默认代码配置保持 `enabled=false` 或等价中性，保证没有 YAML 的调用方行为兼容；仓库真实 `config/config.yaml` 显式使用上方温和配置。
3. `_edge_tts_to_mp3()` 和 `save_to_file()` 必须共用同一个 helper，调用：

   ```python
   edge_tts.Communicate(text, voice, rate=..., pitch=..., volume=...)
   ```

4. 流水线仍是一句一次现有 edge 请求；非流水线仍是一段一次请求；不得因 profile 增加请求、任务、线程或缓存正文。
5. 保留 `detect_edge_voice()`、云 TTS 权限、临时文件所有权、异常清理、fallback、interrupt、shutdown 现有语义。

## F3：温和 RVC 配置调整

仅更新仓库真实 `config/config.yaml`：

```yaml
filter_radius: 1
rms_mix_rate: 0.68
```

保持 `pitch: 0`、`index_rate: 0.55`、`protect: 0.5`、`f0_method: rmvpe`。同步注释，明确：

- `filter_radius=1` 减少 F0 中值平滑，但若真机出现颤音/毛刺可回退 2/3；
- `rms_mix_rate=0.68` 更多保留源响度包络；
- 不声称离线测试可证明听感提升。

不要改 `vc_params.py` 的兼容默认值；只有显式 YAML 使用新调优值，避免无配置调用方静默改变。

## F4：人工 A/B 入口

更新 `test_rvc_conversion.py`，让它的 base edge 音频使用与生产相同的 prosody helper/配置，并把测试文本扩充为四类：普通陈述、疑问、开心感叹、带省略号的舒缓表达。

约束：

1. 保留其“人工真实集成测试、非离线门禁”的身份；不得把它导入 unittest discovery。
2. 增加显式 `--execute` 才进行 edge 网络/RVC/写音频；无参数只打印计划、有效 prosody 和隐私提示，不调用网络、RVC、播放器，不创建音频文件。
3. `--execute` 输出到 `data/test/voice-naturalness/`，保留成对的 `*_base` / `*_rvc` 文件供试听；不得自动播放。
4. 使用 `SafetyPolicy.from_app_config(config)` 和既有 TTS/RVC 能力，不绕过子进程或云 TTS 权限。不得打印完整用户对话；样本是代码内固定非敏感文本。
5. 所有引擎/worker/temp 资源必须在 `finally` 关闭；失败返回非零。

## F5：离线行为测试

新增 `tests/test_voice_naturalness_round1.py`，至少覆盖：

- 格式解析、bool/float/无符号/越界输入、clamp、enabled=false；
- neutral/question/exclamation/ellipsis 与优先级，中英文标点；
- `_edge_tts_to_mp3` 和 `save_to_file` 的 fake `Communicate` 收到精确 kwargs，正文完全不变；
- pipeline 多句恰好每句一次调用且各 profile 正确；关闭/中断/失败时调用数与清理契约不回归；
- assembly 转发配置，默认无 YAML 时中性兼容；
- dry-run A/B 入口零网络、零 RVC、零文件创建；
- RVC 参数构建兼容默认值不变，并验证显式 `1/0.68` 可透传。

测试不得真实访问 edge、RVC、声卡或麦克风。不得仅检查源码字符串。

## 文档与验证

新增简短 ADR `docs/architecture/adr-edge-sentence-prosody.md`，更新 `docs/VOICE.md`、`docs/engineering-baseline.md` 和 `docs/cursor-handoff-status.md` 顶部。明确 edge-tts 会发送朗读文本到外部服务，真实听感需人工 A/B。

运行：

1. 新增专项；
2. `tests.test_stream_playback`、`tests.test_voice_session`、`tests.test_rvc_worker_lifecycle`、`tests.test_security_round3a_review_fixes`、`tests.test_mode_assembly_round2`、自然陪伴 Round 1/2；
3. 全量离线 unittest；
4. `compileall`、`git diff --check`；
5. 只做 A/B 脚本的无参数 dry-run，禁止本轮自行执行 `--execute`。

交接报告真实测试数量与退出码，列出未运行的真实链路。完成后停止等待 Codex Review。
