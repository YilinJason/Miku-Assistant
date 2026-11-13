# 交接：音频准备事务回滚 fixture 最终收口（Cursor → Root）

## 状态

- **生产**：Root7 已通过，**禁止再改**。
- **本轮回**：仅 `tests/test_audio_preparation_rollback.py` + 本文档/状态顶节。

## Fixture 变更

1. **IO pin**：`_PinningSynthClient` 在 fake synth 中对输出 `pin_io_paths`，抛同一 `KeyboardInterrupt` 对象；`writer_alive` 时 `retire_worker.complete=False` 阻塞第二次 synth；teardown 释放 lease → `shutdown_local.complete` + TFM `shutdown.complete` + 文件不存在。
2. **生命周期**：全部 `self.enterContext(TemporaryDirectory())`；Cosy 另 `enterContext(patch TMP_DIR)`；`addCleanup` 回收 backend/engine/TFM **先于**临时目录清除。

## Gate

| Label | 结果 | 报告 |
| --- | --- | --- |
| `audio_preparation_rollback_cursor_20261104e` | **12 OK** | `tmp/codex_gate_audio_preparation_rollback_cursor_20261104e_2026_11_02.json` |

`wav_before=829`, `wav_after=829`, `added=[]`。历史 **826+3** 0B 文件仍在 `data/tmp`，**未删除**（Root 精确回收）。

## STOP

未改生产；未 raw unittest；未接预取。
