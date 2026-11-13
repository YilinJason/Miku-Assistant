# ADR：RVC 电流/金属伪影参数隔离评估

- **状态**：Codex工程评审与真实24/24矩阵通过；等待人工听感选择
- **日期**：2026-10-16
- **任务**：`docs/cursor-task-rvc-artifact-evaluation-round1.md`

## 问题

人工试听确认 CosyVoice → Nahida RVC 比 CosyVoice base 更自然，但有明显电流音。上一轮六状态与近静音频谱已排除文件损坏、持续白噪和并行显存问题。生产参数仍是 `index_rate=0.55`、`filter_radius=1`。先做参数归因，不加入激进低通或降噪。

## 决策

1. 新入口 `diagnose_rvc_artifacts.py`。无参数只打印固定计划；只有 `--execute` 才读固定输入并跑 RVC。
2. 输入只读 `data/test/voice-naturalness/cosyvoice-instruct/` 的六段 `*_base.wav`，label 与 `SAMPLE_CASES` 一致。基线 `idx055_fr1` 只引用上一轮 `*_rvc.wav`，不重跑、不删除。
3. 真实库核对表明`filter_radius`仅Harvest分支有效，RMVPE不受影响；`protect=0.5`关闭清辅音保护，更小值启用。纠正后的四profile为`idx025_p050`(0.25/0.5)、`idx055_p033`(0.55/0.33)、`idx025_p033`(0.25/0.33)、`idx000_p050`(0.0/0.5)，括号为index_rate/protect。固定filter_radius=1、pitch=0、rms_mix_rate=0.68、RMVPE。沿用轻量后处理，fallback关闭。
4. 每组用 `RvcArtifactSession` 走现有 `warm_rvc_worker` / `RVCConverter`。一组六段结束后 `shutdown(stop_rvc=True)`，`stop_rvc_worker()` 必须确认进程已退出，才允许下一组 init。不构造 `TTSEngine` / `AudioPlayer`，避免打开扬声器。
5. 持久 base 先复制到本次唯一 staging 再 `adopt_existing`。RVC 临时输出复制到最终文件后仍由 `TempFileManager` 回收；最终评估文件不长期登记。失败只清理本轮半成品，不删除已完成 profile、基线或 CosyVoice 输入。
6. `complete=true` 仅在四组完成、staging 已清理、Worker 已停止后原子发布。失败报告 `complete=false`，只含固定阶段、label 和 profile id。

## 替代方案

- 直接改 `config/config.yaml` 反复试听：拒绝。生产默认尚未由人工 A/B 选定。
- 用 `TTSEngine` 跑矩阵：拒绝。构造时会初始化播放器，可能打开扬声器。
- 多 profile 并行：拒绝。protect是Worker启动参数，各组需完整停机再加载；不允许并行抢GPU。

## 明确不做

- 不改生产 YAML、`TTSEngine` 默认值或 `polish_rvc_wav`。
- 不直接启动 `scripts/rvc_worker.py` / `rvc_infer.py`。
- 不启动主程序、Ollama、CosyVoice、麦克风、扬声器或浏览器，不读改删 `data/memory`。
- Cursor 不运行真实 `--execute`。

## 后果

Codex独立111项相关/773项全量通过，真实24/24通过；停止失败保留进程与client，阻止重建或一次性fallback并行加载。听感选择仍需用户比较；短时退出后显存恢复不等于已完成30–60分钟长测。完整证据见`docs/codex-review-rvc-artifact-evaluation-round1.md`。
