# 用户选定的声音基准

2026-10-17 用户反馈：`01_warm_companion_rvc.wav` 电流音最少，同时仍像 Nahida。将此文件作为后续生产接入与调音的听感基准，不能替换成名称相近的矩阵候选。

- 路径：`data/test/voice-naturalness/cosyvoice-instruct/01_warm_companion_rvc.wav`
- SHA256：`7C4AEE8B69428E4BBE3BB773CFC47EFAE1618EFB46B7B80EBAF1465739FE19BA`
- 大小382444字节，哈希与矩阵执行前保存的基线一致。
- 来源：CosyVoice-300M-Instruct，中文女，固定warm_companion指令，再经Nahida RVC；同目录report记录6/6成功、冷加载6.987秒、此句基础合成2.997秒。
- 该文件不是`rvc-artifact-eval`下A/B/C/D候选。RVC评估ADR将其引用为idx055_fr1基线，但旧报告没有完整RVC/postprocess配置快照；不能把当前YAML所有值追认为当时值。
- 保留既有音频，不覆盖/清理。生产接入完成后用显式记录配置的固定文本再生成，做基准对照；其它状态仍需听感复验。当前未因选择而切换默认引擎或参数。

2026-10-18 preservation note: 原件mtime=2026-10-05，已超过data/test默认72小时TTL。未启动清理器；Codex已按上列哈希创建字节相同的本地保护副本 `tmp/protected-voice-reference/01_warm_companion_rvc.wav`。仓库根tmp/被Git忽略，且FileCleaner明确不扫描此目录。后续版本整理再迁到正式忽略的参考音频目录；当前不覆盖原件、不纳入提交，不能从测试输出TTL中推断参考音频会永久保留。
