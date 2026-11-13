# ADR：edge-tts 句级韵律与 RVC 保真调优（语音自然度 Round 1）

- **状态**：Round 1 与扩展状态试听集均经 Codex 最终复验 PASS
- **日期**：2026-10-02
- **任务**：`docs/cursor-task-voice-naturalness-round1.md`；扩展 `docs/cursor-task-voice-naturalness-extended-suite.md`
- **评审**：`docs/codex-review-voice-naturalness-round1.md`

## 问题

生产 edge 缺句级韵律；RVC 偏平滑。Codex 复验另确认：`--execute` 须封存 TrustedRvcProfile；Edge 失败不得把空/旧 base 送入 RVC；旧产物 purge 若吞异常或删除仅入延迟队列，仍可能把旧非空文件误判为本轮成功。人工试听需要更多固定语气状态，但不能改主程序或默认 core 行为。

## 决策

1. **句级 `EdgeProsodySettings`** + 统一 `_make_edge_communicate`。
2. **RVC YAML**：`filter_radius=1`、`rms_mix_rate=0.68`；代码默认仍 3/0.75。
3. **A/B `--execute`**：TrustedRvcProfile 一次性封存；`save_to_file` 失败清理并重抛；base size>0。
4. **purge fail-closed（F3）**：`_purge_sample_artifacts` 对每个仍存在路径（含 symlink）经 TempFileManager `adopt_existing`+`remove`，删除后必须确认不存在；登记/删除/确认任一失败立即抛出，禁止吞异常后继续 TTS/网络；不直接 unlink、不削弱身份检查。
5. **扩展套件（optional `--suite`）**：`core`（默认四组，目录 `data/test/voice-naturalness/`）与 `extended`（十组固定语气，目录 `.../extended/`）共用同一 `run_execute` / purge / SafetyPolicy 路径；argparse choices 限定；不新增生产请求或情绪推断。

## 后果

- Round 1 真实 `--execute`（core）已 4/4 通过。
- 扩展套件真实 `--execute --suite extended` 已 **10/10** 通过；十组 RVC 基频跨度为 5.87–11.49 半音，未观察到音高塌缩。人工试听仍是最终听感判断。
