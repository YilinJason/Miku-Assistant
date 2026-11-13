# 交接：speech_queue 生命周期切片返修 b（Cursor → Root）

## 返修 c（仅测试 fixture）

- `ObservedCancel.is_set`：先读 `super().is_set()` 再 `checked.set()` 再返回读值（屏障证明锁外 False 后才置取消）。

## 验证（返修 c）

| 项 | 结果 |
| --- | --- |
| Gate `speech_queue_lifecycle_cursor_20261104d` | **11 OK**，~1.0s，WAV826 零差 |
| 报告 | `tmp/codex_gate_speech_queue_lifecycle_cursor_20261104d_2026_11_02.json` |
| Root9 / 全模块 | 由 Root 独立回归 |

## STOP

生产 Root9 已 PASS；本返修仅 tests fixture，未改生产/Root 脚本。请 Root 做最终相关模块回归后再开预取。

## 返修 b（生产 + 矩阵，已验收）

- 生产：`speech_queue.py`（取消锁内复核、日志无 traceback、`thread_factory` 可选）
- Gate 历史：`speech_queue_lifecycle_cursor_20261104c` → 123 OK

