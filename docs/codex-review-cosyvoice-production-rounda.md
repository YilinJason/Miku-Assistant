# Codex Review：CosyVoice生产切片A

日期2026-10-17；状态**FINAL PASS**。Codex独立74项相关OK(skipped=1)，A1-A3实现已逐行复审。允许进入B，不代表真实worker已运行。Windows拒绝创建symlink的测试跳过，替身分支覆盖链接拒绝。

## A1 路径resolve过早，链接/junction与目录外逃可在封存前被隐藏

`assets.fixed_layout_paths()`对每个相对路径直接resolve；`_seal_trusted_local_tts_profile()`再capture得到已跟随链接的路径，因此原始python/script/model或vendor目录的symlink/junction不可见。`capabilities._resolve_regular_file`同样resolve后才is_symlink，不能拒绝链接。

要求：固定路径保留词法路径，在构造期与每次spawn前检查repo内每级已有祖先及目标的lstat/is_symlink/Windows FILE_ATTRIBUTE_REPARSE_POINT，拒绝symlink/junction；验证实际路径仍在受信repo内。包括vendor、model、worker、python、cache/wetext/matcha路径，未创建cache允许检查其已有祖先，不新增mkdir。存储sealed repo用于spawn复验。不要改既有RVC边界引入无关风险；不要扫描整个vendor。

回归：链接python或祖先vendor/model/cache junction、封存后换祖先到外部均零Popen；真实Windows junction测试使用临时目录（或无法创建时针对lstat元数据的单元行为），不读真实用户数据。

## A2 Popen kwargs黑名单不完整

`merge_local_tts_popen_kwargs()`将未知键原样透传。例如preexec_fn/pass_fds/close_fds/startupinfo/creationflags均未拒绝，可扩大执行或句柄继承能力。这个专用接口所有选项都由固定构建器提供，应拒绝全部caller kwargs（目前无必要扩展选项）。用fakePopen证明任意未知/回调/继承键拒绝且零启动。

## A3 text管道未固定encoding

`build_runtime_popen_options()`仅text=True，而child固定PYTHONUTF8=1。主程序在非UTF8 Windows locale下可能向stdin写中文失败，stdout解码也不一致。固定encoding=utf-8、errors=strict；不允许覆盖。测试固定kwargs并构造中文JSON编码/解码roundtrip，无设备模型。

## 同轮小项

parent_pid不得bool/float/字符串经int静默转换，严格正整数。settings坏值覆盖每个边界/NaN/inf/未知项，极大int需ValueError而非未捕获OverflowError。纯解析测试断言无mkdir/thread/Popen/importtorch副作用。保留诊断重导出兼容，避免通配符导出内部模块名，明确__all__或显式列表。

完成专项+旧expressive/security/artifact相关回归、compile/diff/dryrun，更新交接停止。不得提交推送或接入B。

---

## 返修注记（2026-10-17）

- **A1**：`path_trust` + 词法 `fixed_layout_paths`；封存/`spawn` 前 `revalidate_fixed_layout_paths`；逐级 `lstat`/reparse 拒绝；封存 `repo_root` 复验
- **A2**：`merge_local_tts_popen_kwargs` 拒绝任意 caller 键（含 `preexec_fn`/`creationflags`/`close_fds` 等）
- **A3**：`build_runtime_popen_options` 固定 `encoding=utf-8`、`errors=strict`
- **小项**：严格 `parent_pid`；settings 大整数/NaN/inf → `ValueError`；`assets.__all__` 显式重导出；纯解析零副作用测试
