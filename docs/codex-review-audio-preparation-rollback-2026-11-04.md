# 音频准备事务回滚独立评审

## Root 最终验收 PASS

生产Root独立 `audio_rollback_root7_review_20261104c`：7项、全异常子矩阵PASS（此前26失败已修），仅fake路径。最终仓库fixture已源码确认：实际pin_io_paths、writer未quiet时retire拒绝、最后释放lease/backend/TFM实际complete且文件已删；enterContext确保engine/manager先关闭再卸载临时目录。Root最终 `audio_rollback_root_final_contracts_20261104e`：156 tests/23.098s OK（含最终12、Cosy/RVC IO/security/TFM27）；WAV826精确集合零差、无超时。AST与切片前快照对比：engine仅3准备方法，Cosy仅synthesize_owned_wav/prepare_playable，converter仅convert_file变化，无底层资源/声学/权限改动；diffcheck通过。

三文件已Root精确回收：Cursor承认本会话raw测试产生，Root先CIM确认无该模块遗留进程、逐文件保存普通文件身份/大小0/精确路径，再通过SafetyPolicy+TFM adopt/remove，未扫描或删除其它文件。`tmp/codex_audio_rollback_reclaim_plan_20261104.json`与`tmp/codex_audio_rollback_reclaim_result_20261104.json`：829→826，仅删除具名3项、无新增、owned/pending/inflight/lease=0、worker_alive=False、complete=True。后续156 gate保持826零差。raw测试违规证据保留，不能用初稿829→829掩盖。

此切片闭环；允许下一默认stream跨句预取，不表示真实语音延迟或三模型共存通过。以下为历史。

## 当前：生产 Root7 已通过；fixture 收口完成（Cursor）

Root `audio_rollback_root7_review_20261104c`：7/7 OK。**生产冻结**。Cursor 最终 fixture：

- Cosy KBI：真实 `pin_io_paths` + fake `retire_worker` 在 `writer_alive` 时 `complete=False`；释放 lease → `shutdown_local.complete` + TFM `shutdown.complete` + 路径已删（`_PinningSynthClient`）。
- 全模块 `self.enterContext(TemporaryDirectory())` / `enterContext(patch TMP_DIR)`，backend/engine/TFM cleanup **先于**临时目录卸载；断言 shutdown complete。

Gate **`audio_preparation_rollback_cursor_20261104e`**：12 OK → `tmp/codex_gate_audio_preparation_rollback_cursor_20261104e_2026_11_02.json`（WAV829 零差）。`data/tmp` 三 0B 文件仍由 Root 回收，Cursor 未删。

---

基线自前轮 **826→829**；多出的 3 个 **0 字节** `miku_*.wav`（Root 时间戳 UTC）：

| 文件 | UTC | 大小 |
| --- | --- | --- |
| `miku_82186636bc974b35bad3c6366fd27b05.wav` | 2026-11-04 13:23:50 | 0 |
| `miku_840e3af61c024f38887a44b718b8a13f.wav` | 2026-11-04 13:24:25 | 0 |
| `miku_f53075dfb308453fb84a6b294faab4b3.wav` | 2026-11-04 13:24:42 | 0 |

**本会话（上一 Cursor 轮）在最终 gate `…20261104a` 之前执行的命令**（违反当前「仅 scoped gate」约束的 raw unittest，已停止）：

1. `py -3.12 -X utf8 -m unittest tests.test_audio_preparation_rollback -v`（及无 `-v` 重跑）— 多次迭代，时间窗与上表三文件一致。
2. `CosyPrepareRollbackTests` 当时使用 `default_dir=TMP_DIR`（共享 `data/tmp`）且 `synthesize_owned_wav` 在 KBI 前已 `create(".wav", directory=TMP_DIR)` → **空占位文件**，与 0B 特征一致。
3. 最终 129 项 gate `…20261104a` 的 `wav_before`/`wav_after` 已为 **829**（非 826），监督器未新增 fourth 文件，但**不能**据此掩盖相对 826 的 +3。

**本返修轮（fixture 最终）**：仅 scoped gate `--label audio_preparation_rollback_cursor_20261104e`；Cosy 输出仅在 case 临时根。未删改 `data/tmp`、未改 gate baseline。

---

## 历史：未通过，禁止预取

Root `audio_rollback_root7_review_20261104a`：7 tests包含异常矩阵，**26个子例失败**。都定位到同类遗漏：Edge `_edge_tts_to_mp3`/`_mp3_to_wav` 与RVC infer/polish BaseException分支虽调用共享helper，却未传primary，清理的RuntimeError/KBI/Exit覆盖原始主异常。统一使用 `except BaseException as exc` 并传 `primary=exc`；不是放宽helper吞所有错误。成功/普通RVC失败/普通polish返回语义保持。Root多路径与Cosy fence矩阵已通过，不重写这些。

1. helper docstring声称“最后一个清理失败”实际保存第一个，改正文与实现一致（首个）。RVC普通infer日志仍`exc_info=True`泄露原内容；仅类别。新增失败矩阵必须真正让remove在**调用发生前**抛RuntimeError/KBI/Exit，校验主异常is同一对象、所有新路径均请求、不删输入及无关owner。当前 `test_cleanup_failure_does_not_replace_infer_error` 是转换返回后才设置_fail_on，根本没测试清理失败。
2. 测试隔离未达任务：两个Cosy用例使用真实TMP_DIR/default_dir=TMP_DIR（即共享data/tmp），不能用TemporaryDirectory包装外层就宣称隔离。请patch backend模块TMP_DIR到临时根，TFM及policy allowed目录一致，所有输出只进本case的临时根；所有新engine用addCleanup明确shutdown（当前只manager关闭）。`__class__`换回TempFileManager + remove恒空不是真实pin保护，请用实际TFM IO lease/固定fake-retire未quiet证明，finally释放lease并实际quiet，未quiet时不强删。
3. 共享WAV基线从前轮826变829；最新三项（均0字节）为 `miku_82186636bc974b35bad3c6366fd27b05.wav` (13:23:50UTC)、`miku_840e3af61c024f38887a44b718b8a13f.wav` (13:24:25UTC)、`miku_f53075dfb308453fb84a6b294faab4b3.wav` (13:24:42UTC)，在本CLI任务内、最终gate之前出现。**请先报告本会话具体运行命令与测试归属证据**；不能称“829→829零差”就掩盖826外多出的3个。不要自行删除任何现有路径或扫清理，Root将根据可证实归属再处理；不要更改现有baseline/gate逻辑或擦除记录。今后测试只能现有scoped gate前台顺序唯一label，禁止raw unittest/临时python直接跑tests/discover/并发；Rootprobe由Root独立执行。

范围仅当前适配器/helper/新tests/ADR交接；不改通过的TFM/worker/NativeJob/队列/模型声学权限。返修后唯一label新模块gate再STOP，不提前做预取。Root独立7与异常矩阵、相关回归及精确文件差异复核后才验收。
