# ADR：音频准备事务回滚（Edge / RVC / Cosy）

## 状态

Accepted（2026-11-04），Root独立7全矩阵与最终相关156通过，具名review为准。三件本轮测试遗留物已精确回收，826集合恢复且后续gate零差。未接跨句预取，不代表真实音频性能通过。

## 问题

准备函数在 TFM 已 `create` 输出路径后，若 `KeyboardInterrupt` / `SystemExit` 或二次 `remove` 失败被窄 `except Exception` 吞掉，外层拿不到路径且泄漏；`remove_many` 首路径失败会跳过后续路径清理。

## 决策

1. **`request_owned_paths_rollback`**（`src/voice/audio/preparation_rollback.py`）：逐路径 `TempFileManager.remove`；有主异常时清理失败不覆盖主异常；无主异常时清理失败向上抛出。不直接 unlink、不加线程。
2. **RVC `convert_file`**：`infer` 与 `polish` 分块；普通 `Exception` 仍 `None`；KBI/Exit 回滚输出后原对象上抛；`polish` 普通 `Exception` 仍返回有效音频。
3. **Edge `_edge_tts_to_mp3` / `_mp3_to_wav` / `_prepare_playable_audio`**：`BaseException` 回滚本层新路径；`_prepare_playable_audio` 多路径用 helper，不删输入。
4. **Cosy `synthesize_owned_wav` / `prepare_playable`**：任意 `BaseException` 先 `_mark_failed_output` 或逐路径回滚 + deferred track；不绕过 retire fence 强删 pin。

## 未改

TFM / NativeJob / RVC worker 实现、成功路径与普通 RVC 失败回退语义、预取与 Root 探针。

## 验证

- `py -3.12 -m compileall -q`（上述生产 + `tests/test_audio_preparation_rollback.py`）
- `git diff --check`（同上）
- Gate `audio_preparation_rollback_cursor_20261104c` → **12 OK**（仅本模块）；`…20261104a` 为历史 129 项宽 gate
- **data/tmp +3（0B）**：见 `docs/codex-review-audio-preparation-rollback-2026-11-04.md` 归属表；Cursor 未删除

## 下一步

Root 独立矩阵通过后，才允许有界跨句音频预取接线。
