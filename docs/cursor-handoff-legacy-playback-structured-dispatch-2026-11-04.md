# 交接：structured_dispatch 非流 TTS 测试迁移（Cursor → Root）

## 背景

生产 Root5/5 + 相关 162/162 已 PASS，源码冻结。全仓遗漏：`test_structured_dispatch` 仍 patch 同步 `Thread` factory，会挂起真实 `TurnSpeechPlayer` consumer。

## 改动（仅测试）

| 文件 | 内容 |
| --- | --- |
| `tests/test_structured_dispatch.py` | 成功用例：player + `COMPLETED` + `addCleanup`；失败/空：无 Thread patch，`_speech_player is None` |
| `tests/test_assistant_orchestration.py` | 删除已无引用的 `_immediate_thread_factory` |

生产 **未改**。

## 验证（Cursor）

| 项 | 结果 |
| --- | --- |
| Gate `legacy_playback_structured_dispatch_20261104` | **33 OK**，~1.9s，WAV **826→826** |
| 报告 | `tmp/codex_gate_legacy_playback_structured_dispatch_20261104_2026_11_02.json` |
| compileall / diffcheck | `test_structured_dispatch.py`、`test_assistant_orchestration.py` |

未跑 raw discover；未运行或修改 Root probe/监督器。

## STOP

待 Root 复审迁移并独立复验；未提交/推送。
